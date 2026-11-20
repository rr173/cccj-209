"""生成病害归并工作区的文档化示例（小数据，随仓库提供）。

产物::

    <out_root>/chainage_pkg/    交接包（建 ready 换算库用）
    <out_root>/batch-01.json    录入 A、B、C 并裁定 A同B、B同C、A不同C
    <out_root>/batch-02.json    撤去 B同C
    <out_root>/batch-03.json    加回 B同C 且同时撤去 A不同C

A、B、C 的 ``source_ref`` 按 Unicode 码点序排列（A < B < C），因此组键
始终是最小的报件键 ``["巡查","A"]``。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage.build import build  # noqa: E402
from chainage.defects import (  # noqa: E402
    apply_batch,
    create_workspace,
    fetch_full,
    group_detail,
    workspace_info,
)

FORMAT = "chainage-defect-batch"
DAY0 = 1795161600

CALIB = [
    # 旧线 S：物理 0..60 编号 1000..1060
    {"row_id": "S1", "system": "OLD", "axis_id": "A1", "segment": "S",
     "lo": 0, "hi": 60, "label_lo": 1000, "direction": 1},
    # 新线 G：物理 0..60 编号 2000..2060
    {"row_id": "G1", "system": "NEW", "axis_id": "A1", "segment": "G",
     "lo": 0, "hi": 60, "label_lo": 2000, "direction": 1},
    # 旧线 T：物理 60..80 反向编号 1070..1050
    {"row_id": "T1", "system": "OLD", "axis_id": "A1", "segment": "T",
     "lo": 60, "hi": 80, "label_lo": 1070, "direction": -1},
]

REGS = [
    {"reg_id": "R1", "source_system": "OLD", "target_system": "NEW",
     "from": 1000, "to": 1065},
]


def report(ref, n, t, category="坑槽", lane="右幅行车道"):
    return {"source": "巡查", "source_ref": ref, "category": category,
            "lane": lane, "found_at": t, "reg_id": "R1", "reg_n": n}


def adj(a, b, decision):
    return {"a": {"source": "巡查", "source_ref": a},
            "b": {"source": "巡查", "source_ref": b},
            "decision": decision}


def write_pkg(root):
    pkg = os.path.join(root, "chainage_pkg")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": "defect-demo-2026-11-20",
                   "axes": [{"axis_id": "A1", "length": 100}]},
                  fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(os.path.join(pkg, "calibration.jsonl"), "w",
              encoding="utf-8") as fh:
        for row in CALIB:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(os.path.join(pkg, "registration.jsonl"), "w",
              encoding="utf-8") as fh:
        for row in REGS:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return pkg


def batch(batch_no, revision, workspace_id, reports=None, void=None,
          adjudications=None):
    return {
        "format": FORMAT,
        "batch_no": batch_no,
        "based_on_workspace": workspace_id,
        "based_on_revision": revision,
        "reports": reports or [],
        "void": void or [],
        "adjudications": adjudications or [],
    }


def main(argv):
    out_root = argv[1] if len(argv) > 1 else "examples/defect"
    os.makedirs(out_root, exist_ok=True)
    pkg = write_pkg(out_root)
    chainage_db = os.path.join(out_root, "chainage.db")
    build(pkg, chainage_db)
    ws_dir = os.path.join(out_root, "workspace")
    create_workspace(ws_dir, chainage_db, spatial_threshold=3,
                     time_threshold=600)
    ws_id = workspace_info(ws_dir)["workspace_id"]
    batches = {
        "batch-01.json": batch(
            "demo-01", 0, ws_id,
            reports=[report("A", 1000, DAY0 + 8 * 3600),
                     report("B", 1000, DAY0 + 8 * 3600 + 120),
                     report("C", 1005, DAY0 + 8 * 3600 + 300)],
            adjudications=[adj("A", "B", "same"), adj("B", "C", "same"),
                           adj("A", "C", "different")]),
        "batch-02.json": batch(
            "demo-02", 1, ws_id,
            adjudications=[adj("B", "C", "clear")]),
        "batch-03.json": batch(
            "demo-03", 2, ws_id,
            adjudications=[adj("B", "C", "same"), adj("A", "C", "clear")]),
    }
    receipts = []
    stages = []
    for name in sorted(batches):
        path = os.path.join(out_root, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(batches[name], fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        receipts.append(apply_batch(ws_dir, path)["receipt"])
        # 每批之后记录当时的两张表与 A 组隔离证据（用于对照过程）
        snapshot = fetch_full(ws_dir)
        detail = group_detail(ws_dir, '["巡查","A"]') \
            if any(g["group_key"] == '["巡查","A"]'
                   for g in snapshot["defect_groups"]) else None
        stages.append({
            "batch": batches[name]["batch_no"],
            "revision": snapshot["revision"],
            "groups": snapshot["defect_groups"],
            "members": [{k: m[k] for k in
                         ("source", "source_ref", "group_key")}
                        for m in snapshot["defect_members"]],
            "group_a_evidence": (detail["isolation_evidence"]
                                 if detail else None),
        })
    full = fetch_full(ws_dir)
    summary = {
        "out_root": os.path.abspath(out_root),
        "package": pkg,
        "chainage_db": chainage_db,
        "workspace": ws_dir,
        "workspace_id": ws_id,
        "batches": [os.path.join(out_root, n) for n in sorted(batches)],
        "revisions": [r["revision"] for r in receipts],
        "groups": full["defect_groups"],
        "members": [{k: m[k] for k in
                     ("source", "source_ref", "group_key", "found_at",
                      "location")} for m in full["defect_members"]],
        "stages": stages,
    }
    with open(os.path.join(out_root, "demo_summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(json.dumps({
        "out_root": summary["out_root"],
        "workspace": ws_dir,
        "workspace_id": ws_id,
        "revisions": summary["revisions"],
        "groups": [(g["group_key"], g["isolated"], g["member_count"])
                   for g in full["defect_groups"]],
        "batches": summary["batches"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
