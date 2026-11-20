#!/usr/bin/env python3
"""生成巡检履约结算的文档化小样例（与题目示例同一组数字）。

输出：
    examples/settlement_pkg/       ready 换算库的交接包（物理轴 A1 0..40）
    examples/settlement/obligations.json
    examples/settlement/trips.jsonl

运行::

    python3 -I scripts/make_settlement_example.py examples/settlement_pkg \
        examples/settlement
"""

from __future__ import annotations

import json
import os
import sys

DAY0 = 1795161600  # 2026-11-20 08:00:00 UTC


def at(h: int, m: int = 0) -> int:
    return DAY0 + h * 3600 + m * 60


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(pkg_dir: str, data_dir: str) -> None:
    os.makedirs(pkg_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    manifest = {
        "version": "settlement-demo-2026-11-20",
        "axes": [{"axis_id": "A1", "length": 40}],
        "files": {"manifest": "manifest.json",
                  "calibration": "calibration.jsonl",
                  "registration": "registration.jsonl"},
        "notes": "结算示例：OLD/S 段编号 1000..1040 对应物理 0..40（正方向）",
    }
    with open(os.path.join(pkg_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    calibration = [
        {"row_id": "S1", "system": "OLD", "axis_id": "A1", "segment": "S",
         "lo": 0, "hi": 40, "label_lo": 1000, "direction": 1},
        {"row_id": "G1", "system": "NEW", "axis_id": "A1", "segment": "G",
         "lo": 0, "hi": 40, "label_lo": 2000, "direction": 1},
    ]
    write_jsonl(os.path.join(pkg_dir, "calibration.jsonl"), calibration)
    write_jsonl(os.path.join(pkg_dir, "registration.jsonl"), [
        {"reg_id": "R1", "source_system": "OLD", "target_system": "NEW",
         "from": 1000, "to": 1040, "axis_id": "A1"},
    ])

    obligations = [{
        "obligation_id": "O1", "axis_id": "A1", "lane": "L1",
        "start": 10, "end": 14,
        "window_start": at(8), "window_end": at(16), "min_crews": 2,
        "sub_windows": [{"start": at(8), "end": at(12), "min_crews": 1,
                         "label": "morning"}],
    }]
    with open(os.path.join(data_dir, "obligations.json"), "w",
              encoding="utf-8") as fh:
        json.dump(obligations, fh, ensure_ascii=False, indent=2)

    def frag(n_from, n_to, row="S1"):
        return {"reg_id": "R1", "n_from": 1000 + n_from, "n_to": 1000 + n_to,
                "calib_row_id": row}

    trips = [
        # 甲班上午：版本0 = [10,12]（拆成两片），版本1 = [12,14]
        {"trip_id": "T1", "ingest_time": at(9, 30), "action": "trip",
         "crew": "甲", "survey_time": at(9), "lane": "L1",
         "routes": [
             {"fragments": [frag(10, 11), frag(12, 12)]},
             {"fragments": [frag(12, 14)]},
         ]},
        # 乙班 12:00 起确定巡过 [11,13]
        {"trip_id": "T2", "ingest_time": at(12, 30), "action": "trip",
         "crew": "乙", "survey_time": at(12), "lane": "L1",
         "routes": [{"fragments": [frag(11, 13)]}]},
        # 甲班下午确定巡过 [10,11]
        {"trip_id": "T3", "ingest_time": at(14, 30), "action": "trip",
         "crew": "甲", "survey_time": at(14), "lane": "L1",
         "routes": [{"fragments": [frag(10, 11)]}]},
        # 甲班 15:00 更正上午路线：只承认 [12,14]
        {"trip_id": "T1", "ingest_time": at(15), "action": "trip",
         "crew": "甲", "survey_time": at(9), "lane": "L1",
         "routes": [{"fragments": [frag(12, 14)]}]},
    ]
    write_jsonl(os.path.join(data_dir, "trips.jsonl"), trips)
    print("结算示例交接包已写入", os.path.abspath(pkg_dir))
    print("义务与巡检簿已写入", os.path.abspath(data_dir))


if __name__ == "__main__":
    pkg = sys.argv[1] if len(sys.argv) > 1 else "examples/settlement_pkg"
    data = sys.argv[2] if len(sys.argv) > 2 else "examples/settlement"
    main(pkg, data)
