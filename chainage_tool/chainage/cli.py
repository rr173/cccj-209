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
- defect-*  病害归并工作区：建区、批次录入/裁定、全量/增量、候选与隔离复核

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
        DefectValidationError,
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
    from chainage import defects as defect_ws
else:
    from . import __version__
    from .build import build, validate_only
    from .errors import (
        ChainageError,
        DefectValidationError,
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
    from . import defects as defect_ws

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


def _render_defect_init(obj: dict) -> None:
    print(f"病害工作区已建立: {obj['workspace_dir']}")
    print(f"  workspace_id: {obj['workspace_id']}")
    print(f"  绑定换算库指纹: {obj['chainage_fingerprint']}")
    print(f"  阈值: 空间 {obj['spatial_threshold']} 刻度，"
          f"时间 {obj['time_threshold']} 秒（端点算命中）")
    print(f"  当前修订: {obj['revision']}")


def _render_defect_batch(obj: dict) -> None:
    r = obj["receipt"]
    prefix = "重试回放原收据" if obj.get("replayed") else "批已提交"
    print(f"{prefix}: 批号 {r['batch_no']}，基准修订 {r['base_revision']} "
          f"-> 修订 {r['revision']}")
    c = r["counts"]
    print(f"  录入 {c['reports_entered']}，撤下 {c['reports_voided']}，"
          f"裁定（同一/不同/撤去）{c['adjudications_same']}/"
          f"{c['adjudications_different']}/{c['adjudications_cleared']}")
    print(f"  组 {c['groups_total']}（隔离 {c['groups_isolated']}），"
          f"成员 {c['members_total']}")
    ch = r["changes"]
    print(f"  增量: 共 {ch['total']} 行（组 {ch['defect_group']}，"
          f"成员 {ch['defect_member']}）")


def _render_defect_full(obj: dict) -> None:
    print(f"全量修订 {obj['revision']}，工作区 {obj['workspace_id']}")
    print("病害组表:")
    for g in obj["defect_groups"]:
        print(f"  {g['group_key']}  隔离={'是' if g['isolated'] else '否'}  "
              f"成员 {g['member_count']}  发现 "
              f"[{g['first_found']},{g['last_found']}]")
    print("成员表:")
    for m in obj["defect_members"]:
        loc = m["location"]
        where = (f"轴 {loc['axis_id']} x={loc['lo']}"
                 if loc["status"] == "localizable"
                 else loc["status"])
        print(f"  {m['source']}/{m['source_ref']} -> {m['group_key']}  "
              f"{m['category']} {m['lane']} t={m['found_at']} {where}")


def _render_defect_changes(obj: dict) -> None:
    print(f"增量: 修订 {obj['from_revision']} -> {obj['revision']}")
    if not obj["batches"]:
        print("  无新批")
        return
    for b in obj["batches"]:
        if not b["changes"]:
            print(f"  修订 {b['revision']}（批 {b['batch_no']}）: 无表变化，"
                  f"修订已确认")
            continue
        print(f"  修订 {b['revision']}（批 {b['batch_no']}）:")
        for c in b["changes"]:
            print(f"    {c['op']} {c['table']} {json.dumps(c['key'], ensure_ascii=False)}")


def _render_defect_groups(obj: dict) -> None:
    print(f"修订 {obj['revision']}，病害组 {len(obj['groups'])} 个")
    for g in obj["groups"]:
        print(f"  {g['group_key']}  隔离={'是' if g['isolated'] else '否'}  "
              f"成员 {g['member_count']}  "
              f"发现 [{g['first_found']},{g['last_found']}]")


def _render_defect_group(obj: dict) -> None:
    g = obj["group"]
    print(f"修订 {obj['revision']}，病害组 {g['group_key']}，"
          f"隔离={'是' if g['isolated'] else '否'}，成员 {g['member_count']}")
    for m in obj["members"]:
        loc = m["location"]
        where = (f"轴 {loc['axis_id']} x={loc['lo']}"
                 if loc["status"] == "localizable" else loc["status"])
        print(f"  {m['source']}/{m['source_ref']} 类别 {m['category']} "
              f"车道 {m['lane']} 发现 {m['found_at']} {where} "
              f"登记 {m['registration']['reg_id']}/"
              f"{m['registration']['reg_n']}")
    ev = obj.get("isolation_evidence")
    if ev and ev.get("isolated"):
        print("隔离证据:")
        for e in ev["different_edges"]:
            a, b = e["between"]
            path = " -> ".join(f"{p[0]}/{p[1]}" for p in e["same_path"])
            print(f"  不同 {a[0]}/{a[1]} vs {b[0]}/{b[1]}；"
                  f"同一路径: {path}")


def _render_defect_evidence(obj: dict) -> None:
    if not obj.get("isolated"):
        print(f"病害组 {obj['group_key']} 未被隔离")
        return
    print(f"病害组 {obj['group_key']} 被隔离，导致隔离的不同裁定:")
    for e in obj["different_edges"]:
        a, b = e["between"]
        path = " -> ".join(f"{p[0]}/{p[1]}" for p in e["same_path"])
        print(f"  不同 {a[0]}/{a[1]} vs {b[0]}/{b[1]}")
        print(f"    同一路径: {path}")


def _render_defect_candidates(obj: dict) -> None:
    s = obj["start"]
    print(f"修订 {obj['revision']}，起点 {s['source']}/{s['source_ref']} "
          f"（{s['category']} {s['lane']} t={s['found_at']}）")
    if not obj["eligible"]:
        print("  起点没有唯一物理位置，不参加按位置找候选")
        return
    print(f"  阈值: 空间 {obj['thresholds']['spatial']}，"
          f"时间 {obj['thresholds']['time']}")
    if not obj["candidates"]:
        print("  无未裁定的跨来源候选")
    for c in obj["candidates"]:
        o = c["other"]
        loc = o["location"]
        print(f"  候选 {o['source']}/{o['source_ref']} "
              f"轴 {loc['axis_id']} x={loc['lo']} t={o['found_at']} "
              f"δx={c['spatial_delta']} δt={c['time_delta']} "
              f"登记 {o['registration']['reg_id']}/{o['registration']['reg_n']}")
    if "scan" in obj:
        sc = obj["scan"]
        print(f"  扫描量: 同桶 {sc['bucket_rows']} 行，"
              f"空间+时间窗内 {sc['space_time_preselected']} 行，"
              f"逐对判断 {sc['pairs_examined']}，命中 {sc['candidates']}")


def _render_defect_reports(obj: dict) -> None:
    print(f"修订 {obj['revision']}，报件 {len(obj['reports'])} 条")
    for r in obj["reports"]:
        loc = r["location"]
        where = (f"轴 {loc['axis_id']} x={loc['lo']}"
                 if loc["status"] == "localizable" else loc["status"])
        print(f"  {'[在册]' if r['present'] else '[已撤]'} "
              f"{r['source']}/{r['source_ref']} {r['category']} {r['lane']} "
              f"t={r['found_at']} {where} 登记 {r['registration']['reg_id']}/"
              f"{r['registration']['reg_n']}")


def _render_defect_adjs(obj: dict) -> None:
    print(f"修订 {obj['revision']}，裁定 {len(obj['adjudications'])} 条")
    for a in obj["adjudications"]:
        print(f"  {'生效' if a['effective'] else '已撤'} {a['kind']} "
              f"{a['a'][0]}/{a['a'][1]} - {a['b'][0]}/{a['b'][1]} "
              f"(修订 {a['since_revision']} 批 {a['batch_no']})")


def _render_defect_scope(obj: dict) -> None:
    print(f"批 {obj['batch_no']} -> 修订 {obj['revision']}")
    print(f"  触及组 {len(obj['groups'])} 个:")
    for gk in obj["groups"]:
        print(f"    {gk}")
    print(f"  触及成员行 {len(obj['members'])} 条")
    b = obj.get("batch_counts") or {}
    print(f"  提交时规模: 组 {b.get('groups_total')}，"
          f"成员 {b.get('members_total')}，"
          f"隔离组 {b.get('groups_isolated')}")
    t = obj["current_totals"]
    print(f"  当前工作区总量: 组 {t['groups']}，成员 {t['members']}，"
          f"在册报件 {t['reports_present']}")


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

    # ---------------- 病害报件归并工作区 ----------------
    p_dinit = sub.add_parser(
        "defect-init", help="建病害归并工作区并绑定一份 ready 换算库")
    p_dinit.add_argument("workspace_dir")
    p_dinit.add_argument("db_path", help="ready 换算库")
    p_dinit.add_argument("--spatial-threshold", type=int, required=True,
                         help="物理刻度差阈值（闭区间，端点算命中）")
    p_dinit.add_argument("--time-threshold", type=int, required=True,
                         help="发现时间差阈值（秒，闭区间，端点算命中）")

    p_dbatch = sub.add_parser(
        "defect-batch", help="应用一批病害更正（录入/撤下/裁定）")
    p_dbatch.add_argument("workspace_dir")
    p_dbatch.add_argument("batch_file")

    p_dfull = sub.add_parser(
        "defect-full", help="全量取病害组表 + 成员表及所属修订号")
    p_dfull.add_argument("workspace_dir")

    p_dinc = sub.add_parser(
        "defect-changes", help="从给定修订之后按批取表级增量")
    p_dinc.add_argument("workspace_dir")
    p_dinc.add_argument("--since", type=int, required=True)

    p_dgroups = sub.add_parser("defect-groups", help="列出病害组")
    p_dgroups.add_argument("workspace_dir")
    p_dgroups.add_argument("--isolated", action="store_true",
                           help="只列被隔离的组")

    p_dgroup = sub.add_parser("defect-group", help="查看某组全部成员与隔离证据")
    p_dgroup.add_argument("workspace_dir")
    p_dgroup.add_argument("group_key")

    p_devid = sub.add_parser(
        "defect-evidence", help="列出导致某隔离组隔离的不同裁定与同一路径")
    p_devid.add_argument("workspace_dir")
    p_devid.add_argument("group_key")

    p_dcand = sub.add_parser(
        "defect-candidates", help="从一条在册报件列未裁定的跨来源候选")
    p_dcand.add_argument("workspace_dir")
    p_dcand.add_argument("source")
    p_dcand.add_argument("source_ref")
    p_dcand.add_argument("--spatial-threshold", type=int)
    p_dcand.add_argument("--time-threshold", type=int)
    p_dcand.add_argument("--limit", type=int)
    p_dcand.add_argument("--explain-scan", action="store_true",
                         help="附加预筛读取量与逐对判断数")

    p_dreps = sub.add_parser("defect-reports", help="列出报件（默认仅在册）")
    p_dreps.add_argument("workspace_dir")
    p_dreps.add_argument("--all", action="store_true", help="含已撤下")
    p_dreps.add_argument("--source")

    p_dadjs = sub.add_parser("defect-adjudications", help="列出裁定")
    p_dadjs.add_argument("workspace_dir")
    p_dadjs.add_argument("--all", action="store_true", help="含已撤去")

    p_dscope = sub.add_parser(
        "defect-scope", help="某批实际触及的组/成员（局部重算范围）")
    p_dscope.add_argument("workspace_dir")
    p_dscope.add_argument("--batch")

    for p in (p_val, p_build, p_fwd, p_rev, p_regs, p_cal, p_plan,
              p_settle, p_diff, p_expl, p_dinit, p_dbatch, p_dfull, p_dinc,
              p_dgroups, p_dgroup, p_devid, p_dcand, p_dreps, p_dadjs,
              p_dscope):
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

        if args.cmd == "defect-init":
            obj = defect_ws.create_workspace(
                args.workspace_dir, args.db_path, args.spatial_threshold,
                args.time_threshold)
            obj["workspace_dir"] = os.path.abspath(args.workspace_dir)
            return _emit(obj, args.text, _render_defect_init)

        if args.cmd == "defect-batch":
            obj = defect_ws.apply_batch(args.workspace_dir, args.batch_file)
            return _emit(obj, args.text, _render_defect_batch)

        if args.cmd == "defect-full":
            obj = defect_ws.fetch_full(args.workspace_dir)
            return _emit(obj, args.text, _render_defect_full)

        if args.cmd == "defect-changes":
            obj = defect_ws.fetch_changes(args.workspace_dir, args.since)
            return _emit(obj, args.text, _render_defect_changes)

        if args.cmd == "defect-groups":
            obj = defect_ws.list_groups(args.workspace_dir, args.isolated)
            return _emit(obj, args.text, _render_defect_groups)

        if args.cmd == "defect-group":
            obj = defect_ws.group_detail(args.workspace_dir, args.group_key)
            return _emit(obj, args.text, _render_defect_group)

        if args.cmd == "defect-evidence":
            obj = defect_ws.isolation_evidence(args.workspace_dir,
                                               args.group_key)
            return _emit(obj, args.text, _render_defect_evidence)

        if args.cmd == "defect-candidates":
            obj = defect_ws.candidate_pairs(
                args.workspace_dir, args.source, args.source_ref,
                spatial_threshold=args.spatial_threshold,
                time_threshold=args.time_threshold, limit=args.limit,
                explain_scan=args.explain_scan)
            return _emit(obj, args.text, _render_defect_candidates)

        if args.cmd == "defect-reports":
            obj = defect_ws.list_reports(
                args.workspace_dir, present_only=not args.all,
                source=args.source)
            return _emit(obj, args.text, _render_defect_reports)

        if args.cmd == "defect-adjudications":
            obj = defect_ws.list_adjudications(
                args.workspace_dir, effective_only=not args.all)
            return _emit(obj, args.text, _render_defect_adjs)

        if args.cmd == "defect-scope":
            obj = defect_ws.last_batch_scope(args.workspace_dir, args.batch)
            return _emit(obj, args.text, _render_defect_scope)

    except DefectValidationError as exc:
        payload = {
            "error": "DEFECT_VALIDATION_FAILED",
            "issues": [i.to_dict() for i in exc.issues],
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 2
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
