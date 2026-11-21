#!/usr/bin/env python3
"""生成巡测车采集包的文档化小样例（与题目"现场核一卷"同一组数字）。

输出（examples/acquisition/）：
    manifest.json        采集交接包清单
    plan.jsonl           计划表：轮次 R-7 的三扇窗 [-10,0) [0,8) [9,10)
    pulses.jsonl         参考脉冲表：P-14 -> -10，P-4 -> 10
    devices.json         设备配置：survey-cam，模数 16，G=6
    volumes/vol-a.jsonl  同一启动依次：回执(14)、照片(0,d=4)、读数(1)、回执(4)

ready 换算库直接用仓库既有示例 examples/demo_pkg（登记 R103/R104/RA2）。

运行::

    python3 -I scripts/make_acquisition_example.py examples/acquisition
"""

from __future__ import annotations

import json
import os
import sys


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main(out_dir: str) -> None:
    os.makedirs(os.path.join(out_dir, "volumes"), exist_ok=True)

    write_json(os.path.join(out_dir, "devices.json"), {
        "devices": [
            {"device": "survey-cam", "modulus": 16, "max_advance": 6,
             "channels": [["still", "photo"], ["probe", "scalar"]]},
        ],
    })

    write_jsonl(os.path.join(out_dir, "pulses.jsonl"), [
        {"pulse": "P-14", "utc": -10},
        {"pulse": "P-4", "utc": 10},
    ])

    expected = [["survey-cam", "still"], ["survey-cam", "probe"]]
    write_jsonl(os.path.join(out_dir, "plan.jsonl"), [
        {"round": "R-7", "window": "W-a", "start_utc": -10, "end_utc": 0,
         "registration": "R103", "expected_channels": expected},
        {"round": "R-7", "window": "W-b", "start_utc": 0, "end_utc": 8,
         "registration": "R104", "expected_channels": expected},
        {"round": "R-7", "window": "W-c", "start_utc": 9, "end_utc": 10,
         "registration": "RA2", "expected_channels": expected},
    ])

    write_jsonl(os.path.join(out_dir, "volumes", "vol-a.jsonl"), [
        {"device": "survey-cam", "boot": "B-1", "seq": 0, "kind": "receipt",
         "count": 14, "pulse": "P-14"},
        {"device": "survey-cam", "boot": "B-1", "seq": 1, "kind": "photo",
         "count": 0, "round": "R-7", "channel": "still",
         "resource": "frame/0001.jpg", "exposure": 4},
        {"device": "survey-cam", "boot": "B-1", "seq": 2, "kind": "reading",
         "count": 1, "round": "R-7", "channel": "probe", "value": 42},
        {"device": "survey-cam", "boot": "B-1", "seq": 3, "kind": "receipt",
         "count": 4, "pulse": "P-4"},
    ])

    write_json(os.path.join(out_dir, "manifest.json"), {
        "version": "acq-demo-2026-11-21",
        "files": {
            "plan": "plan.jsonl",
            "pulses": "pulses.jsonl",
            "devices": "devices.json",
            "volumes": ["volumes/vol-a.jsonl"],
        },
        "notes": "题目现场核卷：模数 16、G=6；读数取 UTC 0 归 W-b；"
                 "照片曝光 [-10/3,10] 三窗相交留 [8,9) 空档，均不可归档",
    })
    print(f"已生成采集交接包示例: {out_dir}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "examples/acquisition"
    main(out)
