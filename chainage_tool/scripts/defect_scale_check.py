"""病害归并工作区的规模验收（大数据只落 data/，不入库）。

两组：

1. **局部性**：先建 5000 个互不相干的病害组与 3000 条无关报件，再建一个
   含桥接、环与隔离证据的大组；然后只改大组里的一条裁定，报告该批实际
   触及的组/成员数，以及候选检索在大量无关报件下的预筛读取量。
2. **候选预筛**：单一大型分桶里放入数万条报件，查询一条起点；比较
   "同一分桶总行数"、"空间+时间窗内行数"与"命中数"，并给出 EXPLAIN
   QUERY PLAN，证明走索引而不是全表扫描。

报告写入 ``<workdir>/defect_scale_report.json``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage.build import build  # noqa: E402
from chainage.defects import (  # noqa: E402
    BATCH_FORMAT,
    apply_batch,
    candidate_pairs,
    create_workspace,
    fetch_changes,
    fetch_full,
    last_batch_scope,
    list_groups,
    new_connection,
    workspace_info,
)


def _write_pkg(root):
    pkg = os.path.join(root, "pkg")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": "defect-scale",
                   "axes": [{"axis_id": "A1", "length": 2_000_000}]}, fh)
    cal = [
        {"row_id": "S1", "system": "OLD", "axis_id": "A1", "segment": "S",
         "lo": 0, "hi": 1_999_990, "label_lo": 1000, "direction": 1},
        {"row_id": "G1", "system": "NEW", "axis_id": "A1", "segment": "G",
         "lo": 0, "hi": 1_999_990, "label_lo": 2_000_000, "direction": 1},
    ]
    with open(os.path.join(pkg, "calibration.jsonl"), "w",
              encoding="utf-8") as fh:
        for row in cal:
            fh.write(json.dumps(row) + "\n")
    with open(os.path.join(pkg, "registration.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({"reg_id": "R1", "source_system": "OLD",
                             "target_system": "NEW", "from": 1000,
                             "to": 2_000_990}) + "\n")
    return pkg


def _report(src, ref, n, t, category="pothole", lane="L1"):
    return {"source": src, "source_ref": ref, "category": category,
            "lane": lane, "found_at": t, "reg_id": "R1", "reg_n": n}


def _adj(a, b, decision):
    return {"a": {"source": a[0], "source_ref": a[1]},
            "b": {"source": b[0], "source_ref": b[1]},
            "decision": decision}


def _write_batch(root, name, rev, ws_id, reports=None, adjudications=None):
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"format": BATCH_FORMAT, "batch_no": name,
                   "based_on_workspace": ws_id, "based_on_revision": rev,
                   "reports": reports or [], "void": [],
                   "adjudications": adjudications or []}, fh)
    return path


def run(workdir):
    os.makedirs(workdir, exist_ok=True)
    pkg = _write_pkg(workdir)
    chainage_db = os.path.join(workdir, "chainage.db")
    build(pkg, chainage_db)
    ws_dir = os.path.join(workdir, "ws")
    create_workspace(ws_dir, chainage_db, spatial_threshold=4,
                     time_threshold=120)
    ws_id = workspace_info(ws_dir)["workspace_id"]

    report = {"workdir": os.path.abspath(workdir)}

    # ---------------- 组 1：大量无关病害 + 一个大组 ----------------
    n_isolated = 5000
    reports = []
    for i in range(n_isolated):
        reports.append(_report("SRC", f"S{i:05d}", 1000 + i * 10,
                               1_000_000 + i * 10))
    # 1000 条无关"第二来源"报件，位于另一分桶
    for i in range(1000):
        reports.append(_report("MAP", f"M{i:05d}", 1000 + i * 10,
                               1_000_000 + i * 10, category="crack"))
    # 大组：桥接 + 环 + 隔离证据
    big = [f"L{i:03d}" for i in range(40)]
    for i, ref in enumerate(big):
        reports.append(_report("SRC", ref, 1_500_000 + i, 2_000_000 + i))
    adjs = []
    for i in range(len(big) - 1):
        adjs.append(_adj(("SRC", big[i]), ("SRC", big[i + 1]), "same"))
    adjs.append(_adj(("SRC", big[0]), ("SRC", big[25]), "same"))   # 环
    adjs.append(_adj(("SRC", big[10]), ("SRC", big[20]), "same"))  # 弦
    adjs.append(_adj(("SRC", big[30]), ("SRC", big[0]), "different"))

    t0 = time.time()
    apply_batch(ws_dir, _write_batch(workdir, "g1-01.json", 0, ws_id,
                                     reports, adjs))
    report["bulk_load_seconds"] = round(time.time() - t0, 3)

    tracemalloc.start()
    t0 = time.time()
    # 只改大组里的一条边：组不拆（有环）
    apply_batch(ws_dir, _write_batch(
        workdir, "g1-02.json", 1, ws_id,
        adjudications=[_adj(("SRC", big[0]), ("SRC", big[1]), "clear")]))
    report["local_correction_seconds"] = round(time.time() - t0, 3)
    scope = last_batch_scope(ws_dir)
    report["local_correction"] = {
        "touched_groups": len(scope["groups"]),
        "touched_member_rows": len(scope["members"]),
        "workspace_totals": scope["current_totals"],
    }

    # 再加 2000 条无关单件报件，然后同样改大组一条边，触及范围应不变
    extra = [_report("FLD", f"X{i:05d}", 1000 + i * 37, 3_000_000 + i,
                     category="crack") for i in range(2000)]
    apply_batch(ws_dir, _write_batch(workdir, "g1-03.json", 2, ws_id, extra))
    # 再撤一条边：它的两端仍可由弦 L10-L20 连通，组不拆：只触及该大组
    apply_batch(ws_dir, _write_batch(
        workdir, "g1-04.json", 3, ws_id,
        adjudications=[_adj(("SRC", big[10]), ("SRC", big[11]), "clear")]))
    scope2 = last_batch_scope(ws_dir)
    report["local_correction_after_bulk"] = {
        "touched_groups": len(scope2["groups"]),
        "touched_member_rows": len(scope2["members"]),
        "touched_group_keys": scope2["groups"],
        "workspace_totals": scope2["current_totals"],
    }

    # 候选：从一条无关单件组报件出发，检查它不会把大组/大量无关报件
    # 当成候选（同来源不配对），并报告预筛实际读取量
    t0 = time.time()
    cand = candidate_pairs(ws_dir, "SRC", "S00000", explain_scan=True)
    report["candidate_scan"] = {
        "seconds": round(time.time() - t0, 4),
        "scan": cand["scan"],
        "candidates": len(cand["candidates"]),
        "eligible": cand["eligible"],
    }

    # 全量/增量读取规模
    t0 = time.time()
    full = fetch_full(ws_dir)
    report["full_fetch_seconds"] = round(time.time() - t0, 3)
    report["full_rows"] = {"groups": len(full["defect_groups"]),
                           "members": len(full["defect_members"])}
    t0 = time.time()
    inc = fetch_changes(ws_dir, 0)
    report["increment_fetch_seconds"] = round(time.time() - t0, 3)
    report["increment_rows"] = sum(len(b["changes"]) for b in inc["batches"])
    report["peak_python_bytes"] = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    # ---------------- 组 2：单一大型分桶的候选预筛 ----------------
    ws2 = os.path.join(workdir, "ws-cand")
    create_workspace(ws2, chainage_db, spatial_threshold=3,
                     time_threshold=60)
    ws2_id = workspace_info(ws2)["workspace_id"]
    big_bucket = []
    for i in range(20000):
        # 同类别/车道/轴，位置与时间分散；只有少数落在窗口内
        big_bucket.append(_report("BULK", f"B{i:06d}", 1000 + (i * 41) % 900000,
                                  1_000_000 + i * 7))
    # 起点 B000100 落在 n=1000+4100 -> 物理 4100、t=1_000_700；
    # NEAR 与其同轴同窗、跨来源，应成为唯一候选。
    big_bucket.append(_report("OTHER", "NEAR", 1000 + 4102, 1_000_700 + 30))
    t0 = time.time()
    apply_batch(ws2, _write_batch(workdir, "g2-01.json", 0, ws2_id,
                                  big_bucket))
    report["candidate_bulk_load_seconds"] = round(time.time() - t0, 3)
    t0 = time.time()
    cand2 = candidate_pairs(ws2, "BULK", "B000100", explain_scan=True)
    report["candidate_search"] = {
        "seconds": round(time.time() - t0, 4),
        "scan": cand2["scan"],
        "candidates": [(c["other"]["source"], c["other"]["source_ref"],
                        c["spatial_delta"], c["time_delta"])
                       for c in cand2["candidates"]],
    }
    conn = new_connection(os.path.join(ws2, "workspace.db"))
    try:
        plan = [dict(r) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT source, source_ref FROM pair_bucket "
            "WHERE category=? AND lane=? AND axis_id=? "
            "AND loc_hi >= ? AND loc_lo <= ? AND found_at >= ? "
            "AND found_at <= ?", ("pothole", "L1", "A1", 4000, 4000,
                                  999000, 1001000))]
    finally:
        conn.close()
    report["candidate_query_plan"] = plan

    report["revision"] = workspace_info(ws_dir)["revision"]
    out = os.path.join(workdir, "defect_scale_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="data/defect-scale")
    args = ap.parse_args(argv)
    run(args.workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
