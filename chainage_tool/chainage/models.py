"""交接包加载与严格校验。

交接包目录结构（全部 UTF-8）::

    <package_dir>/
      manifest.json        清单：版本标识、轴及整数长度、文件清单
      calibration.jsonl    标定段表，每行一条标定行
      registration.jsonl   待换算登记表，每行一条登记

设计原则
========
1. 坏字段不抛裸异常：每行包在独立 try 内，问题转成带物理行号的
   :class:`ValidationError`，继续解析其余行，最后一次性聚合报错。
2. 引用类校验（未知轴、未知体系、区间重叠、登记引用）在全部行解析
   完成后统一做，保证一次运行能尽量报全所有问题。
3. 合法的断链复号（不同段名在同一物理位置给出相同/不同标牌）不是
   错误；只有同一轴同一编号体系同一段名的闭区间相互重叠才拒绝。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .errors import ValidationError

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

MANIFEST_NAME = "manifest.json"
CALIB_NAME = "calibration.jsonl"
REG_NAME = "registration.jsonl"


def _is_plain_int(v: Any) -> bool:
    """只接受真正的整数（bool 不算，float 不算，防止隐式舍入）。"""
    return isinstance(v, int) and not isinstance(v, bool)


def _in_int64(v: int) -> bool:
    return INT64_MIN <= v <= INT64_MAX


@dataclass(frozen=True)
class Axis:
    axis_id: str
    length: int  # 物理坐标为 0..length（含）的整数刻度


@dataclass(frozen=True)
class CalibRow:
    """一条标定行（行标识稳定，登记与证据均引用它）。

    仿射关系::

        编号 n(x) = label_lo + direction * (x - lo)
        反解   x(n) = lo + direction * (n - label_lo)

    为让引擎统一处理，预计算 n = a*x + b 与 x = a*n + c（方向 ±1）::

        a = direction
        b = label_lo - direction * lo      # n = a*x + b
        c = lo - direction * label_lo      # x = a*n + c
    """

    row_id: str
    system: str
    axis_id: str
    segment: str
    lo: int
    hi: int
    label_lo: int
    direction: int
    # 该标定行覆盖的编号闭区间（已按编号大小归一）
    label_min: int
    label_max: int
    a: int
    b: int
    c: int
    source_line: int  # calibration.jsonl 物理行号，证据定位用

    def x_of(self, n: int) -> int:
        return self.a * n + self.c

    def n_of(self, x: int) -> int:
        return self.a * x + self.b

    def covers_label(self, n: int) -> bool:
        return self.label_min <= n <= self.label_max


@dataclass(frozen=True)
class RegRow:
    reg_id: str
    source_system: str
    target_system: str
    n_from: int
    n_to: int  # 单点登记时 n_from == n_to
    axis_id: Optional[str]       # None = 未知，不是默认选某个轴
    segment: Optional[str]       # None = 未知，不是默认选某个段
    source_line: int

    @property
    def is_point(self) -> bool:
        return self.n_from == self.n_to


@dataclass
class Package:
    version: str
    axes: Dict[str, Axis]
    calibration: List[CalibRow]
    registration: List[RegRow]
    package_dir: str
    systems: Dict[str, List[CalibRow]] = field(default_factory=dict)
    errors: List[ValidationError] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 基础类型小校验
# --------------------------------------------------------------------------- #
def _require_str(d: dict, key: str, loc: str, errs: List[ValidationError],
                 rid: Optional[str]) -> Optional[str]:
    v = d.get(key)
    if not isinstance(v, str) or v == "":
        errs.append(ValidationError(loc, "BAD_FIELD",
                                    f"字段 {key!r} 必须是非空字符串", rid))
        return None
    return v


def _require_int(d: dict, key: str, loc: str,
                 errs: List[ValidationError], rid: Optional[str],
                 lo: Optional[int] = None,
                 hi: Optional[int] = None) -> Optional[int]:
    v = d.get(key)
    if not _is_plain_int(v):
        errs.append(ValidationError(loc, "BAD_FIELD",
                                    f"字段 {key!r} 必须是整数（不接受小数/布尔/字符串）",
                                    rid))
        return None
    if not _in_int64(v):
        errs.append(ValidationError(loc, "INT64_OVERFLOW",
                                    f"字段 {key!r}={v} 超出有符号 64 位整数范围", rid))
        return None
    if lo is not None and v < lo:
        errs.append(ValidationError(loc, "BAD_FIELD",
                                    f"字段 {key!r}={v} 小于允许下限 {lo}", rid))
        return None
    if hi is not None and v > hi:
        errs.append(ValidationError(loc, "BAD_FIELD",
                                    f"字段 {key!r}={v} 大于允许上限 {hi}", rid))
        return None
    return v


def _read_jsonl(path: str, loc_name: str,
                errs: List[ValidationError]) -> List[Tuple[int, dict]]:
    """逐行解析 JSONL；坏 JSON 记错误并跳过，不让整个入仓崩掉。"""
    rows: List[Tuple[int, dict]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            text = raw.strip()
            if not text:
                continue  # 空行不计行对象，但物理行号仍由 enumerate 保留
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                errs.append(ValidationError(
                    f"{loc_name}:{lineno}", "BAD_JSON",
                    f"不是合法 JSON: {exc.msg}"))
                continue
            if not isinstance(obj, dict):
                errs.append(ValidationError(
                    f"{loc_name}:{lineno}", "BAD_FIELD",
                    "每行必须是 JSON 对象"))
                continue
            rows.append((lineno, obj))
    return rows


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def _load_manifest(package_dir: str,
                   errs: List[ValidationError]) -> Tuple[Optional[str], Dict[str, Axis]]:
    path = os.path.join(package_dir, MANIFEST_NAME)
    loc = MANIFEST_NAME
    if not os.path.isfile(path):
        errs.append(ValidationError(loc, "MISSING_FILE", f"缺少清单文件 {MANIFEST_NAME}"))
        return None, {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except json.JSONDecodeError as exc:
        errs.append(ValidationError(loc, "BAD_JSON", f"清单不是合法 JSON: {exc.msg}"))
        return None, {}
    if not isinstance(manifest, dict):
        errs.append(ValidationError(loc, "BAD_FIELD", "清单必须是 JSON 对象"))
        return None, {}

    version = _require_str(manifest, "version", loc, errs, None)

    axes_raw = manifest.get("axes")
    axes: Dict[str, Axis] = {}
    if not isinstance(axes_raw, list) or not axes_raw:
        errs.append(ValidationError(loc, "BAD_FIELD",
                                    "字段 axes 必须是非空数组"))
    else:
        seen = set()
        for idx, item in enumerate(axes_raw):
            iloc = f"{loc}#axes[{idx}]"
            if not isinstance(item, dict):
                errs.append(ValidationError(iloc, "BAD_FIELD", "轴定义必须是对象"))
                continue
            aid = _require_str(item, "axis_id", iloc, errs, None)
            length = _require_int(item, "length", iloc, errs, None, lo=1)
            if aid is None or length is None:
                continue
            if aid in seen:
                errs.append(ValidationError(iloc, "DUPLICATE_AXIS",
                                            f"轴 {aid!r} 在清单中重复定义", aid))
                continue
            seen.add(aid)
            axes[aid] = Axis(axis_id=aid, length=length)

    # files 字段可选；若给出则核对三张文件名（允许只声明默认名）
    files = manifest.get("files")
    if files is not None and not isinstance(files, dict):
        errs.append(ValidationError(loc, "BAD_FIELD", "字段 files 若存在必须是对象"))
    return version, axes


# --------------------------------------------------------------------------- #
# calibration
# --------------------------------------------------------------------------- #
def _load_calibration(package_dir: str, axes: Dict[str, Axis],
                      errs: List[ValidationError]) -> List[CalibRow]:
    path = os.path.join(package_dir, CALIB_NAME)
    loc = CALIB_NAME
    if not os.path.isfile(path):
        errs.append(ValidationError(loc, "MISSING_FILE", f"缺少标定段表 {CALIB_NAME}"))
        return []
    raw_rows = _read_jsonl(path, loc, errs)

    rows: List[CalibRow] = []
    seen_ids: set = set()
    for lineno, d in raw_rows:
        iloc = f"{loc}:{lineno}"
        rid = d.get("row_id")
        rid = rid if isinstance(rid, str) and rid else None

        if rid is None:
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        "字段 row_id 必须是非空字符串"))
            continue
        if rid in seen_ids:
            # 重复行标识：拒绝该行，后续行仍继续解析
            errs.append(ValidationError(iloc, "DUPLICATE_ROW_ID",
                                        f"标定行标识 {rid!r} 重复", rid))
            continue

        system = _require_str(d, "system", iloc, errs, rid)
        axis_id = _require_str(d, "axis_id", iloc, errs, rid)
        segment = _require_str(d, "segment", iloc, errs, rid)
        lo = _require_int(d, "lo", iloc, errs, rid)
        hi = _require_int(d, "hi", iloc, errs, rid)
        label_lo = _require_int(d, "label_lo", iloc, errs, rid)
        direction = _require_int(d, "direction", iloc, errs, rid)

        if direction is not None and direction not in (1, -1):
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        "字段 direction 只能是 +1 或 -1", rid))
            direction = None

        # 未知轴：定位原行并拒绝（轴范围检查也依赖轴定义）
        if axis_id is not None and axis_id not in axes:
            errs.append(ValidationError(iloc, "UNKNOWN_AXIS",
                                        f"轴 {axis_id!r} 未在清单中声明", rid))
            axis_id = None

        bad_range = False
        if lo is not None and hi is not None and not lo < hi:
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        f"要求 lo < hi，实际 lo={lo}, hi={hi}", rid))
            bad_range = True
        if axis_id is not None and lo is not None and hi is not None:
            axis = axes[axis_id]
            if not (0 <= lo and hi <= axis.length):
                errs.append(ValidationError(
                    iloc, "OUT_OF_AXIS",
                    f"物理区间 [{lo},{hi}] 超出轴 {axis_id!r} 的整数刻度 [0,{axis.length}]",
                    rid))
                bad_range = True

        # 端点实际编号必须可在 int64 表示，且整条段的编号范围不得溢出
        overflow = False
        if None not in (lo, hi, label_lo, direction):
            label_hi = label_lo + direction * (hi - lo)
            if not _in_int64(label_hi):
                errs.append(ValidationError(
                    iloc, "INT64_OVERFLOW",
                    f"hi 端实际编号 {label_hi} 超出有符号 64 位，拒绝以防溢出/舍入",
                    rid))
                overflow = True
        else:
            label_hi = None

        if None in (system, axis_id, segment, lo, hi, label_lo, direction) \
                or bad_range or overflow:
            continue

        a = direction
        b = label_lo - direction * lo
        c = lo - direction * label_lo
        label_min = min(label_lo, label_hi)
        label_max = max(label_lo, label_hi)
        # 仿射参数也必须落在 int64（方向 ±1，刻度与编号已受限，这里兜底）
        if not all(_in_int64(v) for v in (a * lo + b, a * hi + b, c)):
            errs.append(ValidationError(iloc, "INT64_OVERFLOW",
                                        "仿射换算中间量超出 int64", rid))
            continue

        seen_ids.add(rid)
        rows.append(CalibRow(
            row_id=rid, system=system, axis_id=axis_id, segment=segment,
            lo=lo, hi=hi, label_lo=label_lo, direction=direction,
            label_min=label_min, label_max=label_max,
            a=a, b=b, c=c, source_line=lineno))
    return rows


def _check_calib_overlaps(rows: List[CalibRow],
                          errs: List[ValidationError]) -> None:
    """同轴 + 同编号体系 + 同段名的闭区间不得重叠（共享端点也算重叠）。

    不同段名之间的重叠、共享端点、完全空缺均合法——那是断链复号的
    正常形态，本函数绝不拒绝。
    """
    groups: Dict[Tuple[str, str, str], List[CalibRow]] = {}
    for r in rows:
        groups.setdefault((r.axis_id, r.system, r.segment), []).append(r)

    for (axis_id, system, segment), grp in groups.items():
        grp_sorted = sorted(grp, key=lambda r: (r.lo, r.hi, r.row_id))
        for i in range(1, len(grp_sorted)):
            prev = grp_sorted[i - 1]
            cur = grp_sorted[i]
            # 闭区间重叠判定：prev.hi >= cur.lo（相接即重叠，因为两端都含）
            if prev.hi >= cur.lo:
                errs.append(ValidationError(
                    f"{CALIB_NAME}:{cur.source_line}", "SEGMENT_OVERLAP",
                    f"轴 {axis_id!r} 体系 {system!r} 段 {segment!r} 的闭区间 "
                    f"[{cur.lo},{cur.hi}] 与行 {prev.row_id!r} 的 "
                    f"[{prev.lo},{prev.hi}] 重叠或相接（含端点即非法）",
                    cur.row_id))


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def _load_registration(package_dir: str, axes: Dict[str, Axis],
                       calib_rows: List[CalibRow],
                       errs: List[ValidationError]) -> List[RegRow]:
    path = os.path.join(package_dir, REG_NAME)
    loc = REG_NAME
    if not os.path.isfile(path):
        errs.append(ValidationError(loc, "MISSING_FILE", f"缺少登记表 {REG_NAME}"))
        return []
    raw_rows = _read_jsonl(path, loc, errs)

    systems = {r.system for r in calib_rows}
    # 段名是否存在的判定限定在对应体系内
    system_axis_segments = {(r.system, r.axis_id, r.segment) for r in calib_rows}
    system_segments = {(r.system, r.segment) for r in calib_rows}

    rows: List[RegRow] = []
    seen_ids: set = set()
    for lineno, d in raw_rows:
        iloc = f"{loc}:{lineno}"
        rid = d.get("reg_id")
        rid = rid if isinstance(rid, str) and rid else None
        if rid is None:
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        "字段 reg_id 必须是非空字符串"))
            continue
        if rid in seen_ids:
            errs.append(ValidationError(iloc, "DUPLICATE_REG_ID",
                                        f"登记 ID {rid!r} 重复", rid))
            continue

        source_system = _require_str(d, "source_system", iloc, errs, rid)
        target_system = _require_str(d, "target_system", iloc, errs, rid)

        # 点号 n 与区间 [from,to] 二选一；点号等同 from=to
        n_from = n_to = None
        has_point = "n" in d
        has_range = "from" in d or "to" in d
        if has_point and has_range:
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        "点号 n 与区间 from/to 不能同时出现", rid))
        elif has_point:
            n_from = n_to = _require_int(d, "n", iloc, errs, rid)
        elif has_range:
            n_from = _require_int(d, "from", iloc, errs, rid)
            n_to = _require_int(d, "to", iloc, errs, rid)
        else:
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        "必须给出点号 n 或区间 [from,to]", rid))

        if n_from is not None and n_to is not None and n_from > n_to:
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        f"要求 from <= to，实际 from={n_from}, to={n_to}",
                                        rid))
            n_from = n_to = None

        axis_id = d.get("axis_id")
        if axis_id is not None and not isinstance(axis_id, str):
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        "字段 axis_id 若给出必须是字符串", rid))
            axis_id = None
        segment = d.get("segment")
        if segment is not None and not isinstance(segment, str):
            errs.append(ValidationError(iloc, "BAD_FIELD",
                                        "字段 segment 若给出必须是字符串", rid))
            segment = None

        if source_system is not None and source_system not in systems:
            errs.append(ValidationError(iloc, "UNKNOWN_SYSTEM",
                                        f"源编号体系 {source_system!r} 没有任何标定行",
                                        rid))
            source_system = None
        if target_system is not None and target_system not in systems:
            errs.append(ValidationError(iloc, "UNKNOWN_SYSTEM",
                                        f"目标编号体系 {target_system!r} 没有任何标定行",
                                        rid))
            target_system = None
        if source_system is not None and target_system is not None \
                and source_system == target_system:
            errs.append(ValidationError(iloc, "SAME_SYSTEM",
                                        f"源与目标编号体系必须不同，均为 {source_system!r}",
                                        rid))
            target_system = None
        if axis_id is not None and axis_id not in axes:
            errs.append(ValidationError(iloc, "UNKNOWN_AXIS",
                                        f"轴 {axis_id!r} 未在清单中声明", rid))
            axis_id = None
        # 段引用必须能在标定表中找到；轴给定时按 (体系,轴,段) 限定，
        # 轴未给定时允许跨轴复用段名，只按 (体系,段) 判定存在性。
        if segment is not None and source_system is not None:
            if axis_id is not None:
                if (source_system, axis_id, segment) not in system_axis_segments:
                    errs.append(ValidationError(
                        iloc, "UNKNOWN_SEGMENT",
                        f"体系 {source_system!r} 轴 {axis_id!r} 上不存在段 {segment!r}",
                        rid))
                    segment = None
            elif (source_system, segment) not in system_segments:
                errs.append(ValidationError(
                    iloc, "UNKNOWN_SEGMENT",
                    f"体系 {source_system!r} 中不存在段 {segment!r}", rid))
                segment = None

        if None in (source_system, target_system, n_from, n_to):
            continue

        seen_ids.add(rid)
        rows.append(RegRow(
            reg_id=rid, source_system=source_system, target_system=target_system,
            n_from=n_from, n_to=n_to, axis_id=axis_id, segment=segment,
            source_line=lineno))
    return rows


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def load_package(package_dir: str) -> Package:
    """加载并完整校验交接包。

    返回 :class:`Package`；若存在任何校验错误，抛出
    :class:`PackageValidationError`（携带全部错误及原行定位）。
    调用方不应拿到带错误的包继续构建。
    """
    errs: List[ValidationError] = []
    if not os.path.isdir(package_dir):
        errs.append(ValidationError(package_dir, "MISSING_FILE",
                                    "交接包目录不存在"))
        from .errors import PackageValidationError
        raise PackageValidationError(errs)

    version, axes = _load_manifest(package_dir, errs)
    calib = _load_calibration(package_dir, axes, errs)
    _check_calib_overlaps(calib, errs)
    regs = _load_registration(package_dir, axes, calib, errs)

    if errs:
        from .errors import PackageValidationError
        raise PackageValidationError(errs)

    systems: Dict[str, List[CalibRow]] = {}
    for r in calib:
        systems.setdefault(r.system, []).append(r)

    return Package(
        version=version,
        axes=axes,
        calibration=calib,
        registration=regs,
        package_dir=os.path.abspath(package_dir),
        systems=systems,
        errors=[],
    )
