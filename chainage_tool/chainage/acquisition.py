"""巡测车采集包装配（离线）。

在既有 ready 换算库（``chainage build`` 的产物）之上，把巡测车交回的
计划表、参考脉冲表、设备配置与各设备 JSONL 分卷装配成一份可交付质检的
SQLite 采集包：

- 每处计划窗口交付已归属的读数/照片索引与各通道数量；空窗口也列出，
  并逐一标出没有可用证据的期望通道；
- 不能归属的原始记录单列（缺时钟 / 完全窗外 / 局部交叠互不混淆）；
- 任一采样行或未归属记录可回看：原始计数、卷行、展开块，以及确定该
  时刻（或曝光两个端点）所用的回执与其参考时刻。

明确不做：

- 不读取照片二进制（resource 只是资源标识，从不打开）；
- 不从采样数据推测行驶路线（登记 ID 只核实存在并引用交接版本，
  不做桩号换算，也不改动换算库）。

时钟与归属规则（细节见 docs/ACQUISITION.md）：

- 同一 ``(设备, 启动号, 序号)`` 的业务字段一致重复只算一次（保留全部
  卷名/原行号）；内容冲突、未知引用、坏字段、重复窗口键、非法窗口与
  不可能的时钟记录都定位输入并拒绝交付，不按"最后收到"覆盖；
- 每次启动内序号每断开一次切出新的连续块；仅对同块相邻序号取
  ``[0, M)`` 非负模差，差大于 ``G`` 是结构错误，差为零合法；累计差
  得到块内展开坐标（十进制文本存储，大坐标不因整数列回卷）；
- 同一启动不重复回执同一脉冲；回执 UTC 按序号严格向前；块内相邻回执
  展开坐标严格向前。违反即输入错误；
- 块内相邻回执间线性插值（精确有理数，不先四舍五入）；读数落在回执
  坐标上就采用该回执 UTC（即使是全块唯一回执），其余读数必须被同块
  回执夹住；照片开/闭快门都须落在同块回执围住的范围内，闭快门坐标是
  展开开快门坐标加曝光计数 ``d``；不借别的块/启动/范围之外的时钟；
- 回执不足是质量结果而非输入错误：区分没有任何回执、开头不受约束、
  结尾不受约束；
- 读数按它在同轮次所落的一个窗口归档；已对时照片只有整段曝光落在
  一个窗口里才归档（闭快门正好等于窗口右端算在内），否则给出所有
  正长度相交窗口的精确交叠区间与未被任何窗口覆盖的子区间，"曝光
  离开窗口"与"跨越多扇窗"分别标记，完全不选重叠最长的一扇、也不
  重复计作可用证据。

工作内存边界：分卷逐行流入暂存表，拼接/展开/对齐/归因全部走 SQLite
游标分批落盘；进程内只常驻单块回执列表与单轮次窗口数组，不积压全包、
所有曝光或所有相交明细。归因按轮次二分 + 顺序扫描，实际检查的窗口行数
记入 ``stat.window_rows_examined``，不按 记录数×窗口数 做候选扫描。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import sqlite3
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__
from .database import open_ready_database
from .errors import AcquisitionValidationError, DatabaseError, ValidationError

ACQ_SCHEMA_VERSION = 1
STATUS_BUILDING = "building"
STATUS_READY = "ready"
TEMP_SUFFIX = ".tmp"

KIND_RECEIPT = "receipt"
KIND_READING = "reading"
KIND_PHOTO = "photo"
ALL_KINDS = (KIND_RECEIPT, KIND_READING, KIND_PHOTO)

CHANNEL_SCALAR = "scalar"
CHANNEL_PHOTO = "photo"

# 对齐状态 / 未归属类别（质量结果，不是输入错误）
ST_OK = "ok"
CAT_NO_RECEIPTS = "no_receipts"              # 展开块内没有任何回执
CAT_START_UNCONSTRAINED = "start_unconstrained"  # 记录/开快门在块内首个回执之前
CAT_END_UNCONSTRAINED = "end_unconstrained"      # 记录/闭快门在块内末个回执之后
CAT_OUTSIDE_WINDOWS = "outside_windows"      # 已对齐但完全落在所有窗口之外
CAT_PARTIAL_OVERLAP = "partial_overlap"      # 照片与窗口相交但整段曝光不在单一窗口内
ALL_CATEGORIES = (
    CAT_NO_RECEIPTS,
    CAT_START_UNCONSTRAINED,
    CAT_END_UNCONSTRAINED,
    CAT_OUTSIDE_WINDOWS,
    CAT_PARTIAL_OVERLAP,
)

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

_BATCH = 8192


# --------------------------------------------------------------------------- #
# 标识符与精确数
# --------------------------------------------------------------------------- #
def canon_id(value, what="标识符") -> str:
    """轮次/窗口/设备/启动号/通道/脉冲标识符：JSON 字符串或整数。

    库内以 JSON 编码文本存储（字符串 ``"1"`` 与整数 ``1`` 不混淆），
    展示时用 :func:`decode_id` 还原。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(
            "", "ACQ_BAD_IDENTIFIER",
            f"{what}必须是 JSON 字符串或整数，得到 {value!r}")
    if isinstance(value, str) and value == "":
        raise ValidationError("", "ACQ_BAD_IDENTIFIER", f"{what}不能为空串")
    return json.dumps(value, ensure_ascii=False)


def decode_id(canon: str):
    return json.loads(canon)


def parse_cli_id(text: str) -> str:
    """命令行标识符：先按 JSON 字面量解析（``'"W-1"'`` 或 ``1``），
    不是合法 JSON 时按普通字符串处理。"""
    try:
        return canon_id(json.loads(text))
    except json.JSONDecodeError:
        return canon_id(text)


def require_int(value, what, lo=None, hi=None) -> int:
    """UTC 纳秒与离散量只接受有符号 64 位 JSON 整数，拒绝浮点与布尔。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            "", "ACQ_BAD_INTEGER",
            f"{what}必须是 JSON 整数（不接受浮点数），得到 {value!r}")
    if lo is not None and value < lo:
        raise ValidationError("", "ACQ_INT_RANGE",
                              f"{what}={value} 小于下限 {lo}")
    if hi is not None and value > hi:
        raise ValidationError("", "ACQ_INT_RANGE",
                              f"{what}={value} 超出上限 {hi}")
    return value


def frac_parts(f: Fraction) -> Tuple[str, str]:
    return str(f.numerator), str(f.denominator)


def frac_str(f: Fraction) -> str:
    """精确数的文本形式：整数写 ``"123"``，非整数写约分后的 ``"p/q"``。"""
    return str(f.numerator) if f.denominator == 1 \
        else f"{f.numerator}/{f.denominator}"


def frac_from_parts(num: str, den: str) -> Fraction:
    return Fraction(int(num), int(den))


# --------------------------------------------------------------------------- #
# 库结构
# --------------------------------------------------------------------------- #
SCHEMA_SQL = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 交付审计：每个输入文件（计划/脉冲/设备配置/换算库/各分卷）的 sha256
CREATE TABLE inputs (
    kind   TEXT NOT NULL,
    name   TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);

CREATE TABLE device (
    device      TEXT PRIMARY KEY,   -- 设备号（JSON 编码标识符）
    modulus     INTEGER NOT NULL,   -- 计数模数 M
    max_advance INTEGER NOT NULL    -- 最大计数前进量 G（0 < G < M）
);

CREATE TABLE device_channel (
    device  TEXT NOT NULL,
    channel TEXT NOT NULL,
    ctype   TEXT NOT NULL,          -- scalar | photo
    PRIMARY KEY (device, channel)
);

CREATE TABLE pulse (
    pulse TEXT PRIMARY KEY,         -- 参考脉冲号（JSON 编码标识符）
    utc   INTEGER NOT NULL          -- UTC 纳秒（int64）
);

-- 计划窗口：轮次内互不重叠的 UTC 纳秒半开区间 [start_utc, end_utc)
CREATE TABLE plan_window (
    id                INTEGER PRIMARY KEY,
    round_key         TEXT NOT NULL,
    window_key        TEXT NOT NULL,
    start_utc         INTEGER NOT NULL,
    end_utc           INTEGER NOT NULL,
    registration      TEXT NOT NULL,  -- ready 换算库登记 ID（原样引用）
    expected_channels TEXT NOT NULL,  -- JSON: [[device, channel], ...]
    UNIQUE (round_key, window_key)
);

-- 拼接后的采集记录：同一 (device, boot, seq) 业务字段一致的重复只留一行
CREATE TABLE sample_record (
    id         INTEGER PRIMARY KEY,
    device     TEXT NOT NULL,
    boot       TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,       -- receipt | reading | photo
    raw_count  INTEGER NOT NULL,    -- [0, M) 原始计数
    pulse      TEXT,                -- receipt: 参考脉冲号
    round_key  TEXT,                -- reading/photo: 轮次
    channel    TEXT,                -- reading/photo: 通道
    value      INTEGER,             -- reading: 整数读数值
    resource   TEXT,                -- photo: 资源标识（不读取其二进制）
    exposure   INTEGER,             -- photo: 曝光计数 d（正整数，< M）
    business   TEXT NOT NULL,       -- 业务字段规范 JSON（去重/冲突判定）
    UNIQUE (device, boot, seq)
);

-- 原始行址证据：同一记录出现在哪些卷哪一行（重复记录保留全部行址）
CREATE TABLE evidence (
    record_id INTEGER NOT NULL,
    volume    TEXT NOT NULL,
    line      INTEGER NOT NULL,
    PRIMARY KEY (record_id, volume, line)
);

-- 展开块：同一启动内序号连续的一段
CREATE TABLE expand_block (
    id           INTEGER PRIMARY KEY,
    device       TEXT NOT NULL,
    boot         TEXT NOT NULL,
    seq_first    INTEGER NOT NULL,
    seq_last     INTEGER NOT NULL,
    n_records    INTEGER NOT NULL,
    n_receipts   INTEGER NOT NULL,
    max_expanded TEXT NOT NULL      -- 块内最大展开坐标（十进制文本，不回卷）
);

-- 每条记录的展开坐标（十进制文本；大坐标不因数据库整数列回卷）
CREATE TABLE placement (
    record_id INTEGER PRIMARY KEY,
    block_id  INTEGER NOT NULL,
    expanded  TEXT NOT NULL
);

-- 对齐结果：读数一个时刻；照片开/闭快门两个端点；附所用回执记录 id
CREATE TABLE alignment (
    record_id INTEGER PRIMARY KEY,
    status    TEXT NOT NULL,        -- ok | no_receipts | start_unconstrained | end_unconstrained
    utc_num   TEXT, utc_den TEXT,   -- reading: 精确 UTC（约分后的分子/分母）
    open_num  TEXT, open_den TEXT,  -- photo: 开快门 UTC
    close_num TEXT, close_den TEXT, -- photo: 闭快门 UTC
    ro_lo INTEGER, ro_hi INTEGER,   -- 确定读数时刻/开快门的回执（exact 时相同）
    rc_lo INTEGER, rc_hi INTEGER    -- 确定闭快门的回执
);

-- 已归属：记录 -> 窗口
CREATE TABLE attribution (
    record_id INTEGER PRIMARY KEY,
    window_id INTEGER NOT NULL
);

-- 未归属：质量结果单列（类别见 ALL_CATEGORIES）
CREATE TABLE unattributed (
    record_id INTEGER PRIMARY KEY,
    category  TEXT NOT NULL,
    detail    TEXT NOT NULL         -- JSON 细节（原因标志、精确端点等）
);

-- 未归属照片与窗口的全部正长度交叠（精确区间）
CREATE TABLE photo_overlap (
    record_id INTEGER NOT NULL,
    window_id INTEGER NOT NULL,
    lo_num TEXT NOT NULL, lo_den TEXT NOT NULL,
    hi_num TEXT NOT NULL, hi_den TEXT NOT NULL,
    PRIMARY KEY (record_id, window_id)
);

-- 未归属照片曝光内尚未被任何窗口覆盖的子区间 [lo, hi)
CREATE TABLE photo_gap (
    record_id INTEGER NOT NULL,
    seq_no    INTEGER NOT NULL,
    lo_num TEXT NOT NULL, lo_den TEXT NOT NULL,
    hi_num TEXT NOT NULL, hi_den TEXT NOT NULL,
    PRIMARY KEY (record_id, seq_no)
);

-- 每窗口每通道的已归属数量（含非期望通道；不代替缺失期望通道）
CREATE TABLE window_channel_count (
    window_id INTEGER NOT NULL,
    device    TEXT NOT NULL,
    channel   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    n         INTEGER NOT NULL,
    PRIMARY KEY (window_id, device, channel, kind)
);

-- 构建期暂存（finalize 前 DROP，不留在交付库内）
CREATE TABLE staging (
    volume   TEXT NOT NULL,
    line     INTEGER NOT NULL,
    device   TEXT, boot TEXT, seq INTEGER, kind TEXT, raw_count INTEGER,
    pulse TEXT, round_key TEXT, channel TEXT,
    value INTEGER, resource TEXT, exposure INTEGER,
    business TEXT NOT NULL
);
"""

INDEXES_SQL = """
CREATE INDEX idx_placement_block ON placement(block_id);
CREATE INDEX idx_attribution_window ON attribution(window_id);
CREATE INDEX idx_unattributed_cat ON unattributed(category);
CREATE INDEX idx_record_round ON sample_record(round_key, kind);
"""


def _new_build_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = OFF")      # 临时库失败即删，无需回滚日志
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = FILE")       # 排序/临时表走磁盘
    conn.execute("PRAGMA cache_size = -8000")      # 约 8 MiB 页缓存
    return conn


# --------------------------------------------------------------------------- #
# 输入读取
# --------------------------------------------------------------------------- #
class _HashedLines:
    """逐行读取并同步累计 sha256（不把整个文件读进内存）。"""

    def __init__(self, path: str):
        self.path = path
        self._h = hashlib.sha256()
        self.lineno = 0

    def __iter__(self):
        with open(self.path, "rb") as fh:
            for raw in fh:
                self.lineno += 1
                self._h.update(raw)
                yield self.lineno, raw

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(package_dir: str) -> dict:
    path = os.path.join(package_dir, "manifest.json")
    try:
        with open(path, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise AcquisitionValidationError([ValidationError(
            path, "ACQ_BAD_MANIFEST", f"无法读取采集交接包清单: {e}")])
    errors: List[ValidationError] = []
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict):
        errors.append(ValidationError(
            path, "ACQ_BAD_MANIFEST", "清单必须是含 files 对象的 JSON 对象"))
        files = {}
    version = doc.get("version") if isinstance(doc, dict) else None
    if not isinstance(version, str) or not version:
        errors.append(ValidationError(
            path, "ACQ_BAD_MANIFEST", "清单缺少非空 version 字符串"))
    out = {"version": version}
    for key in ("plan", "pulses", "devices"):
        v = files.get(key)
        if not isinstance(v, str) or not v:
            errors.append(ValidationError(
                path, "ACQ_BAD_MANIFEST", f"files.{key} 必须是相对路径字符串"))
        else:
            out[key] = os.path.normpath(os.path.join(package_dir, v))
    volumes = files.get("volumes")
    if (not isinstance(volumes, list) or not volumes
            or any(not isinstance(v, str) or not v for v in volumes)):
        errors.append(ValidationError(
            path, "ACQ_BAD_MANIFEST",
            "files.volumes 必须是非空相对路径数组（顺序即分卷处理顺序）"))
    else:
        out["volumes"] = [os.path.normpath(os.path.join(package_dir, v))
                          for v in volumes]
    if errors:
        raise AcquisitionValidationError(errors)
    return out


def _load_devices(path: str, errors: List[ValidationError]):
    """设备配置：{devices: [{device, modulus, max_advance,
    channels: [[通道, scalar|photo], ...]}]}"""
    cfg: Dict[str, dict] = {}
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        errors.append(ValidationError(path, "ACQ_IO", f"无法读取设备配置: {e}"))
        return cfg, None
    sha = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        errors.append(ValidationError(
            path, "ACQ_BAD_JSON", f"设备配置不是合法 JSON: {e}"))
        return cfg, sha
    devices = doc.get("devices") if isinstance(doc, dict) else None
    if not isinstance(devices, list) or not devices:
        errors.append(ValidationError(
            path, "ACQ_BAD_FIELD", "设备配置必须是含非空 devices 数组的 JSON 对象"))
        return cfg, sha
    for i, entry in enumerate(devices):
        where = f"{path}:devices[{i}]"
        try:
            if not isinstance(entry, dict):
                raise ValidationError("", "ACQ_BAD_FIELD", "设备条目必须是对象")
            unknown = set(entry) - {"device", "modulus", "max_advance",
                                    "channels"}
            if unknown:
                raise ValidationError("", "ACQ_UNKNOWN_FIELD",
                                      f"未知字段 {sorted(unknown)}")
            dev = canon_id(entry.get("device"), "设备号")
            modulus = require_int(entry.get("modulus"), "计数模数 M",
                                  lo=2, hi=INT64_MAX)
            advance = require_int(entry.get("max_advance"), "最大计数前进量 G",
                                  lo=1, hi=INT64_MAX)
            if not advance < modulus:
                raise ValidationError(
                    "", "ACQ_BAD_FIELD",
                    f"需要 0 < G < M，得到 G={advance}, M={modulus}")
            channels = entry.get("channels")
            if not isinstance(channels, list) or not channels:
                raise ValidationError(
                    "", "ACQ_BAD_FIELD",
                    "channels 必须是非空 [[通道, 类型], ...] 数组")
            chmap: Dict[str, str] = {}
            for pair in channels:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValidationError(
                        "", "ACQ_BAD_FIELD",
                        f"通道条目必须是 [通道, 类型] 二元组: {pair!r}")
                ch = canon_id(pair[0], "通道号")
                ctype = pair[1]
                if ctype not in (CHANNEL_SCALAR, CHANNEL_PHOTO):
                    raise ValidationError(
                        "", "ACQ_BAD_FIELD",
                        f"通道类型必须是 scalar 或 photo，得到 {ctype!r}")
                if ch in chmap:
                    raise ValidationError(
                        "", "ACQ_DUP_CHANNEL",
                        f"设备 {json.loads(dev)!r} 的通道 {pair[0]!r} 重复声明")
                chmap[ch] = ctype
            if dev in cfg:
                raise ValidationError(
                    "", "ACQ_DUP_DEVICE",
                    f"设备 {entry.get('device')!r} 重复配置")
            cfg[dev] = {"M": modulus, "G": advance, "channels": chmap}
        except ValidationError as e:
            e.location = e.location or where
            errors.append(e)
    return cfg, sha


def _load_pulses(path: str, errors: List[ValidationError]):
    pulses: Dict[str, int] = {}
    lines = _HashedLines(path)
    try:
        for lineno, raw in lines:
            where = f"{path}:{lineno}"
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                errors.append(ValidationError(
                    where, "ACQ_BAD_JSON", f"非法 JSON 行: {e}"))
                continue
            try:
                if not isinstance(obj, dict) or set(obj) != {"pulse", "utc"}:
                    raise ValidationError(
                        "", "ACQ_BAD_FIELD",
                        "脉冲行必须是且只含 {pulse, utc} 的对象")
                p = canon_id(obj["pulse"], "参考脉冲号")
                utc = require_int(obj["utc"], "脉冲 UTC 纳秒",
                                  lo=INT64_MIN, hi=INT64_MAX)
                if p in pulses:
                    raise ValidationError(
                        "", "ACQ_DUP_PULSE",
                        f"参考脉冲号 {obj['pulse']!r} 重复")
                pulses[p] = utc
            except ValidationError as e:
                e.location = where
                errors.append(e)
    except OSError as e:
        errors.append(ValidationError(path, "ACQ_IO", f"无法读取脉冲表: {e}"))
    return pulses, lines.hexdigest()


def _load_plan(path: str, registrations: set, cfg: Dict[str, dict],
               errors: List[ValidationError]):
    """计划表 JSONL：{round, window, start_utc, end_utc, registration,
    expected_channels}。返回按 (轮次, 起点, 窗口号) 排序的窗口行。"""
    rows: List[dict] = []
    lines = _HashedLines(path)
    try:
        for lineno, raw in lines:
            where = f"{path}:{lineno}"
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                errors.append(ValidationError(
                    where, "ACQ_BAD_JSON", f"非法 JSON 行: {e}"))
                continue
            try:
                if not isinstance(obj, dict):
                    raise ValidationError("", "ACQ_BAD_FIELD",
                                          "计划行必须是 JSON 对象")
                required = {"round", "window", "start_utc", "end_utc",
                            "registration", "expected_channels"}
                unknown = set(obj) - required
                if unknown:
                    raise ValidationError("", "ACQ_UNKNOWN_FIELD",
                                          f"未知字段 {sorted(unknown)}")
                missing = required - set(obj)
                if missing:
                    raise ValidationError("", "ACQ_MISSING_FIELD",
                                          f"缺少字段 {sorted(missing)}")
                rnd = canon_id(obj["round"], "巡测轮次")
                win = canon_id(obj["window"], "窗口号")
                start = require_int(obj["start_utc"], "窗口起点 UTC",
                                    lo=INT64_MIN, hi=INT64_MAX)
                end = require_int(obj["end_utc"], "窗口终点 UTC",
                                  lo=INT64_MIN, hi=INT64_MAX)
                if not start < end:
                    raise ValidationError(
                        "", "ACQ_BAD_WINDOW",
                        f"非法窗口：需要 start_utc < end_utc，得到 "
                        f"[{start}, {end})")
                reg = obj["registration"]
                if not isinstance(reg, str) or not reg:
                    raise ValidationError(
                        "", "ACQ_BAD_FIELD",
                        "登记 ID 必须是非空字符串（ready 换算库中的 reg_id）")
                if reg not in registrations:
                    raise ValidationError(
                        "", "ACQ_UNKNOWN_REGISTRATION",
                        f"登记 ID {reg!r} 不在 ready 换算库中")
                exp = obj["expected_channels"]
                if not isinstance(exp, list):
                    raise ValidationError(
                        "", "ACQ_BAD_FIELD",
                        "expected_channels 必须是 [[设备号, 通道号], ...] 数组")
                exp_pairs: List[List[str]] = []
                seen: set = set()
                for pair in exp:
                    if not isinstance(pair, list) or len(pair) != 2:
                        raise ValidationError(
                            "", "ACQ_BAD_FIELD",
                            f"期望通道必须是 [设备号, 通道号] 二元组: {pair!r}")
                    d = canon_id(pair[0], "期望通道设备号")
                    c = canon_id(pair[1], "期望通道号")
                    if d not in cfg or c not in cfg[d]["channels"]:
                        raise ValidationError(
                            "", "ACQ_UNKNOWN_CHANNEL",
                            f"期望通道 ({pair[0]!r}, {pair[1]!r}) "
                            f"不在设备配置的合法通道中")
                    if (d, c) in seen:
                        raise ValidationError(
                            "", "ACQ_DUP_EXPECTED",
                            f"期望通道 ({pair[0]!r}, {pair[1]!r}) 重复")
                    seen.add((d, c))
                    exp_pairs.append([d, c])
                rows.append({"round": rnd, "window": win, "start": start,
                             "end": end, "registration": reg,
                             "expected": exp_pairs, "line": lineno})
            except ValidationError as e:
                e.location = e.location or where
                errors.append(e)
    except OSError as e:
        errors.append(ValidationError(path, "ACQ_IO", f"无法读取计划表: {e}"))
        return rows, lines.hexdigest()

    # 重复窗口键（同轮次同窗口号）
    seen_keys: Dict[Tuple[str, str], int] = {}
    for r in rows:
        key = (r["round"], r["window"])
        if key in seen_keys:
            errors.append(ValidationError(
                f"{path}:{r['line']}", "ACQ_DUP_WINDOW",
                f"重复窗口键 (轮次 {json.loads(key[0])!r}, "
                f"窗口 {json.loads(key[1])!r})，首次出现在 {path}:"
                f"{seen_keys[key]}"))
        else:
            seen_keys[key] = r["line"]
    # 轮次内窗口不得重叠（可以紧邻或留空）
    by_round: Dict[str, List[dict]] = {}
    for r in rows:
        by_round.setdefault(r["round"], []).append(r)
    for rnd, ws in by_round.items():
        ws.sort(key=lambda r: (r["start"], r["end"], r["window"]))
        for a, b in zip(ws, ws[1:]):
            if b["start"] < a["end"]:
                errors.append(ValidationError(
                    f"{path}:{b['line']}", "ACQ_OVERLAP_WINDOW",
                    f"非法窗口：[{b['start']},{b['end']}) 与同轮次窗口 "
                    f"[{a['start']},{a['end']}) 重叠"))
    rows.sort(key=lambda r: (r["round"], r["start"], r["window"]))
    return rows, lines.hexdigest()


# --------------------------------------------------------------------------- #
# 分卷记录校验与暂存
# --------------------------------------------------------------------------- #
_RECORD_COMMON = {"device", "boot", "seq", "kind", "count"}
_KIND_FIELDS = {
    KIND_RECEIPT: {"pulse"},
    KIND_READING: {"round", "channel", "value"},
    KIND_PHOTO: {"round", "channel", "resource", "exposure"},
}


def _validate_record(obj, cfg, pulses, rounds):
    """返回 (device, boot, seq, kind, count, pulse, round, channel,
    value, resource, exposure, business)；未知引用/坏字段抛 ValidationError。"""
    if not isinstance(obj, dict):
        raise ValidationError("", "ACQ_BAD_FIELD", "记录必须是 JSON 对象")
    kind = obj.get("kind")
    if kind not in ALL_KINDS:
        raise ValidationError("", "ACQ_BAD_KIND",
                              f"kind 必须是 receipt/reading/photo，得到 {kind!r}")
    allowed = _RECORD_COMMON | _KIND_FIELDS[kind]
    unknown = set(obj) - allowed
    if unknown:
        raise ValidationError("", "ACQ_UNKNOWN_FIELD",
                              f"{kind} 记录不允许字段 {sorted(unknown)}")
    missing = allowed - set(obj)
    if missing:
        raise ValidationError("", "ACQ_MISSING_FIELD",
                              f"缺少字段 {sorted(missing)}")
    dev = canon_id(obj["device"], "设备号")
    if dev not in cfg:
        raise ValidationError("", "ACQ_UNKNOWN_DEVICE",
                              f"设备 {obj['device']!r} 不在设备配置中")
    modulus = cfg[dev]["M"]
    boot = canon_id(obj["boot"], "启动号")
    seq = require_int(obj["seq"], "序号", lo=0, hi=INT64_MAX)
    count = require_int(obj["count"], "原始计数", lo=0, hi=modulus - 1)
    pulse = rnd = ch = None
    value = resource = exposure = None
    if kind == KIND_RECEIPT:
        pulse = canon_id(obj["pulse"], "参考脉冲号")
        if pulse not in pulses:
            raise ValidationError("", "ACQ_UNKNOWN_PULSE",
                                  f"参考脉冲号 {obj['pulse']!r} 不在脉冲表中")
        business = json.dumps([kind, count, pulse],
                              ensure_ascii=False, separators=(",", ":"))
        return (dev, boot, seq, kind, count, pulse, None, None,
                None, None, None, business)
    rnd = canon_id(obj["round"], "巡测轮次")
    if rnd not in rounds:
        raise ValidationError("", "ACQ_UNKNOWN_ROUND",
                              f"巡测轮次 {obj['round']!r} 不在计划表中")
    ch = canon_id(obj["channel"], "通道号")
    ctype = cfg[dev]["channels"].get(ch)
    if ctype is None:
        raise ValidationError(
            "", "ACQ_UNKNOWN_CHANNEL",
            f"通道 ({obj['device']!r}, {obj['channel']!r}) 不是设备的合法通道")
    want = CHANNEL_SCALAR if kind == KIND_READING else CHANNEL_PHOTO
    if ctype != want:
        raise ValidationError(
            "", "ACQ_CHANNEL_TYPE",
            f"通道 ({obj['device']!r}, {obj['channel']!r}) 声明为 {ctype}，"
            f"不能记录 {kind}")
    if kind == KIND_READING:
        value = require_int(obj["value"], "读数值", lo=INT64_MIN, hi=INT64_MAX)
        business = json.dumps([kind, count, rnd, ch, value],
                              ensure_ascii=False, separators=(",", ":"))
    else:
        resource = obj["resource"]
        if not isinstance(resource, str) or resource == "":
            raise ValidationError("", "ACQ_BAD_FIELD",
                                  "照片资源标识必须是非空字符串")
        exposure = require_int(obj["exposure"], "曝光计数",
                               lo=1, hi=modulus - 1)
        business = json.dumps([kind, count, rnd, ch, resource, exposure],
                              ensure_ascii=False, separators=(",", ":"))
    return (dev, boot, seq, kind, count, None, rnd, ch,
            value, resource, exposure, business)


def _ingest_volumes(conn, volume_paths, cfg, pulses, rounds,
                    errors, input_rows, stats):
    sql = ("INSERT INTO staging(volume, line, device, boot, seq, kind,"
           " raw_count, pulse, round_key, channel, value, resource, exposure,"
           " business) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    batch: List[tuple] = []
    for vol in volume_paths:
        lines = _HashedLines(vol)
        try:
            for lineno, raw in lines:
                stats["volume_lines"] += 1
                where = f"{vol}:{lineno}"
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    errors.append(ValidationError(
                        where, "ACQ_BAD_JSON", f"非法 JSON 行: {e}"))
                    continue
                try:
                    rec = _validate_record(obj, cfg, pulses, rounds)
                except ValidationError as e:
                    e.location = where
                    errors.append(e)
                    continue
                batch.append((vol, lineno) + rec)
                if len(batch) >= _BATCH:
                    conn.executemany(sql, batch)
                    batch.clear()
        except OSError as e:
            errors.append(ValidationError(vol, "ACQ_IO", f"无法读取分卷: {e}"))
        input_rows.append(("volume", vol, lines.hexdigest()))
    if batch:
        conn.executemany(sql, batch)


def _materialize_records(conn, errors, stats) -> bool:
    """按 (设备, 启动号, 序号) 拼接：业务字段一致的重复只算一次（证据全
    保留），同键内容冲突定位输入并拒绝，不按"最后收到"覆盖。"""
    conflicts = conn.execute(
        "SELECT device, boot, seq, COUNT(DISTINCT business) nb FROM staging "
        "GROUP BY device, boot, seq HAVING nb > 1 "
        "ORDER BY device, boot, seq").fetchall()
    for dev, boot, seq, _nb in conflicts[:100]:
        locs = conn.execute(
            "SELECT volume, line FROM staging "
            "WHERE device=? AND boot=? AND seq=? ORDER BY volume, line",
            (dev, boot, seq)).fetchall()
        where = ", ".join(f"{v}:{ln}" for v, ln in locs[:6])
        errors.append(ValidationError(
            where, "ACQ_CONFLICT",
            f"同一 (设备 {json.loads(dev)!r}, 启动号 {json.loads(boot)!r}, "
            f"序号 {seq}) 收到内容冲突的记录（共 {len(locs)} 条），"
            f"拒绝交付，不按最后收到覆盖"))
    if conflicts:
        if len(conflicts) > 100:
            errors.append(ValidationError(
                "", "ACQ_CONFLICT",
                f"另有 {len(conflicts) - 100} 组同键冲突未逐条列出"))
        return False
    conn.execute(
        "INSERT INTO sample_record(device, boot, seq, kind, raw_count, pulse,"
        " round_key, channel, value, resource, exposure, business) "
        "SELECT device, boot, seq, kind, raw_count, pulse, round_key, channel,"
        " value, resource, exposure, business FROM staging "
        "GROUP BY device, boot, seq ORDER BY device, boot, seq")
    conn.execute("CREATE UNIQUE INDEX idx_record_key "
                 "ON sample_record(device, boot, seq)")
    conn.execute(
        "INSERT OR IGNORE INTO evidence(record_id, volume, line) "
        "SELECT r.id, s.volume, s.line FROM staging s JOIN sample_record r "
        "ON r.device=s.device AND r.boot=s.boot AND r.seq=s.seq")
    conn.execute("DROP TABLE staging")
    stats["records"] = conn.execute(
        "SELECT COUNT(*) FROM sample_record").fetchone()[0]
    stats["evidence_rows"] = conn.execute(
        "SELECT COUNT(*) FROM evidence").fetchone()[0]
    # 重复行 = 有效行数 - 拼接后记录数（业务字段一致的重复只算一次）
    stats["duplicate_lines"] = stats["volume_lines"] - stats["records"]
    return True


# --------------------------------------------------------------------------- #
# 展开：切块 + 展开坐标 + 回执结构校验
# --------------------------------------------------------------------------- #
def _expand(conn, cfg, errors, stats) -> bool:
    """同一启动内序号每断开一次切新块；仅对同块相邻序号取 [0,M) 非负模差，
    差 > G 是结构错误（差为零合法），累计差得到块内展开坐标。

    回执校验（违反即输入错误）：同一启动不重复回执同一脉冲；回执 UTC
    按序号严格向前；块内相邻回执的展开坐标严格向前。
    """
    cur = conn.execute(
        "SELECT r.id, r.device, r.boot, r.seq, r.kind, r.raw_count, r.pulse,"
        " p.utc FROM sample_record r LEFT JOIN pulse p ON p.pulse = r.pulse "
        "ORDER BY r.device, r.boot, r.seq")
    placement: List[tuple] = []
    blocks: List[tuple] = []
    located: List[Tuple[ValidationError, int]] = []   # (错误, record_id)
    st: Optional[dict] = None    # 当前 (设备, 启动号) 状态
    blk: Optional[dict] = None   # 当前展开块
    next_block_id = 1

    def close_block():
        if blk is not None:
            blocks.append((blk["id"], blk["device"], blk["boot"],
                           blk["seq_first"], blk["last_seq"], blk["n_records"],
                           blk["n_receipts"], str(blk["max_expanded"])))

    for (rid, dev, boot, seq, kind, count, pulse_id, pulse_utc) in cur:
        if st is None or (dev, boot) != (st["device"], st["boot"]):
            close_block()
            blk = None
            st = {"device": dev, "boot": boot, "pulses": set(),
                  "last_receipt_utc": None}
        if blk is None or seq != blk["last_seq"] + 1:
            close_block()
            blk = {"id": next_block_id, "device": dev, "boot": boot,
                   "seq_first": seq, "last_seq": seq, "last_count": count,
                   "max_expanded": 0, "n_records": 0, "n_receipts": 0,
                   "last_receipt_expanded": None}
            next_block_id += 1
            expanded = 0
        else:
            modulus = cfg[dev]["M"]
            advance = cfg[dev]["G"]
            diff = (count - blk["last_count"]) % modulus
            if diff > advance:
                located.append((ValidationError(
                    "", "ACQ_COUNT_JUMP",
                    f"设备 {json.loads(dev)!r} 启动号 {json.loads(boot)!r} "
                    f"序号 {blk['last_seq']} -> {seq}：相邻计数模差 {diff} "
                    f"超过 G={advance}（结构错误）"), rid))
            expanded = blk["max_expanded"] + diff
        blk["last_seq"] = seq
        blk["last_count"] = count
        blk["max_expanded"] = expanded
        blk["n_records"] += 1
        placement.append((rid, blk["id"], str(expanded)))
        if kind == KIND_RECEIPT:
            if pulse_id in st["pulses"]:
                located.append((ValidationError(
                    "", "ACQ_DUP_RECEIPT",
                    f"设备 {json.loads(dev)!r} 启动号 {json.loads(boot)!r} "
                    f"重复回执参考脉冲 {json.loads(pulse_id)!r}"), rid))
            if (st["last_receipt_utc"] is not None
                    and pulse_utc <= st["last_receipt_utc"]):
                located.append((ValidationError(
                    "", "ACQ_RECEIPT_ORDER",
                    f"回执 UTC 未按序号严格向前：{pulse_utc} 出现在 "
                    f"{st['last_receipt_utc']} 之后"), rid))
            if (blk["last_receipt_expanded"] is not None
                    and expanded <= blk["last_receipt_expanded"]):
                located.append((ValidationError(
                    "", "ACQ_RECEIPT_COORD",
                    f"块内相邻回执的展开坐标未严格向前：{expanded} 出现在 "
                    f"{blk['last_receipt_expanded']} 之后"), rid))
            st["pulses"].add(pulse_id)
            st["last_receipt_utc"] = pulse_utc
            blk["last_receipt_expanded"] = expanded
            blk["n_receipts"] += 1
        if len(placement) >= _BATCH:
            conn.executemany(
                "INSERT INTO placement(record_id, block_id, expanded) "
                "VALUES (?,?,?)", placement)
            placement.clear()
    close_block()
    if placement:
        conn.executemany(
            "INSERT INTO placement(record_id, block_id, expanded) "
            "VALUES (?,?,?)", placement)
    conn.executemany(
        "INSERT INTO expand_block(id, device, boot, seq_first, seq_last,"
        " n_records, n_receipts, max_expanded) VALUES (?,?,?,?,?,?,?,?)",
        blocks)
    stats["blocks"] = len(blocks)
    if located:
        # 用证据表把结构错误定位到卷名:行号
        ev: Dict[int, List[str]] = {}
        ids = sorted({rid for _, rid in located})
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for rid2, vol, ln in conn.execute(
                    f"SELECT record_id, volume, line FROM evidence "
                    f"WHERE record_id IN ({q}) ORDER BY volume, line", chunk):
                ev.setdefault(rid2, []).append(f"{vol}:{ln}")
        for err, rid in located:
            err.location = ", ".join(ev.get(rid, [f"record#{rid}"]))
            errors.append(err)
        return False
    return True


# --------------------------------------------------------------------------- #
# 对齐：块内回执线性插值（精确有理数）
# --------------------------------------------------------------------------- #
def _align_point(exps, utcs, rids, x):
    """返回 (status, Fraction|None, lo_rid, hi_rid)。

    落在回执坐标上就采用该回执 UTC（即使是全块唯一回执）；其余必须被
    同块回执夹住，两端做线性插值。
    """
    n = len(exps)
    if n == 0:
        return CAT_NO_RECEIPTS, None, None, None
    i = bisect.bisect_right(exps, x) - 1
    if i >= 0 and exps[i] == x:
        return ST_OK, Fraction(utcs[i]), rids[i], rids[i]
    if i < 0:
        return CAT_START_UNCONSTRAINED, None, None, None
    if i + 1 >= n:
        return CAT_END_UNCONSTRAINED, None, None, None
    u = (Fraction(utcs[i])
         + Fraction(utcs[i + 1] - utcs[i]) * (x - exps[i])
         / (exps[i + 1] - exps[i]))
    return ST_OK, u, rids[i], rids[i + 1]


def _align(conn, stats):
    block_cur = conn.execute("SELECT id FROM expand_block ORDER BY id")
    batch: List[tuple] = []
    sql = ("INSERT INTO alignment(record_id, status, utc_num, utc_den,"
           " open_num, open_den, close_num, close_den,"
           " ro_lo, ro_hi, rc_lo, rc_hi) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
    for (bid,) in block_cur:
        receipts = conn.execute(
            "SELECT r.id, p.expanded, pu.utc FROM placement p "
            "JOIN sample_record r ON r.id = p.record_id "
            "JOIN pulse pu ON pu.pulse = r.pulse "
            "WHERE p.block_id = ? AND r.kind = 'receipt' ORDER BY r.seq",
            (bid,)).fetchall()
        exps = [int(r[1]) for r in receipts]
        utcs = [r[2] for r in receipts]
        rids = [r[0] for r in receipts]
        n = len(exps)
        rec_cur = conn.execute(
            "SELECT r.id, r.kind, r.exposure, p.expanded FROM placement p "
            "JOIN sample_record r ON r.id = p.record_id "
            "WHERE p.block_id = ? AND r.kind != 'receipt' ORDER BY r.seq",
            (bid,))
        for rid, kind, exposure, exp_s in rec_cur:
            x = int(exp_s)
            if kind == KIND_READING:
                stats["readings"] += 1
                status, u, lo, hi = _align_point(exps, utcs, rids, x)
                if u is None:
                    batch.append((rid, status) + (None,) * 10)
                    stats["unaligned_" + status] += 1
                else:
                    num, den = frac_parts(u)
                    batch.append((rid, ST_OK, num, den, None, None, None,
                                  None, lo, hi, None, None))
                    stats["aligned"] += 1
            else:  # photo：开/闭快门都必须在同块回执围住的范围内
                stats["photos"] += 1
                d = exposure
                status = None
                if n == 0:
                    status = CAT_NO_RECEIPTS
                elif x < exps[0]:
                    status = CAT_START_UNCONSTRAINED
                elif x + d > exps[-1]:
                    status = CAT_END_UNCONSTRAINED
                if status is not None:
                    batch.append((rid, status) + (None,) * 10)
                    stats["unaligned_" + status] += 1
                else:
                    _, u_open, lo1, hi1 = _align_point(exps, utcs, rids, x)
                    _, u_close, lo2, hi2 = _align_point(
                        exps, utcs, rids, x + d)
                    on, od = frac_parts(u_open)
                    cn, cd = frac_parts(u_close)
                    batch.append((rid, ST_OK, None, None, on, od, cn, cd,
                                  lo1, hi1, lo2, hi2))
                    stats["aligned"] += 1
            if len(batch) >= _BATCH:
                conn.executemany(sql, batch)
                batch.clear()
    if batch:
        conn.executemany(sql, batch)


# --------------------------------------------------------------------------- #
# 归属：读数落窗 / 照片整段曝光落窗，其余单列
# --------------------------------------------------------------------------- #
class _Examined:
    """归因阶段实际检查的窗口行数（证明不按 记录×窗口 做候选扫描）。"""

    def __init__(self):
        self.n = 0


def _bisect_right_counted(arr, x, ctr: _Examined) -> int:
    lo, hi = 0, len(arr)
    while lo < hi:
        ctr.n += 1
        mid = (lo + hi) // 2
        if x < arr[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _attribute(conn, stats):
    ctr = _Examined()
    attr_batch: List[tuple] = []
    un_batch: List[tuple] = []
    ov_batch: List[tuple] = []
    gap_batch: List[tuple] = []

    def flush():
        nonlocal attr_batch, un_batch, ov_batch, gap_batch
        if attr_batch:
            conn.executemany(
                "INSERT INTO attribution(record_id, window_id) VALUES (?,?)",
                attr_batch)
            attr_batch = []
        if un_batch:
            conn.executemany(
                "INSERT INTO unattributed(record_id, category, detail) "
                "VALUES (?,?,?)", un_batch)
            un_batch = []
        if ov_batch:
            conn.executemany(
                "INSERT INTO photo_overlap(record_id, window_id, lo_num,"
                " lo_den, hi_num, hi_den) VALUES (?,?,?,?,?,?)", ov_batch)
            ov_batch = []
        if gap_batch:
            conn.executemany(
                "INSERT INTO photo_gap(record_id, seq_no, lo_num, lo_den,"
                " hi_num, hi_den) VALUES (?,?,?,?,?,?)", gap_batch)
            gap_batch = []

    rounds = [r[0] for r in conn.execute(
        "SELECT DISTINCT round_key FROM plan_window ORDER BY round_key")]
    for rnd in rounds:
        # 单轮次窗口驻留为紧凑 int64 数组（10 万窗口约 2.4 MiB），
        # 不按记录数 x 窗口数做候选扫描
        wids = array("q")
        starts = array("q")
        ends = array("q")
        for wid, s, e in conn.execute(
                "SELECT id, start_utc, end_utc FROM plan_window "
                "WHERE round_key=? ORDER BY start_utc", (rnd,)):
            wids.append(wid)
            starts.append(s)
            ends.append(e)
        nw = len(wids)
        # 读数：按它在同轮次所落的一个窗口归档（start <= t < end）
        for rid, num, den in conn.execute(
                "SELECT r.id, a.utc_num, a.utc_den FROM sample_record r "
                "JOIN alignment a ON a.record_id = r.id "
                "WHERE r.round_key=? AND r.kind='reading' AND a.status='ok' "
                "ORDER BY r.id", (rnd,)):
            t = frac_from_parts(num, den)
            j = _bisect_right_counted(starts, t, ctr) - 1
            ctr.n += 1  # ends[j] 检查
            if j >= 0 and t < ends[j]:
                attr_batch.append((rid, wids[j]))
                stats["attributed_readings"] += 1
            else:
                un_batch.append((rid, CAT_OUTSIDE_WINDOWS, json.dumps(
                    {"utc": frac_str(t)}, ensure_ascii=False)))
                stats["unattributed_" + CAT_OUTSIDE_WINDOWS] += 1
            if len(attr_batch) + len(un_batch) >= _BATCH:
                flush()
        # 照片：只有整段曝光 [open, close] 落在一个窗口里才归档
        # （闭快门正好等于窗口右端算在内）；否则给出全部正长度相交
        # 窗口的精确交叠区间与未覆盖子区间，不选重叠最长的一扇。
        for (rid, on, od, cn, cd) in conn.execute(
                "SELECT r.id, a.open_num, a.open_den, a.close_num, a.close_den "
                "FROM sample_record r JOIN alignment a ON a.record_id = r.id "
                "WHERE r.round_key=? AND r.kind='photo' AND a.status='ok' "
                "ORDER BY r.id", (rnd,)):
            o = frac_from_parts(on, od)
            c = frac_from_parts(cn, cd)
            j = _bisect_right_counted(starts, o, ctr) - 1
            ctr.n += 1  # ends[j] 检查
            if j >= 0 and c <= ends[j]:
                attr_batch.append((rid, wids[j]))
                stats["attributed_photos"] += 1
                continue
            i = _bisect_right_counted(ends, o, ctr)
            overlaps: List[Tuple[int, Fraction, Fraction]] = []
            while i < nw:
                ctr.n += 1
                if starts[i] >= c:
                    break
                lo = o if o > starts[i] else starts[i]
                hi = c if c < ends[i] else ends[i]
                overlaps.append((wids[i], lo, hi))
                i += 1
            if not overlaps:
                un_batch.append((rid, CAT_OUTSIDE_WINDOWS, json.dumps(
                    {"open": frac_str(o), "close": frac_str(c)},
                    ensure_ascii=False)))
                stats["unattributed_" + CAT_OUTSIDE_WINDOWS] += 1
                continue
            gaps: List[Tuple[Fraction, Fraction]] = []
            prev = o
            for _wid, lo, hi in overlaps:
                if prev < lo:
                    gaps.append((prev, lo))
                prev = hi
            if prev < c:
                gaps.append((prev, c))
            detail = {"open": frac_str(o), "close": frac_str(c),
                      "spans_multiple": len(overlaps) > 1,
                      "has_uncovered": len(gaps) > 0}
            un_batch.append((rid, CAT_PARTIAL_OVERLAP,
                             json.dumps(detail, ensure_ascii=False)))
            stats["unattributed_" + CAT_PARTIAL_OVERLAP] += 1
            for wid, lo, hi in overlaps:
                ov_batch.append((rid, wid) + frac_parts(lo) + frac_parts(hi))
                stats["photo_overlap_rows"] += 1
            for k, (lo, hi) in enumerate(gaps):
                gap_batch.append((rid, k) + frac_parts(lo) + frac_parts(hi))
                stats["photo_gap_rows"] += 1
            if len(un_batch) + len(ov_batch) >= _BATCH:
                flush()
        flush()
    # 缺时钟记录（所有轮次一趟）：质量结果，不是输入错误
    for rid, status, block_id, expanded in conn.execute(
            "SELECT a.record_id, a.status, p.block_id, p.expanded "
            "FROM alignment a JOIN placement p ON p.record_id = a.record_id "
            "WHERE a.status != 'ok'"):
        un_batch.append((rid, status, json.dumps(
            {"block_id": block_id, "expanded": expanded},
            ensure_ascii=False)))
        stats["unattributed_" + status] += 1
        if len(un_batch) >= _BATCH:
            flush()
    flush()
    stats["window_rows_examined"] = ctr.n


# --------------------------------------------------------------------------- #
# 收尾与发布
# --------------------------------------------------------------------------- #
def _finalize(conn, version, lib_version, input_rows, stats):
    conn.execute(
        "INSERT INTO window_channel_count(window_id, device, channel, kind, n)"
        " SELECT a.window_id, r.device, r.channel, r.kind, COUNT(*) "
        "FROM attribution a JOIN sample_record r ON r.id = a.record_id "
        "GROUP BY a.window_id, r.device, r.channel, r.kind")
    conn.executescript(INDEXES_SQL)
    conn.executemany(
        "INSERT INTO inputs(kind, name, sha256) VALUES (?,?,?)", input_rows)
    meta = {
        "schema_version": str(ACQ_SCHEMA_VERSION),
        "package_version": version,
        "tool": f"chainage-acquisition/{__version__}",
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "chainage_lib_version": lib_version,
    }
    for key, value in sorted(stats.items()):
        meta["stat." + key] = str(value)
    conn.execute("UPDATE meta SET value=? WHERE key='status'", (STATUS_READY,))
    conn.executemany("INSERT INTO meta(key, value) VALUES (?,?)",
                     list(meta.items()))


@dataclass
class AcqBuildStats:
    package_version: str
    chainage_lib_version: str
    rounds: int
    windows: int
    pulses: int
    devices: int
    volume_lines: int
    records: int
    duplicate_lines: int
    evidence_rows: int
    blocks: int
    attributed_readings: int
    attributed_photos: int
    unattributed: int
    photo_overlap_rows: int
    photo_gap_rows: int
    window_rows_examined: int
    db_path: str

    def to_dict(self) -> dict:
        return {
            "package_version": self.package_version,
            "chainage_lib_version": self.chainage_lib_version,
            "rounds": self.rounds,
            "windows": self.windows,
            "pulses": self.pulses,
            "devices": self.devices,
            "volume_lines": self.volume_lines,
            "records": self.records,
            "duplicate_lines": self.duplicate_lines,
            "evidence_rows": self.evidence_rows,
            "blocks": self.blocks,
            "attributed_readings": self.attributed_readings,
            "attributed_photos": self.attributed_photos,
            "unattributed": self.unattributed,
            "photo_overlap_rows": self.photo_overlap_rows,
            "photo_gap_rows": self.photo_gap_rows,
            "window_rows_examined": self.window_rows_examined,
            "db_path": self.db_path,
        }


def build_acquisition(package_dir: str, chainage_db: str,
                      out_path: str) -> AcqBuildStats:
    """校验并装配采集包；失败抛 AcquisitionValidationError 且不动已有库。

    输出库已存在时直接拒绝（不覆盖上一份交付）；构建在临时库上进行，
    全部校验、索引与统计完成后才以硬链接原子发布为正式库。
    """
    if os.path.exists(out_path):
        raise DatabaseError(
            f"输出库已存在，拒绝覆盖上一份交付: {out_path}")
    manifest = _load_manifest(package_dir)

    errors: List[ValidationError] = []
    # 既有 ready 换算库：只核实登记 ID 存在并引用交接版本，不做桩号换算
    with open_ready_database(chainage_db) as lib:
        row = lib.execute(
            "SELECT value FROM meta WHERE key='package_version'").fetchone()
        lib_version = row[0] if row else ""
        registrations = {r[0] for r in lib.execute(
            "SELECT reg_id FROM registration")}

    input_rows: List[Tuple[str, str, str]] = []
    stats = {k: 0 for k in (
        "volume_lines", "records", "duplicate_lines", "evidence_rows",
        "blocks", "readings", "photos", "aligned",
        "unaligned_" + CAT_NO_RECEIPTS,
        "unaligned_" + CAT_START_UNCONSTRAINED,
        "unaligned_" + CAT_END_UNCONSTRAINED,
        "attributed_readings", "attributed_photos",
        "unattributed_" + CAT_NO_RECEIPTS,
        "unattributed_" + CAT_START_UNCONSTRAINED,
        "unattributed_" + CAT_END_UNCONSTRAINED,
        "unattributed_" + CAT_OUTSIDE_WINDOWS,
        "unattributed_" + CAT_PARTIAL_OVERLAP,
        "photo_overlap_rows", "photo_gap_rows", "window_rows_examined")}

    cfg, dev_sha = _load_devices(manifest["devices"], errors)
    pulses, pul_sha = _load_pulses(manifest["pulses"], errors)
    plan_rows, plan_sha = _load_plan(manifest["plan"], registrations, cfg,
                                     errors)
    if dev_sha is not None:
        input_rows.append(("devices", manifest["devices"], dev_sha))
    if pul_sha is not None:
        input_rows.append(("pulses", manifest["pulses"], pul_sha))
    if plan_sha is not None:
        input_rows.append(("plan", manifest["plan"], plan_sha))
    input_rows.append(("chainage_lib", chainage_db,
                       _sha256_file(chainage_db)))
    if errors:
        raise AcquisitionValidationError(errors)

    tmp_path = out_path + TEMP_SUFFIX
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _new_build_connection(tmp_path)
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO meta(key, value) VALUES('status', ?)",
                     (STATUS_BUILDING,))
        # 基础表：设备配置、参考脉冲、计划窗口
        conn.executemany(
            "INSERT INTO device(device, modulus, max_advance) VALUES (?,?,?)",
            [(dev, c["M"], c["G"]) for dev, c in cfg.items()])
        conn.executemany(
            "INSERT INTO device_channel(device, channel, ctype) VALUES (?,?,?)",
            [(dev, ch, t) for dev, c in cfg.items()
             for ch, t in c["channels"].items()])
        conn.executemany("INSERT INTO pulse(pulse, utc) VALUES (?,?)",
                         list(pulses.items()))
        conn.executemany(
            "INSERT INTO plan_window(round_key, window_key, start_utc,"
            " end_utc, registration, expected_channels) VALUES (?,?,?,?,?,?)",
            [(r["round"], r["window"], r["start"], r["end"], r["registration"],
              json.dumps(r["expected"], ensure_ascii=False,
                         separators=(",", ":")))
             for r in plan_rows])
        # 计划行/脉冲表已入库，释放进程内副本（规模随窗口/脉冲数增长）
        round_keys = {r["round"] for r in plan_rows}
        n_rounds = len(round_keys)
        n_windows = len(plan_rows)
        n_pulses = len(pulses)
        del plan_rows

        _ingest_volumes(conn, manifest["volumes"], cfg, pulses,
                        round_keys, errors, input_rows, stats)
        del pulses
        if errors:
            raise AcquisitionValidationError(errors)
        if not _materialize_records(conn, errors, stats):
            raise AcquisitionValidationError(errors)
        if not _expand(conn, cfg, errors, stats):
            raise AcquisitionValidationError(errors)
        _align(conn, stats)
        _attribute(conn, stats)
        # 汇总统计（随库交付，摘要与验收都读它）
        stats["rounds"] = n_rounds
        stats["windows"] = n_windows
        stats["pulses"] = n_pulses
        stats["devices"] = len(cfg)
        stats["receipts"] = stats["records"] - stats["readings"] \
            - stats["photos"]
        stats["unattributed"] = sum(
            v for k, v in stats.items() if k.startswith("unattributed_"))
        _finalize(conn, manifest["version"], lib_version, input_rows, stats)
        conn.commit()
        conn.close()
        conn = None
        # 原子发布且不覆盖：硬链接在目标已存在时失败，不破坏上一份交付
        os.link(tmp_path, out_path)
        os.unlink(tmp_path)
    except Exception:
        if conn is not None:
            conn.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    stats_out = AcqBuildStats(
        package_version=manifest["version"],
        chainage_lib_version=lib_version,
        rounds=n_rounds,
        windows=n_windows,
        pulses=n_pulses,
        devices=len(cfg),
        volume_lines=stats["volume_lines"],
        records=stats["records"],
        duplicate_lines=stats["duplicate_lines"],
        evidence_rows=stats["evidence_rows"],
        blocks=stats["blocks"],
        attributed_readings=stats["attributed_readings"],
        attributed_photos=stats["attributed_photos"],
        unattributed=sum(v for k, v in stats.items()
                         if k.startswith("unattributed_")),
        photo_overlap_rows=stats["photo_overlap_rows"],
        photo_gap_rows=stats["photo_gap_rows"],
        window_rows_examined=stats["window_rows_examined"],
        db_path=os.path.abspath(out_path),
    )
    return stats_out
