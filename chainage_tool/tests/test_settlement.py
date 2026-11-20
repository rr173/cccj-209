"""巡检履约结算验收测试。

覆盖：
1. 题目给的主样例（10..14，全窗 2 班、上午子窗 1 班）：派工 10、14，
   核实 11、13，12 已达标；
2. 更正记录复算：甲班上午只承认 [12,14] 后，11 缺巡、13 达标；
   更正不污染先前截止点；
3. 乙班正好在 16:00 巡过（窗口半开，不计入）；追加撤销；
4. 片段放到另一轴但保留相同数值；
5. 相邻刻度人数相同而班组不同——取证不得沿用邻点；
6. 不可解释引用定位到趟次/入簿时刻/片段并拒绝整份结算；
7. 重复入簿键、结构错误；
8. 旧版本/未来版本的坏引用不解析；
9. 与独立逐刻度参考程序在短道路随机数据上全量核对；
10. 两截止点比较按实际分界对齐，只报状态变化。
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage.errors import SettlementValidationError  # noqa: E402
import settlement_oracle as oracle  # noqa: E402
from settlement_fixture import (  # noqa: E402
    SettlementFixture, at, oracle_calibs, oracle_obligation, oracle_regs,
    oracle_trips,
)

DAY0 = 1795161600


def frag(reg_id, n_from, n_to, calib_row_id):
    return {"reg_id": reg_id, "n_from": n_from, "n_to": n_to,
            "calib_row_id": calib_row_id}


def tick_states(obligation: dict) -> dict:
    """把某义务的产品区段展开成 {刻度: 状态}，便于逐点断言。"""
    out = {}
    for seg in obligation["segments"]:
        for x in range(seg["lo"], seg["hi"] + 1):
            out[x] = seg["state"]
    return out


def main_obligation(**over):
    ob = {
        "obligation_id": "O1", "axis_id": "A1", "lane": "L1",
        "start": 10, "end": 14,
        "window_start": at(8), "window_end": at(16), "min_crews": 2,
        "sub_windows": [{"start": at(8), "end": at(12), "min_crews": 1,
                         "label": "morning"}],
    }
    ob.update(over)
    return ob


class MainExampleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = SettlementFixture(self.tmp.name)
        self.fx.axis("A1", 100)
        self.fx.calib_row("OLD", "A1", "S", 0, 60, 1000, +1, row_id="S1")
        self.fx.calib_row("NEW", "A1", "G", 0, 60, 2000, +1, row_id="G1")
        self.fx.reg("OLD", "NEW", 1000, 1060, reg_id="R1")
        self.fx.build()
        self.obligations = self.fx.write_obligations([main_obligation()])

    def tearDown(self):
        self.tmp.cleanup()

    def _trips(self):
        """题目主样例：甲上午两版本、下午一趟；乙 12:00 起一趟。"""
        return [
            # 甲上午：版本0 = [10,12]（可拆两片），版本1 = [12,14]
            {"trip_id": "T1", "ingest_time": at(9, 30), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [
                 {"fragments": [frag("R1", 1010, 1011, "S1"),
                                frag("R1", 1012, 1012, "S1")]},
                 {"fragments": [frag("R1", 1012, 1014, "S1")]},
             ]},
            # 乙从 12:00 起确定巡过 [11,13]
            {"trip_id": "T2", "ingest_time": at(12, 30), "action": "trip",
             "crew": "乙", "survey_time": at(12), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1011, 1013, "S1")]}]},
            # 甲下午确定巡过 [10,11]
            {"trip_id": "T3", "ingest_time": at(14, 30), "action": "trip",
             "crew": "甲", "survey_time": at(14), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1011, "S1")]}]},
        ]

    def test_main_example_ledger(self):
        trips = self.fx.write_trips(self._trips())
        out = self.fx.settle(self.obligations, trips, cutoff=at(15))
        ob = out["ledger"]["obligations"][0]
        seg = {s["lo"]: s for s in ob["segments"]}
        self.assertEqual(seg[10]["state"], "UNMET")
        self.assertEqual(seg[11]["state"], "REVIEW")
        self.assertEqual(seg[12]["state"], "MET")
        self.assertEqual(seg[13]["state"], "REVIEW")
        self.assertEqual(seg[14]["state"], "UNMET")
        # 两张清单可直接取用
        dispatch = [(d["lo"], d["hi"]) for d in out["ledger"]["dispatch"]]
        review = [(d["lo"], d["hi"]) for d in out["ledger"]["review"]]
        self.assertEqual(dispatch, [(10, 10), (14, 14)])
        self.assertEqual(review, [(11, 11), (13, 13)])
        # 12 处上午门槛也满足（甲）
        s12 = seg[12]["thresholds"]
        by_label = {t["label"]: t for t in s12}
        self.assertEqual(by_label["full"]["lower"], 2)
        self.assertEqual(by_label["morning"]["lower"], 1)

    def test_morning_route_correction_recompute(self):
        trips = self._trips()
        # 甲在 15:00 更正上午路线：只承认 [12,14]
        trips.append({"trip_id": "T1", "ingest_time": at(15), "action": "trip",
                      "crew": "甲", "survey_time": at(9), "lane": "L1",
                      "routes": [{"fragments": [frag("R1", 1012, 1014,
                                                    "S1")]}]})
        path = self.fx.write_trips(trips, "trips_corrected.jsonl")

        # 更正前截止点：11 需核实、13 需核实
        before = self.fx.settle(self.obligations, path,
                                cutoff=at(14, 45))
        seg_b = tick_states(before["ledger"]["obligations"][0])
        self.assertEqual(seg_b[11], "REVIEW")
        self.assertEqual(seg_b[13], "REVIEW")

        # 更正后截止点：11 缺巡、13 达标
        after = self.fx.settle(self.obligations, path, cutoff=at(15, 30))
        seg_a = tick_states(after["ledger"]["obligations"][0])
        self.assertEqual(seg_a[11], "UNMET")
        self.assertEqual(seg_a[13], "MET")

        # 比较：只报状态改变，按两边分界对齐
        diff = self.fx.settle(self.obligations, path, cutoff=at(15, 30),
                              cutoff_before=at(14, 45))
        changes = diff["comparison"]["obligations"][0]["changes"]
        by_lo = {c["lo"]: c for c in changes}
        self.assertEqual(by_lo[11]["before_state"], "REVIEW")
        self.assertEqual(by_lo[11]["after_state"], "UNMET")
        self.assertEqual(by_lo[13]["before_state"], "REVIEW")
        self.assertEqual(by_lo[13]["after_state"], "MET")
        # 没有把仅人数变化的路段误报成状态变化
        self.assertTrue(all(c["before_state"] != c["after_state"]
                            for c in changes))

    def test_exactly_window_end_excluded(self):
        # 乙改成正好 16:00 巡过：窗口 [8,16) 半开，不计入
        trips = [
            {"trip_id": "B", "ingest_time": at(16), "action": "trip",
             "crew": "乙", "survey_time": at(16), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
        ]
        path = self.fx.write_trips(trips, "end16.jsonl")
        out = self.fx.settle(self.obligations, path, cutoff=at(17))
        ob = out["ledger"]["obligations"][0]
        self.assertEqual(ob["state"], "UNMET")
        for s in ob["segments"]:
            self.assertEqual(s["state"], "UNMET")

    def test_void_removes_trip(self):
        trips = [
            {"trip_id": "B", "ingest_time": at(15), "action": "trip",
             "crew": "乙", "survey_time": at(15), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
            {"trip_id": "B", "ingest_time": at(16), "action": "void"},
        ]
        path = self.fx.write_trips(trips, "void.jsonl")
        before = self.fx.settle(self.obligations, path, cutoff=at(15, 30))
        self.assertEqual(tick_states(before["ledger"]["obligations"][0])[12],
                         "UNMET")
        after = self.fx.settle(self.obligations, path, cutoff=at(17))
        self.assertTrue(all(s["state"] == "UNMET"
                            for s in after["ledger"]["obligations"][0][
                                "segments"]))

    def test_adjacent_same_count_different_crew(self):
        # A 覆盖 10..11，B 覆盖 12..13：相邻刻度人数相同但班组不同
        ob = self.fx.write_obligations(
            [main_obligation(min_crews=1, sub_windows=[])], "ob_adj.json")
        trips = [
            {"trip_id": "TA", "ingest_time": at(9), "action": "trip",
             "crew": "A", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1011, "S1")]}]},
            {"trip_id": "TB", "ingest_time": at(10), "action": "trip",
             "crew": "B", "survey_time": at(10), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1012, 1013, "S1")]}]},
        ]
        path = self.fx.write_trips(trips, "adj.jsonl")
        e11 = self.fx.settle(ob, path, cutoff=at(15),
                             explain=("O1", 11))["explain"]
        e12 = self.fx.settle(ob, path, cutoff=at(15),
                             explain=("O1", 12))["explain"]
        self.assertEqual(e11["thresholds"][0]["definite_crews"], ["A"])
        self.assertEqual(e12["thresholds"][0]["definite_crews"], ["B"])

    def test_uninterpretable_reference_rejected_with_location(self):
        trips = [
            {"trip_id": "BAD", "ingest_time": at(9), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [
                 frag("R1", 1010, 1014, "S1"),
                 frag("R1", 1010, 1014, "GHOST")]}]},
        ]
        path = self.fx.write_trips(trips, "bad.jsonl")
        with self.assertRaises(SettlementValidationError) as ctx:
            self.fx.settle(self.obligations, path, cutoff=at(15))
        issues = ctx.exception.issues
        self.assertEqual(len(issues), 1)
        i = issues[0]
        self.assertEqual(i.code, "UNKNOWN_CALIB_ROW")
        self.assertEqual(i.trip_id, "BAD")
        self.assertEqual(i.ingest_time, at(9))
        self.assertEqual(i.fragment_index, 1)

    def test_uncovered_subfragment_rejected(self):
        # 选定行的编号范围无法解释整个子区间
        trips = [
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 999, 1010, "S1")]}]},
        ]
        path = self.fx.write_trips(trips, "uncovered.jsonl")
        with self.assertRaises(SettlementValidationError) as ctx:
            self.fx.settle(self.obligations, path, cutoff=at(15))
        codes = {i.code for i in ctx.exception.issues}
        self.assertTrue(codes & {"CALIB_DOES_NOT_COVER_FRAGMENT",
                                 "FRAGMENT_OUT_OF_REGISTRATION"})

    def test_duplicate_ingest_key(self):
        trips = [
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "乙", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
        ]
        path = self.fx.write_trips(trips, "dup.jsonl")
        with self.assertRaises(SettlementValidationError) as ctx:
            self.fx.settle(self.obligations, path, cutoff=at(15))
        self.assertEqual(ctx.exception.issues[0].code, "DUPLICATE_INGEST_KEY")

    def test_survey_after_ingest_rejected(self):
        trips = [
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "甲", "survey_time": at(10), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
        ]
        path = self.fx.write_trips(trips, "late.jsonl")
        with self.assertRaises(SettlementValidationError) as ctx:
            self.fx.settle(self.obligations, path, cutoff=at(15))
        self.assertEqual(ctx.exception.issues[0].code, "SURVEY_AFTER_INGEST")

    def test_future_bad_reference_not_parsed(self):
        trips = [
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
            # 未来版本引用坏标定行：在较早截止点不可见，不解析
            {"trip_id": "T", "ingest_time": at(15), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "GHOST")]}]},
        ]
        path = self.fx.write_trips(trips, "futurebad.jsonl")
        out = self.fx.settle(self.obligations, path, cutoff=at(12))
        self.assertEqual(out["ledger"]["obligations"][0]["segments"],
                         out["ledger"]["obligations"][0]["segments"])

    def test_superseded_bad_reference_not_parsed(self):
        trips = [
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "GHOST")]}]},
            {"trip_id": "T", "ingest_time": at(10), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
        ]
        path = self.fx.write_trips(trips, "oldbad.jsonl")
        out = self.fx.settle(self.obligations, path, cutoff=at(15))
        self.assertEqual(out["ledger"]["obligations"][0]["state"], "UNMET")

    def test_explain_shows_route_versions_and_evidence(self):
        trips = self.fx.write_trips(self._trips())
        e = self.fx.settle(self.obligations, trips, cutoff=at(15),
                           explain=("O1", 11))["explain"]
        self.assertEqual(e["state"], "REVIEW")
        # 11 处：全窗门槛甲（下午 T3 确定到过 10..11）与乙都确定；
        # REVEIW 来自上午门槛：甲上午两个版本只有一个到过 11（仅可能）。
        full = e["thresholds"][0]
        self.assertEqual(full["definite_crews"], ["乙", "甲"])
        self.assertEqual(full["possible_only_crews"], [])
        morning = e["thresholds"][1]
        self.assertEqual(morning["definite_crews"], [])
        self.assertEqual(morning["possible_only_crews"], ["甲"])
        trips_by_id = {t["trip_id"]: t for t in e["trips"]}
        self.assertEqual(trips_by_id["T1"]["definite"], False)
        self.assertEqual(trips_by_id["T1"]["possible"], True)
        # 甲的两条路线版本都要出现，其中一条命中、一条未命中
        route_hits = {r["route_index"]: r["covers_point"]
                      for r in trips_by_id["T1"]["routes"]}
        self.assertEqual(route_hits, {0: True, 1: False})
        # 命中片段能追到登记与源标定行
        hit = [f for r in trips_by_id["T1"]["routes"] if r["covers_point"]
               for f in r["fragments"] if f["covers_point"]]
        self.assertTrue(hit)
        self.assertEqual(hit[0]["reg_id"], "R1")
        self.assertEqual(hit[0]["calib_row_id"], "S1")


class MultiObligationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = SettlementFixture(self.tmp.name)
        self.fx.axis("A1", 100)
        self.fx.axis("A2", 100)
        self.fx.calib_row("OLD", "A1", "S", 0, 60, 1000, +1, row_id="S1")
        self.fx.calib_row("OLD", "A2", "S2", 0, 60, 1000, +1, row_id="S2")
        self.fx.calib_row("NEW", "A1", "G", 0, 60, 2000, +1, row_id="G1")
        self.fx.calib_row("NEW", "A2", "G2", 0, 60, 2000, +1, row_id="G2")
        self.fx.reg("OLD", "NEW", 1000, 1060, axis_id="A1", reg_id="R1")
        self.fx.reg("OLD", "NEW", 1000, 1060, axis_id="A2", reg_id="R2")
        self.fx.build()

    def tearDown(self):
        self.tmp.cleanup()

    def test_overlapping_obligations_settle_separately(self):
        obs = [
            {"obligation_id": "O1", "axis_id": "A1", "lane": "L1",
             "start": 10, "end": 20, "window_start": at(8),
             "window_end": at(16), "min_crews": 1, "sub_windows": []},
            {"obligation_id": "O2", "axis_id": "A1", "lane": "L1",
             "start": 15, "end": 25, "window_start": at(8),
             "window_end": at(16), "min_crews": 1, "sub_windows": []},
        ]
        op = self.fx.write_obligations(obs, "ob2.json")
        trips = self.fx.write_trips([
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "A", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
        ])
        out = self.fx.settle(op, trips, cutoff=at(15))
        by_id = {o["obligation_id"]: o for o in out["ledger"]["obligations"]}
        d1 = [(d["lo"], d["hi"]) for d in out["ledger"]["dispatch"]
              if d["obligation_id"] == "O1"]
        d2 = [(d["lo"], d["hi"]) for d in out["ledger"]["dispatch"]
              if d["obligation_id"] == "O2"]
        self.assertEqual(d1, [(15, 20)])
        self.assertEqual(d2, [(15, 25)])
        self.assertEqual(by_id["O1"]["state"], "UNMET")
        self.assertEqual(by_id["O2"]["state"], "UNMET")

    def test_fragment_on_other_axis_does_not_count(self):
        obs = [main_obligation(axis_id="A1", min_crews=1, sub_windows=[])]
        op = self.fx.write_obligations(obs, "oax.json")
        # 合法片段，但落在 A2：数值相同也不计入 A1 义务
        trips = self.fx.write_trips([
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "A", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R2", 1010, 1014, "S2")]}]},
        ])
        out = self.fx.settle(op, trips, cutoff=at(15))
        self.assertEqual(out["ledger"]["obligations"][0]["state"], "UNMET")
        self.assertEqual(
            [(d["lo"], d["hi"]) for d in out["ledger"]["dispatch"]],
            [(10, 14)])

    def test_other_lane_ignored(self):
        obs = [main_obligation(min_crews=1, sub_windows=[])]
        op = self.fx.write_obligations(obs, "olane.json")
        trips = self.fx.write_trips([
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "A", "survey_time": at(9), "lane": "L2",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]},
        ])
        out = self.fx.settle(op, trips, cutoff=at(15))
        self.assertEqual(out["ledger"]["obligations"][0]["state"], "UNMET")


class OracleCrosscheckTest(unittest.TestCase):
    """短道路 + 少量趟次：产品与逐刻度参考程序全量比对。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_random_small_vs_oracle(self):
        rng = random.Random(20261120)
        for trial in range(10):
            fx = SettlementFixture(
                os.path.join(self.tmp.name, f"t{trial}"))
            fx.axis("A1", 40)
            # 一条正向源段 S1：物理 0..30 -> 编号 100..130
            fx.calib_row("OLD", "A1", "S", 0, 30, 100, +1, row_id="S1")
            # 一条反向源段 T1：物理 0..30 -> 编号 130..100，与 S1 重叠
            fx.calib_row("OLD", "A1", "T", 0, 30, 130, -1, row_id="T1")
            fx.calib_row("NEW", "A1", "G", 0, 30, 900, +1, row_id="G1")
            fx.reg("OLD", "NEW", 100, 130, axis_id="A1", reg_id="R1")
            fx.reg("OLD", "NEW", 100, 130, reg_id="R2")  # 不限轴（单轴下同）
            fx.build()

            n_ob = rng.randint(1, 2)
            obligations = []
            for oi in range(n_ob):
                a = rng.randint(0, 20)
                b = rng.randint(a, min(30, a + rng.randint(1, 10)))
                w0 = at(rng.randint(8, 10))
                w1 = w0 + rng.choice([3600, 7200, 14400])
                obs = {
                    "obligation_id": f"O{oi}", "axis_id": "A1",
                    "lane": "L1", "start": a, "end": b,
                    "window_start": w0, "window_end": w1,
                    "min_crews": rng.randint(1, 2), "sub_windows": []}
                if rng.random() < 0.5:
                    s0 = w0
                    s1 = w0 + rng.choice([1800, 3600])
                    obs["sub_windows"] = [{
                        "start": s0, "end": s1,
                        "min_crews": rng.randint(1, 2), "label": "sub"}]
                obligations.append(obs)
            op = fx.write_obligations(obligations, "obs.json")

            trips = []
            for ti in range(rng.randint(3, 8)):
                crew = rng.choice(["A", "B", "C"])
                st = at(rng.randint(8, 12))
                n_from = rng.randint(100, 128)
                n_to = min(130, n_from + rng.randint(0, 6))
                routes = []
                for _ in range(rng.randint(1, 3)):
                    fn = rng.randint(100, 128)
                    ft = min(130, fn + rng.randint(0, 6))
                    row = "S1" if fn <= 115 else "T1"
                    routes.append({"fragments": [
                        frag("R1", fn, ft, row)]})
                if rng.random() < 0.15 and n_to > n_from:
                    # 拆成两片
                    mid = rng.randint(n_from, n_to - 1)
                    row1 = "S1" if n_from <= 115 else "T1"
                    row2 = "S1" if mid + 1 <= 115 else "T1"
                    routes = [{"fragments": [
                        frag("R1", n_from, mid, row1),
                        frag("R1", mid + 1, n_to, row2)]}]
                trips.append({
                    "trip_id": f"T{ti}", "ingest_time": st + 60,
                    "action": "trip", "crew": crew, "survey_time": st,
                    "lane": "L1", "routes": routes})
            # 排序保证入簿时间单调；否则按 (trip, ingest) 顺序任意都行
            tp = fx.write_trips(trips, "trips.jsonl")

            cutoff = at(13)
            out = fx.settle(op, tp, cutoff=cutoff)

            calibs = oracle_calibs(fx)
            regs = oracle_regs(fx)
            o_trips = oracle_trips(trips, cutoff)
            for ob_raw in obligations:
                ob = oracle_obligation(ob_raw)
                expected = oracle.evaluate_obligation(
                    ob, o_trips, regs, calibs)
                got = next(o for o in out["ledger"]["obligations"]
                           if o["obligation_id"] == ob_raw["obligation_id"])
                # 展开产品区段成逐刻度
                got_ticks = {}
                for seg in got["segments"]:
                    for x in range(seg["lo"], seg["hi"] + 1):
                        got_ticks[x] = (seg["state"],
                                        [(t["label"], t["lower"], t["upper"])
                                         for t in seg["thresholds"]])
                exp_ticks = oracle.expand_segments(expected)
                self.assertEqual(set(got_ticks), set(exp_ticks),
                                 (trial, ob_raw["obligation_id"]))
                for x in exp_ticks:
                    self.assertEqual(got_ticks[x], exp_ticks[x],
                                     (trial, ob_raw["obligation_id"], x))
                # 单点取证：在每个区段起点（以及紧邻刻度）核对，验证
                # "不沿用邻点班组/路线"。覆盖所有 x 太慢，这里抽样。
                probe_points = set()
                for seg in got["segments"]:
                    probe_points.add(seg["lo"])
                    if seg["hi"] != seg["lo"]:
                        probe_points.add(seg["lo"] + 1)
                    probe_points.add(seg["hi"])
                for x in sorted(probe_points):
                    if x not in exp_ticks:
                        continue
                    exp_pt = oracle.evaluate_point(ob, o_trips, regs, calibs, x)
                    got_pt = fx.settle(
                        op, tp, cutoff=cutoff,
                        explain=(ob_raw["obligation_id"], x))["explain"]
                    self.assertEqual(got_pt["state"], exp_pt["state"],
                                     (trial, x))
                    for gt, et in zip(got_pt["thresholds"],
                                      exp_pt["thresholds"]):
                        self.assertEqual(gt["lower"], et["lower"])
                        self.assertEqual(gt["upper"], et["upper"])
                        self.assertEqual(gt["definite_crews"],
                                         et["definite_crews"])
                        self.assertEqual(gt["possible_only_crews"],
                                         et["possible_only_crews"])

    def test_two_cutoffs_match_oracle_state_changes(self):
        rng = random.Random(99117)
        fx = SettlementFixture(os.path.join(self.tmp.name, "diff"))
        fx.axis("A1", 40)
        fx.calib_row("OLD", "A1", "S", 0, 30, 100, +1, row_id="S1")
        fx.calib_row("NEW", "A1", "G", 0, 30, 900, +1, row_id="G1")
        fx.reg("OLD", "NEW", 100, 130, axis_id="A1", reg_id="R1")
        fx.build()
        ob_raw = {"obligation_id": "O1", "axis_id": "A1", "lane": "L1",
                  "start": 5, "end": 20, "window_start": at(8),
                  "window_end": at(16), "min_crews": 2, "sub_windows": []}
        op = fx.write_obligations([ob_raw], "obs.json")
        trips = []
        for ti in range(10):
            n_from = rng.randint(100, 125)
            n_to = min(130, n_from + rng.randint(0, 5))
            trips.append({
                "trip_id": f"T{ti}", "ingest_time": at(9, ti),
                "action": "trip", "crew": rng.choice(["A", "B", "C"]),
                "survey_time": at(8, ti), "lane": "L1",
                "routes": [{"fragments": [frag("R1", n_from, n_to, "S1")]}]})
        tp = fx.write_trips(trips, "trips.jsonl")
        before, after = at(9, 4), at(9, 9)
        out = fx.settle(op, tp, cutoff=after, cutoff_before=before)

        calibs = oracle_calibs(fx)
        regs = oracle_regs(fx)
        ob = oracle_obligation(ob_raw)
        exp_b = oracle.evaluate_obligation(
            ob, oracle_trips(trips, before), regs, calibs)
        exp_a = oracle.evaluate_obligation(
            ob, oracle_trips(trips, after), regs, calibs)
        bmap = oracle.expand_segments(exp_b)
        amap = oracle.expand_segments(exp_a)
        changed = {x for x in bmap
                   if bmap[x][0] != amap[x][0]}
        got = out["comparison"]["obligations"][0]["changes"]
        got_changed = set()
        for c in got:
            for x in range(c["lo"], c["hi"] + 1):
                got_changed.add(x)
        self.assertEqual(got_changed, changed)
        for c in got:
            for x in range(c["lo"], c["hi"] + 1):
                self.assertEqual(c["before_state"], bmap[x][0])
                self.assertEqual(c["after_state"], amap[x][0])


class InputValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = SettlementFixture(self.tmp.name)
        self.fx.axis("A1", 100)
        self.fx.calib_row("OLD", "A1", "S", 0, 60, 1000, +1, row_id="S1")
        self.fx.calib_row("NEW", "A1", "G", 0, 60, 2000, +1, row_id="G1")
        self.fx.reg("OLD", "NEW", 1000, 1060, reg_id="R1")
        self.fx.build()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_crew_field(self):
        op = self.fx.write_obligations([main_obligation()])
        tp = self.fx.write_trips([
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "S1")]}]}])
        with self.assertRaises(SettlementValidationError) as ctx:
            self.fx.settle(op, tp, cutoff=at(15))
        self.assertEqual(ctx.exception.issues[0].code, "MISSING_FIELD")

    def test_bad_obligation_range(self):
        ob = main_obligation(start=20, end=10)
        op = self.fx.write_obligations([ob])
        tp = self.fx.write_trips([])
        with self.assertRaises(SettlementValidationError) as ctx:
            self.fx.settle(op, tp, cutoff=at(15))
        self.assertEqual(ctx.exception.issues[0].code, "BAD_RANGE")

    def test_empty_routes(self):
        op = self.fx.write_obligations([main_obligation()])
        tp = self.fx.write_trips([
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "A", "survey_time": at(9), "lane": "L1", "routes": []}])
        with self.assertRaises(SettlementValidationError) as ctx:
            self.fx.settle(op, tp, cutoff=at(15))
        self.assertEqual(ctx.exception.issues[0].code, "BAD_FIELD")

    def test_no_partial_output_on_error(self):
        # 校验失败必须抛异常（而不是返回部分清单）
        op = self.fx.write_obligations([main_obligation()])
        tp = self.fx.write_trips([
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "A", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "GHOST")]}]}])
        with self.assertRaises(SettlementValidationError):
            self.fx.settle(op, tp, cutoff=at(15))


class CliSettlementTest(unittest.TestCase):
    """CLI 冒烟：三个子命令与退出码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = SettlementFixture(self.tmp.name)
        self.fx.axis("A1", 100)
        self.fx.calib_row("OLD", "A1", "S", 0, 60, 1000, +1, row_id="S1")
        self.fx.calib_row("NEW", "A1", "G", 0, 60, 2000, +1, row_id="G1")
        self.fx.reg("OLD", "NEW", 1000, 1060, reg_id="R1")
        self.fx.build()
        self.ob = self.fx.write_obligations([main_obligation()])
        self.tp = self.fx.write_trips([
            {"trip_id": "T2", "ingest_time": at(12, 30), "action": "trip",
             "crew": "乙", "survey_time": at(12), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1011, 1013, "S1")]}]},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=root)
        return subprocess.run(
            [sys.executable, "-m", "chainage", *args],
            cwd=root, capture_output=True, text=True, env=env)

    def test_settle_cli_and_exit_codes(self):
        r = self._run("settle", self.fx.db_path, self.ob, self.tp,
                      "--cutoff", str(at(15)), "--text")
        self.assertEqual(r.returncode, 0)
        self.assertIn("待核实", r.stdout)
        r = self._run("settle", self.fx.db_path, self.ob, self.tp,
                      "--cutoff", str(at(15)), "--list", "dispatch")
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        self.assertTrue(all(d["state"] == "UNMET" for d in payload))
        r = self._run("settle-diff", self.fx.db_path, self.ob, self.tp,
                      "--before", str(at(10)), "--after", str(at(15)),
                      "--text")
        self.assertEqual(r.returncode, 0)
        r = self._run("settle-explain", self.fx.db_path, self.ob, self.tp,
                      "--cutoff", str(at(15)), "--obligation", "O1",
                      "--x", "12", "--text")
        self.assertEqual(r.returncode, 0)
        self.assertIn("乙", r.stdout)

    def test_cli_validation_failure_exit_2(self):
        bad = self.fx.write_trips([
            {"trip_id": "T", "ingest_time": at(9), "action": "trip",
             "crew": "A", "survey_time": at(9), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1014, "GHOST")]}]},
        ], "bad.jsonl")
        r = self._run("settle", self.fx.db_path, self.ob, bad,
                      "--cutoff", str(at(15)))
        self.assertEqual(r.returncode, 2)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["error"], "SETTLEMENT_VALIDATION_FAILED")


class OutputConsistencyTest(unittest.TestCase):
    """三种输出互相一致：台账、清单、取证、两截止点比较。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = SettlementFixture(self.tmp.name)
        self.fx.axis("A1", 100)
        self.fx.calib_row("OLD", "A1", "S", 0, 60, 1000, +1, row_id="S1")
        self.fx.calib_row("NEW", "A1", "G", 0, 60, 2000, +1, row_id="G1")
        self.fx.reg("OLD", "NEW", 1000, 1060, reg_id="R1")
        self.fx.build()
        self.ob = self.fx.write_obligations([main_obligation()])
        self.tp = self.fx.write_trips(self._trips())

    def tearDown(self):
        self.tmp.cleanup()

    def _trips(self):
        return [
            {"trip_id": "T1", "ingest_time": at(9, 30), "action": "trip",
             "crew": "甲", "survey_time": at(9), "lane": "L1",
             "routes": [
                 {"fragments": [frag("R1", 1010, 1011, "S1"),
                                frag("R1", 1012, 1012, "S1")]},
                 {"fragments": [frag("R1", 1012, 1014, "S1")]},
             ]},
            {"trip_id": "T2", "ingest_time": at(12, 30), "action": "trip",
             "crew": "乙", "survey_time": at(12), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1011, 1013, "S1")]}]},
            {"trip_id": "T3", "ingest_time": at(14, 30), "action": "trip",
             "crew": "甲", "survey_time": at(14), "lane": "L1",
             "routes": [{"fragments": [frag("R1", 1010, 1011, "S1")]}]},
        ]

    def test_lists_are_partition_of_ledger_and_match_explain(self):
        out = self.fx.settle(self.ob, self.tp, cutoff=at(15))
        ob = out["ledger"]["obligations"][0]
        ledger_states = tick_states(ob)
        # dispatch / review 恰好是台账中 UNMET / REVIEW 刻度，互不重叠
        disp = set()
        for d in out["ledger"]["dispatch"]:
            disp.update(range(d["lo"], d["hi"] + 1))
        rev = set()
        for d in out["ledger"]["review"]:
            rev.update(range(d["lo"], d["hi"] + 1))
        self.assertEqual(disp, {x for x, s in ledger_states.items()
                                if s == "UNMET"})
        self.assertEqual(rev, {x for x, s in ledger_states.items()
                               if s == "REVIEW"})
        self.assertFalse(disp & rev)
        # 取证在每点的状态与台账一致
        for x, st in ledger_states.items():
            e = self.fx.settle(self.ob, self.tp, cutoff=at(15),
                               explain=("O1", x))["explain"]
            self.assertEqual(e["state"], st, x)

    def test_diff_matches_independent_settle(self):
        trips = self._trips()
        trips.append({"trip_id": "T1", "ingest_time": at(15), "action": "void"})
        tp = self.fx.write_trips(trips, "trips_void.jsonl")
        before_cut, after_cut = at(14, 45), at(16)
        before = self.fx.settle(self.ob, tp, cutoff=before_cut)
        after = self.fx.settle(self.ob, tp, cutoff=after_cut)
        bmap = tick_states(before["ledger"]["obligations"][0])
        amap = tick_states(after["ledger"]["obligations"][0])
        expected_changed = {x for x in bmap if bmap[x] != amap[x]}
        diff = self.fx.settle(self.ob, tp, cutoff=after_cut,
                              cutoff_before=before_cut)
        got = diff["comparison"]["obligations"][0]["changes"]
        got_changed = set()
        for c in got:
            got_changed.update(range(c["lo"], c["hi"] + 1))
            for x in range(c["lo"], c["hi"] + 1):
                self.assertEqual(c["before_state"], bmap[x])
                self.assertEqual(c["after_state"], amap[x])
        self.assertEqual(got_changed, expected_changed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
