"""验收测试。

覆盖
====
1. 题目指定的断链轴（S/T 源段 + U/V 目标段）：四态、别名、反查不改写状态；
2. 两个不同物理位置映射到同一目标编号；
3. 唯一位置缺目标标牌；
4. 不同轴重复使用同一段名；
5. 同一段两个闭区间相接 -> 非法重叠并定位原行；
6. 同轴反向标定区间内相遇：唯一整数交点合并单列；有理非整数交点不合并；
7. 参考程序逐刻度枚举 vs 产品正查展开 / 反查命中刻度集合（随机包）；
8. 校验错误：重复行标识、坏字段、未知轴、坏 JSON；
9. 构建失败不覆盖已有库；CLI 冒烟。
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage.build import build, validate_only  # noqa: E402
from chainage.errors import PackageValidationError  # noqa: E402
from chainage.query import forward_query, reverse_query  # noqa: E402
import reference_oracle as oracle  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# 交接包构造工具
# --------------------------------------------------------------------------- #
class PackageBuilder:
    def __init__(self, version="handover-v1"):
        self.version = version
        self.axes = {}
        self.calib = []
        self.regs = []
        self._calib_seq = 0
        self._reg_seq = 0

    def add_axis(self, axis_id, length):
        self.axes[axis_id] = length
        return self

    def add_calib(self, system, axis_id, segment, lo, hi, label_lo, direction,
                  row_id=None, line=None):
        self._calib_seq += 1
        row_id = row_id or f"C{self._calib_seq:03d}"
        self.calib.append({
            "row_id": row_id, "system": system, "axis_id": axis_id,
            "segment": segment, "lo": lo, "hi": hi,
            "label_lo": label_lo, "direction": direction,
        })
        return row_id

    def add_reg(self, src, tgt, n_from=None, n_to=None, n=None,
                axis_id=None, segment=None, reg_id=None):
        self._reg_seq += 1
        reg_id = reg_id or f"R{self._reg_seq:03d}"
        row = {"reg_id": reg_id, "source_system": src, "target_system": tgt}
        if n is not None:
            row["n"] = n
        else:
            row["from"], row["to"] = n_from, n_to
        if axis_id is not None:
            row["axis_id"] = axis_id
        if segment is not None:
            row["segment"] = segment
        self.regs.append(row)
        return reg_id

    def write(self, dirpath, bad_calib_lines=None, bad_manifest=False):
        manifest = {
            "version": self.version,
            "axes": [{"axis_id": a, "length": l} for a, l in self.axes.items()],
            "files": {"manifest": "manifest.json",
                      "calibration": "calibration.jsonl",
                      "registration": "registration.jsonl"},
        }
        with open(os.path.join(dirpath, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            if bad_manifest:
                fh.write("{not json")
            else:
                json.dump(manifest, fh, ensure_ascii=False)
        with open(os.path.join(dirpath, "calibration.jsonl"), "w",
                  encoding="utf-8") as fh:
            for row in self.calib:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            for extra in bad_calib_lines or []:
                fh.write(extra + "\n")
        with open(os.path.join(dirpath, "registration.jsonl"), "w",
                  encoding="utf-8") as fh:
            for row in self.regs:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return dirpath


def _expand_forward(result):
    """把产品正查的压缩分段展开回逐刻度 dict，便于与参考程序对照。"""
    ticks = {}
    for seg in result["segments"]:
        for n in range(seg["source_label_lo"], seg["source_label_hi"] + 1):
            positions = {}
            for p in seg["positions"]:
                x = p["x_slope"] * n + p["x_intercept"]
                src = tuple(sorted(r["row_id"] for r in p["source_rows"]))
                target = {}
                for e in p["target_evidence"]:
                    label = e["label_slope"] * n + e["label_intercept"]
                    target[(e["target_segment"], label)] = tuple(
                        sorted(r["row_id"] for r in e["target_rows"]))
                positions[(p["axis_id"], x)] = {"source": src, "target": target}
            ticks[n] = (seg["status"], positions)
    return ticks


def _oracle_ticks(pb: PackageBuilder):
    calibs = [oracle.make_calib(r) for r in pb.calib]
    out = {}
    for rr in pb.regs:
        reg = oracle.make_reg(rr)
        ticks = {}
        for t in oracle.enumerate_registration(reg, calibs):
            positions = {}
            for (ax, x), pos in t.positions.items():
                positions[(ax, x)] = {
                    "source": tuple(sorted(pos["source"])),
                    "target": {k: tuple(sorted(v))
                               for k, v in pos["target"].items()},
                }
            ticks[t.n] = (t.status, positions)
        out[rr["reg_id"]] = ticks
    return out


class AcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.pkg_dir = os.path.join(self.root, "pkg")
        os.makedirs(self.pkg_dir)
        self.db_path = os.path.join(self.root, "chainage.db")

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, pb):
        pb.write(self.pkg_dir)
        return build(self.pkg_dir, self.db_path)

    # ------------------------------------------------------------------ #
    # 1) 题目指定的断链轴
    # ------------------------------------------------------------------ #
    def _build_spec_axis(self):
        pb = PackageBuilder()
        pb.add_axis("A1", 100)
        # 源段 S：物理 [0,4] 标号 100..104（+1）
        pb.add_calib("OLD", "A1", "S", 0, 4, 100, +1, row_id="S1")
        # 源段 T：物理 [4,8] 标号 104..100（-1）
        pb.add_calib("OLD", "A1", "T", 4, 8, 104, -1, row_id="T1")
        # 目标 U：物理 [3,4] 给号 700,701
        pb.add_calib("NEW", "A1", "U", 3, 4, 700, +1, row_id="U1")
        # 目标 V：物理 [4,5] 给号 900,899（反向）
        pb.add_calib("NEW", "A1", "V", 4, 5, 900, -1, row_id="V1")
        pb.add_reg("OLD", "NEW", n_from=103, n_to=105, reg_id="R103")
        pb.add_reg("OLD", "NEW", n=104, reg_id="R104")
        return pb

    def test_spec_axis_statuses_and_aliases(self):
        pb = self._build_spec_axis()
        stats = self.build(pb)
        self.assertEqual(stats.registration_rows, 2)

        full = forward_query(self.db_path, "R103")
        ticks = _expand_forward(full)

        # 103 -> 物理 3(S) 与 5(T)：两个源位置
        st, pos = ticks[103]
        self.assertEqual(st, "MULTIPLE_SOURCE")
        self.assertEqual(set(pos.keys()), {("A1", 3), ("A1", 5)})
        # 每个位置仍带可得目标证据：物理3 -> U/700；物理5 -> V/899
        self.assertEqual(pos[("A1", 3)]["target"], {("U", 700): ("U1",)})
        self.assertEqual(pos[("A1", 5)]["target"], {("V", 899): ("V1",)})

        # 104 -> 仅物理 4，但有 S1/T1 两条源依据；目标 U/701 与 V/900 两别名
        st, pos = ticks[104]
        self.assertEqual(st, "RESOLVED")
        self.assertEqual(list(pos.keys()), [("A1", 4)])
        self.assertEqual(pos[("A1", 4)]["source"], ("S1", "T1"))
        self.assertEqual(pos[("A1", 4)]["target"],
                         {("U", 701): ("U1",), ("V", 900): ("V1",)})

        # 105 -> 没有源位置
        st, pos = ticks[105]
        self.assertEqual(st, "NO_SOURCE")
        self.assertEqual(pos, {})

        # 正查单点 104：两个别名
        p104 = forward_query(self.db_path, "R104")
        t104 = _expand_forward(p104)[104]
        self.assertEqual(t104[0], "RESOLVED")
        labels = sorted(k for k in t104[1][("A1", 4)]["target"])
        self.assertEqual(labels, [("U", 701), ("V", 900)])

        # 可入仓区段只含 104（单点）
        wh = [(w["source_label_lo"], w["source_label_hi"])
              for w in full["warehousable_segments"]]
        self.assertEqual(wh, [(104, 104)])

        # 分段压缩：103 与 105 状态不同不能合并；104 单列（交点）
        spans = [(s["source_label_lo"], s["source_label_hi"], s["status"])
                 for s in full["segments"]]
        self.assertEqual(spans,
                         [(103, 103, "MULTIPLE_SOURCE"),
                          (104, 104, "RESOLVED"),
                          (105, 105, "NO_SOURCE")])

    def test_spec_axis_reverse_keeps_original_status(self):
        pb = self._build_spec_axis()
        pb.write(self.pkg_dir)
        build(self.pkg_dir, self.db_path)

        # 查 V 的 899：只命中源 103 的物理 5，但返回须标明两个源位置、原状态
        rev = reverse_query(self.db_path, "NEW", n=899)
        self.assertEqual(len(rev["matches"]), 1)
        m = rev["matches"][0]
        self.assertEqual(m["reg_id"], "R103")
        self.assertEqual(m["original_status"], "MULTIPLE_SOURCE")
        self.assertEqual((m["matched_source_label_lo"],
                          m["matched_source_label_hi"]), (103, 103))
        # 两个候选位置都要回来，不得因争议删掉任何一个
        axes_x = sorted((p["axis_id"], p["x_lo"]) for p in m["positions"])
        self.assertEqual(axes_x, [("A1", 3), ("A1", 5)])
        # 只有物理5那条证据标 matched
        matched = [(p["axis_id"], e["target_segment"], e["target_label_lo"])
                   for p in m["positions"] for e in p["target_evidence"]
                   if e["matched"]]
        self.assertEqual(matched, [("A1", "V", 899)])

        # 点 701/900 反查命中 R104；注意 701 在 R103 的物理 4（争议位置之一）
        # 同样存在，因此 701 同时命中 R103（原状态 MULTIPLE，不改写）与 R104。
        rev701 = reverse_query(self.db_path, "NEW", n=701)
        by_reg = {m["reg_id"]: m for m in rev701["matches"]}
        self.assertEqual(set(by_reg), {"R103", "R104"})
        # R103 命中的是它自己的 104 刻度（唯一位置 RESOLVED 段），
        # 原状态如实返回 RESOLVED——争议（MULTI）只发生在 103 刻度。
        self.assertEqual(by_reg["R103"]["original_status"], "RESOLVED")
        self.assertEqual(by_reg["R103"]["matched_source_label_lo"], 104)
        self.assertEqual(by_reg["R104"]["original_status"], "RESOLVED")
        self.assertEqual(by_reg["R104"]["matched_source_label_lo"], 104)

        rev900 = reverse_query(self.db_path, "NEW", n=900)
        # V/900 在物理 4：R104 的 104 与 R103 的 104 刻度都含该证据
        self.assertEqual({m["reg_id"] for m in rev900["matches"]},
                         {"R103", "R104"})
        self.assertTrue(all(m["original_status"] == "RESOLVED"
                            for m in rev900["matches"]))

        # 区间反查 899..900：
        #   R103 的 103 命中 V/899（物理5，MULTI 段），104 命中 V/900（物理4）；
        #   R104 的 104 命中 V/900。
        rev = reverse_query(self.db_path, "NEW", n_from=899, n_to=900)
        got = {(m["reg_id"], m["matched_source_label_lo"],
                m["matched_source_label_hi"]) for m in rev["matches"]}
        self.assertEqual(got, {("R103", 103, 103), ("R103", 104, 104),
                               ("R104", 104, 104)})
        # 命中 103 的那条记录属于 MULTI 段，原状态必须保持 MULTIPLE_SOURCE；
        # 命中 104 的属于 RESOLVED 段（物理4 唯一位置）。
        m103 = next(m for m in rev["matches"]
                    if m["reg_id"] == "R103"
                    and m["matched_source_label_lo"] == 103)
        self.assertEqual(m103["original_status"], "MULTIPLE_SOURCE")
        # 两个候选位置都在，且只有物理5的 V/899 标为命中
        self.assertEqual(sorted((p["axis_id"], p["x_lo"])
                                for p in m103["positions"]),
                         [("A1", 3), ("A1", 5)])
        hit_marks = [(p["axis_id"], e["target_segment"],
                      e["target_label_lo"])
                     for p in m103["positions"]
                     for e in p["target_evidence"]
                     if e["matched"]]
        # 恰好只有物理5的 V/899 是本次命中证据
        self.assertEqual(hit_marks, [("A1", "V", 899)])
        # 物理3的 U/700 作为候选证据仍完整保留，未被删除
        # （刻度 103 上物理3 -> U/700，物理5 -> V/899）
        all_marks = [(p["axis_id"], e["target_segment"],
                      e["target_label_lo"])
                     for p in m103["positions"]
                     for e in p["target_evidence"]]
        self.assertEqual(sorted(all_marks),
                         [("A1", "U", 700), ("A1", "V", 899)])

        # 限定目标段 V 后 U 的证据不再命中（700/701 全在 U 段）
        rev = reverse_query(self.db_path, "NEW", n_from=700, n_to=701,
                            target_segment="V")
        self.assertEqual(rev["matches"], [])
        rev = reverse_query(self.db_path, "NEW", n_from=700, n_to=701,
                            target_segment="U")
        # U/700 仅 R103；U/701 命中 R103 与 R104
        self.assertEqual({m["reg_id"] for m in rev["matches"]},
                         {"R103", "R104"})
        r103_hits = {(m["matched_source_label_lo"], m["original_status"])
                     for m in rev["matches"] if m["reg_id"] == "R103"}
        # R103 的 103 刻度属 MULTI 段、104 刻度属 RESOLVED 段，各自如实返回
        self.assertEqual(r103_hits,
                         {(103, "MULTIPLE_SOURCE"), (104, "RESOLVED")})
        r104 = next(m for m in rev["matches"] if m["reg_id"] == "R104")
        self.assertEqual(r104["original_status"], "RESOLVED")

    # ------------------------------------------------------------------ #
    # 2) 两个不同物理位置 -> 同一目标编号，仍是 MULTIPLE
    # ------------------------------------------------------------------ #
    def test_two_positions_same_target_label(self):
        pb = PackageBuilder()
        pb.add_axis("AX", 20)
        # 两个不同物理位置在源编号 50 处相遇：
        # P: n(x)=50+(x-0)=x+50，n=50 -> x=0；编号范围 50..54
        pb.add_calib("SRC", "AX", "P", 0, 4, 50, +1, row_id="P1")
        # Q: n(x)=50-(x-6)=56-x，n=50 -> x=6；编号范围 46..50
        pb.add_calib("SRC", "AX", "Q", 6, 10, 50, -1, row_id="Q1")
        # 目标：两个位置 x0 与 x6 都给号 10（不同段名，允许同号；lo<hi 取最小段）
        pb.add_calib("DST", "AX", "G1", 0, 1, 10, +1, row_id="G1")
        pb.add_calib("DST", "AX", "G2", 6, 7, 10, +1, row_id="G2")
        pb.add_reg("SRC", "DST", n=50, reg_id="R50")
        self.build(pb)
        res = forward_query(self.db_path, "R50")
        ticks = _expand_forward(res)
        st, pos = ticks[50]
        self.assertEqual(st, "MULTIPLE_SOURCE")
        self.assertEqual(set(pos.keys()), {("AX", 0), ("AX", 6)})
        target_labels = {lab for p in pos.values() for lab in p["target"]}
        self.assertEqual(target_labels, {("G1", 10), ("G2", 10)})
        # 即使换成同号，也没有可入仓段
        self.assertEqual(res["warehousable_segments"], [])

    # ------------------------------------------------------------------ #
    # 3) 唯一位置缺目标
    # ------------------------------------------------------------------ #
    def test_unique_position_without_target(self):
        pb = PackageBuilder()
        pb.add_axis("AX", 10)
        pb.add_calib("SRC", "AX", "P", 0, 5, 200, +1, row_id="P1")
        pb.add_calib("DST", "AX", "G", 8, 9, 1, +1, row_id="G1")  # 与 x0..5 不交
        pb.add_reg("SRC", "DST", n=202, reg_id="R1")
        self.build(pb)
        ticks = _expand_forward(forward_query(self.db_path, "R1"))
        self.assertEqual(ticks[202][0], "NO_TARGET")
        self.assertEqual(list(ticks[202][1].keys()), [("AX", 2)])

    # ------------------------------------------------------------------ #
    # 4) 不同轴复用同一段名
    # ------------------------------------------------------------------ #
    def test_same_segment_name_on_different_axes(self):
        pb = PackageBuilder()
        pb.add_axis("AX1", 10)
        pb.add_axis("AX2", 10)
        pb.add_calib("SRC", "AX1", "COMMON", 0, 4, 10, +1, row_id="K1")
        pb.add_calib("SRC", "AX2", "COMMON", 0, 4, 10, +1, row_id="K2")
        pb.add_calib("DST", "AX1", "H", 0, 4, 90, +1, row_id="H1")
        pb.add_calib("DST", "AX2", "H", 0, 4, 90, +1, row_id="H2")
        # 不限定轴：同段名跨轴 -> 多位置
        pb.add_reg("SRC", "DST", n=12, reg_id="R-ANY")
        # 限定 AX2：唯一位置
        pb.add_reg("SRC", "DST", n=12, axis_id="AX2", reg_id="R-AX2")
        self.build(pb)
        t_any = _expand_forward(forward_query(self.db_path, "R-ANY"))
        self.assertEqual(t_any[12][0], "MULTIPLE_SOURCE")
        self.assertEqual(set(t_any[12][1].keys()), {("AX1", 2), ("AX2", 2)})
        t_ax2 = _expand_forward(forward_query(self.db_path, "R-AX2"))
        self.assertEqual(t_ax2[12][0], "RESOLVED")
        self.assertEqual(list(t_ax2[12][1].keys()), [("AX2", 2)])

    # ------------------------------------------------------------------ #
    # 5) 同段两闭区间相接 -> 非法重叠，定位原行
    # ------------------------------------------------------------------ #
    def test_touching_closed_intervals_are_illegal_overlap(self):
        pb = PackageBuilder()
        pb.add_axis("AX", 20)
        pb.add_calib("S", "AX", "SEG", 0, 5, 100, +1, row_id="A")
        pb.add_calib("S", "AX", "SEG", 5, 9, 200, +1, row_id="B")  # 端点 5 相接
        # 不同段名相接合法
        pb.add_calib("S", "AX", "OTHER", 5, 9, 300, +1, row_id="C")
        pb.write(self.pkg_dir)
        with self.assertRaises(PackageValidationError) as ctx:
            build(self.pkg_dir, self.db_path)
        codes = {(e.location, e.code, e.row_id) for e in ctx.exception.errors}
        self.assertTrue(any(c == "SEGMENT_OVERLAP" and rid == "B"
                            for _, c, rid in codes))
        # 不同段名的 OTHER 不产生错误
        self.assertFalse(any(rid == "C" for _, _, rid in codes))
        self.assertFalse(os.path.exists(self.db_path))

    # ------------------------------------------------------------------ #
    # 6) 反向相遇：整数交点合并单列；非整数有理交点不合并
    # ------------------------------------------------------------------ #
    def test_reverse_intersection_integer_single_point(self):
        pb = PackageBuilder()
        pb.add_axis("AX", 40)
        # 段 P：x[10,18]，n=100+(x-10)=x+90 -> n 范围 100..108
        pb.add_calib("SRC", "AX", "P", 10, 18, 100, +1, row_id="P1")
        # 段 Q：反向，n=104 落在 x=14 => label_lo(x=10)=108；
        # n(x)=108-(x-10)=118-x，x[10,18] -> n 范围 108..100
        # 交点 x+90=118-x -> x=14（整数），n=104：唯一整数交点
        pb.add_calib("SRC", "AX", "Q", 10, 18, 108, -1, row_id="Q1")
        # 目标体系给个占位标定，保证体系存在
        pb.add_calib("SRCX", "AX", "Z", 0, 1, 1, +1, row_id="Z1")
        pb.add_reg("SRC", "SRCX", n_from=100, n_to=108, reg_id="R1")
        self.build(pb)
        res = forward_query(self.db_path, "R1")
        ticks = _expand_forward(res)
        # 104 单点：两源行合并到同一物理位置 x=14，证据两条 -> 位置数 1
        st, pos = ticks[104]
        self.assertEqual(list(pos.keys()), [("AX", 14)])
        self.assertEqual(pos[("AX", 14)]["source"], ("P1", "Q1"))
        # 两侧刻度（103、105）是两个独立位置
        self.assertEqual(len(ticks[103][1]), 2)
        self.assertEqual(len(ticks[105][1]), 2)
        # 104 必须单列成段，不与相邻段合并
        spans = [(s["source_label_lo"], s["source_label_hi"])
                 for s in res["segments"]]
        self.assertIn((104, 104), spans)
        # 交点两侧不连续相同，没有跨 104 的分段
        self.assertFalse(any(lo <= 104 <= hi and lo != hi
                             for lo, hi in spans))
        # 103 与 105 的结构相同（同为两位置、同样的两条源行），可压缩为两大段
        self.assertEqual(spans, [(100, 103), (104, 104), (105, 108)])

    def test_reverse_intersection_non_integer_not_merged(self):
        pb = PackageBuilder()
        pb.add_axis("AX", 40)
        # 构造半整数交点：P: n=x（x[0,10]，n 0..10）；
        # Q: n=10-(x-5)=15-x（x[5,15]，n 10..0）
        # 交点 x = 15-x -> x=7.5（有理非整数），整数刻度上永不重合
        pb.add_calib("SRC", "AX", "P", 0, 10, 0, +1, row_id="P1")
        pb.add_calib("SRC", "AX", "Q", 5, 15, 10, -1, row_id="Q1")
        pb.add_calib("SRCX", "AX", "Z", 0, 1, 1, +1, row_id="Z1")
        pb.add_reg("SRC", "SRCX", n_from=0, n_to=10, reg_id="R1")
        self.build(pb)
        ticks = _expand_forward(forward_query(self.db_path, "R1"))
        for n in range(0, 11):
            # 每个整数刻度都应保持两个不同物理位置（半整数交点不在整数刻度上）
            self.assertEqual(len(ticks[n][1]), 2, f"刻度 {n} 不应合并")
        # 逐行核对：P 的位置是 n，Q 的位置是 15-n，任何整数 n 二者都不相等
        for n in range(0, 11):
            positions = ticks[n][1]
            by_rows = {
                next(iter(v["source"])): k for k, v in positions.items()
            }
            self.assertEqual(by_rows["P1"], ("AX", n))
            self.assertEqual(by_rows["Q1"], ("AX", 15 - n))
            self.assertNotEqual(by_rows["P1"], by_rows["Q1"])

    # ------------------------------------------------------------------ #
    # 7) 随机包：参考程序全量对照
    # ------------------------------------------------------------------ #
    def test_randomized_oracle_crosscheck(self):
        rng = random.Random(20261120)
        for trial in range(8):
            pb = PackageBuilder(version=f"rand-{trial}")
            n_axes = rng.randint(1, 3)
            axes = [f"A{i}" for i in range(n_axes)]
            for ax in axes:
                pb.add_axis(ax, rng.randint(20, 60))
            systems = ["S0", "T0", "T1"]
            # 每个体系若干标定行，允许断链复号与跨轴复用段名
            seg_names = ["seg_a", "seg_b"]
            for sysname in systems:
                for _ in range(rng.randint(4, 9)):
                    ax = rng.choice(axes)
                    L = pb.axes[ax]
                    lo = rng.randint(0, L - 2)
                    hi = rng.randint(lo + 1, min(L, lo + rng.randint(2, 12)))
                    direction = rng.choice((+1, -1))
                    label_lo = rng.randint(-50, 50)
                    # 避免同 (轴,体系,段) 闭区间重叠：段名随机但行少，重叠则换名
                    seg = rng.choice(seg_names)
                    pb.add_calib(sysname, ax, seg, lo, hi, label_lo, direction)
            # 去掉会触发非法重叠的行：重新用工具校验，若失败则剥离冲突行
            pb = self._sanitize_overlaps(pb)
            target_systems = ["T0", "T1"]
            for _ in range(rng.randint(5, 10)):
                tgt = rng.choice(target_systems)
                n_from = rng.randint(-60, 60)
                n_to = n_from + rng.randint(0, 12)
                kw = {}
                if rng.random() < 0.4:
                    kw["axis_id"] = rng.choice(axes)
                if rng.random() < 0.3:
                    kw["segment"] = rng.choice(seg_names)
                try:
                    pb.add_reg("S0", tgt, n_from=n_from, n_to=n_to, **kw)
                except Exception:
                    continue
            # 引用类坏数据在 sanitize 后可能仍有未知段，validate 失败则整轮重建
            try:
                pb.write(self.pkg_dir)
                validate_only(self.pkg_dir)
            except PackageValidationError:
                continue
            if not pb.regs:
                continue
            build(self.pkg_dir, self.db_path)

            expected = _oracle_ticks(pb)
            for reg_id, exp_ticks in expected.items():
                result = forward_query(self.db_path, reg_id)
                got_ticks = _expand_forward(result)
                self.assertEqual(set(got_ticks), set(exp_ticks), reg_id)
                for n, (est, epos) in exp_ticks.items():
                    gst, gpos = got_ticks[n]
                    self.assertEqual(gst, est, (reg_id, n))
                    self.assertEqual(gpos.keys(), epos.keys(), (reg_id, n))
                    for key in epos:
                        self.assertEqual(gpos[key]["source"], epos[key]["source"],
                                         (reg_id, n, key))
                        self.assertEqual(gpos[key]["target"], epos[key]["target"],
                                         (reg_id, n, key))
                self._crosscheck_reverse(pb, reg_id)
            os.remove(self.db_path)

    def _crosscheck_reverse(self, pb, reg_id):
        calibs = [oracle.make_calib(r) for r in pb.calib]
        reg_raw = next(r for r in pb.regs if r["reg_id"] == reg_id)
        reg = oracle.make_reg(reg_raw)

        # 用参考程序枚举该登记每个刻度上真实出现的目标标牌，
        # 收集 (目标体系, 目标编号) 作为反查查询点——查询编号属于目标空间。
        query_points = set()
        for n in range(reg.n_from, reg.n_to + 1):
            tick = oracle.enumerate_tick(reg, n, calibs)
            for pos in tick.positions.values():
                for (seg, label) in pos["target"]:
                    query_points.add(label)
        # 再补几个肯定不存在的编号，验证空命中
        query_points.add(reg.n_from + 100000)

        for label in sorted(query_points):
            exp = oracle.reverse_oracle(
                reg, reg.target_system, label, label, calibs)
            got = reverse_query(self.db_path, reg.target_system, n=label)
            got_match_ns = {m["matched_source_label_lo"] for m in
                            got["matches"] if m["reg_id"] == reg_id}
            self.assertEqual(got_match_ns, set(exp.keys()),
                             (reg_id, "点查", label))
            for m in got["matches"]:
                if m["reg_id"] != reg_id:
                    continue
                n0 = m["matched_source_label_lo"]
                # 原状态必须与该刻度参考状态一致，反查不得改写
                self.assertEqual(
                    m["original_status"],
                    oracle.enumerate_tick(reg, n0, calibs).status,
                    (reg_id, n0, label))
                # 命中刻度必须是单点（点号查询），且候选位置与参考完全一致
                self.assertEqual(
                    m["matched_source_label_lo"],
                    m["matched_source_label_hi"])
                exp_pos = {
                    (p["axis_id"], p["x"]):
                        (tuple(p["source_rows"]),
                         tuple((seg, label, tuple(rows))
                               for seg, label, rows in p["target"]))
                    for p in exp[n0]
                }
                got_pos = {}
                for p in m["positions"]:
                    x = p["x_lo"]  # 单点区间，x_lo==x_hi
                    got_pos[(p["axis_id"], x)] = (
                        tuple(sorted(r["row_id"] for r in p["source_rows"])),
                        tuple(sorted(
                            (e["target_segment"], e["target_label_lo"],
                             tuple(sorted(r["row_id"]
                                          for r in e["target_rows"])))
                            for e in p["target_evidence"])))
                self.assertEqual(got_pos, exp_pos, (reg_id, n0, label))

        # 区间反查：取已有点位的最小/最大编号做闭区间
        if len(query_points) >= 2:
            pts = sorted(l for l in query_points if l < reg.n_from + 100000)
            q_lo, q_hi = pts[0], pts[-1]
            exp = oracle.reverse_oracle(
                reg, reg.target_system, q_lo, q_hi, calibs)
            got = reverse_query(self.db_path, reg.target_system,
                                n_from=q_lo, n_to=q_hi)
            got_match_ns = set()
            for m in got["matches"]:
                if m["reg_id"] != reg_id:
                    continue
                # 区间查询命中的是源刻度闭区间，逐刻度都应在参考命中集合内
                for n0 in range(m["matched_source_label_lo"],
                                m["matched_source_label_hi"] + 1):
                    got_match_ns.add(n0)
                    self.assertEqual(
                        m["original_status"],
                        oracle.enumerate_tick(reg, n0, calibs).status)
            self.assertEqual(got_match_ns, set(exp.keys()),
                             (reg_id, "区间查", q_lo, q_hi))

    def _sanitize_overlaps(self, pb: PackageBuilder) -> PackageBuilder:
        """剥离同 (轴,体系,段) 闭区间重叠的标定行，保留合法断链复号。"""
        keep = []
        seen = {}
        for r in pb.calib:
            key = (r["axis_id"], r["system"], r["segment"])
            lo, hi = r["lo"], r["hi"]
            conflict = False
            for (plo, phi) in seen.get(key, []):
                if not (hi < plo or lo > phi):  # 闭区间重叠（含相接）
                    conflict = True
                    break
            if conflict:
                continue
            seen.setdefault(key, []).append((lo, hi))
            keep.append(r)
        pb.calib = keep
        return pb

    # ------------------------------------------------------------------ #
    # 8) 校验错误定位
    # ------------------------------------------------------------------ #
    def test_validation_errors_are_located(self):
        pb = PackageBuilder()
        pb.add_axis("AX", 10)
        pb.add_calib("S", "AX", "P", 0, 4, 100, +1, row_id="OK1")
        pb.write(self.pkg_dir, bad_calib_lines=[
            json.dumps({"row_id": "OK1", "system": "S", "axis_id": "AX",
                        "segment": "P", "lo": 1, "hi": 2,
                        "label_lo": 1, "direction": 1}),  # 重复 id
            '{"row_id": "BADJSON',  # 坏 JSON
            json.dumps({"row_id": "BAD1", "system": "S", "axis_id": "GHOST",
                        "segment": "P", "lo": 0, "hi": 1,
                        "label_lo": 1, "direction": 1}),  # 未知轴
            json.dumps({"row_id": "BAD2", "system": "S", "axis_id": "AX",
                        "segment": "P", "lo": 4, "hi": 0,
                        "label_lo": 1, "direction": 1}),  # lo>=hi
            json.dumps({"row_id": "BAD3", "system": "S", "axis_id": "AX",
                        "segment": "P", "lo": 0, "hi": 1,
                        "label_lo": 1, "direction": 0}),  # 坏方向
        ])
        with self.assertRaises(PackageValidationError) as ctx:
            build(self.pkg_dir, self.db_path)
        errs = ctx.exception.errors
        codes = {e.code for e in errs}
        self.assertIn("DUPLICATE_ROW_ID", codes)
        self.assertIn("BAD_JSON", codes)
        self.assertIn("UNKNOWN_AXIS", codes)
        self.assertIn("BAD_FIELD", codes)
        # 物理行号定位：坏 JSON 是标定表第 3 行（OK1 在 1，重复在 2）
        locs = {e.location for e in errs}
        self.assertIn("calibration.jsonl:3", locs)
        self.assertTrue(all(l.startswith("calibration.jsonl:") for l in locs))

    def test_int64_overflow_rejected(self):
        pb = PackageBuilder()
        pb.add_axis("AX", 10)
        huge = 2 ** 63 - 1
        pb.add_calib("S", "AX", "P", 0, 4, huge, +1, row_id="OVF")
        pb.write(self.pkg_dir)
        with self.assertRaises(PackageValidationError) as ctx:
            build(self.pkg_dir, self.db_path)
        self.assertTrue(any(e.code == "INT64_OVERFLOW"
                            for e in ctx.exception.errors))

    # ------------------------------------------------------------------ #
    # 9) 失败不覆盖旧库 + CLI 冒烟
    # ------------------------------------------------------------------ #
    def test_failed_build_keeps_existing_db(self):
        pb_good = PackageBuilder()
        pb_good.add_axis("AX", 10)
        pb_good.add_calib("S", "AX", "P", 0, 4, 100, +1, row_id="P1")
        pb_good.add_calib("T", "AX", "Q", 0, 4, 200, +1, row_id="Q1")
        pb_good.add_reg("S", "T", n=101, reg_id="R1")
        self.build(pb_good)
        good_mtime = os.path.getmtime(self.db_path)

        pb_bad = PackageBuilder()
        pb_bad.add_axis("AX", 10)
        pb_bad.add_calib("S", "AX", "P", 0, 4, 100, +1, row_id="X1")
        pb_bad.add_calib("S", "AX", "P", 4, 8, 200, +1, row_id="X2")  # 相接重叠
        pb_bad.write(self.pkg_dir)
        with self.assertRaises(PackageValidationError):
            build(self.pkg_dir, self.db_path)
        self.assertTrue(os.path.exists(self.db_path))
        self.assertEqual(os.path.getmtime(self.db_path), good_mtime)
        # 旧库仍可查询
        res = forward_query(self.db_path, "R1")
        self.assertTrue(res["segments"])
        self.assertFalse(os.path.exists(self.db_path + ".tmp"))

    def test_cli_smoke(self):
        pb = self._build_spec_axis()
        pb.write(self.pkg_dir)
        env = dict(os.environ, PYTHONPATH=PROJECT_ROOT)
        run = lambda *a: subprocess.run(
            [sys.executable, "-m", "chainage", *a],
            cwd=PROJECT_ROOT, capture_output=True, text=True, env=env, check=True)

        out = run("validate", self.pkg_dir)
        self.assertIn("calibration_rows", out.stdout)
        out = run("build", self.pkg_dir, self.db_path)
        self.assertIn("segments", out.stdout)
        out = run("forward", self.db_path, "R103", "--text")
        self.assertIn("多个源位置", out.stdout)
        out = run("reverse", self.db_path, "NEW", "--n", "899", "--text")
        self.assertIn("MULTIPLE_SOURCE", out.stdout)
        # JSON 默认输出可解析
        out = run("reverse", self.db_path, "NEW", "--n", "899")
        payload = json.loads(out.stdout)
        self.assertEqual(payload["matches"][0]["original_status"],
                         "MULTIPLE_SOURCE")
        out = run("plan", self.db_path, "NEW", "--n", "899")
        plan_text = out.stdout
        self.assertIn("idx_target", plan_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
