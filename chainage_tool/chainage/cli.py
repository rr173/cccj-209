"""命令行入口。

子命令
======
- validate  仅校验交接包，打印结构摘要或全部定位错误
- build     校验并构建 SQLite 库（失败不覆盖已有库）
- forward   正查：按登记 ID（+可选源编号范围）取换算明细与可入仓区段
- reverse   反查：按目标体系标牌点/区间选查登记，可再限轴或目标段
- regs      列出库内登记
- calib     按稳定行标识取标定行（复核拒绝依据）
- plan      展示反查的 SQLite 查询计划（验收用，证明走索引）

默认输出 UTF-8 JSON（ensure_ascii=False）；加 --text 给班组看紧凑表格。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, List

# 同时支持 `python3 -m chainage`（包内相对导入）与直接以文件路径执行
# （python3 -I /path/to/chainage/cli.py，便于在隔离模式处理不可信交接包）。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from chainage import __version__
    from chainage.build import build, validate_only
    from chainage.errors import ChainageError, PackageValidationError
    from chainage.query import (
        explain_reverse_plan,
        forward_query,
        get_calibration,
        list_registrations,
        reverse_query,
    )
else:
    from . import __version__
    from .build import build, validate_only
    from .errors import ChainageError, PackageValidationError
    from .query import (
        explain_reverse_plan,
        forward_query,
        get_calibration,
        list_registrations,
        reverse_query,
    )

_STATUS_CN = {
    "NO_SOURCE": "无源位置",
    "MULTIPLE_SOURCE": "多个源位置",
    "NO_TARGET": "缺目标标牌",
    "RESOLVED": "可入仓",
}


def _emit(obj: Any, as_text: bool, text_renderer=None) -> int:
    if as_text and text_renderer is not None:
        text_renderer(obj)
    else:
        json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    return 0


def _render_forward(obj: dict) -> None:
    reg = obj["registration"]
    print(f"登记 {reg['reg_id']}: {reg['source_system']} -> "
          f"{reg['target_system']}，源区间 [{reg['n_from']},{reg['n_to']}]"
          f"，交接版本 {obj['package_version']}")
    print(f"查询范围: [{obj['query_range']['n_from']},"
          f"{obj['query_range']['n_to']}]")
    for s in obj["segments"]:
        print(f"  [{s['source_label_lo']},{s['source_label_hi']}] "
              f"{s['status']}（{_STATUS_CN[s['status']]}），"
              f"位置数 {s['n_positions']}")
        for p in s["positions"]:
            ev = ", ".join(
                f"{e['target_segment']}/[{e['target_label_lo']},"
                f"{e['target_label_hi']}]" for e in p["target_evidence"]) \
                or "无目标标牌"
            src = ",".join(r["row_id"] for r in p["source_rows"])
            print(f"      轴 {p['axis_id']} 物理[{p['x_lo']},{p['x_hi']}] "
                  f"源依据[{src}] 目标: {ev}")
    if obj["warehousable_segments"]:
        print("可自动入仓区段:")
        for w in obj["warehousable_segments"]:
            print(f"  [{w['source_label_lo']},{w['source_label_hi']}]")
    else:
        print("可自动入仓区段: 无")


def _render_reverse(obj: dict) -> None:
    q = obj["query"]
    print(f"反查目标体系 {q['target_system']} 标牌 "
          f"[{q['target_label_from']},{q['target_label_to']}]"
          + (f"，轴 {q['axis_id']}" if q["axis_id"] else "")
          + (f"，目标段 {q['target_segment']}" if q["target_segment"] else "")
          + f"，交接版本 {obj['package_version']}")
    if not obj["matches"]:
        print("无命中")
        return
    for m in obj["matches"]:
        print(f"登记 {m['reg_id']} 分段 "
              f"[{m['segment_source_label_lo']},"
              f"{m['segment_source_label_hi']}] "
              f"实际命中源刻度 [{m['matched_source_label_lo']},"
              f"{m['matched_source_label_hi']}] "
              f"原状态 {m['original_status']}"
              f"（{_STATUS_CN[m['original_status']]}，不改写）")
        for p in m["positions"]:
            ev = ", ".join(
                f"{e['target_segment']}/[{e['target_label_lo']},"
                f"{e['target_label_hi']}]{'*命中*' if e['matched'] else ''}"
                for e in p["target_evidence"]) or "无目标标牌"
            print(f"      轴 {p['axis_id']} 物理[{p['x_lo']},{p['x_hi']}] "
                  f"目标: {ev}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chainage",
        description="道路桩号断链离线入仓工具（Python + SQLite，纯本地）")
    parser.add_argument("--version", action="version",
                        version=f"chainage {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="仅校验交接包")
    p_val.add_argument("package_dir")

    p_build = sub.add_parser("build", help="校验并构建 SQLite 库")
    p_build.add_argument("package_dir")
    p_build.add_argument("db_path")

    p_fwd = sub.add_parser("forward", help="正查：按登记 ID 取换算明细")
    p_fwd.add_argument("db_path")
    p_fwd.add_argument("reg_id")
    p_fwd.add_argument("--from", dest="n_from", type=int)
    p_fwd.add_argument("--to", dest="n_to", type=int)

    p_rev = sub.add_parser("reverse", help="反查：按目标标牌选查登记")
    p_rev.add_argument("db_path")
    p_rev.add_argument("target_system")
    p_rev.add_argument("--n", type=int, help="目标标牌点号")
    p_rev.add_argument("--from", dest="n_from", type=int, help="目标标牌区间起")
    p_rev.add_argument("--to", dest="n_to", type=int, help="目标标牌区间止")
    p_rev.add_argument("--axis", dest="axis_id")
    p_rev.add_argument("--segment", dest="target_segment")

    p_regs = sub.add_parser("regs", help="列出库内登记")
    p_regs.add_argument("db_path")

    p_cal = sub.add_parser("calib", help="按行标识取标定行")
    p_cal.add_argument("db_path")
    p_cal.add_argument("row_id")

    p_plan = sub.add_parser("plan", help="反查查询计划（验收用）")
    p_plan.add_argument("db_path")
    p_plan.add_argument("target_system")
    p_plan.add_argument("--n", type=int, required=True)

    for p in (p_val, p_build, p_fwd, p_rev, p_regs, p_cal, p_plan):
        p.add_argument("--text", action="store_true", help="紧凑文本输出（默认 JSON）")
    return parser


def main(argv: List[str] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "validate":
            summary = validate_only(args.package_dir)
            if args.text:
                print("校验通过：" + json.dumps(summary, ensure_ascii=False))
            else:
                _emit(summary, False)
            return 0

        if args.cmd == "build":
            stats = build(args.package_dir, args.db_path)
            return _emit(stats.to_dict(), args.text,
                         lambda o: print("构建完成: " +
                                         json.dumps(o, ensure_ascii=False)))

        if args.cmd == "forward":
            obj = forward_query(args.db_path, args.reg_id,
                                args.n_from, args.n_to)
            return _emit(obj, args.text, _render_forward)

        if args.cmd == "reverse":
            obj = reverse_query(
                args.db_path, args.target_system, n=args.n,
                n_from=args.n_from, n_to=args.n_to,
                axis_id=args.axis_id, target_segment=args.target_segment)
            return _emit(obj, args.text, _render_reverse)

        if args.cmd == "regs":
            return _emit(list_registrations(args.db_path), args.text,
                         lambda rows: [print(r["reg_id"], r["source_system"],
                                             r["target_system"],
                                             f"[{r['n_from']},{r['n_to']}]",
                                             r["axis_id"] or "-",
                                             r["segment"] or "-")
                                       for r in rows])

        if args.cmd == "calib":
            row = get_calibration(args.db_path, args.row_id)
            if row is None:
                raise ChainageError(f"标定行标识不存在: {args.row_id}")
            return _emit(row, args.text,
                         lambda r: print(json.dumps(r, ensure_ascii=False)))

        if args.cmd == "plan":
            plan = explain_reverse_plan(
                args.db_path, args.target_system, args.n, args.n)
            return _emit(plan, args.text,
                         lambda rows: [print(r["detail"]) for r in rows])

    except PackageValidationError as exc:
        payload = {
            "error": "PACKAGE_VALIDATION_FAILED",
            "validation_errors": [e.to_dict() for e in exc.errors],
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 2
    except ChainageError as exc:
        json.dump({"error": exc.__class__.__name__, "message": str(exc)},
                  sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
