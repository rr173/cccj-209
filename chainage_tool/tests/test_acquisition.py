"""巡测车采集包装配验收测试。

覆盖：
1. 题目现场核卷（M=16, G=6）：读数 UTC 0 归第二窗；照片 [-10/3,10]
   三窗相交且留 [8,9) 空档，三窗均不能计作可用照片；
2. 卷打散 + 重复一行：归属与计数不变、证据多一份；
3. 拿走中间序号但保留两端脉冲：不跨断点补钟；
4. 另一启动复用同样计数而换掉脉冲时刻：不串档；
5. 闭快门恰好触边、单脉冲点、跨斜率曝光、大 UTC 亚纳秒、
   展开坐标超出 int64 不回卷；
6. 校验拒绝：同键冲突、未知引用、坏字段、重复窗口键、非法窗口、
   不可能的时钟记录；失败不留库、不覆盖上一份交付；
7. 空窗口 = 缺读数而非没处理；非期望通道不代替缺失通道；
8. 分卷处理顺序无关性；建库后查询不再读取原始分卷；
9. 与独立精确有理数参考程序（acquisition_oracle）在随机输入上全量对照。
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage.acquisition import (  # noqa: E402
    CAT_END_UNCONSTRAINED,
    CAT_NO_RECEIPTS,
    CAT_OUTSIDE_WINDOWS,
    CAT_PARTIAL_OVERLAP,
    CAT_START_UNCONSTRAINED,
    build_acquisition,
    parse_cli_id,
)
from chainage.acquisition_query import (  # noqa: E402
    acq_summary,
    acq_trace,
    acq_unattributed,
    acq_windows,
    open_acq,
)
from chainage.build import build as build_chainage  # noqa: E402
from chainage.errors import (  # noqa: E402
    AcquisitionValidationError,
    DatabaseError,
)
import acquisition_oracle as oracle  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_jsonl(path, rows):
    _write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                         for r in rows))


def write_json(path, obj):
    _write(path, json.dumps(obj, ensure_ascii=False))


def make_chainage_pkg(pkg_dir, reg_ids, version="test-lib-1"):
    """最小合法 ready 换算库交接包（只要求登记 ID 建库后存在）。"""
    write_json(os.path.join(pkg_dir, "manifest.json"), {
        "version": version,
        "axes": [{"axis_id": "A1", "length": 1000}],
        "files": {"manifest": "manifest.json",
                  "calibration": "calibration.jsonl",
                  "registration": "registration.jsonl"},
    })
    write_jsonl(os.path.join(pkg_dir, "calibration.jsonl"), [
        {"row_id": "S1", "system": "OLD", "axis_id": "A1", "segment": "S",
         "lo": 0, "hi": 1000, "label_lo": 0, "direction": 1},
        {"row_id": "U1", "system": "NEW", "axis_id": "A1", "segment": "U",
         "lo": 0, "hi": 1000, "label_lo": 0, "direction": 1},
    ])
    write_jsonl(os.path.join(pkg_dir, "registration.jsonl"), [
        {"reg_id": rid, "source_system": "OLD", "target_system": "NEW",
         "from": 0, "to": 10} for rid in reg_ids
    ])


def build_lib(tmp, reg_ids=("REG-1",), version="test-lib-1"):
    pkg = os.path.join(tmp, "chainage_pkg")
    make_chainage_pkg(pkg, reg_ids, version)
    db = os.path.join(tmp, "lib.db")
    build_chainage(pkg, db)
    return db


class AcqPack:
    """采集交接包构建辅助：逐文件写内容，最后 write() 落盘。"""

    def __init__(self, root, version="acq-test-1"):
        self.root = root
        self.version = version
        self.devices = {"devices": []}
        self.pulses = []
        self.plan = []
        self.volumes = []  # (文件名, [行...])

    def add_device(self, device, M, G, channels):
        self.devices["devices"].append({
            "device": device, "modulus": M, "max_advance": G,
            "channels": channels})

    def add_pulse(self, pulse, utc):
        self.pulses.append({"pulse": pulse, "utc": utc})

    def add_window(self, rnd, win, start, end, reg, expected):
        self.plan.append({"round": rnd, "window": win, "start_utc": start,
                          "end_utc": end, "registration": reg,
                          "expected_channels": expected})

    def add_volume(self, name, lines):
        self.volumes.append((name, lines))

    def write(self):
        write_json(os.path.join(self.root, "devices.json"), self.devices)
        write_jsonl(os.path.join(self.root, "pulses.jsonl"), self.pulses)
        write_jsonl(os.path.join(self.root, "plan.jsonl"), self.plan)
        names = []
        for name, lines in self.volumes:
            write_jsonl(os.path.join(self.root, name), lines)
            names.append(name)
        write_json(os.path.join(self.root, "manifest.json"), {
            "version": self.version,
            "files": {"plan": "plan.jsonl", "pulses": "pulses.jsonl",
                      "devices": "devices.json", "volumes": names},
        })
        return self.root


def worked_pack(root, boot="B-1", pulse_lo=("P-14", -10), pulse_hi=("P-4", 10),
                shuffle_seed=None, duplicate_line=False):
    """题目现场核卷：M=16，G=6；回执(14,-10)、照片(0,d=4)、读数(1)、
    回执(4,10)；窗口 [-10,0) [0,8) [9,10)。"""
    pack = AcqPack(root)
    pack.add_device("survey-cam", 16, 6,
                    [["still", "photo"], ["probe", "scalar"]])
    pack.add_pulse(*pulse_lo)
    pack.add_pulse(*pulse_hi)
    for win, s, e, reg in (("W-a", -10, 0, "REG-1"),
                           ("W-b", 0, 8, "REG-2"),
                           ("W-c", 9, 10, "REG-3")):
        pack.add_window("R-7", win, s, e, reg,
                        [["survey-cam", "still"], ["survey-cam", "probe"]])
    lines = [
        {"device": "survey-cam", "boot": boot, "seq": 0, "kind": "receipt",
         "count": 14, "pulse": pulse_lo[0]},
        {"device": "survey-cam", "boot": boot, "seq": 1, "kind": "photo",
         "count": 0, "round": "R-7", "channel": "still",
         "resource": "frame/0001.jpg", "exposure": 4},
        {"device": "survey-cam", "boot": boot, "seq": 2, "kind": "reading",
         "count": 1, "round": "R-7", "channel": "probe", "value": 42},
        {"device": "survey-cam", "boot": boot, "seq": 3, "kind": "receipt",
         "count": 4, "pulse": pulse_hi[0]},
    ]
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(lines)
    if duplicate_line:
        lines.append(dict(lines[-1]))
    pack.add_volume("volumes/vol-a.jsonl", lines)
    return pack


def build_acq(tmp, pkg_dir, lib_db, name="acq.db"):
    return build_acquisition(pkg_dir, lib_db, os.path.join(tmp, name))


def table_dump(db_path, table, order_by):
    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute(
            f"PRAGMA table_info({table})")]
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


class WorkedExample(unittest.TestCase):
    """题目现场核卷的精确数字。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acq-t")
        self.lib = build_lib(self.tmp, ("REG-1", "REG-2", "REG-3"))
        pack = worked_pack(os.path.join(self.tmp, "pkg"))
        self.pkg_dir = pack.write()
        self.db = os.path.join(self.tmp, "acq.db")
        self.stats = build_acquisition(self.pkg_dir, self.lib, self.db)

    def test_reading_utc_and_window(self):
        with open_acq(self.db) as conn:
            obj = acq_windows(conn, round_c=parse_cli_id("R-7"),
                              window_c=parse_cli_id("W-b"))
        (w,) = obj["windows"]
        self.assertEqual(w["totals"], {"readings": 1, "photos": 0,
                                       "records": 1})
        (rec,) = w["records"]
        self.assertEqual(rec["kind"], "reading")
        self.assertEqual(rec["utc"], "0")          # 读数取 UTC 0
        self.assertEqual(rec["value"], "42")
        self.assertTrue(rec["expected"])
        # 期望通道 still 没有可用证据
        self.assertEqual(w["missing_expected_channels"],
                         [{"device": "survey-cam", "channel": "still"}])

    def test_photo_spans_three_windows_with_gap(self):
        with open_acq(self.db) as conn:
            obj = acq_unattributed(conn, category=CAT_PARTIAL_OVERLAP)
        self.assertEqual(obj["total_unattributed"], 1)
        (rec,) = obj["records"]
        self.assertEqual(rec["kind"], "photo")
        self.assertEqual(rec["detail"]["open"], "-10/3")
        self.assertEqual(rec["detail"]["close"], "10")
        self.assertTrue(rec["detail"]["spans_multiple"])
        self.assertTrue(rec["detail"]["has_uncovered"])
        got = [(o["window"], o["overlap"]["lo"], o["overlap"]["hi"])
               for o in rec["overlaps"]]
        self.assertEqual(got, [("W-a", "-10/3", "0"),
                               ("W-b", "0", "8"),
                               ("W-c", "9", "10")])
        self.assertEqual(rec["gaps"], [{"lo": "8", "hi": "9"}])

    def test_empty_windows_listed_with_missing_channels(self):
        with open_acq(self.db) as conn:
            obj = acq_windows(conn, round_c=parse_cli_id("R-7"))
        by_key = {w["window"]: w for w in obj["windows"]}
        for key in ("W-a", "W-c"):
            w = by_key[key]
            # 窗口已处理（包 ready、窗口行在），只是缺读数
            self.assertEqual(w["records"], [])
            self.assertEqual(w["totals"]["records"], 0)
            self.assertEqual(len(w["missing_expected_channels"]), 2)
        with open_acq(self.db) as conn:
            summary = acq_summary(conn)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["chainage_lib_version"], "test-lib-1")
        self.assertEqual(summary["stats"]["unattributed"], 1)
        self.assertEqual(summary["stats"]["photo_overlap_rows"], 3)
        self.assertEqual(summary["stats"]["photo_gap_rows"], 1)

    def test_trace_shows_clock_evidence(self):
        with open_acq(self.db) as conn:
            photo = acq_trace(conn, 2)
            reading = acq_trace(conn, 3)
        self.assertEqual(photo["record"]["raw_count"], "0")
        self.assertEqual(photo["block"]["expanded"], "2")
        self.assertEqual(photo["clock"]["exposure_utc"],
                         {"open": "-10/3", "close": "10"})
        roles = {tuple(r["roles"]): r for r in photo["clock"]["receipts"]}
        self.assertIn(("open_lo",), roles)
        self.assertEqual(roles[("open_lo",)]["pulse_utc"], "-10")
        both = roles[("open_hi", "close_lo", "close_hi")]
        self.assertEqual(both["pulse_utc"], "10")
        self.assertEqual(reading["clock"]["utc"], "0")
        self.assertEqual(reading["attribution"]["window"], "W-b")
        self.assertEqual(reading["evidence"],
                         [{"volume": os.path.join(self.tmp, "pkg",
                                                  "volumes", "vol-a.jsonl"),
                           "line": 3}])

    def test_queries_do_not_reread_volumes(self):
        # 建库后删除原始分卷，取数与回看不受影响
        os.remove(os.path.join(self.pkg_dir, "volumes", "vol-a.jsonl"))
        with open_acq(self.db) as conn:
            obj = acq_windows(conn, round_c=parse_cli_id("R-7"))
            self.assertEqual(obj["total_windows"], 3)
            trace = acq_trace(conn, 2)
        self.assertEqual(trace["clock"]["exposure_utc"]["open"], "-10/3")


class ShuffleDuplicateBreakBoot(unittest.TestCase):
    """打散/重复/断块/另一启动。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acq-t")
        self.lib = build_lib(self.tmp, ("REG-1", "REG-2", "REG-3"))

    def test_shuffle_and_duplicate_line(self):
        pack = worked_pack(os.path.join(self.tmp, "pkg"),
                           shuffle_seed=7, duplicate_line=True)
        db = build_acq(self.tmp, pack.write(), self.lib)
        self.assertEqual(db.records, 4)            # 计数不变
        self.assertEqual(db.volume_lines, 5)
        self.assertEqual(db.duplicate_lines, 1)    # 证据多一份
        self.assertEqual(db.evidence_rows, 5)
        with open_acq(os.path.join(self.tmp, "acq.db")) as conn:
            obj = acq_windows(conn, round_c=parse_cli_id("R-7"),
                              window_c=parse_cli_id("W-b"))
            (w,) = obj["windows"]
            self.assertEqual(w["totals"]["readings"], 1)
            # 被重复的那一行（打散后最后一行）有两条卷行证据
            dup_line = None
            for rid in range(1, 5):
                tr = acq_trace(conn, rid)
                if len(tr["evidence"]) == 2:
                    dup_line = rid
            self.assertIsNotNone(dup_line)

    def test_block_break_no_borrowed_clock(self):
        # 拿走中间序号（照片 seq 1），保留两端脉冲：读数落到断点后新块，
        # 块内只有后面的回执 -> 开头不受约束，而不是跨断点借钟
        pack = AcqPack(os.path.join(self.tmp, "pkg"))
        pack.add_device("survey-cam", 16, 6,
                        [["still", "photo"], ["probe", "scalar"]])
        pack.add_pulse("P-14", -10)
        pack.add_pulse("P-4", 10)
        pack.add_window("R-7", "W-b", 0, 8, "REG-2",
                        [["survey-cam", "probe"]])
        pack.add_volume("volumes/vol-a.jsonl", [
            {"device": "survey-cam", "boot": "B-1", "seq": 0,
             "kind": "receipt", "count": 14, "pulse": "P-14"},
            {"device": "survey-cam", "boot": "B-1", "seq": 2,
             "kind": "reading", "count": 1, "round": "R-7",
             "channel": "probe", "value": 42},
            {"device": "survey-cam", "boot": "B-1", "seq": 3,
             "kind": "receipt", "count": 4, "pulse": "P-4"},
        ])
        db = build_acq(self.tmp, pack.write(), self.lib)
        self.assertEqual(db.blocks, 2)
        with open_acq(os.path.join(self.tmp, "acq.db")) as conn:
            obj = acq_unattributed(conn, category=CAT_START_UNCONSTRAINED)
            self.assertEqual(obj["total_unattributed"], 1)
            # 读数没有被赋 UTC 0（没有跨断点补钟）
            self.assertEqual(acq_windows(conn).get("windows")[0]
                             ["totals"]["records"], 0)

    def test_second_boot_reuses_counts_not_clocks(self):
        # 另一启动复用同样计数而换掉脉冲时刻：各自按各自启动的钟
        pack = worked_pack(os.path.join(self.tmp, "pkg"))
        pack.add_pulse("P-14b", -9)
        pack.add_pulse("P-4b", 11)
        pack.add_volume("volumes/vol-b.jsonl", [
            {"device": "survey-cam", "boot": "B-2", "seq": 0,
             "kind": "receipt", "count": 14, "pulse": "P-14b"},
            {"device": "survey-cam", "boot": "B-2", "seq": 1,
             "kind": "reading", "count": 1, "round": "R-7",
             "channel": "probe", "value": 7},
            {"device": "survey-cam", "boot": "B-2", "seq": 2,
             "kind": "receipt", "count": 4, "pulse": "P-4b"},
        ])
        db = build_acq(self.tmp, pack.write(), self.lib)
        self.assertEqual(db.records, 7)
        with open_acq(os.path.join(self.tmp, "acq.db")) as conn:
            obj = acq_windows(conn, round_c=parse_cli_id("R-7"),
                              window_c=parse_cli_id("W-b"))
            (w,) = obj["windows"]
            utcs = sorted(r["utc"] for r in w["records"])
            # B-1 的读数 UTC 0；B-2 的读数按自己的钟：-9 + 20*(3/6) = 1
            self.assertEqual(utcs, ["0", "1"])


class ClockEdgeCases(unittest.TestCase):
    """触边、单脉冲点、跨斜率、大 UTC 亚纳秒、超大展开坐标。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acq-t")
        self.lib = build_lib(self.tmp, ("REG-1", "REG-2"))

    def _single_device_pack(self, root, records, windows, M=100, G=60,
                            pulses=(), channels=(("cam", "photo"),
                                                ("probe", "scalar"))):
        pack = AcqPack(root)
        pack.add_device("dev", M, G, list(channels))
        for p, u in pulses:
            pack.add_pulse(p, u)
        for i, (s, e) in enumerate(windows):
            pack.add_window("R", f"W{i}", s, e, "REG-1",
                            [["dev", "cam"], ["dev", "probe"]])
        pack.add_volume("volumes/v.jsonl", records)
        return pack

    def test_close_shutter_exactly_at_window_end(self):
        # 斜率 10 ns/计数；照片开 20、闭 100，窗口 [0,100)：闭快门正好
        # 等于窗口右端，算在内
        pack = self._single_device_pack(
            os.path.join(self.tmp, "pkg"),
            [{"device": "dev", "boot": "B", "seq": 0, "kind": "receipt",
              "count": 0, "pulse": "P0"},
             {"device": "dev", "boot": "B", "seq": 1, "kind": "photo",
              "count": 2, "round": "R", "channel": "cam",
              "resource": "x.jpg", "exposure": 8},
             {"device": "dev", "boot": "B", "seq": 2, "kind": "receipt",
              "count": 10, "pulse": "P1"}],
            [(0, 100)], pulses=[("P0", 0), ("P1", 100)])
        db = build_acq(self.tmp, pack.write(), self.lib)
        self.assertEqual(db.attributed_photos, 1)
        with open_acq(os.path.join(self.tmp, "acq.db")) as conn:
            tr = acq_trace(conn, 2)
        self.assertEqual(tr["clock"]["exposure_utc"],
                         {"open": "20", "close": "100"})
        self.assertEqual(tr["attribution"]["window"], "W0")

    def test_single_receipt_point(self):
        pack = self._single_device_pack(
            os.path.join(self.tmp, "pkg"),
            [{"device": "dev", "boot": "B", "seq": 0, "kind": "reading",
              "count": 4, "round": "R", "channel": "probe", "value": 1},
             {"device": "dev", "boot": "B", "seq": 1, "kind": "receipt",
              "count": 5, "pulse": "P0"},
             {"device": "dev", "boot": "B", "seq": 2, "kind": "reading",
              "count": 5, "round": "R", "channel": "probe", "value": 2},
             {"device": "dev", "boot": "B", "seq": 3, "kind": "reading",
              "count": 6, "round": "R", "channel": "probe", "value": 3},
             {"device": "dev", "boot": "B", "seq": 4, "kind": "photo",
              "count": 6, "round": "R", "channel": "cam",
              "resource": "y.jpg", "exposure": 2}],
            [(0, 2000)], pulses=[("P0", 999)])
        db = build_acq(self.tmp, pack.write(), self.lib)
        with open_acq(os.path.join(self.tmp, "acq.db")) as conn:
            # 记录 id 按 (设备,启动号,序号) 排序分配：#2 是回执，
            # 落在全块唯一回执坐标上的读数（#3）采用该回执 UTC
            receipt = acq_trace(conn, 2)
            self.assertEqual(receipt["clock"]["role"], "receipt")
            self.assertEqual(receipt["clock"]["utc"], "999")
            on = acq_trace(conn, 3)
            self.assertEqual(on["clock"]["utc"], "999")
            self.assertEqual(on["attribution"]["window"], "W0")
            before = acq_trace(conn, 1)
            after = acq_trace(conn, 4)
            photo = acq_trace(conn, 5)
        self.assertEqual(before["unattributed"]["category"],
                         CAT_START_UNCONSTRAINED)
        self.assertEqual(after["unattributed"]["category"],
                         CAT_END_UNCONSTRAINED)
        # 单回执块里照片闭快门越界 -> 结尾不受约束
        self.assertEqual(photo["unattributed"]["category"],
                         CAT_END_UNCONSTRAINED)
        self.assertEqual(db.attributed_readings, 1)

    def test_exposure_crossing_slopes(self):
        # 回执 (0,0) (10,100) (20,150)：两段斜率 10 与 5
        pack = self._single_device_pack(
            os.path.join(self.tmp, "pkg"),
            [{"device": "dev", "boot": "B", "seq": 0, "kind": "receipt",
              "count": 0, "pulse": "P0"},
             {"device": "dev", "boot": "B", "seq": 1, "kind": "photo",
              "count": 5, "round": "R", "channel": "cam",
              "resource": "z.jpg", "exposure": 10},
             {"device": "dev", "boot": "B", "seq": 2, "kind": "receipt",
              "count": 10, "pulse": "P1"},
             {"device": "dev", "boot": "B", "seq": 3, "kind": "receipt",
              "count": 20, "pulse": "P2"}],
            [(0, 200)], pulses=[("P0", 0), ("P1", 100), ("P2", 150)])
        db = build_acq(self.tmp, pack.write(), self.lib)
        self.assertEqual(db.attributed_photos, 1)
        with open_acq(os.path.join(self.tmp, "acq.db")) as conn:
            tr = acq_trace(conn, 2)
        # 开快门 50（第一段），闭快门跨段：100 + 5*5 = 125
        self.assertEqual(tr["clock"]["exposure_utc"],
                         {"open": "50", "close": "125"})

    def test_large_utc_subnanosecond(self):
        big = 2 ** 53
        pack = self._single_device_pack(
            os.path.join(self.tmp, "pkg"),
            [{"device": "dev", "boot": "B", "seq": 0, "kind": "receipt",
              "count": 0, "pulse": "P0"},
             {"device": "dev", "boot": "B", "seq": 1, "kind": "reading",
              "count": 1, "round": "R", "channel": "probe", "value": 1},
             {"device": "dev", "boot": "B", "seq": 2, "kind": "receipt",
              "count": 2, "pulse": "P1"}],
            [(big - 2, big - 1), (big - 1, big)],
            pulses=[("P0", big - 1), ("P1", big)])
        build_acq(self.tmp, pack.write(), self.lib)
        with open_acq(os.path.join(self.tmp, "acq.db")) as conn:
            tr = acq_trace(conn, 2)
            # t = 2^53 - 1/2：浮点会舍入到 2^53 而落出所有窗口
            self.assertEqual(tr["clock"]["utc"], "18014398509481983/2")
            self.assertEqual(tr["attribution"]["window"], "W1")

    def test_expanded_coordinate_beyond_int64(self):
        M = 2 ** 62
        G = 2 ** 61
        step = 2 ** 61
        counts = [0, step, 0, step, 0, step]
        pack = AcqPack(os.path.join(self.tmp, "pkg"))
        pack.add_device("dev", M, G, [["cam", "photo"], ["probe", "scalar"]])
        pack.add_pulse("P0", 0)
        pack.add_pulse("P1", 10)
        pack.add_window("R", "W0", 0, 10, "REG-1", [["dev", "probe"]])
        lines = [
            {"device": "dev", "boot": "B", "seq": 0, "kind": "receipt",
             "count": counts[0], "pulse": "P0"},
            {"device": "dev", "boot": "B", "seq": 1, "kind": "reading",
             "count": counts[1], "round": "R", "channel": "probe",
             "value": 5},
            {"device": "dev", "boot": "B", "seq": 5, "kind": "receipt",
             "count": counts[5], "pulse": "P1"},
        ]
        # 补齐 seq 2..4（无回执的中间记录），使块连续
        for i in (2, 3, 4):
            lines.insert(i, {"device": "dev", "boot": "B", "seq": i,
                             "kind": "reading", "count": counts[i],
                             "round": "R", "channel": "probe", "value": i})
        pack.add_volume("volumes/v.jsonl", lines)
        build_acq(self.tmp, pack.write(), self.lib)
        with open_acq(os.path.join(self.tmp, "acq.db")) as conn:
            tr = acq_trace(conn, 2)
            # 展开坐标 2^61 与块最大 5*2^61（> int64 上限）按十进制文本存
            self.assertEqual(tr["block"]["expanded"],
                             str(2 ** 61))
            self.assertEqual(tr["block"]["block_max_expanded"],
                             str(5 * 2 ** 61))
            # u = 10 * 2^61 / (5*2^61) = 2，精确落在窗内
            self.assertEqual(tr["clock"]["utc"], "2")
            self.assertEqual(tr["attribution"]["window"], "W0")


class ValidationRejects(unittest.TestCase):
    """各类输入错误定位并拒绝交付；失败不留库。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acq-t")
        self.lib = build_lib(self.tmp, ("REG-1", "REG-2", "REG-3"))

    def _base_pack(self, root):
        pack = AcqPack(root)
        pack.add_device("dev", 16, 6, [["cam", "photo"], ["probe", "scalar"]])
        pack.add_pulse("P0", 0)
        pack.add_pulse("P1", 10)
        pack.add_window("R", "W0", 0, 10, "REG-1", [["dev", "probe"]])
        pack.add_volume("volumes/v.jsonl", [
            {"device": "dev", "boot": "B", "seq": 0, "kind": "receipt",
             "count": 0, "pulse": "P0"},
            {"device": "dev", "boot": "B", "seq": 1, "kind": "reading",
             "count": 1, "round": "R", "channel": "probe", "value": 1},
            {"device": "dev", "boot": "B", "seq": 2, "kind": "receipt",
             "count": 2, "pulse": "P1"},
        ])
        return pack

    def _expect_reject(self, pack, code, name="out.db"):
        pkg_dir = pack.write()
        out = os.path.join(self.tmp, name)
        with self.assertRaises(AcquisitionValidationError) as cm:
            build_acquisition(pkg_dir, self.lib, out)
        codes = {e.code for e in cm.exception.errors}
        self.assertIn(code, codes,
                      f"期望错误码 {code}，实际 {sorted(codes)}")
        self.assertFalse(os.path.exists(out), "失败不得留下看似完成的库")
        return cm.exception

    def test_conflict_same_key(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.add_volume("volumes/v2.jsonl", [
            {"device": "dev", "boot": "B", "seq": 1, "kind": "reading",
             "count": 1, "round": "R", "channel": "probe", "value": 999},
        ])
        self._expect_reject(pack, "ACQ_CONFLICT")

    def test_unknown_pulse(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][0]["pulse"] = "PX"
        self._expect_reject(pack, "ACQ_UNKNOWN_PULSE")

    def test_unknown_channel(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1]["channel"] = "nope"
        self._expect_reject(pack, "ACQ_UNKNOWN_CHANNEL")

    def test_channel_type_mismatch(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1]["channel"] = "cam"  # cam 是 photo 通道
        self._expect_reject(pack, "ACQ_CHANNEL_TYPE")

    def test_float_not_accepted(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1]["count"] = 1.0
        self._expect_reject(pack, "ACQ_BAD_INTEGER")

    def test_duplicate_window_key(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.add_window("R", "W0", 20, 30, "REG-1", [])
        self._expect_reject(pack, "ACQ_DUP_WINDOW")

    def test_overlapping_windows(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.add_window("R", "W1", 5, 15, "REG-1", [])
        self._expect_reject(pack, "ACQ_OVERLAP_WINDOW")

    def test_bad_window_interval(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.plan[0]["end_utc"] = 0
        self._expect_reject(pack, "ACQ_BAD_WINDOW")

    def test_duplicate_pulse_id(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.add_pulse("P0", 5)
        self._expect_reject(pack, "ACQ_DUP_PULSE")

    def test_unknown_round(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1]["round"] = "RR"
        self._expect_reject(pack, "ACQ_UNKNOWN_ROUND")

    def test_unknown_device(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1]["device"] = "ghost"
        self._expect_reject(pack, "ACQ_UNKNOWN_DEVICE")

    def test_bad_exposure(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1] = {"device": "dev", "boot": "B", "seq": 1,
                                 "kind": "photo", "count": 1, "round": "R",
                                 "channel": "cam", "resource": "a.jpg",
                                 "exposure": 16}  # d 必须 < M=16
        self._expect_reject(pack, "ACQ_INT_RANGE")

    def test_count_out_of_modulus(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1]["count"] = 16
        self._expect_reject(pack, "ACQ_INT_RANGE")

    def test_receipt_utc_not_increasing(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.pulses[1]["utc"] = -1  # P1 的 UTC 早于 P0
        self._expect_reject(pack, "ACQ_RECEIPT_ORDER")

    def test_repeated_pulse_same_boot(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][2]["pulse"] = "P0"  # 同一启动重复回执 P0
        self._expect_reject(pack, "ACQ_DUP_RECEIPT")

    def test_count_jump_over_g(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1]["count"] = 9  # 0 -> 9 模差 9 > G=6
        self._expect_reject(pack, "ACQ_COUNT_JUMP")

    def test_receipt_coordinate_not_increasing(self):
        # 两个回执之间模差为 0：展开坐标不增
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1] = {"device": "dev", "boot": "B", "seq": 1,
                                 "kind": "reading", "count": 0, "round": "R",
                                 "channel": "probe", "value": 1}
        pack.volumes[0][1][2]["count"] = 0  # 回执计数不变
        self._expect_reject(pack, "ACQ_RECEIPT_COORD")

    def test_unknown_extra_field(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.volumes[0][1][1]["note"] = "hello"
        self._expect_reject(pack, "ACQ_UNKNOWN_FIELD")

    def test_unknown_registration(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pack.plan[0]["registration"] = "NOPE"
        self._expect_reject(pack, "ACQ_UNKNOWN_REGISTRATION")

    def test_bad_g_m_relation(self):
        pack = AcqPack(os.path.join(self.tmp, "pkg"))
        pack.add_device("dev", 16, 16, [["probe", "scalar"]])  # G 必须 < M
        pack.add_pulse("P0", 0)
        pack.add_window("R", "W0", 0, 10, "REG-1", [])
        pack.add_volume("volumes/v.jsonl", [])
        self._expect_reject(pack, "ACQ_BAD_FIELD")

    def test_output_not_overwritten(self):
        pack = self._base_pack(os.path.join(self.tmp, "pkg"))
        pkg_dir = pack.write()
        out = os.path.join(self.tmp, "out.db")
        build_acquisition(pkg_dir, self.lib, out)
        with open(out, "rb") as fh:
            before = fh.read()
        # 再次构建到同一路径：拒绝且不动已有库
        with self.assertRaises(DatabaseError):
            build_acquisition(pkg_dir, self.lib, out)
        with open(out, "rb") as fh:
            self.assertEqual(before, fh.read())
        # 输入冲突 + 新路径：命令失败，不留下看似完成的库
        pack2 = self._base_pack(os.path.join(self.tmp, "pkg2"))
        pack2.volumes[0][1][1]["count"] = 9  # 结构错误
        out2 = os.path.join(self.tmp, "out2.db")
        with self.assertRaises(AcquisitionValidationError):
            build_acquisition(pack2.write(), self.lib, out2)
        self.assertFalse(os.path.exists(out2))
        self.assertFalse(os.path.exists(out2 + ".tmp"))
        # 输入冲突 + 目标已存在：同样拒绝，不覆盖上一份交付
        with self.assertRaises(DatabaseError):
            build_acquisition(pack2.write(), self.lib, out)
        with open(out, "rb") as fh:
            self.assertEqual(before, fh.read())


class OrderIndependence(unittest.TestCase):
    """相同业务输入调整分卷处理顺序后，归属/诊断/窗口数量不变。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acq-t")
        self.lib = build_lib(self.tmp, ("REG-1", "REG-2", "REG-3"))

    def _three_volume_pack(self, root, order, shuffle_seed=None,
                           duplicate=False):
        pack = worked_pack(root)
        lines = pack.volumes[0][1]
        if shuffle_seed is not None:
            random.Random(shuffle_seed).shuffle(lines)
        if duplicate:
            lines = lines + [dict(lines[0])]
        # 按行分散到三个卷
        vols = {"volumes/v1.jsonl": [], "volumes/v2.jsonl": [],
                "volumes/v3.jsonl": []}
        for i, ln in enumerate(lines):
            vols[["volumes/v1.jsonl", "volumes/v2.jsonl",
                  "volumes/v3.jsonl"][i % 3]].append(ln)
        pack.volumes = [(name, vols[name]) for name in order]
        return pack

    def _dump_all(self, db):
        tables = {
            "sample_record": "id",
            "placement": "record_id",
            "expand_block": "id",
            "alignment": "record_id",
            "attribution": "record_id",
            "unattributed": "record_id",
            "photo_overlap": "record_id, window_id",
            "photo_gap": "record_id, seq_no",
            "window_channel_count": "window_id, device, channel, kind",
            "plan_window": "id",
            "evidence": "record_id, volume, line",
        }
        return {t: table_dump(db, t, o) for t, o in tables.items()}

    def test_volume_order_does_not_matter(self):
        order_a = ["volumes/v1.jsonl", "volumes/v2.jsonl",
                   "volumes/v3.jsonl"]
        order_b = ["volumes/v3.jsonl", "volumes/v1.jsonl",
                   "volumes/v2.jsonl"]
        pack_a = self._three_volume_pack(os.path.join(self.tmp, "pa"),
                                         order_a, shuffle_seed=3)
        pack_b = self._three_volume_pack(os.path.join(self.tmp, "pb"),
                                         order_b, shuffle_seed=3)
        db_a = build_acq(self.tmp, pack_a.write(), self.lib, "a.db")
        db_b = build_acq(self.tmp, pack_b.write(), self.lib, "b.db")
        dump_a = self._dump_all(os.path.join(self.tmp, "a.db"))
        dump_b = self._dump_all(os.path.join(self.tmp, "b.db"))
        # 卷名只相差包根（pa/pb），比较时剥到包内相对路径
        def rel(rows):
            return [{**r, "volume": "/".join(
                r["volume"].replace(os.sep, "/").split("/")[-2:])}
                for r in rows]
        for t in dump_a:
            rows_a = rel(dump_a[t]) if t == "evidence" else dump_a[t]
            rows_b = rel(dump_b[t]) if t == "evidence" else dump_b[t]
            self.assertEqual(rows_a, rows_b, f"表 {t} 不一致")
        self.assertEqual(db_a.window_rows_examined,
                         db_b.window_rows_examined)

    def test_shuffle_and_duplicate_keep_results(self):
        pack = self._three_volume_pack(os.path.join(self.tmp, "pc"),
                                       ["volumes/v1.jsonl",
                                        "volumes/v2.jsonl",
                                        "volumes/v3.jsonl"],
                                       shuffle_seed=11, duplicate=True)
        db = build_acq(self.tmp, pack.write(), self.lib, "c.db")
        self.assertEqual(db.records, 4)
        self.assertEqual(db.duplicate_lines, 1)
        with open_acq(os.path.join(self.tmp, "c.db")) as conn:
            obj = acq_windows(conn, round_c=parse_cli_id("R-7"),
                              window_c=parse_cli_id("W-b"))
            (w,) = obj["windows"]
            self.assertEqual(w["totals"]["readings"], 1)
            un = acq_unattributed(conn, category=CAT_PARTIAL_OVERLAP)
            self.assertEqual(un["total_unattributed"], 1)


class UnexpectedChannel(unittest.TestCase):
    """非期望通道收到的合法记录照常保存，不代替缺失通道。"""

    def test_unexpected_channel_kept(self):
        tmp = tempfile.mkdtemp(prefix="acq-t")
        lib = build_lib(tmp, ("REG-1",))
        pack = AcqPack(os.path.join(tmp, "pkg"))
        pack.add_device("dev", 16, 6,
                        [["main", "scalar"], ["aux", "scalar"]])
        pack.add_pulse("P0", 0)
        pack.add_pulse("P1", 10)
        pack.add_window("R", "W0", 0, 10, "REG-1", [["dev", "main"]])
        pack.add_volume("volumes/v.jsonl", [
            {"device": "dev", "boot": "B", "seq": 0, "kind": "receipt",
             "count": 0, "pulse": "P0"},
            {"device": "dev", "boot": "B", "seq": 1, "kind": "reading",
             "count": 1, "round": "R", "channel": "aux", "value": 1},
            {"device": "dev", "boot": "B", "seq": 2, "kind": "receipt",
             "count": 2, "pulse": "P1"},
        ])
        build_acq(tmp, pack.write(), lib)
        with open_acq(os.path.join(tmp, "acq.db")) as conn:
            obj = acq_windows(conn)
        (w,) = obj["windows"]
        # 非期望通道 aux 的记录照常保存
        self.assertEqual(w["channel_counts"],
                         [{"device": "dev", "channel": "aux",
                           "kind": "reading", "n": 1, "expected": False}])
        # 但不代替缺失的期望通道 main
        self.assertEqual(w["missing_expected_channels"],
                         [{"device": "dev", "channel": "main"}])


class OracleCrossCheck(unittest.TestCase):
    """与独立精确有理数参考程序在随机输入上全量对照。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acq-t")
        self.lib = build_lib(self.tmp, ("REG-1", "REG-2", "REG-3"))

    def _random_pack(self, rng, root):
        devices = {}
        pulses = {}
        windows_by_round = {}
        records_by_boot = {}
        pack = AcqPack(root)
        n_dev = rng.randint(1, 2)
        rounds = ["R0"] if rng.random() < 0.5 else ["R0", "R1"]
        for rnd in rounds:
            ws = []
            x = rng.randint(-60, -40)
            for i in range(rng.randint(1, 6)):  # 轮次由计划表列出，至少一窗
                length = rng.randint(1, 20)
                ws.append({"start": x, "end": x + length,
                           "key": f"{rnd}-W{i}"})
                pack.add_window(rnd, f"{rnd}-W{i}", x, x + length,
                                rng.choice(["REG-1", "REG-2", "REG-3"]), [])
                x += length + rng.randint(0, 5)
            windows_by_round[rnd] = ws
        for di in range(n_dev):
            dev = f"D{di}"
            M = rng.randint(4, 40)
            G = rng.randint(1, M - 1)
            devices[dev] = (M, G)
            pack.add_device(dev, M, G, [["s", "scalar"], ["p", "photo"]])
            for bi in range(rng.randint(1, 2)):
                boot = f"{dev}-B{bi}"
                recs = []
                seq = rng.randint(0, 3)
                count = rng.randint(0, M - 1)
                last_receipt_utc = None
                prev_was_receipt = False
                for _ in range(rng.randint(4, 30)):
                    if prev_was_receipt is not None and rng.random() < 0.12:
                        seq += rng.randint(2, 4)  # 序号断开 -> 新块
                    is_receipt = rng.random() < 0.3
                    lo = 1 if prev_was_receipt else 0  # 回执后保证展开递增
                    count = (count + rng.randint(lo, G)) % M
                    if is_receipt:
                        pulse = f"P{len(pulses)}"
                        base = -50 if last_receipt_utc is None \
                            else last_receipt_utc
                        utc = base + rng.randint(1, 30)
                        last_receipt_utc = utc
                        pulses[pulse] = utc
                        pack.add_pulse(pulse, utc)
                        rec = {"device": dev, "boot": boot, "seq": seq,
                               "kind": "receipt", "count": count,
                               "pulse": pulse}
                    elif rng.random() < 0.6:
                        rec = {"device": dev, "boot": boot, "seq": seq,
                               "kind": "reading", "count": count,
                               "round": rng.choice(rounds), "channel": "s",
                               "value": rng.randint(-100, 100)}
                    else:
                        rec = {"device": dev, "boot": boot, "seq": seq,
                               "kind": "photo", "count": count,
                               "round": rng.choice(rounds), "channel": "p",
                               "resource": f"r{seq}.jpg",
                               "exposure": rng.randint(1, M - 1)}
                    recs.append(rec)
                    prev_was_receipt = is_receipt
                    seq += 1
                records_by_boot[(dev, boot)] = recs
        all_lines = [r for recs in records_by_boot.values() for r in recs]
        rng.shuffle(all_lines)
        pack.add_volume("volumes/v.jsonl", all_lines)
        return pack, records_by_boot, pulses, devices, windows_by_round

    def test_oracle_crosscheck(self):
        for case in range(150):
            rng = random.Random(1000 + case)
            root = os.path.join(self.tmp, f"case{case}")
            pack, records_by_boot, pulses, devices, windows_by_round = \
                self._random_pack(rng, root)
            db_path = os.path.join(self.tmp, f"case{case}.db")
            build_acquisition(pack.write(), self.lib, db_path)
            expected = oracle.oracle_run(records_by_boot, pulses, devices,
                                         windows_by_round)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                win_by_id = {w["id"]: w for w in conn.execute(
                    "SELECT * FROM plan_window")}
                recs = {r["id"]: r for r in conn.execute(
                    "SELECT * FROM sample_record WHERE kind != 'receipt'")}
                self.assertEqual(len(recs), len(expected),
                                 f"case {case}: 记录数不一致")
                for rid, rec in recs.items():
                    key = self._match_rec(rec, records_by_boot)
                    exp = expected[key]
                    al = conn.execute(
                        "SELECT * FROM alignment WHERE record_id=?",
                        (rid,)).fetchone()
                    self.assertEqual(al["status"], exp["status"],
                                     f"case {case} rec {rid} 状态")
                    if exp["status"] == "ok":
                        if rec["kind"] == "reading":
                            got = Fraction(int(al["utc_num"]),
                                           int(al["utc_den"]))
                            self.assertEqual(got, exp["utc"],
                                             f"case {case} rec {rid} utc")
                        else:
                            go = Fraction(int(al["open_num"]),
                                          int(al["open_den"]))
                            gc = Fraction(int(al["close_num"]),
                                          int(al["close_den"]))
                            self.assertEqual((go, gc),
                                             (exp["open"], exp["close"]),
                                             f"case {case} rec {rid} 曝光")
                        at = conn.execute(
                            "SELECT window_id FROM attribution "
                            "WHERE record_id=?", (rid,)).fetchone()
                        if exp["result"][0] == "attributed":
                            self.assertIsNotNone(at,
                                                 f"case {case} rec {rid}")
                            w = win_by_id[at["window_id"]]
                            self.assertEqual(
                                (w["start_utc"], w["end_utc"]),
                                (exp["result"][1]["start"],
                                 exp["result"][1]["end"]))
                        else:
                            self.assertIsNone(at)
                            un = conn.execute(
                                "SELECT category FROM unattributed "
                                "WHERE record_id=?", (rid,)).fetchone()
                            want = {"outside": CAT_OUTSIDE_WINDOWS,
                                    "partial": CAT_PARTIAL_OVERLAP}[
                                exp["result"][0]]
                            self.assertEqual(un["category"], want)
                            if want == CAT_PARTIAL_OVERLAP:
                                self._check_overlaps(
                                    conn, rid, exp, win_by_id, case)
                    else:
                        un = conn.execute(
                            "SELECT category FROM unattributed "
                            "WHERE record_id=?", (rid,)).fetchone()
                        self.assertEqual(un["category"], exp["status"])
            finally:
                conn.close()

    def _match_rec(self, db_rec, records_by_boot):
        """按业务字段把库记录对回输入记录对象（取 id()）。"""
        device = json.loads(db_rec["device"])
        boot = json.loads(db_rec["boot"])
        for rec in records_by_boot[(device, boot)]:
            if rec["seq"] != db_rec["seq"]:
                continue
            if rec["kind"] != db_rec["kind"]:
                continue
            if rec["count"] != db_rec["raw_count"]:
                continue
            if rec["kind"] == "reading" and rec["value"] != db_rec["value"]:
                continue
            if rec["kind"] == "photo" and (
                    rec["exposure"] != db_rec["exposure"]
                    or rec["resource"] != db_rec["resource"]):
                continue
            return id(rec)
        raise AssertionError("库记录对不回输入记录")

    def _check_overlaps(self, conn, rid, exp, win_by_id, case):
        ovs = list(conn.execute(
            "SELECT * FROM photo_overlap WHERE record_id=? "
            "ORDER BY window_id", (rid,)))
        want_ov = exp["result"][1]
        self.assertEqual(len(ovs), len(want_ov),
                         f"case {case} rec {rid} 交叠数")
        got = sorted(
            ((Fraction(int(o["lo_num"]), int(o["lo_den"])),
              Fraction(int(o["hi_num"]), int(o["hi_den"])))
             for o in ovs))
        want = sorted((lo, hi) for _w, lo, hi in want_ov)
        self.assertEqual(got, want, f"case {case} rec {rid} 交叠区间")
        gaps = list(conn.execute(
            "SELECT * FROM photo_gap WHERE record_id=? ORDER BY seq_no",
            (rid,)))
        got_gaps = [(Fraction(int(g["lo_num"]), int(g["lo_den"])),
                     Fraction(int(g["hi_num"]), int(g["hi_den"])))
                    for g in gaps]
        self.assertEqual(got_gaps, exp["result"][2],
                         f"case {case} rec {rid} 空档")


class IntegerIdentifiers(unittest.TestCase):
    """标识符可以是 JSON 整数；字符串 "1" 与整数 1 不混淆。"""

    def test_int_ids(self):
        tmp = tempfile.mkdtemp(prefix="acq-t")
        lib = build_lib(tmp, ("REG-1",))
        pack = AcqPack(os.path.join(tmp, "pkg"))
        pack.add_device(5, 16, 6, [[1, "scalar"]])
        pack.add_pulse(100, 0)
        pack.add_pulse(101, 10)
        pack.add_window(7, 2, 0, 10, "REG-1", [[5, 1]])
        pack.add_volume("volumes/v.jsonl", [
            {"device": 5, "boot": 0, "seq": 0, "kind": "receipt",
             "count": 0, "pulse": 100},
            {"device": 5, "boot": 0, "seq": 1, "kind": "reading",
             "count": 1, "round": 7, "channel": 1, "value": 3},
            {"device": 5, "boot": 0, "seq": 2, "kind": "receipt",
             "count": 2, "pulse": 101},
        ])
        build_acq(tmp, pack.write(), lib)
        with open_acq(os.path.join(tmp, "acq.db")) as conn:
            obj = acq_windows(conn, round_c=parse_cli_id("7"),
                              window_c=parse_cli_id("2"))
        (w,) = obj["windows"]
        self.assertEqual(w["round"], 7)
        self.assertEqual(w["window"], 2)
        self.assertEqual(w["totals"]["readings"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
