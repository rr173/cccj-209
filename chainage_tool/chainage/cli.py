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
    from chainage.errors import (
        ChainageError,
        PackageValidationError,
        SettlementValidationError,
    )
    from chainage.query import (
        explain_reverse_plan,
        forward_query,
        get_calibration,
        list_registrations,
        reverse_query,
    )
    from chainage.settlement import run_settlement
else:
    from . import __version__
    from .build import build, validate_only
    from .errors import (
        ChainageError,
        PackageValidationError,
        SettlementValidationError,
    )
    from .query import (
        explain_reverse_plan,
        forward_query,
        get_calibration,
        list_registrations,
        reverse_query,
    )
    from .settlement import run_settlement

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


def _render_list(rows: List[dict], list_mode: str) -> None:
    label = ("可直接派人补巡的路段（确定缺巡）" if list_mode == "dispatch"
             else "先核对行车记录、暂不派工的路段（待核实）")
    print(label + ":")
    if not rows:
        print("  无")
    for d in rows:
        print(f"  {d['obligation_id']} 轴 {d['axis_id']} 车道 {d['lane']} "
              f"[{d['lo']},{d['hi']}]")


def _render_settle(obj: dict, list_mode: str) -> None:
    ledger = obj["ledger"]
    print(f"结算截止点 {obj['cutoff']}，换算库 {obj['db_path']}")
    if list_mode == "dispatch":
        print("可直接派人补巡的路段（确定缺巡）:")
        if not ledger["dispatch"]:
            print("  无")
        for d in ledger["dispatch"]:
            print(f"  {d['obligation_id']} 轴 {d['axis_id']} 车道 {d['lane']} "
                  f"[{d['lo']},{d['hi']}]")
        return
    if list_mode == "review":
        print("先核对行车记录、暂不派工的路段（待核实）:")
        if not ledger["review"]:
            print("  无")
        for d in ledger["review"]:
            print(f"  {d['obligation_id']} 轴 {d['axis_id']} 车道 {d['lane']} "
                  f"[{d['lo']},{d['hi']}]")
        return
    for ob in ledger["obligations"]:
        print(f"义务 {ob['obligation_id']} 轴 {ob['axis_id']} 车道 {ob['lane']} "
              f"物理[{ob['start']},{ob['end']}] 状态 {ob['state_cn']}"
              f"（{ob['state']}）")
        for s in ob["segments"]:
            thr = " ".join(
                f"{t['label']}:{t['lower']}/{t['upper']}(需{t['min_crews']})"
                for t in s["thresholds"])
            print(f"  [{s['lo']},{s['hi']}] {s['state_cn']} {thr}")
    print("缺巡清单:")
    for d in ledger["dispatch"]:
        print(f"  {d['obligation_id']} 轴 {d['axis_id']} 车道 {d['lane']} "
              f"[{d['lo']},{d['hi']}]")
    if not ledger["dispatch"]:
        print("  无")
    print("待核实清单:")
    for d in ledger["review"]:
        print(f"  {d['obligation_id']} 轴 {d['axis_id']} 车道 {d['lane']} "
              f"[{d['lo']},{d['hi']}]")
    if not ledger["review"]:
        print("  无")


def _render_diff(obj: dict) -> None:
    print(f"截止点比较: {obj['cutoff_before']} -> {obj['cutoff']}"
          f"（换算库 {obj['db_path']}）")
    for ob in obj["comparison"]["obligations"]:
        print(f"义务 {ob['obligation_id']} 轴 {ob['axis_id']} 车道 {ob['lane']} "
              f"[{ob['start']},{ob['end']}]: {ob['change_count']} 处状态改变")
        for c in ob["changes"]:
            b = " ".join(f"{t['label']}:{t['lower']}/{t['upper']}"
                         for t in c["before_thresholds"])
            a = " ".join(f"{t['label']}:{t['lower']}/{t['upper']}"
                         for t in c["after_thresholds"])
            print(f"  [{c['lo']},{c['hi']}] {c['before_state']} -> "
                  f"{c['after_state']}  人数 {b} => {a}")


def _render_explain(obj: dict) -> None:
    print(f"义务 {obj['obligation_id']} 轴 {obj['axis_id']} 物理位置 {obj['x']}: "
          f"{obj['state_cn']}（{obj['state']}）")
    for t in obj["thresholds"]:
        print(f" 门槛 {t['label']} [{t['window_start']},{t['window_end']}) "
              f"需 {t['min_crews']} 班组，下界 {t['lower']} / 上界 {t['upper']}")
        print(f"   确定班组: {', '.join(t['definite_crews']) or '无'}")
        print(f"   仅可能班组: {', '.join(t['possible_only_crews']) or '无'}")
    for trip in obj["trips"]:
        print(f" 趟次 {trip['trip_id']} 班组 {trip['crew']} "
              f"入簿版本 ingest={trip['ingest_time']} "
              f"实测={trip['survey_time']} "
              f"{'确定' if trip['definite'] else ('可能' if trip['possible'] else '未命中')}")
        for r in trip["routes"]:
            print(f"   路线版本 #{r['route_index']} "
                  f"{'命中' if r['covers_point'] else '未命中'}")
            for f in r["fragments"]:
                print(f"     片段#{f['fragment_index']} 登记 {f['reg_id']} "
                      f"源[{f['n_from']},{f['n_to']}] -> 物理[{f['x_lo']},{f['x_hi']}] "
                      f"标定行 {f['calib_row_id']}(第 {f['calib_source_line']} 行)"
                      f"{' 覆盖该点' if f['covers_point'] else ''}")


def _settle_call(args) -> dict:
    return run_settlement(args.db_path, args.obligations, args.trip_log,
                          cutoff=args.cutoff)


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

    # ---------------- 巡检履约结算 ----------------
    p_settle = sub.add_parser(
        "settle", help="单截止点：按义务出达标/缺巡/待核实台账与两张清单")
    p_settle.add_argument("db_path")
    p_settle.add_argument("obligations")
    p_settle.add_argument("trip_log")
    p_settle.add_argument("--cutoff", type=int, required=True,
                          help="结算截止点（UTC 整数秒，含）")
    p_settle.add_argument("--list", choices=("ledger", "dispatch", "review"),
                          default="ledger",
                          help="输出整份台账，或直接取缺巡/待核实清单")

    p_diff = sub.add_parser(
        "settle-diff", help="两个截止点：列出状态改变的区段与前后人数")
    p_diff.add_argument("db_path")
    p_diff.add_argument("obligations")
    p_diff.add_argument("trip_log")
    p_diff.add_argument("--before", type=int, required=True,
                        help="较早截止点（含）")
    p_diff.add_argument("--after", type=int, required=True,
                        help="较晚截止点（含）")

    p_expl = sub.add_parser(
        "settle-explain", help="单点取证：某义务在某物理位置的逐门槛解释")
    p_expl.add_argument("db_path")
    p_expl.add_argument("obligations")
    p_expl.add_argument("trip_log")
    p_expl.add_argument("--cutoff", type=int, required=True)
    p_expl.add_argument("--obligation", required=True, dest="obligation_id")
    p_expl.add_argument("--x", type=int, required=True,
                        help="义务区间内的物理刻度")

    for p in (p_val, p_build, p_fwd, p_rev, p_regs, p_cal, p_plan,
              p_settle, p_diff, p_expl):
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

        if args.cmd == "settle":
            obj = _settle_call(args)
            if args.list != "ledger":
                # 清单可直接取用：只输出该张清单（缺巡或待核实）
                payload = obj["ledger"][args.list]
                return _emit(payload, args.text,
                             lambda rows: _render_list(rows, args.list))
            return _emit(obj, args.text,
                         lambda o: _render_settle(o, args.list))

        if args.cmd == "settle-diff":
            obj = run_settlement(args.db_path, args.obligations,
                                 args.trip_log, cutoff=args.after,
                                 cutoff_before=args.before)
            return _emit(obj, args.text, _render_diff)

        if args.cmd == "settle-explain":
            obj = run_settlement(
                args.db_path, args.obligations, args.trip_log,
                cutoff=args.cutoff,
                explain=(args.obligation_id, args.x))
            return _emit(obj, args.text,
                         lambda o: _render_explain(o["explain"]))

    except SettlementValidationError as exc:
        payload = {
            "error": "SETTLEMENT_VALIDATION_FAILED",
            "issues": [i.to_dict() for i in exc.issues],
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 2
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
