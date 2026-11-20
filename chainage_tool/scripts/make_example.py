#!/usr/bin/env python3
"""生成文档化示例交接包（与验收测试同一根断链轴，便于人工核对）。

运行::

    python3 -I scripts/make_example.py examples/demo_pkg
"""

from __future__ import annotations

import json
import os
import sys


def write(path, name, rows):
    with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    manifest = {
        "version": "demo-2026-11-20",
        "axes": [
            {"axis_id": "A1", "length": 100},
            {"axis_id": "A2", "length": 50},
        ],
        "files": {
            "manifest": "manifest.json",
            "calibration": "calibration.jsonl",
            "registration": "registration.jsonl",
        },
        "notes": "示例：A1 上旧线 S/T 在物理 4 断链复号；新线 U/V 反向接号",
    }
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    calibration = [
        # 源体系 OLD：S 正向 100..104，T 反向 104..100，物理 4 处复号 104
        {"row_id": "S1", "system": "OLD", "axis_id": "A1", "segment": "S",
         "lo": 0, "hi": 4, "label_lo": 100, "direction": 1},
        {"row_id": "T1", "system": "OLD", "axis_id": "A1", "segment": "T",
         "lo": 4, "hi": 8, "label_lo": 104, "direction": -1},
        # 目标体系 NEW：U 正向 700..701，V 反向 900..899
        {"row_id": "U1", "system": "NEW", "axis_id": "A1", "segment": "U",
         "lo": 3, "hi": 4, "label_lo": 700, "direction": 1},
        {"row_id": "V1", "system": "NEW", "axis_id": "A1", "segment": "V",
         "lo": 4, "hi": 5, "label_lo": 900, "direction": -1},
        # 跨轴复用段名 S：A2 上的同名段是另一条独立标定
        {"row_id": "S2", "system": "OLD", "axis_id": "A2", "segment": "S",
         "lo": 0, "hi": 6, "label_lo": 100, "direction": 1},
        {"row_id": "W1", "system": "NEW", "axis_id": "A2", "segment": "W",
         "lo": 0, "hi": 6, "label_lo": 300, "direction": -1},
    ]
    write(out_dir, "calibration.jsonl", calibration)

    registration = [
        # 不写段名：103 两位置、104 唯一位置双依据、105 无源位置
        {"reg_id": "R103", "source_system": "OLD", "target_system": "NEW",
         "from": 103, "to": 105},
        # 单点登记
        {"reg_id": "R104", "source_system": "OLD", "target_system": "NEW",
         "n": 104},
        # 限定轴 A2：段名 S 跨轴复用时消歧
        {"reg_id": "RA2", "source_system": "OLD", "target_system": "NEW",
         "from": 101, "to": 103, "axis_id": "A2"},
    ]
    write(out_dir, "registration.jsonl", registration)
    print("示例交接包已写入", os.path.abspath(out_dir))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "examples/demo_pkg")
