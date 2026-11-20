"""病害报件归并工作区（离线，可纠错的病害主数据）。

本模块只做一件事：把来源不同、可能重复的**病害报件**按审核员的
「同一 / 不同」裁定归并成可下发的病害组，并以带批号的批文件维护一张
可增量交付的主数据表。它**不做**巡检结算、不做工单分派、也不自动判同：
候选列表只是给人复核的线索，候选的出现或消失不改变任何病害身份。

关键设计
========
- **主键**：报件 ``(source, source_ref)``。报件内容含病害类别、车道、
  实际发现时间（UTC 整数秒）以及一份**现有登记的源编号点**（登记 ID +
  编号）。
- **工作区绑定**：建工作区时把一份 ready 换算库复制进工作区，并记下
  其 ``workspace_id`` 与内容指纹；批文件必须声明 ``based_on_workspace``。
  换算库文件之后被替换不会改写已入仓位置，同一工作区也不会混用两份标定。
- **定位沿用既有正查语义**：恰好一个物理位置（``RESOLVED`` 或
  ``NO_TARGET``）就可用于找候选，不要求有目标标牌；``NO_SOURCE`` 与
  ``MULTIPLE_SOURCE`` 仍可入仓、可人工裁定，只是不参与按位置找候选。
  登记不存在与编号超出登记范围是输入错误；合法但无法唯一定位是业务
  状态，不是错误。
- **批**：录入/更正、撤下、裁定（同一/不同/撤去）在一个事务里整批
  生效；行序不影响结果（内部先合并录入与撤下、再合并裁定、最后一次性
  重算）。同一键或同一对在一批内只能出现一次。
- **组**：按当前生效的「同一」边求连通分量（并查集）。组内有任一生效
  的「不同」边即整组隔离。组键取组内按 ``(source, source_ref)`` 逐项
  Unicode 码点序最小的报件键，不分配流水号。
- **修订与增量**：每次成功批提交修订号加一；批内先把「旧表 vs 最终表」
  的行级差异写入增量表，再在同一事务里替换两张表，因此读者只会看到
  某个完整修订，增量也不会泄漏批内临时拆组/合组。
- **局部重算**：每批只重算受该批影响（连接、拆开、裁定变化）的组以及
  新涉及的组，其余组整行沿用；不预存全体两两可达关系、不预生成候选对。

生产者（审核员）与消费者（工单系统）都用这一个模块的入口：

- 录入/更正/撤下/裁定：:func:`apply_batch`
- 全量：:func:`fetch_full`
- 增量：:func:`fetch_changes`
- 复核隔离：:func:`isolation_evidence`
- 候选：:func:`candidate_pairs`（命中数与扫描量分开报告）
- 组：:func:`list_groups` / :func:`group_detail`
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .database import new_connection, open_ready_database
from .errors import (
    ChainageError,
    DatabaseError,
    DefectIssue,
    DefectValidationError,
)
from .models import INT64_MAX, INT64_MIN
from .query import forward_query

FORMAT = "chainage-defect-workspace"
BATCH_FORMAT = "chainage-defect-batch"
WORKSPACE_SCHEMA_VERSION = 1

# 病害类别 / 车道 / 来源都不是定位字段，但同样必须有界、可索引。
MAX_ID_LEN = 200

SAME = "same"            # 同一
DIFFERENT = "different"   # 不同

LOC_LOCALIZABLE = "localizable"   # 恰好一个物理位置（有无目标标牌都可）
LOC_NO_SOURCE = "no_source"       # 没有源位置
LOC_MULTIPLE = "multiple_source"  # 多个源物理位置

_WORKSPACE_SCHEMA = """
PRAGMA foreign_keys = OFF;

CREATE TABLE wmeta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 报件：主键 (source, source_ref)。
-- present = 1 表示在册；撤下的报件保留行与定位结果，重新录入同键时复用。
CREATE TABLE defect (
    source          TEXT NOT NULL,
    source_ref      TEXT NOT NULL,
    present         INTEGER NOT NULL,
    present_rev     INTEGER NOT NULL,
    present_batch   TEXT NOT NULL,
    category        TEXT NOT NULL,
    lane            TEXT NOT NULL,
    found_at        INTEGER NOT NULL,
    reg_id          TEXT NOT NULL,
    reg_n           INTEGER NOT NULL,
    reg_source_system TEXT,
    reg_target_system TEXT,
    reg_n_from      INTEGER,
    reg_n_to        INTEGER,
    location_status TEXT NOT NULL,
    position_count  INTEGER NOT NULL,
    axis_id         TEXT,
    loc_lo          INTEGER,
    loc_hi          INTEGER,
    evidence_json   TEXT,
    PRIMARY KEY (source, source_ref)
);

CREATE INDEX idx_defect_present ON defect(present);

-- 人工裁定：键序规范化后 lkey < rkey，每对至多一条；present=0 即"已撤去"。
CREATE TABLE adjudication (
    lsource       TEXT NOT NULL,
    lref          TEXT NOT NULL,
    rsource       TEXT NOT NULL,
    rref          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('same', 'different')),
    present       INTEGER NOT NULL,
    present_rev   INTEGER NOT NULL,
    present_batch TEXT NOT NULL,
    PRIMARY KEY (lsource, lref, rsource, rref)
);
CREATE INDEX idx_adj_l ON adjudication(lsource, lref);
CREATE INDEX idx_adj_r ON adjudication(rsource, rref);
CREATE INDEX idx_adj_eff ON adjudication(present, kind);

-- 唯一可定位报件的检索行：只为候选预筛服务，不存任何两两关系。
CREATE TABLE pair_bucket (
    source      TEXT NOT NULL,
    source_ref  TEXT NOT NULL,
    category    TEXT NOT NULL,
    lane        TEXT NOT NULL,
    axis_id     TEXT NOT NULL,
    loc_lo      INTEGER NOT NULL,
    loc_hi      INTEGER NOT NULL,
    found_at    INTEGER NOT NULL,
    PRIMARY KEY (source, source_ref)
);
CREATE INDEX idx_pair_bucket
    ON pair_bucket(category, lane, axis_id, loc_lo, loc_hi);
CREATE INDEX idx_pair_time
    ON pair_bucket(category, lane, axis_id, found_at);

-- 病害组：组键 + 是否隔离 + 成员数 + 首次/末次发现时间。
CREATE TABLE defect_group (
    group_key    TEXT PRIMARY KEY,
    isolated     INTEGER NOT NULL,
    member_count INTEGER NOT NULL,
    first_found  INTEGER,
    last_found   INTEGER
);
CREATE INDEX idx_group_isolated ON defect_group(isolated);

-- 成员表：按报件键标识，带组键与报件内容（供工单侧全量/增量接表）。
CREATE TABLE defect_member (
    source      TEXT NOT NULL,
    source_ref  TEXT NOT NULL,
    group_key   TEXT NOT NULL,
    category    TEXT NOT NULL,
    lane        TEXT NOT NULL,
    found_at    INTEGER NOT NULL,
    reg_id      TEXT NOT NULL,
    reg_n       INTEGER NOT NULL,
    reg_source_system TEXT,
    reg_target_system TEXT,
    reg_n_from  INTEGER,
    reg_n_to    INTEGER,
    location_status TEXT NOT NULL,
    position_count  INTEGER NOT NULL,
    axis_id     TEXT,
    loc_lo      INTEGER,
    loc_hi      INTEGER,
    evidence_json TEXT,
    PRIMARY KEY (source, source_ref)
);
CREATE INDEX idx_member_group ON defect_member(group_key);

-- 批收据：批号全局唯一；重试原基准修订号 + 完全相同的文件内容直接回放。
CREATE TABLE batch_receipt (
    batch_no         TEXT PRIMARY KEY,
    base_revision    INTEGER NOT NULL,
    base_workspace   TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    counts_json      TEXT NOT NULL,
    changes_total    INTEGER NOT NULL,
    changes_group    INTEGER NOT NULL,
    changes_member   INTEGER NOT NULL,
    receipt_json     TEXT NOT NULL,
    batch_fingerprint TEXT NOT NULL
);

-- 增量：每批只记录"该批前后两张表的行级差异"（前像/后像都存）。
-- op: + 新增  - 删除  ~ 变更。row_key 为组键或报件键的规范 JSON。
CREATE TABLE change (
    revision    INTEGER NOT NULL,
    batch_no    TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    row_key     TEXT NOT NULL,
    op          TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    PRIMARY KEY (revision, table_name, row_key)
);
CREATE INDEX idx_change_rev ON change(revision);

CREATE TABLE revision_stat (
    revision INTEGER PRIMARY KEY,
    batch_no TEXT NOT NULL
);

-- 每批实际重算的组与触及的成员行（验收局部性用；不参与业务结果）。
CREATE TABLE batch_scope_group (
    revision  INTEGER NOT NULL,
    group_key TEXT NOT NULL,
    PRIMARY KEY (revision, group_key)
);
CREATE TABLE batch_scope_member (
    revision   INTEGER NOT NULL,
    source     TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    PRIMARY KEY (revision, source, source_ref)
);
"""


# --------------------------------------------------------------------------- #
# 键与序列化
# --------------------------------------------------------------------------- #
def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def key_of(source: str, source_ref: str) -> Tuple[str, str]:
    return (source, source_ref)


def key_json(source: str, source_ref: str) -> str:
    """报件键的规范文本形式（也是成员表增量 row_key）。"""
    return json.dumps([source, source_ref], ensure_ascii=False,
                      separators=(",", ":"))


def _key_tuple(key_json_text: str) -> Tuple[str, str]:
    obj = json.loads(key_json_text)
    return (obj[0], obj[1])


def _norm_pair(a: Tuple[str, str], b: Tuple[str, str]
               ) -> Optional[Tuple[Tuple[str, str], Tuple[str, str]]]:
    """把一对报件键规范化成 (较小, 较大)；自反对返回 None。"""
    if a == b:
        return None
    return (a, b) if a < b else (b, a)


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #
WS_DB = "workspace.db"
WS_CHAINAGE = "chainage.db"


def workspace_paths(workspace_dir: str) -> Tuple[str, str]:
    return (os.path.join(workspace_dir, WS_DB),
            os.path.join(workspace_dir, WS_CHAINAGE))


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO wmeta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def _get_meta(conn: sqlite3.Connection, key: str,
              required: bool = True) -> Optional[str]:
    row = conn.execute("SELECT value FROM wmeta WHERE key=?", (key,)).fetchone()
    if row is None:
        if required:
            raise DatabaseError(f"工作区缺少元数据 {key!r}")
        return None
    return row[0]


def _chainage_fingerprint(db_path: str) -> str:
    """换算库内容指纹：轴、标定行、登记与交接版本（不依赖文件字节）。"""
    h = hashlib.sha256()
    with open_ready_database(db_path) as conn:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='package_version'").fetchone()
        h.update(("v=" + (version[0] if version else "") + "\n").encode())
        for row in conn.execute(
                "SELECT axis_id, length FROM axis ORDER BY axis_id"):
            h.update(f"a|{row[0]}|{row[1]}\n".encode())
        for row in conn.execute(
                "SELECT row_id, system, axis_id, segment, lo, hi, label_lo, "
                "direction FROM calibration ORDER BY row_id"):
            h.update(("c|" + "|".join(str(x) for x in row) + "\n").encode())
        for row in conn.execute(
                "SELECT reg_id, source_system, target_system, n_from, n_to, "
                "axis_id, segment FROM registration ORDER BY reg_id"):
            h.update(("r|" + "|".join("" if x is None else str(x)
                                     for x in row) + "\n").encode())
    return h.hexdigest()


def create_workspace(workspace_dir: str, chainage_db: str,
                     spatial_threshold: int, time_threshold: int
                     ) -> Dict[str, Any]:
    """建一个全新的病害归并工作区，并绑定一份 ready 换算库的拷贝。

    ``spatial_threshold`` 是物理刻度差阈值，``time_threshold`` 是发现
    时间差阈值（秒）；两者都按**闭区间**（端点算命中）使用，建后不可改。
    """
    if not _is_int(spatial_threshold) or spatial_threshold < 0:
        raise ChainageError("spatial_threshold 必须是 >=0 的整数刻度")
    if not _is_int(time_threshold) or time_threshold < 0:
        raise ChainageError("time_threshold 必须是 >=0 的整数秒")
    if not os.path.isfile(chainage_db):
        raise DatabaseError(f"换算库文件不存在: {chainage_db}")
    db_path, ch_path = workspace_paths(workspace_dir)
    if os.path.exists(db_path) or os.path.exists(ch_path):
        raise ChainageError(f"工作区已存在: {workspace_dir}")

    fingerprint = _chainage_fingerprint(chainage_db)
    os.makedirs(workspace_dir, exist_ok=True)
    # 先写临时文件再改名，避免中途失败留下半个工作区
    tmp_ch = ch_path + ".tmp"
    from urllib.parse import quote
    src = sqlite3.connect(f"file:{quote(os.path.abspath(chainage_db))}?mode=ro",
                          uri=True)
    dst = sqlite3.connect(tmp_ch)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(tmp_ch, ch_path)

    tmp_db = db_path + ".tmp"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    conn = sqlite3.connect(tmp_db)
    try:
        conn.executescript(_WORKSPACE_SCHEMA)
        workspace_id = uuid.uuid4().hex
        _set_meta(conn, "format", FORMAT)
        _set_meta(conn, "workspace_schema", str(WORKSPACE_SCHEMA_VERSION))
        _set_meta(conn, "workspace_id", workspace_id)
        _set_meta(conn, "chainage_fingerprint", fingerprint)
        _set_meta(conn, "spatial_threshold", str(spatial_threshold))
        _set_meta(conn, "time_threshold", str(time_threshold))
        _set_meta(conn, "revision", "0")
        conn.commit()
    except Exception:
        conn.close()
        os.remove(tmp_db)
        os.remove(ch_path)
        raise
    conn.close()
    os.replace(tmp_db, db_path)
    return workspace_info(workspace_dir)


def open_workspace(workspace_dir: str) -> sqlite3.Connection:
    """打开工作区并校验格式、绑定换算库未被替换。"""
    db_path, ch_path = workspace_paths(workspace_dir)
    if not os.path.isfile(db_path):
        raise DatabaseError(f"工作区不存在: {workspace_dir}")
    if not os.path.isfile(ch_path):
        raise DatabaseError(f"工作区绑定的换算库副本缺失: {ch_path}")
    conn = new_connection(db_path)
    try:
        fmt = conn.execute(
            "SELECT value FROM wmeta WHERE key='format'").fetchone()
        if fmt is None or fmt[0] != FORMAT:
            raise DatabaseError("不是病害归并工作区")
        expected = _get_meta(conn, "chainage_fingerprint")
        actual = _chainage_fingerprint(ch_path)
        if actual != expected:
            raise DatabaseError(
                "工作区绑定的换算库副本内容与建区指纹不一致（疑似被替换）")
        return conn
    except Exception:
        conn.close()
        raise


def workspace_info(workspace_dir: str) -> Dict[str, Any]:
    db_path, _ = workspace_paths(workspace_dir)
    conn = new_connection(db_path)
    try:
        rev = int(_get_meta(conn, "revision"))
        members = conn.execute(
            "SELECT COUNT(*) FROM defect_member").fetchone()[0]
        groups = conn.execute(
            "SELECT COUNT(*) FROM defect_group").fetchone()[0]
        isolated = conn.execute(
            "SELECT COUNT(*) FROM defect_group WHERE isolated=1").fetchone()[0]
        present = conn.execute(
            "SELECT COUNT(*) FROM defect WHERE present=1").fetchone()[0]
        return {
            "format": FORMAT,
            "workspace_id": _get_meta(conn, "workspace_id"),
            "revision": rev,
            "spatial_threshold": int(_get_meta(conn, "spatial_threshold")),
            "time_threshold": int(_get_meta(conn, "time_threshold")),
            "chainage_fingerprint": _get_meta(conn, "chainage_fingerprint"),
            "reports_present": present,
            "groups": groups,
            "groups_isolated": isolated,
            "members": members,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 批文件解析与校验
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReportInput:
    source: str
    source_ref: str
    category: str
    lane: str
    found_at: int
    reg_id: str
    reg_n: int


@dataclass(frozen=True)
class BatchContents:
    batch_no: str
    based_on_workspace: str
    based_on_revision: int
    reports: List[ReportInput]
    void: List[Tuple[str, str]]
    adj_same: List[Tuple[Tuple[str, str], Tuple[str, str]]]
    adj_different: List[Tuple[Tuple[str, str], Tuple[str, str]]]
    adj_clear: List[Tuple[Tuple[str, str], Tuple[str, str]]]


def _batch_fingerprint(contents: "BatchContents") -> str:
    """单批操作的规范指纹（排序后的录入/撤下/裁定），与行序无关。"""
    payload = {
        "batch_no": contents.batch_no,
        "based_on_workspace": contents.based_on_workspace,
        "based_on_revision": contents.based_on_revision,
        "reports": sorted(
            [[r.source, r.source_ref, r.category, r.lane, r.found_at,
              r.reg_id, r.reg_n] for r in contents.reports]),
        "void": sorted([list(k) for k in contents.void]),
        "same": sorted([list(a) + list(b) for a, b in contents.adj_same]),
        "different": sorted([list(a) + list(b)
                             for a, b in contents.adj_different]),
        "clear": sorted([list(a) + list(b) for a, b in contents.adj_clear]),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_batch_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except FileNotFoundError:
        raise DefectValidationError(
            [DefectIssue("batch", "MISSING_FILE", f"批文件不存在: {path}")])
    except json.JSONDecodeError as exc:
        raise DefectValidationError(
            [DefectIssue("batch", "BAD_JSON",
                         f"批文件不是合法 JSON: {exc.msg}")])
    if not isinstance(obj, dict):
        raise DefectValidationError(
            [DefectIssue("batch", "BAD_FIELD", "批文件顶层必须是对象")])
    return obj


def _check_str(d: dict, key: str, loc: str, issues: List[DefectIssue],
               ctx: Dict[str, Any], limit: int = 0) -> Optional[str]:
    v = d.get(key)
    if not isinstance(v, str) or v == "":
        issues.append(DefectIssue(loc, "BAD_FIELD",
                                  f"字段 {key!r} 必须是非空字符串", **ctx))
        return None
    if limit and len(v) > limit:
        issues.append(DefectIssue(loc, "BAD_FIELD",
                                  f"字段 {key!r} 过长（上限 {limit}）", **ctx))
        return None
    return v


def _check_int(d: dict, key: str, loc: str, issues: List[DefectIssue],
               ctx: Dict[str, Any]) -> Optional[int]:
    v = d.get(key)
    if not _is_int(v):
        issues.append(DefectIssue(
            loc, "BAD_FIELD",
            f"字段 {key!r} 必须是整数（不接受小数/布尔/字符串）", **ctx))
        return None
    if not (INT64_MIN <= v <= INT64_MAX):
        issues.append(DefectIssue(
            loc, "INT64_OVERFLOW",
            f"字段 {key!r}={v} 超出有符号 64 位整数范围", **ctx))
        return None
    return v


def _check_key(d: dict, source_key: str, ref_key: str, loc: str,
               issues: List[DefectIssue],
               ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    source = _check_str(d, source_key, loc, issues, ctx)
    ref = _check_str(d, ref_key, loc, issues, ctx)
    if source is None or ref is None:
        return None
    return (source, ref)


def validate_batch_json(batch: dict) -> BatchContents:
    """结构与内容校验；同一键/同一对重复、自反对都拒绝（聚合报错）。

    这里只做纯结构校验，不读工作区；引用是否已知由 :func:`apply_batch`
    在同一事务内判定。
    """
    issues: List[DefectIssue] = []
    if batch.get("format") != BATCH_FORMAT:
        issues.append(DefectIssue(
            "batch", "BAD_FORMAT", f"批文件 format 必须是 {BATCH_FORMAT!r}"))
    batch_no = _check_str(batch, "batch_no", "batch", issues, {}, limit=200)
    based_ws = _check_str(batch, "based_on_workspace", "batch", issues, {},
                          limit=200)
    based_rev = _check_int(batch, "based_on_revision", "batch", issues, {})
    if based_rev is not None and based_rev < 0:
        issues.append(DefectIssue("batch", "BAD_FIELD",
                                  "based_on_revision 必须 >= 0"))

    reports: List[ReportInput] = []
    report_seen: Dict[Tuple[str, str], str] = {}
    raw_reports = batch.get("reports", [])
    if not isinstance(raw_reports, list):
        issues.append(DefectIssue("batch", "BAD_FIELD",
                                  "字段 reports 必须是数组"))
        raw_reports = []
    for i, rd in enumerate(raw_reports):
        loc = f"reports[{i}]"
        if not isinstance(rd, dict):
            issues.append(DefectIssue(loc, "BAD_FIELD",
                                      "报件录入项必须是对象"))
            continue
        key = _check_key(rd, "source", "source_ref", loc, issues, {})
        ctx = ({"source": key[0], "source_ref": key[1]} if key else {})
        if key is not None:
            if key in report_seen:
                issues.append(DefectIssue(
                    loc, "DUPLICATE_REPORT_IN_BATCH",
                    f"报件 {key!r} 在本批出现多次（首次 {report_seen[key]}）",
                    **ctx))
                key = None
            else:
                report_seen[key] = loc
        category = _check_str(rd, "category", loc, issues, ctx, MAX_ID_LEN)
        lane = _check_str(rd, "lane", loc, issues, ctx, MAX_ID_LEN)
        found_at = _check_int(rd, "found_at", loc, issues, ctx)
        reg_id = _check_str(rd, "reg_id", loc, issues, ctx)
        reg_n = _check_int(rd, "reg_n", loc, issues, ctx)
        if None in (key, category, lane, found_at, reg_id, reg_n):
            continue
        reports.append(ReportInput(key[0], key[1], category, lane, found_at,
                                   reg_id, reg_n))

    void: List[Tuple[str, str]] = []
    void_seen: Dict[Tuple[str, str], str] = {}
    raw_void = batch.get("void", [])
    if not isinstance(raw_void, list):
        issues.append(DefectIssue("batch", "BAD_FIELD",
                                  "字段 void 必须是数组"))
        raw_void = []
    for i, vd in enumerate(raw_void):
        loc = f"void[{i}]"
        if not isinstance(vd, dict):
            issues.append(DefectIssue(loc, "BAD_FIELD", "撤下项必须是对象"))
            continue
        key = _check_key(vd, "source", "source_ref", loc, issues, {})
        if key is None:
            continue
        if key in void_seen:
            issues.append(DefectIssue(
                loc, "DUPLICATE_VOID_IN_BATCH",
                f"撤下项 {key!r} 在本批出现多次（首次 {void_seen[key]}）",
                source=key[0], source_ref=key[1]))
            continue
        if key in report_seen:
            issues.append(DefectIssue(
                loc, "REPORT_AND_VOID_SAME_BATCH",
                f"报件 {key!r} 在本批既有录入又有撤下",
                source=key[0], source_ref=key[1]))
            continue
        void_seen[key] = loc
        void.append(key)

    adj = _parse_adjudications(batch, issues)
    if issues:
        raise DefectValidationError(issues)
    assert batch_no is not None and based_ws is not None and based_rev is not None
    return BatchContents(
        batch_no=batch_no,
        based_on_workspace=based_ws,
        based_on_revision=based_rev,
        reports=reports,
        void=void,
        adj_same=adj[SAME],
        adj_different=adj[DIFFERENT],
        adj_clear=adj["clear"],
    )


def _parse_adjudications(batch: dict, issues: List[DefectIssue]
                         ) -> Dict[str, List[Tuple[Tuple[str, str],
                                                   Tuple[str, str]]]]:
    out: Dict[str, List[Tuple[Tuple[str, str], Tuple[str, str]]]] = {
        SAME: [], DIFFERENT: [], "clear": []}
    raw = batch.get("adjudications", [])
    if not isinstance(raw, list):
        issues.append(DefectIssue("batch", "BAD_FIELD",
                                  "字段 adjudications 必须是数组"))
        return out
    seen: Dict[Tuple[Tuple[str, str], Tuple[str, str]], str] = {}
    for i, ad in enumerate(raw):
        loc = f"adjudications[{i}]"
        if not isinstance(ad, dict):
            issues.append(DefectIssue(loc, "BAD_FIELD", "裁定项必须是对象"))
            continue
        a = _side_key(ad, "a", loc, issues)
        b = _side_key(ad, "b", loc, issues)
        decision = ad.get("decision", ad.get("relation"))
        if decision not in (SAME, DIFFERENT, "clear"):
            issues.append(DefectIssue(
                loc, "BAD_FIELD",
                "裁定 decision 只能是 'same' / 'different' / 'clear'"))
            continue
        if a is None or b is None:
            continue
        pair = _norm_pair(a, b)
        if pair is None:
            issues.append(DefectIssue(
                loc, "SELF_ADJUDICATION",
                f"裁定两端必须是不同报件，实际均为 {a!r}",
                source=a[0], source_ref=a[1]))
            continue
        if pair in seen:
            issues.append(DefectIssue(
                loc, "DUPLICATE_PAIR_IN_BATCH",
                f"报件对 {pair[0]!r} - {pair[1]!r} 在本批出现多次"
                f"（首次 {seen[pair]}）",
                source=pair[0][0], source_ref=pair[0][1],
                other_source=pair[1][0], other_ref=pair[1][1]))
            continue
        seen[pair] = loc
        out[decision].append(pair)
    return out


def _side_key(ad: dict, side: str, loc: str, issues: List[DefectIssue]
              ) -> Optional[Tuple[str, str]]:
    """裁定的一端：优先 ``{"source","source_ref"}`` 嵌套对象，兼容平铺字段。"""
    obj = ad.get(side)
    if obj is not None:
        if not isinstance(obj, dict):
            issues.append(DefectIssue(
                f"{loc}.{side}", "BAD_FIELD", "裁定一端必须是对象"))
            return None
        return _check_key(obj, "source", "source_ref", f"{loc}.{side}",
                          issues, {})
    return _check_key(ad, f"{side}_source", f"{side}_ref", loc, issues, {})


# --------------------------------------------------------------------------- #
# 报件的落盘形态（用于比较行变化）
# --------------------------------------------------------------------------- #
_DEFECT_COLS = (
    "source", "source_ref", "present", "present_rev", "present_batch",
    "category", "lane", "found_at", "reg_id", "reg_n", "reg_source_system",
    "reg_target_system", "reg_n_from", "reg_n_to", "location_status",
    "position_count", "axis_id", "loc_lo", "loc_hi", "evidence_json",
)
_DEFECT_CONTENT_COLS = (
    "category", "lane", "found_at", "reg_id", "reg_n", "reg_source_system",
    "reg_target_system", "reg_n_from", "reg_n_to", "location_status",
    "position_count", "axis_id", "loc_lo", "loc_hi", "evidence_json",
)

def _member_row(key: Tuple[str, str], group_key: str, defect: dict) -> dict:
    row = {"source": key[0], "source_ref": key[1], "group_key": group_key}
    for col in _DEFECT_CONTENT_COLS:
        row[col] = defect[col]
    return row


def _defect_from_row(row: sqlite3.Row) -> dict:
    return {col: row[col] for col in _DEFECT_COLS}


# --------------------------------------------------------------------------- #
# 定位（沿用既有正查语义）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Location:
    status: str                 # localizable / no_source / multiple_source
    position_count: int
    axis_id: Optional[str]
    x: Optional[int]
    evidence: Dict[str, Any]
    registration: Dict[str, Any]

    @property
    def localizable(self) -> bool:
        return self.status == LOC_LOCALIZABLE


def resolve_location(chainage_path: str, reg_id: str, n: int) -> Location:
    """把一份登记的源编号点定位到物理轴（只读 ready 换算库副本）。

    恰好一个物理位置即可用于找候选，不要求有目标标牌。登记不存在或
    编号超出登记范围抛 :class:`DefectValidationError`（输入错误）；合法
    但无位置/多位置返回对应业务状态，不拒绝入仓。
    """
    try:
        result = forward_query(chainage_path, reg_id, n, n)
    except DatabaseError as exc:
        msg = str(exc)
        if msg.startswith("登记 ID 不存在"):
            code = "UNKNOWN_REGISTRATION"
        elif "无交集" in msg:
            code = "N_OUT_OF_REGISTRATION"
        else:
            code = "BAD_REGISTRATION_REFERENCE"
        raise DefectValidationError([
            DefectIssue("batch", code, f"报件定位失败: {msg}")])
    reg = result["registration"]
    # 一条登记在查询点上可能有多个压缩分段；把全部位置合并后再看唯一性，
    # 不能只看第一段（否则会在分段边界处误判为可定位）。
    positions = [p for seg in result["segments"] for p in seg["positions"]]
    seg = result["segments"][0]
    evidence_positions = [{
        "axis_id": p["axis_id"],
        "x": p["x_lo"],
        "source_rows": [r["row_id"] for r in p["source_rows"]],
        "target_evidence": [{
            "target_segment": e["target_segment"],
            "target_label_lo": e["target_label_lo"],
            "target_label_hi": e["target_label_hi"],
            "target_rows": [r["row_id"] for r in e["target_rows"]],
        } for e in p["target_evidence"]],
    } for p in positions]
    reg_info = {
        "reg_id": reg["reg_id"],
        "source_system": reg["source_system"],
        "target_system": reg["target_system"],
        "n_from": reg["n_from"],
        "n_to": reg["n_to"],
        "axis_id": reg["axis_id"],
        "segment": reg["segment"],
    }
    evidence = {
        "registration": reg_info,
        "reg_id": reg["reg_id"],
        "reg_n": n,
        "segment_status": seg["status"],
        "positions": evidence_positions,
    }
    if len(positions) == 1:
        p = positions[0]
        return Location(LOC_LOCALIZABLE, 1, p["axis_id"], p["x_lo"],
                        evidence, reg_info)
    if not positions:
        return Location(LOC_NO_SOURCE, 0, None, None, evidence, reg_info)
    return Location(LOC_MULTIPLE, len(positions), None, None, evidence,
                    reg_info)


# --------------------------------------------------------------------------- #
# 图访问辅助（全部走主键/索引，不扫全表）
# --------------------------------------------------------------------------- #
def _present_defect(conn: sqlite3.Connection, key: Tuple[str, str]
                    ) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM defect WHERE source=? AND source_ref=?", key).fetchone()
    if r is None or not r["present"]:
        return None
    return _defect_from_row(r)


def _any_defect(conn: sqlite3.Connection, key: Tuple[str, str]
                ) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM defect WHERE source=? AND source_ref=?", key).fetchone()
    return _defect_from_row(r) if r is not None else None


def _effective_same_neighbors(conn: sqlite3.Connection, key: Tuple[str, str]
                              ) -> List[Tuple[str, str]]:
    src, ref = key
    rows = conn.execute(
        "SELECT lsource, lref, rsource, rref FROM adjudication "
        "WHERE present=1 AND kind='same' AND "
        "((lsource=? AND lref=?) OR (rsource=? AND rref=?))",
        (src, ref, src, ref)).fetchall()
    out: List[Tuple[str, str]] = []
    for ls, lr, rs, rr in rows:
        other = (rs, rr) if (ls, lr) == key else (ls, lr)
        out.append(other)
    return out


def _component_from(conn: sqlite3.Connection, seed: Tuple[str, str],
                    seen: Set[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """从 seed 出发沿当前生效的「同一」边求连通分量（只在册报件）。

    不预存全体两两可达关系：每次都从当前裁定实时走图，只访问本分量
    的节点与其相邻裁定。
    """
    if seed in seen:
        return []
    if _present_defect(conn, seed) is None:
        seen.add(seed)
        return []
    comp: List[Tuple[str, str]] = []
    stack = [seed]
    seen.add(seed)
    while stack:
        cur = stack.pop()
        comp.append(cur)
        for nb in _effective_same_neighbors(conn, cur):
            if nb in seen:
                continue
            seen.add(nb)
            if _present_defect(conn, nb) is not None:
                stack.append(nb)
    return comp


def _different_edges_within(conn: sqlite3.Connection,
                             nodes: Sequence[Tuple[str, str]]) -> List[Tuple[Tuple[str, str], Tuple[str, str]]]:
    """返回两端都在 nodes 内的当前生效「不同」裁定（只查与本组相关的边）。"""
    if len(nodes) < 2:
        return []
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _tmp_group_nodes("
                 "source TEXT, source_ref TEXT, PRIMARY KEY(source, source_ref))")
    conn.execute("DELETE FROM _tmp_group_nodes")
    conn.executemany(
        "INSERT INTO _tmp_group_nodes(source, source_ref) VALUES (?,?)", nodes)
    rows = conn.execute(
        "SELECT a.lsource, a.lref, a.rsource, a.rref FROM adjudication a "
        "JOIN _tmp_group_nodes l ON l.source=a.lsource AND l.source_ref=a.lref "
        "JOIN _tmp_group_nodes r ON r.source=a.rsource AND r.source_ref=a.rref "
        "WHERE a.present=1 AND a.kind='different' "
        "ORDER BY a.lsource, a.lref, a.rsource, a.rref").fetchall()
    conn.execute("DELETE FROM _tmp_group_nodes")
    return [((r[0], r[1]), (r[2], r[3])) for r in rows]


# --------------------------------------------------------------------------- #
# 批提交：单一事务内 校验 -> 旧组集 -> 报件落盘 -> 裁定落盘 -> 组重算 -> 增量
# --------------------------------------------------------------------------- #
def apply_batch(workspace_dir: str, batch_path: str) -> Dict[str, Any]:
    """应用一份批文件并返回收据。

    - 同号重试原基准修订号 + 完全相同的文件内容：只回放原收据；
    - 同号不同内容、未见批号却引用过期修订、输入错误：整批拒绝；
    - 报件、裁定、两张表、收据与增量在同一事务提交，读者只能看到完整修订。
    """
    batch = load_batch_file(batch_path)
    contents = validate_batch_json(batch)   # 纯结构校验，不碰库
    # 规范指纹：录入/撤下/裁定按键排序，裁定对已规范化（a/b 无序），
    # 因此行序、字段次序与裁定两端顺序都不影响"是否同一批"的判断。
    fingerprint = _batch_fingerprint(contents)
    db_path, ch_path = workspace_paths(workspace_dir)
    conn = open_workspace(workspace_dir)
    try:
        return _apply_validated(conn, ch_path, contents, fingerprint)
    finally:
        conn.close()


def _apply_validated(conn: sqlite3.Connection, ch_path: str,
                     contents: BatchContents, fingerprint: str) -> Dict[str, Any]:
    """整批提交。旧组快照 -> 报件/裁定落盘 -> 只重算受影响的组 -> 写增量。

    组表/成员表的新内容先算进内存（只限受影响范围），再在**同一事务**里
    删除旧组行、写入新组行、写入增量与收据；批内任何临时拆组/合组都不会
    出现在增量里——增量只比较"批前表"和"批后表"。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise ChainageError(f"工作区被另一个写者占用: {exc}")

    try:
        # 1) 重号/基准修订检查（收据在事务内读取，防止并发双写）
        prev = conn.execute(
            "SELECT * FROM batch_receipt WHERE batch_no=?",
            (contents.batch_no,)).fetchone()
        if prev is not None:
            same = (prev["base_workspace"] == contents.based_on_workspace
                    and prev["base_revision"] == contents.based_on_revision
                    and prev["content_sha256"] == fingerprint)
            if same:
                conn.rollback()
                return {"receipt": json.loads(prev["receipt_json"]),
                        "replayed": True}
            conn.rollback()
            raise DefectValidationError([DefectIssue(
                "batch", "BATCH_NO_REUSED",
                f"批号 {contents.batch_no!r} 已用于另一份内容"
                f"（原基准修订 {prev['base_revision']}，内容指纹不符）",
                batch_no=contents.batch_no)])

        current_rev = int(_get_meta(conn, "revision"))
        workspace_id = _get_meta(conn, "workspace_id")
        batch_no = contents.batch_no
        if contents.based_on_workspace != workspace_id:
            conn.rollback()
            raise DefectValidationError([DefectIssue(
                "batch", "WORKSPACE_MISMATCH",
                f"批文件声明工作区 {contents.based_on_workspace!r}，"
                f"当前工作区为 {workspace_id!r}", batch_no=batch_no)])
        if contents.based_on_revision > current_rev:
            conn.rollback()
            raise DefectValidationError([DefectIssue(
                "batch", "UNKNOWN_REVISION",
                f"批文件基准修订 {contents.based_on_revision} 晚于当前修订 "
                f"{current_rev}，未知修订不能被引用", batch_no=batch_no)])
        if contents.based_on_revision < current_rev:
            conn.rollback()
            raise DefectValidationError([DefectIssue(
                "batch", "STALE_REVISION",
                f"批文件基准修订 {contents.based_on_revision} 已过期"
                f"（当前 {current_rev}）；请基于最新修订重新提交",
                batch_no=batch_no)])

        new_rev = current_rev + 1

        # 2) 定位全部录入报件（登记不存在/编号越界 -> 整批输入错误）
        locations: Dict[Tuple[str, str], Location] = {}
        before_defect: Dict[Tuple[str, str], Optional[dict]] = {}
        issues: List[DefectIssue] = []
        for rep in contents.reports:
            key = key_of(rep.source, rep.source_ref)
            try:
                locations[key] = resolve_location(ch_path, rep.reg_id,
                                                  rep.reg_n)
            except DefectValidationError as exc:
                for issue in exc.issues:
                    issue.batch_no = batch_no
                    issue.source = rep.source
                    issue.source_ref = rep.source_ref
                issues.extend(exc.issues)
                continue
            before_defect[key] = _any_defect(conn, key)
        if issues:
            conn.rollback()
            raise DefectValidationError(issues)

        # 3) 裁定只能引用仓内已知报件（含本批新录入及此前撤下的报件）
        new_keys = set(locations)
        for pairs in (contents.adj_same, contents.adj_different,
                      contents.adj_clear):
            for a, b in pairs:
                for side in (a, b):
                    if side in new_keys:
                        continue
                    if _any_defect(conn, side) is None:
                        issues.append(DefectIssue(
                            "adjudications", "UNKNOWN_REPORT",
                            f"裁定引用了仓内不存在的报件 {side!r}",
                            source=side[0], source_ref=side[1],
                            batch_no=batch_no))
        if issues:
            conn.rollback()
            raise DefectValidationError(issues)

        # 4) 批前快照：只触及本批涉及的报件所在组，不扫无关组/全部报件。
        #    先按键取当前组（成员表主键查找），再按组键取该组成员。
        candidate_keys: Set[Tuple[str, str]] = set(new_keys)
        candidate_keys.update(contents.void)
        for pairs in (contents.adj_same, contents.adj_different,
                      contents.adj_clear):
            for a, b in pairs:
                candidate_keys.add(a)
                candidate_keys.add(b)
        # 内容更正可能改变报件的类别/车道/位置：它们自身已经在
        # new_keys 中；无需再扫描全表。

        old_group_of: Dict[Tuple[str, str], str] = {}
        for key in candidate_keys:
            r = conn.execute(
                "SELECT group_key FROM defect_member WHERE source=? AND "
                "source_ref=?", key).fetchone()
            if r is not None:
                old_group_of[key] = r["group_key"]
        affected_old_groups: Set[str] = set(old_group_of.values())

        old_group_rows: Dict[str, dict] = {}
        old_member_rows: Dict[Tuple[str, str], dict] = {}
        old_members_by_group: Dict[str, List[Tuple[str, str]]] = {}
        seen_group: Set[str] = set()

        def load_old_group(gk: str) -> None:
            if gk in seen_group:
                return
            seen_group.add(gk)
            row = conn.execute(
                "SELECT * FROM defect_group WHERE group_key=?", (gk,)).fetchone()
            if row is not None:
                old_group_rows[gk] = {c: row[c] for c in row.keys()}
            for r in conn.execute(
                    "SELECT * FROM defect_member WHERE group_key=?", (gk,)):
                key = (r["source"], r["source_ref"])
                old_member_rows[key] = {c: r[c] for c in r.keys()}
                old_members_by_group.setdefault(gk, []).append(key)

        for gk in sorted(affected_old_groups):
            load_old_group(gk)

        # 受影响的组整组重算：组成员 + 本批涉及的报件
        seeds: Set[Tuple[str, str]] = set(candidate_keys)
        for keys_in_group in old_members_by_group.values():
            seeds.update(keys_in_group)

        # 5) 报件与定位落盘（本批事务内，供候选与组重算直接使用）
        _upsert_reports(conn, contents, locations, new_rev, batch_no, issues)
        if issues:
            conn.rollback()
            raise DefectValidationError(issues)

        # 6) 裁定落盘（clear 也写成 present=0 的行，保留历史）
        for kind, pairs in ((SAME, contents.adj_same),
                            (DIFFERENT, contents.adj_different)):
            for a, b in pairs:
                _write_adjudication(conn, a, b, kind, present=1,
                                    revision=new_rev, batch_no=batch_no)
        for a, b in contents.adj_clear:
            _write_adjudication(conn, a, b, SAME, present=0,
                                revision=new_rev, batch_no=batch_no)
            _write_adjudication(conn, a, b, DIFFERENT, present=0,
                                revision=new_rev, batch_no=batch_no)

        # 7) 只从受影响种子出发求新组（其余组整行沿用）
        seen: Set[Tuple[str, str]] = set()
        new_groups: Dict[str, Dict[str, Any]] = {}
        for seed in sorted(seeds):
            if seed in seen:
                continue
            comp = _component_from(conn, seed, seen)
            if not comp:
                continue
            comp.sort()
            gk = key_json(*comp[0])
            defect_rows = [_present_defect(conn, k) for k in comp]
            different_edges = _different_edges_within(conn, comp)
            new_groups[gk] = {
                "keys": comp,
                "isolated": 1 if different_edges else 0,
                "defects": defect_rows,
                "first_found": min(d["found_at"] for d in defect_rows),
                "last_found": max(d["found_at"] for d in defect_rows),
            }

        # 7b) 新组可能吸收原先属于"不在候选键旧组集合"的报件（例如裁定
        #     涉及一条曾撤下、但其所在旧组不在本批候选里的报件，会把旧组
        #     整体接回）。这些旧组同样属于本批影响范围，必须补进旧快照，
        #     否则增量会漏发旧组删除/变更。
        extra_old_groups: Set[str] = set()
        for gk, info in new_groups.items():
            for key in info["keys"]:
                r = conn.execute(
                    "SELECT group_key FROM defect_member WHERE source=? AND "
                    "source_ref=?", key).fetchone()
                if r is not None and r["group_key"] != gk:
                    extra_old_groups.add(r["group_key"])
        if extra_old_groups:
            affected_old_groups |= extra_old_groups
            for gk in sorted(extra_old_groups):
                load_old_group(gk)

        # 8) 计算成员表的新旧差（只在本批影响范围内）
        new_member_rows: Dict[Tuple[str, str], dict] = {}
        for gk, info in new_groups.items():
            for key, d in zip(info["keys"], info["defects"]):
                new_member_rows[key] = _member_row(key, gk, d)

        # 9) 在一个事务里替换受影响组。删除严格按旧组键/旧成员键，
        #    不使用 LIKE 或组前缀，避免误伤无关大组。
        for gk in affected_old_groups:
            conn.execute("DELETE FROM defect_group WHERE group_key=?", (gk,))
        for key in old_member_rows:
            conn.execute("DELETE FROM defect_member WHERE source=? AND "
                         "source_ref=?", key)
        for gk in new_groups:
            conn.execute("DELETE FROM defect_group WHERE group_key=?", (gk,))
        for key in new_member_rows:
            conn.execute("DELETE FROM defect_member WHERE source=? AND "
                         "source_ref=?", key)
        for gk in sorted(new_groups):
            info = new_groups[gk]
            conn.execute(
                "INSERT INTO defect_group(group_key, isolated, member_count, "
                "first_found, last_found) VALUES (?,?,?,?,?)",
                (gk, info["isolated"], len(info["keys"]),
                 info["first_found"], info["last_found"]))
        for row in new_member_rows.values():
            _insert_member(conn, row)

        # 10) 增量：批前表 vs 批后表（仅受影响组 + 撞名的沿用组）
        group_changes = _diff_groups(conn, new_rev, batch_no,
                                     affected_old_groups, old_group_rows,
                                     new_groups)
        member_changes = _diff_members(conn, new_rev, batch_no,
                                       old_member_rows, new_member_rows)
        _insert_changes(conn, new_rev, batch_no, group_changes + member_changes)

        # 11) 收据 + 修订
        changes_total, changes_group, changes_member = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(table_name='defect_group'),0), "
            "COALESCE(SUM(table_name='defect_member'),0) "
            "FROM change WHERE revision=?", (new_rev,)).fetchone()
        counts = {
            "reports_entered": len(contents.reports),
            "reports_voided": len(contents.void),
            "adjudications_same": len(contents.adj_same),
            "adjudications_different": len(contents.adj_different),
            "adjudications_cleared": len(contents.adj_clear),
            "groups_total": conn.execute(
                "SELECT COUNT(*) FROM defect_group").fetchone()[0],
            "groups_isolated": conn.execute(
                "SELECT COUNT(*) FROM defect_group WHERE isolated=1"
            ).fetchone()[0],
            "members_total": conn.execute(
                "SELECT COUNT(*) FROM defect_member").fetchone()[0],
        }
        receipt = {
            "batch_no": batch_no,
            "base_revision": contents.based_on_revision,
            "base_workspace": contents.based_on_workspace,
            "revision": new_rev,
            "counts": counts,
            "changes": {
                "total": changes_total,
                "defect_group": changes_group,
                "defect_member": changes_member,
            },
        }
        # 记录本批实际重算范围（旧受影响组 + 新组；写入与删除的成员行）
        conn.executemany(
            "INSERT INTO batch_scope_group(revision, group_key) VALUES (?,?)",
            [(new_rev, gk) for gk in
             sorted(set(affected_old_groups) | set(new_groups))])
        scope_members = set(old_member_rows) | set(new_member_rows)
        conn.executemany(
            "INSERT INTO batch_scope_member(revision, source, source_ref) "
            "VALUES (?,?,?)",
            [(new_rev, k[0], k[1]) for k in sorted(scope_members)])
        conn.execute(
            "INSERT INTO revision_stat(revision, batch_no) VALUES (?,?)",
            (new_rev, batch_no))
        conn.execute(
            "INSERT INTO batch_receipt(batch_no, base_revision, base_workspace,"
            " content_sha256, revision, counts_json, changes_total, "
            "changes_group, changes_member, receipt_json, batch_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (batch_no, contents.based_on_revision, contents.based_on_workspace,
             fingerprint, new_rev,
             json.dumps(counts, ensure_ascii=False, sort_keys=True),
             changes_total, changes_group, changes_member,
             json.dumps(receipt, ensure_ascii=False, sort_keys=True),
             _batch_fingerprint(contents)))
        conn.execute("UPDATE wmeta SET value=? WHERE key='revision'",
                     (str(new_rev),))
        conn.commit()
        return {"receipt": receipt, "replayed": False}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


# --------------------------------------------------------------------------- #
# 行落盘辅助
# --------------------------------------------------------------------------- #
def _upsert_reports(conn: sqlite3.Connection, contents: BatchContents,
                    locations: Dict[Tuple[str, str], Location],
                    revision: int, batch_no: str,
                    issues: List[DefectIssue]) -> None:
    """先落报件与定位结果（含撤下），并刷新候选预筛行。"""
    for rep in contents.reports:
        key = key_of(rep.source, rep.source_ref)
        _upsert_defect(conn, rep, locations[key], present=1,
                       revision=revision, batch_no=batch_no)
    for key in contents.void:
        row = conn.execute(
            "SELECT present FROM defect WHERE source=? AND source_ref=?",
            key).fetchone()
        if row is None or not row["present"]:
            issues.append(DefectIssue(
                "void", "VOID_UNKNOWN_REPORT",
                f"撤下的报件 {key!r} 不在册", source=key[0],
                source_ref=key[1], batch_no=batch_no))
            continue
        conn.execute(
            "UPDATE defect SET present=0, present_rev=?, present_batch=? "
            "WHERE source=? AND source_ref=?",
            (revision, batch_no, key[0], key[1]))
        conn.execute(
            "DELETE FROM pair_bucket WHERE source=? AND source_ref=?", key)


def _upsert_defect(conn: sqlite3.Connection, rep: ReportInput, loc: Location,
                   present: int, revision: int, batch_no: str) -> None:
    reg = loc.registration
    ev = json.dumps(loc.evidence, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"))
    conn.execute(
        "INSERT INTO defect(source, source_ref, present, present_rev, "
        "present_batch, category, lane, found_at, reg_id, reg_n, "
        "reg_source_system, reg_target_system, reg_n_from, reg_n_to, "
        "location_status, position_count, axis_id, loc_lo, loc_hi, "
        "evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source, source_ref) DO UPDATE SET "
        "present=excluded.present, present_rev=excluded.present_rev, "
        "present_batch=excluded.present_batch, category=excluded.category, "
        "lane=excluded.lane, found_at=excluded.found_at, "
        "reg_id=excluded.reg_id, reg_n=excluded.reg_n, "
        "reg_source_system=excluded.reg_source_system, "
        "reg_target_system=excluded.reg_target_system, "
        "reg_n_from=excluded.reg_n_from, reg_n_to=excluded.reg_n_to, "
        "location_status=excluded.location_status, "
        "position_count=excluded.position_count, axis_id=excluded.axis_id, "
        "loc_lo=excluded.loc_lo, loc_hi=excluded.loc_hi, "
        "evidence_json=excluded.evidence_json",
        (rep.source, rep.source_ref, present, revision, batch_no,
         rep.category, rep.lane, rep.found_at, reg["reg_id"], rep.reg_n,
         reg["source_system"], reg["target_system"], reg["n_from"],
         reg["n_to"], loc.status, loc.position_count, loc.axis_id,
         loc.x, loc.x, ev))
    # 候选预筛行：只保留唯一可定位的报件；撤下/多位置/无位置一律移除
    conn.execute("DELETE FROM pair_bucket WHERE source=? AND source_ref=?",
                 (rep.source, rep.source_ref))
    if present and loc.localizable:
        conn.execute(
            "INSERT INTO pair_bucket(source, source_ref, category, lane, "
            "axis_id, loc_lo, loc_hi, found_at) VALUES (?,?,?,?,?,?,?,?)",
            (rep.source, rep.source_ref, rep.category, rep.lane, loc.axis_id,
             loc.x, loc.x, rep.found_at))


def _write_adjudication(conn: sqlite3.Connection, a: Tuple[str, str],
                        b: Tuple[str, str], kind: str, present: int,
                        revision: int, batch_no: str) -> None:
    conn.execute(
        "INSERT INTO adjudication(lsource, lref, rsource, rref, kind, present,"
        " present_rev, present_batch) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(lsource, lref, rsource, rref) DO UPDATE SET "
        "kind=excluded.kind, present=excluded.present, "
        "present_rev=excluded.present_rev, "
        "present_batch=excluded.present_batch",
        (a[0], a[1], b[0], b[1], kind, present, revision, batch_no))


def _insert_member(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        "INSERT INTO defect_member(source, source_ref, group_key, category, "
        "lane, found_at, reg_id, reg_n, reg_source_system, reg_target_system, "
        "reg_n_from, reg_n_to, location_status, position_count, axis_id, "
        "loc_lo, loc_hi, evidence_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["source"], row["source_ref"], row["group_key"], row["category"],
         row["lane"], row["found_at"], row["reg_id"], row["reg_n"],
         row["reg_source_system"], row["reg_target_system"],
         row["reg_n_from"], row["reg_n_to"], row["location_status"],
         row["position_count"], row["axis_id"], row["loc_lo"],
         row["loc_hi"], row["evidence_json"]))


_MEMBER_COLS = ("source", "source_ref", "group_key", "category", "lane",
                "found_at", "reg_id", "reg_n", "reg_source_system",
                "reg_target_system", "reg_n_from", "reg_n_to",
                "location_status", "position_count", "axis_id", "loc_lo",
                "loc_hi", "evidence_json")
_GROUP_COLS = ("group_key", "isolated", "member_count", "first_found",
               "last_found")


def group_public(row: dict) -> dict:
    """组行的公开形态（全量与增量 after/before 完全一致）。"""
    row = dict(row)
    return {
        "group_key": row["group_key"],
        "isolated": bool(row["isolated"]),
        "member_count": row["member_count"],
        "first_found": row["first_found"],
        "last_found": row["last_found"],
    }


def member_public(row: dict) -> dict:
    """成员行的公开形态（全量与增量 after/before 完全一致）。"""
    row = dict(row)
    return {
        "source": row["source"],
        "source_ref": row["source_ref"],
        "group_key": row["group_key"],
        "category": row["category"],
        "lane": row["lane"],
        "found_at": row["found_at"],
        "registration": {
            "reg_id": row["reg_id"],
            "reg_n": row["reg_n"],
            "source_system": row["reg_source_system"],
            "target_system": row["reg_target_system"],
            "n_from": row["reg_n_from"],
            "n_to": row["reg_n_to"],
        },
        "location": {
            "status": row["location_status"],
            "position_count": row["position_count"],
            "axis_id": row["axis_id"],
            "lo": row["loc_lo"],
            "hi": row["loc_hi"],
        },
        "evidence": (json.loads(row["evidence_json"])
                     if row["evidence_json"] is not None else None),
    }


def _group_row_json(row: dict) -> str:
    return json.dumps(group_public(row), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def _member_row_json(row: dict) -> str:
    return json.dumps(member_public(row), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def _diff_groups(conn, revision, batch_no, affected_old_groups,
                 old_group_rows, new_groups) -> List[tuple]:
    out: List[tuple] = []
    for gk in sorted(affected_old_groups | set(new_groups)):
        before = old_group_rows.get(gk)
        info = new_groups.get(gk)
        after = None if info is None else {
            "group_key": gk, "isolated": bool(info["isolated"]),
            "member_count": len(info["keys"]),
            "first_found": info["first_found"],
            "last_found": info["last_found"]}
        if before is None and after is None:
            continue
        if before is None:
            out.append(("defect_group", gk, "+", None, _group_row_json(after)))
        elif after is None:
            out.append(("defect_group", gk, "-", _group_row_json(before), None))
        elif _group_row_json(before) != _group_row_json(after):
            out.append(("defect_group", gk, "~", _group_row_json(before),
                        _group_row_json(after)))
    return out


def _diff_members(conn, revision, batch_no, old_member_rows,
                  new_member_rows) -> List[tuple]:
    out: List[tuple] = []
    candidates = set(old_member_rows) | set(new_member_rows)
    for key in sorted(candidates):
        before = old_member_rows.get(key)
        after = new_member_rows.get(key)
        if before is None and after is None:
            continue
        if before is None:
            out.append(("defect_member", key_json(*key), "+", None,
                        _member_row_json(after)))
        elif after is None:
            # 只有旧组受影响（被拆/被合）时才可能有删除
            out.append(("defect_member", key_json(*key), "-",
                        _member_row_json(before), None))
        elif _member_row_json(before) != _member_row_json(after):
            out.append(("defect_member", key_json(*key), "~",
                        _member_row_json(before), _member_row_json(after)))
    return out


def _insert_changes(conn, revision, batch_no, changes: List[tuple]) -> None:
    conn.executemany(
        "INSERT INTO change(revision, batch_no, table_name, row_key, op, "
        "before_json, after_json) VALUES (?,?,?,?,?,?,?)",
        [(revision, batch_no, table, key, op, before, after)
         for table, key, op, before, after in changes])


# --------------------------------------------------------------------------- #
# 全量与增量（工单侧接表）
# --------------------------------------------------------------------------- #
def fetch_full(workspace_dir: str) -> Dict[str, Any]:
    """全量：一次取得两张表与它们所属的修订号（同一读事务内）。"""
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        revision = int(_get_meta(conn, "revision"))
        groups = [_group_out(r) for r in conn.execute(
            "SELECT * FROM defect_group ORDER BY group_key")]
        members = [_member_out(r) for r in conn.execute(
            "SELECT * FROM defect_member ORDER BY source, source_ref")]
        conn.rollback()
        return {
            "format": FORMAT,
            "workspace_id": _get_meta(conn, "workspace_id"),
            "revision": revision,
            "defect_groups": groups,
            "defect_members": members,
        }
    finally:
        conn.close()


def fetch_changes(workspace_dir: str, since_revision: int
                  ) -> Dict[str, Any]:
    """增量：返回 ``since_revision`` 之后每个成功批的行级差异。

    每个批以"该批前/后两张表"的差异为准；没有表变化也返回一个空批，
    以确认该修订号已处理。批按修订升序，读者可逐批应用。
    """
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        current = int(_get_meta(conn, "revision"))
        if since_revision > current:
            conn.rollback()
            raise ChainageError(
                f"起始修订 {since_revision} 晚于当前修订 {current}")
        batches: List[dict] = []
        for rev, batch_no in conn.execute(
                "SELECT revision, batch_no FROM revision_stat "
                "WHERE revision > ? ORDER BY revision", (since_revision,)):
            changes = []
            for r in conn.execute(
                    "SELECT * FROM change WHERE revision=? "
                    "ORDER BY table_name, row_key", (rev,)):
                changes.append({
                    "table": r["table_name"],
                    "key": (r["row_key"] if r["table_name"] == "defect_group"
                            else _key_tuple(r["row_key"])),
                    "op": r["op"],
                    "before": (json.loads(r["before_json"])
                               if r["before_json"] is not None else None),
                    "after": (json.loads(r["after_json"])
                              if r["after_json"] is not None else None),
                })
            batches.append({"revision": rev, "batch_no": batch_no,
                            "changes": changes})
        conn.rollback()
        return {
            "format": FORMAT,
            "workspace_id": _get_meta(conn, "workspace_id"),
            "from_revision": since_revision,
            "revision": current,
            "batches": batches,
        }
    finally:
        conn.close()


def _group_out(r: sqlite3.Row) -> dict:
    return group_public(dict(r))


def _member_out(r: sqlite3.Row) -> dict:
    return member_public(dict(r))


# --------------------------------------------------------------------------- #
# 组视图（隔离成员仍可复核）
# --------------------------------------------------------------------------- #
def list_groups(workspace_dir: str, isolated_only: bool = False
                ) -> Dict[str, Any]:
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        revision = int(_get_meta(conn, "revision"))
        sql = "SELECT * FROM defect_group"
        if isolated_only:
            sql += " WHERE isolated=1"
        sql += " ORDER BY group_key"
        groups = [_group_out(r) for r in conn.execute(sql)]
        conn.rollback()
        return {"revision": revision, "groups": groups}
    finally:
        conn.close()


def group_detail(workspace_dir: str, group_key: str) -> Dict[str, Any]:
    """取某个组的成员（含隔离成员），并给出隔离证据（若被隔离）。"""
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        revision = int(_get_meta(conn, "revision"))
        g = conn.execute(
            "SELECT * FROM defect_group WHERE group_key=?",
            (group_key,)).fetchone()
        if g is None:
            conn.rollback()
            raise ChainageError(f"病害组不存在: {group_key}")
        members = [_member_out(r) for r in conn.execute(
            "SELECT * FROM defect_member WHERE group_key=? "
            "ORDER BY source, source_ref", (group_key,))]
        evidence = _isolation_evidence_conn(conn, group_key) if g["isolated"] \
            else None
        conn.rollback()
        return {
            "revision": revision,
            "group": _group_out(g),
            "members": members,
            "isolation_evidence": evidence,
        }
    finally:
        conn.close()


def isolation_evidence(workspace_dir: str, group_key: str) -> Dict[str, Any]:
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        g = conn.execute(
            "SELECT * FROM defect_group WHERE group_key=?",
            (group_key,)).fetchone()
        if g is None:
            conn.rollback()
            raise ChainageError(f"病害组不存在: {group_key}")
        if not g["isolated"]:
            conn.rollback()
            return {"group_key": group_key, "isolated": False,
                    "different_edges": []}
        out = _isolation_evidence_conn(conn, group_key)
        out["isolated"] = True
        conn.rollback()
        return out
    finally:
        conn.close()


def _isolation_evidence_conn(conn: sqlite3.Connection,
                             group_key: str) -> Dict[str, Any]:
    """列出导致隔离的每条生效「不同」裁定，并各给一条「同一」路径。

    路径只用当前在册报件与当前生效的「同一」裁定，不用已撤下报件或
    已撤去裁定做解释。只给一条路径（不枚举所有路径）。
    """
    keys = [(r["source"], r["source_ref"]) for r in conn.execute(
        "SELECT source, source_ref FROM defect_member WHERE group_key=? "
        "ORDER BY source, source_ref", (group_key,))]
    edges = _different_edges_within(conn, keys)
    out_edges = []
    for a, b in edges:
        path = _same_path(conn, a, b, keys)
        out_edges.append({
            "kind": DIFFERENT,
            "between": [list(a), list(b)],
            "same_path": [list(k) for k in path],
        })
    return {"group_key": group_key, "isolated": bool(edges),
            "different_edges": out_edges}


def _same_path(conn: sqlite3.Connection, start: Tuple[str, str],
               goal: Tuple[str, str],
               allowed: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """一条由当前生效「同一」边串起、两端都在册的 BFS 路径（含两端）。"""
    allowed_set = set(allowed)
    prev: Dict[Tuple[str, str], Optional[Tuple[str, str]]] = {start: None}
    queue = [start]
    while queue:
        cur = queue.pop(0)
        if cur == goal:
            break
        for nb in _effective_same_neighbors(conn, cur):
            if nb in prev or nb not in allowed_set:
                continue
            if _present_defect(conn, nb) is None:
                continue
            prev[nb] = cur
            queue.append(nb)
    if goal not in prev:
        # 理论上不会发生：组是按同一连通分量建的
        raise ChainageError("内部错误：隔离证据的同一路径不可达")
    path: List[Tuple[str, str]] = []
    cur: Optional[Tuple[str, str]] = goal
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    path.reverse()
    return path


# --------------------------------------------------------------------------- #
# 候选：审核员从一条在册报件出发的跨来源配对（只读，不改变病害身份）
# --------------------------------------------------------------------------- #
def _direct_adjudicated_pairs(conn: sqlite3.Connection,
                              start: Tuple[str, str]) -> Set[Tuple[Tuple[str, str], Tuple[str, str]]]:
    """起点当前有生效裁定的所有报件对（同一或不同）。

    "还没有直接裁定"按**当前生效**的裁定理解：撤去裁定使该对重新回到
    待核对状态，候选会再次出现；历史裁定行仍保留在裁定表中备查。
    """
    src, ref = start
    rows = conn.execute(
        "SELECT lsource, lref, rsource, rref FROM adjudication "
        "WHERE present=1 AND ((lsource=? AND lref=?) OR (rsource=? AND rref=?))",
        (src, ref, src, ref)).fetchall()
    out = set()
    for ls, lr, rs, rr in rows:
        a, b = (ls, lr), (rs, rr)
        pair = _norm_pair(a, b)
        if pair is not None:
            out.add(pair)
    return out


def candidate_pairs(workspace_dir: str, start_source: str,
                    start_ref: str, spatial_threshold: Optional[int] = None,
                    time_threshold: Optional[int] = None,
                    limit: Optional[int] = None,
                    explain_scan: bool = False) -> Dict[str, Any]:
    """从一条在册报件出发，列出还没有直接裁定的跨来源候选配对。

    候选条件（全部同时满足，端点算命中）：
    - 对方也在册，``source`` 与起点不同；
    - 同病害类别、同车道、同轴；
    - 双方都恰好一个物理位置；
    - 刻度差 ``|x1-x2| <= spatial_threshold``；
    - 发现时间差 ``|t1-t2| <= time_threshold``。

    这只是候选：即使两条报件当前处在同一病害里，也不代表有直接裁定；
    候选的出现或消失本身不改变任何病害身份。

    ``explain_scan`` 为真时附加 ``scan`` 报告：预筛访问的索引行数、
    预筛后逐对精确判断数、最终命中数——命中条数不等于扫描量。
    """
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        revision = int(_get_meta(conn, "revision"))
        st = (int(_get_meta(conn, "spatial_threshold"))
              if spatial_threshold is None else spatial_threshold)
        tt = (int(_get_meta(conn, "time_threshold"))
              if time_threshold is None else time_threshold)
        if not _is_int(st) or not _is_int(tt) or st < 0 or tt < 0:
            conn.rollback()
            raise ChainageError("阈值必须是 >= 0 的整数")

        start_row = conn.execute(
            "SELECT * FROM defect WHERE source=? AND source_ref=? AND present=1",
            (start_source, start_ref)).fetchone()
        if start_row is None:
            conn.rollback()
            raise ChainageError(
                f"报件不在册: ({start_source!r}, {start_ref!r})")
        base = {
            "source": start_row["source"],
            "source_ref": start_row["source_ref"],
            "category": start_row["category"],
            "lane": start_row["lane"],
            "found_at": start_row["found_at"],
            "location": {
                "status": start_row["location_status"],
                "position_count": start_row["position_count"],
                "axis_id": start_row["axis_id"],
                "lo": start_row["loc_lo"],
                "hi": start_row["loc_hi"],
            },
            "registration": {
                "reg_id": start_row["reg_id"], "reg_n": start_row["reg_n"],
            },
        }
        adjudicated = _direct_adjudicated_pairs(
            conn, (start_source, start_ref))

        scan = {
            "bucket_rows": 0,
            "space_time_preselected": 0,
            "pairs_examined": 0,
            "candidates": 0,
        }
        candidates: List[dict] = []
        if start_row["location_status"] != LOC_LOCALIZABLE:
            # 起点没有唯一位置：不参加按位置找候选
            conn.rollback()
            return {
                "revision": revision,
                "start": base,
                "thresholds": {"spatial": st, "time": tt},
                "eligible": False,
                "reason": "start_not_locatable",
                "candidates": [],
                **({"scan": scan} if explain_scan else {}),
            }

        sx = start_row["loc_lo"]
        sy = start_row["loc_hi"]
        s_axis = start_row["axis_id"]
        s_time = start_row["found_at"]

        # 同 (类别,车道,轴) 分桶 + 空间窗口 + 时间窗口三重预筛，一次
        # 索引范围查询剔除轴/空间/时间之外的无关报件；不在 SQL 里枚举对。
        scan["bucket_rows"] = conn.execute(
            "SELECT COUNT(*) FROM pair_bucket WHERE category=? AND lane=? "
            "AND axis_id=?",
            (start_row["category"], start_row["lane"], s_axis)).fetchone()[0]
        rows = conn.execute(
            "SELECT source, source_ref, found_at, loc_lo, loc_hi "
            "FROM pair_bucket "
            "WHERE category=? AND lane=? AND axis_id=? "
            "AND loc_hi >= ? AND loc_lo <= ? "
            "AND found_at >= ? AND found_at <= ? "
            "ORDER BY source, source_ref",
            (start_row["category"], start_row["lane"], s_axis,
             sx - st, sy + st, s_time - tt, s_time + tt)).fetchall()
        scan["space_time_preselected"] = len(rows)

        for r in rows:
            key = (r["source"], r["source_ref"])
            if key == (start_source, start_ref):
                continue
            if r["source"] == start_source:
                continue              # 只列跨来源配对
            scan["pairs_examined"] += 1
            # 两个窗口都要满足（空间预筛已过，时间在此处精确判断）
            if abs(r["found_at"] - s_time) > tt:
                continue
            if abs(r["loc_lo"] - sx) > st or abs(r["loc_hi"] - sy) > st:
                continue
            pair = _norm_pair((start_source, start_ref), key)
            if pair in adjudicated:
                continue
            other = conn.execute(
                "SELECT * FROM defect WHERE source=? AND source_ref=?",
                key).fetchone()
            candidates.append({
                "other": {
                    "source": r["source"],
                    "source_ref": r["source_ref"],
                    "found_at": r["found_at"],
                    "location": {
                        "status": other["location_status"],
                        "position_count": other["position_count"],
                        "axis_id": other["axis_id"],
                        "lo": other["loc_lo"],
                        "hi": other["loc_hi"],
                    },
                    "registration": {
                        "reg_id": other["reg_id"],
                        "reg_n": other["reg_n"],
                    },
                },
                "spatial_delta": abs(other["loc_lo"] - sx),
                "time_delta": abs(r["found_at"] - s_time),
            })
        candidates.sort(key=lambda c: (c["other"]["source"],
                                       c["other"]["source_ref"]))
        scan["candidates"] = len(candidates)
        if limit is not None:
            if not _is_int(limit) or limit < 0:
                conn.rollback()
                raise ChainageError("limit 必须是 >= 0 的整数")
            candidates = candidates[:limit]
        conn.rollback()
        out = {
            "revision": revision,
            "start": base,
            "thresholds": {"spatial": st, "time": tt},
            "eligible": True,
            "candidates": candidates,
        }
        if explain_scan:
            out["scan"] = scan
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 审核侧只读视图：在册报件、裁定列表
# --------------------------------------------------------------------------- #
def list_reports(workspace_dir: str, present_only: bool = True,
                 source: Optional[str] = None) -> Dict[str, Any]:
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        revision = int(_get_meta(conn, "revision"))
        sql = "SELECT * FROM defect"
        cond = []
        params: List[Any] = []
        if present_only:
            cond.append("present=1")
        if source is not None:
            cond.append("source=?")
            params.append(source)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY source, source_ref"
        rows = []
        for r in conn.execute(sql, params):
            rows.append({
                "source": r["source"],
                "source_ref": r["source_ref"],
                "present": bool(r["present"]),
                "category": r["category"],
                "lane": r["lane"],
                "found_at": r["found_at"],
                "registration": {"reg_id": r["reg_id"],
                                 "reg_n": r["reg_n"]},
                "location": {
                    "status": r["location_status"],
                    "position_count": r["position_count"],
                    "axis_id": r["axis_id"],
                    "lo": r["loc_lo"],
                    "hi": r["loc_hi"],
                },
                "report_content": {
                    "category": r["category"],
                    "lane": r["lane"],
                    "found_at": r["found_at"],
                    "reg_id": r["reg_id"],
                    "reg_n": r["reg_n"],
                },
                "evidence": json.loads(r["evidence_json"])
                if r["evidence_json"] else None,
            })
        conn.rollback()
        return {"revision": revision, "reports": rows}
    finally:
        conn.close()


def list_adjudications(workspace_dir: str,
                       effective_only: bool = True) -> Dict[str, Any]:
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        revision = int(_get_meta(conn, "revision"))
        sql = "SELECT * FROM adjudication"
        if effective_only:
            sql += " WHERE present=1"
        sql += " ORDER BY lsource, lref, rsource, rref"
        rows = [{
            "a": [r["lsource"], r["lref"]],
            "b": [r["rsource"], r["rref"]],
            "kind": r["kind"],
            "effective": bool(r["present"]),
            "since_revision": r["present_rev"],
            "batch_no": r["present_batch"],
        } for r in conn.execute(sql)]
        conn.rollback()
        return {"revision": revision, "adjudications": rows}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 局部重算度量（验收/运维用；不改变业务结果）
# --------------------------------------------------------------------------- #
def last_batch_scope(workspace_dir: str,
                     batch_no: Optional[str] = None) -> Dict[str, Any]:
    """某批实际触及的组与行（用于验收局部重算范围）。

    只报告该批增量里出现的组键与成员键，以及工作区当时的全量规模；
    不计算、也不预存任何非相邻报件的可达关系。
    """
    conn = open_workspace(workspace_dir)
    try:
        conn.execute("BEGIN")
        if batch_no is None:
            row = conn.execute(
                "SELECT batch_no FROM revision_stat ORDER BY revision DESC "
                "LIMIT 1").fetchone()
            if row is None:
                conn.rollback()
                return {"batch_no": None, "revision": 0, "groups": [],
                        "members": [], "counts": {}}
            batch_no = row[0]
        rec = conn.execute(
            "SELECT * FROM batch_receipt WHERE batch_no=?",
            (batch_no,)).fetchone()
        if rec is None:
            conn.rollback()
            raise ChainageError(f"批号不存在: {batch_no}")
        # 局部重算范围：该批实际读旧组、重算并写回的组与成员行，
        # 不论它们在增量里是否出现行变化（撤一条边不拆组时也能看到范围）。
        groups = [r["group_key"] for r in conn.execute(
            "SELECT group_key FROM batch_scope_group WHERE revision=? "
            "ORDER BY group_key", (rec["revision"],))]
        members = [(r["source"], r["source_ref"]) for r in conn.execute(
            "SELECT source, source_ref FROM batch_scope_member "
            "WHERE revision=? ORDER BY source, source_ref",
            (rec["revision"],))]
        changes = [{"table": r["table_name"], "key": r["row_key"],
                    "op": r["op"]}
                   for r in conn.execute(
                       "SELECT table_name, row_key, op FROM change "
                       "WHERE revision=? ORDER BY table_name, row_key",
                       (rec["revision"],))]
        # 读取/写入的行数：受累组数量、成员数量、全库组数量
        total_groups = conn.execute(
            "SELECT COUNT(*) FROM defect_group").fetchone()[0]
        total_members = conn.execute(
            "SELECT COUNT(*) FROM defect_member").fetchone()[0]
        total_reports = conn.execute(
            "SELECT COUNT(*) FROM defect WHERE present=1").fetchone()[0]
        conn.rollback()
        return {
            "batch_no": batch_no,
            "revision": rec["revision"],
            "groups": groups,
            "members": members,
            "changes": changes,
            "batch_counts": json.loads(rec["counts_json"]),
            "current_totals": {
                "groups": total_groups,
                "members": total_members,
                "reports_present": total_reports,
            },
        }
    finally:
        conn.close()
