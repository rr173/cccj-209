"""病害归并工作区验收测试。

覆盖
====
1. 题目主样例：A、B、C 三报件，A同B、B同C、A不同C -> 组 A 三件、隔离，
   隔离证据的同一路径必须经过 B；撤去 B同C 后组 A 只剩 A、B 且可下发，
   新增可下发单件组 C，只有 C 的成员行因换组而变；A不同C 仍在但不隔离。
2. 下一批加回 B同C 并同时撤去 A不同C：组 C 撤出，组 A 纳回 C，且任何
   增量批里都不出现"临时隔离"的中间态。
3. 有其他路径时撤一条边不拆组；撤下再恢复中间报件会重新启用旧裁定。
4. 换算出现复号时（多源位置）不乱发候选；登记不存在/编号越界是输入错误。
5. 旧批重试、坏批回滚、两个写入者基于同一修订提交时没有丢更正。
6. 候选阈值为闭区间，按轴/空间/时间剔除无关报件，命中与实际扫描量分开。
7. 局部更正只触及相关组；扩大无关数据后受影响组不变。
8. 独立脚本逐批重建全图，与产品全量/增量重放互校。
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

from chainage.build import build  # noqa: E402
from chainage.defects import (  # noqa: E402
    BATCH_FORMAT,
    LOC_LOCALIZABLE,
    LOC_MULTIPLE,
    apply_batch,
    candidate_pairs,
    create_workspace,
    fetch_changes,
    fetch_full,
    group_detail,
    isolation_evidence,
    list_adjudications,
    list_groups,
    list_reports,
    workspace_info,
)
from chainage.errors import DefectValidationError  # noqa: E402
import defect_oracle as oracle  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# 夹具：ready 换算库 + 工作区 + 批文件
# --------------------------------------------------------------------------- #
class WorkspaceFixture:
    """一条 100 刻度的旧线 OLD 与两条新线（可制造同号/断链/复号）。"""

    def __init__(self, root: str):
        self.root = root
        self.pkg_dir = os.path.join(root, "pkg")
        os.makedirs(self.pkg_dir, exist_ok=True)
        self.db_path = os.path.join(root, "chainage.db")
        self.ws_dir = os.path.join(root, "workspace")
        self.axes = {}
        self.calib = []
        self.regs = []
        self._cseq = 0
        self._rseq = 0
        self._batch_seq = 0

    def axis(self, axis_id, length):
        self.axes[axis_id] = length
        return self

    def calib_row(self, system, axis_id, segment, lo, hi, label_lo, direction,
                  row_id=None):
        self._cseq += 1
        row_id = row_id or f"C{self._cseq:03d}"
        self.calib.append({
            "row_id": row_id, "system": system, "axis_id": axis_id,
            "segment": segment, "lo": lo, "hi": hi,
            "label_lo": label_lo, "direction": direction})
        return self

    def reg(self, src, tgt, n_from, n_to, axis_id=None, segment=None,
            reg_id=None):
        self._rseq += 1
        reg_id = reg_id or f"R{self._rseq:03d}"
        row = {"reg_id": reg_id, "source_system": src,
               "target_system": tgt, "from": n_from, "to": n_to}
        if axis_id is not None:
            row["axis_id"] = axis_id
        if segment is not None:
            row["segment"] = segment
        self.regs.append(row)
        return self

    def build(self):
        manifest = {"version": "defect-fixture",
                    "axes": [{"axis_id": a, "length": l}
                             for a, l in self.axes.items()]}
        with open(os.path.join(self.pkg_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)
        with open(os.path.join(self.pkg_dir, "calibration.jsonl"), "w",
                  encoding="utf-8") as fh:
            for r in self.calib:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(os.path.join(self.pkg_dir, "registration.jsonl"), "w",
                  encoding="utf-8") as fh:
            for r in self.regs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        build(self.pkg_dir, self.db_path)
        return self

    def workspace(self, spatial=3, time=100):
        create_workspace(self.ws_dir, self.db_path, spatial, time)
        return self

    @property
    def workspace_id(self):
        return workspace_info(self.ws_dir)["workspace_id"]

    def report(self, source, ref, reg_n, found_at, category="pothole",
               lane="L1", reg_id="R1"):
        return {"source": source, "source_ref": ref, "category": category,
                "lane": lane, "found_at": found_at, "reg_id": reg_id,
                "reg_n": reg_n}

    def adj(self, a, b, decision):
        return {"a": {"source": a[0], "source_ref": a[1]},
                "b": {"source": b[0], "source_ref": b[1]},
                "decision": decision}

    def write_batch(self, base_revision, reports=None, void=None,
                    adjudications=None, batch_no=None, name=None):
        self._batch_seq += 1
        batch_no = batch_no or f"B{self._batch_seq:03d}"
        batch = {
            "format": BATCH_FORMAT,
            "batch_no": batch_no,
            "based_on_workspace": self.workspace_id,
            "based_on_revision": base_revision,
            "reports": reports or [],
            "void": void or [],
            "adjudications": adjudications or [],
        }
        path = os.path.join(self.root, name or f"{batch_no}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(batch, fh, ensure_ascii=False, indent=2)
        return path, batch

    def apply(self, base_revision, **kwargs):
        path, batch = self.write_batch(base_revision, **kwargs)
        out = apply_batch(self.ws_dir, path)
        return out, batch

    def full(self):
        return fetch_full(self.ws_dir)


def _member_map(full):
    return {(m["source"], m["source_ref"]): m for m in full["defect_members"]}


def _group_map(full):
    return {g["group_key"]: g for g in full["defect_groups"]}


def _key(source, ref):
    return json.dumps([source, ref], ensure_ascii=False,
                      separators=(",", ":"))


# --------------------------------------------------------------------------- #
# 主样例
# --------------------------------------------------------------------------- #
class MainExampleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 100)
                   .calib_row("OLD", "A1", "S", 0, 60, 1000, +1,
                              row_id="S1")
                   .calib_row("NEW", "A1", "G", 0, 60, 2000, +1,
                              row_id="G1")
                   .reg("OLD", "NEW", 1000, 1060, reg_id="R1")
                   .build()
                   .workspace(spatial=3, time=100))
        self.wid = self.fx.workspace_id

    def tearDown(self):
        self.tmp.cleanup()

    def _abc_batch(self):
        return dict(
            reports=[self.fx.report("SYS", "A", 1000, 100),
                     self.fx.report("SYS", "B", 1000, 200),
                     self.fx.report("SYS", "C", 1005, 900)],
            adjudications=[
                self.fx.adj(("SYS", "A"), ("SYS", "B"), "same"),
                self.fx.adj(("SYS", "B"), ("SYS", "C"), "same"),
                self.fx.adj(("SYS", "A"), ("SYS", "C"), "different"),
            ])

    def test_isolated_group_and_evidence_path_through_b(self):
        out, _ = self.fx.apply(0, **self._abc_batch())
        self.assertEqual(out["receipt"]["revision"], 1)
        full = self.fx.full()
        gk = _key("SYS", "A")
        groups = _group_map(full)
        self.assertEqual(list(groups), [gk])
        self.assertTrue(groups[gk]["isolated"])
        self.assertEqual(groups[gk]["member_count"], 3)
        self.assertEqual(groups[gk]["first_found"], 100)
        self.assertEqual(groups[gk]["last_found"], 900)
        # 每个成员都指向同一组键
        self.assertEqual({m["group_key"] for m in full["defect_members"]},
                         {gk})
        # 隔离证据：只有 A 与 C 的不同裁定；同一路径必须经过 B
        ev = isolation_evidence(self.fx.ws_dir, gk)
        self.assertTrue(ev["isolated"])
        self.assertEqual(len(ev["different_edges"]), 1)
        edge = ev["different_edges"][0]
        self.assertEqual({tuple(x) for x in edge["between"]},
                         {("SYS", "A"), ("SYS", "C")})
        path = [tuple(x) for x in edge["same_path"]]
        self.assertEqual(path[0], ("SYS", "A"))
        self.assertEqual(path[-1], ("SYS", "C"))
        self.assertIn(("SYS", "B"), path)
        # 隔离成员仍可复核：组详情包含全部成员与证据
        detail = group_detail(self.fx.ws_dir, gk)
        self.assertEqual(len(detail["members"]), 3)
        self.assertEqual(detail["isolation_evidence"], ev)

    def test_clear_same_edge_splits_group_and_only_c_moves(self):
        self.fx.apply(0, **self._abc_batch())
        before = self.fx.full()
        out, _ = self.fx.apply(
            1, adjudications=[
                self.fx.adj(("SYS", "B"), ("SYS", "C"), "clear")])
        after = self.fx.full()
        gk_a, gk_c = _key("SYS", "A"), _key("SYS", "C")
        groups = _group_map(after)
        self.assertFalse(groups[gk_a]["isolated"])
        self.assertEqual(groups[gk_a]["member_count"], 2)
        self.assertEqual(groups[gk_c]["member_count"], 1)
        self.assertFalse(groups[gk_c]["isolated"])
        # A不同C 仍在，但两端已分属不同组，不隔离任何组
        adjs = list_adjudications(self.fx.ws_dir)["adjudications"]
        diff = [a for a in adjs if a["kind"] == "different"]
        self.assertEqual(len(diff), 1)
        self.assertTrue(diff[0]["effective"])
        self.assertFalse(any(g["isolated"] for g in groups.values()))
        # 只有 C 的成员行因换组而变
        bm, am = _member_map(before), _member_map(after)
        changed = [k for k in am if bm[k] != am[k]]
        self.assertEqual(changed, [("SYS", "C")])
        # 增量如实反映：A 组变、C 组新增、C 成员变
        inc = fetch_changes(self.fx.ws_dir, 1)["batches"]
        self.assertEqual(len(inc), 1)
        ops = {(c["table"], c["op"]) for c in inc[0]["changes"]}
        self.assertIn(("defect_group", "~"), ops)
        self.assertIn(("defect_group", "+"), ops)
        self.assertIn(("defect_member", "~"), ops)
        self.assertEqual(
            [tuple(c["key"]) for c in inc[0]["changes"]
             if c["table"] == "defect_member"], [("SYS", "C")])

    def test_readd_and_clear_never_emits_temporary_isolation(self):
        # 第 1 批：只录入 A、B、C 和 A同B、B同C（尚无任何不同裁定）
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "A", 1000, 100),
                               self.fx.report("SYS", "B", 1000, 200),
                               self.fx.report("SYS", "C", 1005, 900)],
                      adjudications=[
                          self.fx.adj(("SYS", "A"), ("SYS", "B"), "same"),
                          self.fx.adj(("SYS", "B"), ("SYS", "C"), "same")])
        # 第 2 批：加 A不同C -> 隔离
        self.fx.apply(1, adjudications=[
            self.fx.adj(("SYS", "A"), ("SYS", "C"), "different")])
        # 第 3 批：撤去 B同C -> 组 A 两件可下发 + 单件组 C
        self.fx.apply(2, adjudications=[
            self.fx.adj(("SYS", "B"), ("SYS", "C"), "clear")])
        # 第 4 批：同时加回 B同C 并撤去 A不同C -> 组 A 纳回 C，从不临时隔离
        self.fx.apply(3, adjudications=[
            self.fx.adj(("SYS", "B"), ("SYS", "C"), "same"),
            self.fx.adj(("SYS", "A"), ("SYS", "C"), "clear")])
        groups = _group_map(self.fx.full())
        gk = _key("SYS", "A")
        self.assertEqual(list(groups), [gk])
        self.assertEqual(groups[gk]["member_count"], 3)
        self.assertFalse(groups[gk]["isolated"])
        # 只有第 2 批（显式加 A不同C）出现隔离；第 3、4 批都不得泄漏中间隔离
        for batch in fetch_changes(self.fx.ws_dir, 0)["batches"]:
            if batch["revision"] == 2:
                continue
            for c in batch["changes"]:
                if c["table"] != "defect_group":
                    continue
                self.assertFalse(c["after"] and c["after"]["isolated"],
                                 f"批 {batch['batch_no']} 泄漏临时隔离")

    def test_clearing_one_edge_with_other_path_does_not_split(self):
        # A-B-C 三角都同为同一边；再加 A-D；撤 A-B 仍有 A-C-B 连通
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "A", 1000, 100),
                               self.fx.report("SYS", "B", 1000, 200),
                               self.fx.report("SYS", "C", 1005, 900)],
                      adjudications=[
                          self.fx.adj(("SYS", "A"), ("SYS", "B"), "same"),
                          self.fx.adj(("SYS", "B"), ("SYS", "C"), "same"),
                          self.fx.adj(("SYS", "A"), ("SYS", "C"), "same")])
        self.fx.apply(1, adjudications=[
            self.fx.adj(("SYS", "A"), ("SYS", "B"), "clear")])
        groups = _group_map(self.fx.full())
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[_key("SYS", "A")]["member_count"], 3)

    def test_merge_then_split_keeps_isolation_and_evidence_valid(self):
        # 两个链 A-B-C-D 与 E-F-G；用 D同E 合并，再用 B不同F 隔离
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", x, 1000 + i, 100)
                               for i, x in
                               enumerate("ABCDEFG")],
                      adjudications=[
                          self.fx.adj(("SYS", "A"), ("SYS", "B"), "same"),
                          self.fx.adj(("SYS", "B"), ("SYS", "C"), "same"),
                          self.fx.adj(("SYS", "C"), ("SYS", "D"), "same"),
                          self.fx.adj(("SYS", "E"), ("SYS", "F"), "same"),
                          self.fx.adj(("SYS", "F"), ("SYS", "G"), "same")])
        self.assertEqual(set(_group_map(self.fx.full())),
                         {_key("SYS", "A"), _key("SYS", "E")})
        self.fx.apply(1, adjudications=[
            self.fx.adj(("SYS", "D"), ("SYS", "E"), "same"),
            self.fx.adj(("SYS", "B"), ("SYS", "F"), "different")])
        groups = _group_map(self.fx.full())
        self.assertEqual(list(groups), [_key("SYS", "A")])
        self.assertEqual(groups[_key("SYS", "A")]["member_count"], 7)
        self.assertTrue(groups[_key("SYS", "A")]["isolated"])
        # 撤 A同B：A 拆出单件，其余 6 件仍同组且仍隔离（B不同F 两端都在）
        self.fx.apply(2, adjudications=[
            self.fx.adj(("SYS", "A"), ("SYS", "B"), "clear")])
        groups = _group_map(self.fx.full())
        self.assertEqual(set(groups), {_key("SYS", "A"), _key("SYS", "B")})
        self.assertEqual(groups[_key("SYS", "A")]["member_count"], 1)
        self.assertFalse(groups[_key("SYS", "A")]["isolated"])
        self.assertEqual(groups[_key("SYS", "B")]["member_count"], 6)
        self.assertTrue(groups[_key("SYS", "B")]["isolated"])
        ev = isolation_evidence(self.fx.ws_dir, _key("SYS", "B"))
        edge = ev["different_edges"][0]
        path = [tuple(x) for x in edge["same_path"]]
        self.assertEqual(path[0], ("SYS", "B"))
        self.assertEqual(path[-1], ("SYS", "F"))
        self.assertEqual(path, [("SYS", "B"), ("SYS", "C"), ("SYS", "D"),
                                ("SYS", "E"), ("SYS", "F")])

    def test_void_and_reenter_reactivates_old_adjudication(self):
        self.fx.apply(0, **self._abc_batch())
        # 撤下 B：组拆成 A、C 两件；A不同C 仍存在（两端都在册才生效）
        self.fx.apply(1, void=[{"source": "SYS", "source_ref": "B"}])
        groups = _group_map(self.fx.full())
        self.assertEqual(set(groups), {_key("SYS", "A"), _key("SYS", "C")})
        self.assertFalse(any(g["isolated"] for g in groups.values()))
        # 重新录入同键 B：旧的 A同B、B同C 重新参与，组恢复三件且隔离
        self.fx.apply(2, reports=[self.fx.report("SYS", "B", 1000, 200)])
        groups = _group_map(self.fx.full())
        self.assertEqual(list(groups), [_key("SYS", "A")])
        self.assertEqual(groups[_key("SYS", "A")]["member_count"], 3)
        self.assertTrue(groups[_key("SYS", "A")]["isolated"])
        # 撤下 B 时 A/C 各自单件，但裁定不删除
        adjs = list_adjudications(self.fx.ws_dir,
                                  effective_only=False)["adjudications"]
        self.assertEqual(len(adjs), 3)
        self.assertTrue(all(a["effective"] for a in adjs))

    def test_void_keeps_adjudication_but_group_isolates_only_both_present(self):
        self.fx.apply(0, **self._abc_batch())
        self.fx.apply(1, void=[{"source": "SYS", "source_ref": "C"}])
        # A、B 同组两件且不隔离（A不同C 有一端已撤下，不生效）
        groups = _group_map(self.fx.full())
        self.assertEqual(list(groups), [_key("SYS", "A")])
        self.assertEqual(groups[_key("SYS", "A")]["member_count"], 2)
        self.assertFalse(groups[_key("SYS", "A")]["isolated"])
        # 隔离证据里不得出现已撤下报件
        ev = isolation_evidence(self.fx.ws_dir, _key("SYS", "A"))
        self.assertFalse(ev["isolated"])
        self.assertEqual(ev["different_edges"], [])


# --------------------------------------------------------------------------- #
# 定位状态与输入错误
# --------------------------------------------------------------------------- #
class LocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 100)
                   .axis("A2", 100)
                   # 同轴两条 OLD 段在物理 10 同号（断链复号）：一处登记
                   # 在 n=1010 上有两个物理位置
                   .calib_row("OLD", "A1", "S", 0, 5, 1000, +1, row_id="S1")
                   .calib_row("OLD", "A1", "T", 10, 20, 1010, -1,
                              row_id="T1")
                   .calib_row("NEW", "A1", "G", 0, 60, 2000, +1,
                              row_id="G1")
                   # 另一轴 U 有编号 1000..1100
                   .calib_row("OLD", "A2", "U", 0, 60, 1000, +1,
                              row_id="U1")
                   .calib_row("NEW", "A2", "V", 0, 60, 3000, +1,
                              row_id="V1")
                   .reg("OLD", "NEW", 1000, 1030, axis_id="A1",
                        reg_id="R1")
                   .reg("OLD", "NEW", 1040, 1100, axis_id="A2",
                        reg_id="R2")
                   .build()
                   .workspace(spatial=5, time=100))
        self.wid = self.fx.workspace_id

    def tearDown(self):
        self.tmp.cleanup()

    def test_multi_position_report_enters_but_is_not_a_candidate(self):
        # n=1000 在 A1 上同时命中 S 段 x=0 与 T 段 x=20（两个物理位置）
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "M", 1000, 0),
                               self.fx.report("SYS", "N", 1001, 0)])
        reps = {r["source_ref"]: r for r in
                list_reports(self.fx.ws_dir)["reports"]}
        self.assertEqual(reps["M"]["location"]["status"], LOC_MULTIPLE)
        self.assertEqual(reps["M"]["location"]["position_count"], 2)
        rep = candidate_pairs(self.fx.ws_dir, "SYS", "M")
        self.assertFalse(rep["eligible"])
        self.assertEqual(rep["candidates"], [])
        # 多位置报件仍可入仓、可人工裁定
        self.fx.apply(1, adjudications=[
            self.fx.adj(("SYS", "M"), ("SYS", "N"), "same")])
        full = self.fx.full()
        self.assertEqual(len(full["defect_members"]), 2)
        self.assertEqual(full["defect_groups"][0]["member_count"], 2)

    def test_high_number_on_separate_axis_stays_localizable(self):
        # R2 只在 A2 上，n=1041 在 A2 上恰有一个物理位置
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "P", 1041, 0,
                                              reg_id="R2")])
        reps = list_reports(self.fx.ws_dir)["reports"]
        self.assertEqual(reps[0]["location"]["status"], LOC_LOCALIZABLE)
        self.assertEqual(reps[0]["location"]["axis_id"], "A2")

    def test_unknown_registration_is_input_error(self):
        with self.assertRaises(DefectValidationError) as cm:
            self.fx.apply(0, reports=[self.fx.report("SYS", "X", 1000, 0,
                                                     reg_id="NOPE")])
        self.assertEqual(cm.exception.issues[0].code, "UNKNOWN_REGISTRATION")
        self.assertEqual(workspace_info(self.fx.ws_dir)["revision"], 0)

    def test_out_of_range_is_input_error(self):
        with self.assertRaises(DefectValidationError) as cm:
            self.fx.apply(0, reports=[self.fx.report("SYS", "X", 1031, 0,
                                                     reg_id="R1")])
        self.assertEqual(cm.exception.issues[0].code,
                         "N_OUT_OF_REGISTRATION")
        self.assertEqual(workspace_info(self.fx.ws_dir)["revision"], 0)

    def test_candidates_skip_multi_and_other_axis(self):
        # R1 在 A1 上 1000-1030；S 只到 1005，故 1006 起只在 T 上唯一
        # n=1000 同时命中 S 与 T（多位置）；1006 只在 T 上（唯一）
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "A", 1006, 0),
                               self.fx.report("MAP", "B", 1010, 0),
                               self.fx.report("SYS", "M", 1000, 0),
                               self.fx.report("SYS", "P", 1041, 0,
                                              reg_id="R2")])
        reps = {r["source_ref"]: r for r in
                list_reports(self.fx.ws_dir)["reports"]}
        # 显式确认起点可定位（而不是想当然）
        self.assertEqual(reps["A"]["location"]["status"], LOC_LOCALIZABLE)
        # M 是多位置，P 在另一轴；都不应出现
        cands = candidate_pairs(self.fx.ws_dir, "SYS", "A")
        got = {(c["other"]["source"], c["other"]["source_ref"])
               for c in cands["candidates"]}
        self.assertIn(("MAP", "B"), got)
        self.assertNotIn(("SYS", "M"), got)
        self.assertNotIn(("SYS", "P"), got)


# --------------------------------------------------------------------------- #
# 候选语义
# --------------------------------------------------------------------------- #
class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 1000)
                   .calib_row("OLD", "A1", "S", 0, 900, 1000, +1,
                              row_id="S1")
                   .calib_row("NEW", "A1", "G", 0, 900, 2000, +1,
                              row_id="G1")
                   .reg("OLD", "NEW", 1000, 1900, reg_id="R1")
                   .build()
                   .workspace(spatial=5, time=60))
        self.wid = self.fx.workspace_id

    def tearDown(self):
        self.tmp.cleanup()

    def test_thresholds_are_closed_and_same_stake_not_a_reason(self):
        reports = [
            self.fx.report("SYS", "A", 1000, 1000, category="pothole"),
            # 端点命中：x=5、t=1060
            self.fx.report("BI", "X", 1005, 1060, category="pothole"),
            self.fx.report("BI", "Y", 1010, 1000, category="pothole"),
            self.fx.report("BI", "Z", 1000, 1061, category="pothole"),
            # 同来源不算跨来源
            self.fx.report("SYS", "S", 1000, 1000, category="pothole"),
            # 不同类别/车道不算
            self.fx.report("BI", "C", 1000, 1000, category="crack"),
            self.fx.report("BI", "L", 1000, 1000, lane="L2"),
        ]
        self.fx.apply(0, reports=reports)
        out = candidate_pairs(self.fx.ws_dir, "SYS", "A",
                              explain_scan=True)
        got = {c["other"]["source_ref"] for c in out["candidates"]}
        self.assertEqual(got, {"X"})
        c = out["candidates"][0]
        self.assertEqual(c["spatial_delta"], 5)
        self.assertEqual(c["time_delta"], 60)
        self.assertEqual(c["other"]["registration"]["reg_n"], 1005)

    def test_same_defect_group_does_not_imply_direct_adjudication(self):
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "A", 1000, 1000),
                               self.fx.report("BI", "X", 1000, 1000),
                               self.fx.report("BI", "Y", 1002, 1000)],
                      adjudications=[
                          self.fx.adj(("SYS", "A"), ("BI", "X"), "same"),
                          self.fx.adj(("BI", "X"), ("BI", "Y"), "same")])
        # A 与 Y 同处一个病害组，但没有直接裁定 -> 仍是候选
        out = candidate_pairs(self.fx.ws_dir, "SYS", "A")
        got = {c["other"]["source_ref"] for c in out["candidates"]}
        self.assertIn("Y", got)
        # 已有直接裁定的 X 不再出现在候选
        self.assertNotIn("X", got)
        # 候选查询不改变病害身份
        before = self.fx.full()
        candidate_pairs(self.fx.ws_dir, "BI", "Y")
        self.assertEqual(before, self.fx.full())

    def test_cleared_adjudication_returns_to_candidate_list(self):
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "A", 1000, 1000),
                               self.fx.report("BI", "X", 1000, 1000)],
                      adjudications=[
                          self.fx.adj(("SYS", "A"), ("BI", "X"), "same")])
        # 已生效的直接裁定使该对不再列为候选
        self.assertEqual(
            candidate_pairs(self.fx.ws_dir, "SYS", "A")["candidates"], [])
        self.fx.apply(1, adjudications=[
            self.fx.adj(("SYS", "A"), ("BI", "X"), "clear")])
        # 撤去裁定后该对回到待核对状态，再次出现在候选里
        out = candidate_pairs(self.fx.ws_dir, "SYS", "A")
        self.assertEqual([c["other"]["source_ref"] for c in out["candidates"]],
                         ["X"])
        # 手工裁定可以超出候选条件 / 未定位，不使用候选过滤
        self.fx.apply(2, reports=[self.fx.report("BI", "Y", 1090, 9999)],
                      adjudications=[
                          self.fx.adj(("SYS", "A"), ("BI", "X"), "same"),
                          self.fx.adj(("SYS", "A"), ("BI", "Y"), "same")])
        groups = self.fx.full()["defect_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["member_count"], 3)

    def test_scan_report_separates_hits_from_rows_read(self):
        reports = [self.fx.report("SYS", "A", 1000, 1000)]
        for i in range(200):
            reports.append(self.fx.report(
                "BULK", f"F{i:03d}", 1700 + (i % 150), 5000 + i * 10,
                category="crack" if i % 2 else "pothole"))
        for i in range(50):
            reports.append(self.fx.report(
                "BULK", f"G{i:03d}", 1000 + i * 5, 1000))
        # 时间窗内的同桶同轴报件只有 50 条，命中 1 条（i=0 处 1000）
        self.fx.apply(0, reports=reports)
        out = candidate_pairs(self.fx.ws_dir, "SYS", "A",
                              explain_scan=True)
        sc = out["scan"]
        self.assertGreater(sc["bucket_rows"], sc["space_time_preselected"])
        self.assertEqual(sc["candidates"], len(out["candidates"]))
        self.assertLess(sc["space_time_preselected"], 55)
        self.assertTrue(all(c["time_delta"] <= 60 for c in out["candidates"]))
        self.assertTrue(all(c["spatial_delta"] <= 5 for c in out["candidates"]))

    def test_threshold_override_does_not_change_workspace(self):
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "A", 1000, 1000),
                               self.fx.report("BI", "X", 1020, 1000)])
        base = candidate_pairs(self.fx.ws_dir, "SYS", "A")
        self.assertEqual(base["candidates"], [])
        wider = candidate_pairs(self.fx.ws_dir, "SYS", "A",
                                 spatial_threshold=50)
        self.assertEqual(len(wider["candidates"]), 1)
        # 工作区阈值不变
        self.assertEqual(workspace_info(self.fx.ws_dir)["spatial_threshold"], 5)
        self.assertEqual(candidate_pairs(self.fx.ws_dir, "SYS", "A"),
                         base)


# --------------------------------------------------------------------------- #
# 批、修订与回滚
# --------------------------------------------------------------------------- #
class BatchRevisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 100)
                   .calib_row("OLD", "A1", "S", 0, 60, 1000, +1,
                              row_id="S1")
                   .calib_row("NEW", "A1", "G", 0, 60, 2000, +1,
                              row_id="G1")
                   .reg("OLD", "NEW", 1000, 1060, reg_id="R1")
                   .build()
                   .workspace(spatial=3, time=100))
        self.wid = self.fx.workspace_id

    def tearDown(self):
        self.tmp.cleanup()

    def test_retry_replays_receipt_even_after_later_batches(self):
        path, batch = self.fx.write_batch(
            0, reports=[self.fx.report("SYS", "A", 1000, 100)],
            batch_no="B_retry")
        first = apply_batch(self.fx.ws_dir, path)
        self.assertFalse(first["replayed"])
        # 中间再提交另一批
        self.fx.apply(1, reports=[self.fx.report("SYS", "B", 1005, 200)])
        self.assertEqual(workspace_info(self.fx.ws_dir)["revision"], 2)
        # 原样重试旧批：只回放原收据，不重新执行，不产生修订
        again = apply_batch(self.fx.ws_dir, path)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["receipt"], first["receipt"])
        self.assertEqual(workspace_info(self.fx.ws_dir)["revision"], 2)

    def test_reordered_batch_replays_same_content(self):
        path, batch = self.fx.write_batch(
            0, reports=[self.fx.report("SYS", "A", 1000, 100)],
            batch_no="B_order")
        first = apply_batch(self.fx.ws_dir, path)
        # 同一操作内容：报件字段次序打乱、裁定两端颠倒（这里只有报件）
        reordered = json.loads(json.dumps(batch))
        reordered["reports"][0] = dict(
            reversed(list(reordered["reports"][0].items())))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(reordered, fh, ensure_ascii=False)
        again = apply_batch(self.fx.ws_dir, path)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["receipt"], first["receipt"])

    def test_same_batch_no_different_content_rejected(self):
        path, _ = self.fx.write_batch(
            0, reports=[self.fx.report("SYS", "A", 1000, 100)],
            batch_no="B_dup")
        apply_batch(self.fx.ws_dir, path)
        path2, _ = self.fx.write_batch(
            1, reports=[self.fx.report("SYS", "B", 1005, 200)],
            batch_no="B_dup", name="dup2.json")
        with self.assertRaises(DefectValidationError) as cm:
            apply_batch(self.fx.ws_dir, path2)
        self.assertEqual(cm.exception.issues[0].code, "BATCH_NO_REUSED")
        self.assertEqual(workspace_info(self.fx.ws_dir)["revision"], 1)

    def test_bad_batch_rolls_back_everything(self):
        self.fx.apply(0, reports=[self.fx.report("SYS", "A", 1000, 100)])
        before = self.fx.full()
        # 批内：一条好报件 + 一条坏引用 -> 整批拒绝，修订不变
        with self.assertRaises(DefectValidationError):
            self.fx.apply(1,
                          reports=[self.fx.report("SYS", "B", 1005, 200),
                                   self.fx.report("SYS", "C", 9999, 300)],
                          adjudications=[
                              self.fx.adj(("SYS", "A"), ("SYS", "B"), "same")])
        self.assertEqual(self.fx.full(), before)
        self.assertEqual(workspace_info(self.fx.ws_dir)["revision"], 1)

    def test_duplicate_keys_in_batch_rejected(self):
        with self.assertRaises(DefectValidationError) as cm:
            self.fx.apply(0,
                          reports=[self.fx.report("SYS", "A", 1000, 100),
                                   self.fx.report("SYS", "A", 1001, 100)])
        codes = {i.code for i in cm.exception.issues}
        self.assertIn("DUPLICATE_REPORT_IN_BATCH", codes)

    def test_duplicate_pair_in_batch_rejected(self):
        with self.assertRaises(DefectValidationError) as cm:
            self.fx.apply(0,
                          reports=[self.fx.report("SYS", "A", 1000, 100),
                                   self.fx.report("SYS", "B", 1001, 100)],
                          adjudications=[
                              self.fx.adj(("SYS", "A"), ("SYS", "B"), "same"),
                              self.fx.adj(("SYS", "B"), ("SYS", "A"),
                                          "different")])
        self.assertIn("DUPLICATE_PAIR_IN_BATCH",
                      {i.code for i in cm.exception.issues})

    def test_self_adjudication_rejected(self):
        with self.assertRaises(DefectValidationError) as cm:
            self.fx.apply(0,
                          reports=[self.fx.report("SYS", "A", 1000, 100)],
                          adjudications=[
                              self.fx.adj(("SYS", "A"), ("SYS", "A"), "same")])
        self.assertIn("SELF_ADJUDICATION",
                      {i.code for i in cm.exception.issues})

    def test_adjudication_unknown_report_rejected(self):
        with self.assertRaises(DefectValidationError) as cm:
            self.fx.apply(0,
                          reports=[self.fx.report("SYS", "A", 1000, 100)],
                          adjudications=[
                              self.fx.adj(("SYS", "A"), ("SYS", "Z"), "same")])
        self.assertIn("UNKNOWN_REPORT",
                      {i.code for i in cm.exception.issues})

    def test_adjudication_may_reference_voided_report(self):
        self.fx.apply(0, reports=[self.fx.report("SYS", "A", 1000, 100),
                                  self.fx.report("SYS", "B", 1001, 100)])
        self.fx.apply(1, void=[{"source": "SYS", "source_ref": "B"}])
        # 已撤下的 B 仍是仓内已知报件，可以裁定；生效后只要 B 重新在册才连接
        self.fx.apply(2, adjudications=[
            self.fx.adj(("SYS", "A"), ("SYS", "B"), "same")])
        groups = self.fx.full()["defect_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["member_count"], 1)
        self.fx.apply(3, reports=[self.fx.report("SYS", "B", 1001, 100)])
        groups = self.fx.full()["defect_groups"]
        self.assertEqual(groups[0]["member_count"], 2)

    def test_adjudication_to_voided_report_then_restore(self):
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "A", 1000, 100),
                               self.fx.report("SYS", "B", 1001, 100),
                               self.fx.report("SYS", "C", 1010, 100),
                               self.fx.report("SYS", "D", 1011, 100)],
                      adjudications=[
                          self.fx.adj(("SYS", "C"), ("SYS", "D"), "same")])
        self.fx.apply(1, void=[{"source": "SYS", "source_ref": "B"}])
        # A 与已撤下的 B 裁定同一：B 不在册时不连接任何组
        self.fx.apply(2, adjudications=[
            self.fx.adj(("SYS", "A"), ("SYS", "B"), "same")])
        groups = _group_map(self.fx.full())
        self.assertEqual(set(groups), {_key("SYS", "A"), _key("SYS", "C")})
        self.assertEqual(groups[_key("SYS", "A")]["member_count"], 1)
        # 重新录入 B：旧裁定恢复，A+B 成组；C+D 组不受影响
        self.fx.apply(3, reports=[self.fx.report("SYS", "B", 1001, 100)])
        groups = _group_map(self.fx.full())
        self.assertEqual(set(groups), {_key("SYS", "A"), _key("SYS", "C")})
        self.assertEqual(groups[_key("SYS", "A")]["member_count"], 2)
        self.assertEqual(groups[_key("SYS", "C")]["member_count"], 2)
        # 增量重放仍一致
        full = self.fx.full()
        state = ({}, {})
        for b in fetch_changes(self.fx.ws_dir, 0)["batches"]:
            _apply_changes(state, b)
        self.assertEqual(state[0], _group_map(full))
        self.assertEqual(state[1], _member_map(full))

    def test_unknown_workspace_and_future_or_stale_revision(self):
        path, _ = self.fx.write_batch(0, batch_no="B_ws")
        with open(path, encoding="utf-8") as fh:
            batch = json.load(fh)
        batch["based_on_workspace"] = "0" * 32
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(batch, fh)
        with self.assertRaises(DefectValidationError) as cm:
            apply_batch(self.fx.ws_dir, path)
        self.assertEqual(cm.exception.issues[0].code, "WORKSPACE_MISMATCH")

        path2, _ = self.fx.write_batch(99, batch_no="B_future")
        with self.assertRaises(DefectValidationError) as cm:
            apply_batch(self.fx.ws_dir, path2)
        self.assertEqual(cm.exception.issues[0].code, "UNKNOWN_REVISION")

    def test_two_writers_same_revision_no_lost_update(self):
        # 两个写者都基于修订 0 准备批；先提交者成功，后提交者被拒（不静默覆盖）
        p1, _ = self.fx.write_batch(
            0, reports=[self.fx.report("SYS", "A", 1000, 100)],
            batch_no="W1", name="w1.json")
        p2, _ = self.fx.write_batch(
            0, reports=[self.fx.report("SYS", "B", 1005, 200)],
            batch_no="W2", name="w2.json")
        apply_batch(self.fx.ws_dir, p1)
        with self.assertRaises(DefectValidationError) as cm:
            apply_batch(self.fx.ws_dir, p2)
        self.assertEqual(cm.exception.issues[0].code, "STALE_REVISION")
        # 后写者基于最新修订可继续，A 的更正没丢
        p2b, _ = self.fx.write_batch(
            1, reports=[self.fx.report("SYS", "B", 1005, 200)],
            batch_no="W2b", name="w2b.json")
        apply_batch(self.fx.ws_dir, p2b)
        self.assertEqual(workspace_info(self.fx.ws_dir)["revision"], 2)
        refs = {r["source_ref"] for r in
                list_reports(self.fx.ws_dir)["reports"]}
        self.assertEqual(refs, {"A", "B"})

    def test_concurrent_writers_do_not_lose_corrections(self):
        # 两个写者同时基于修订 0 提交不同更正：串行化后只有一个成功，
        # 另一个必须被拒（不静默覆盖），之后基于最新修订仍可继续。
        p1, _ = self.fx.write_batch(
            0, reports=[self.fx.report("SYS", "A", 1000, 100)],
            batch_no="C1", name="c1.json")
        p2, _ = self.fx.write_batch(
            0, reports=[self.fx.report("SYS", "B", 1005, 200)],
            batch_no="C2", name="c2.json")
        import threading
        results = {}

        def run(name, path):
            try:
                results[name] = ("ok", apply_batch(self.fx.ws_dir, path))
            except DefectValidationError as exc:
                results[name] = ("rejected",
                                 {i.code for i in exc.issues})
            except Exception as exc:          # 锁超时也视为拒绝，不算成功
                results[name] = ("error", str(exc))

        t1 = threading.Thread(target=run, args=("C1", p1))
        t2 = threading.Thread(target=run, args=("C2", p2))
        t1.start(); t2.start(); t1.join(); t2.join()
        outcomes = sorted(v[0] for v in results.values())
        self.assertEqual(outcomes.count("ok"), 1, results)
        self.assertEqual(outcomes.count("ok") + outcomes.count("rejected") +
                         outcomes.count("error"), 2)
        # 成功者的更正没丢
        refs = {r["source_ref"] for r in
                list_reports(self.fx.ws_dir)["reports"]}
        winner = "A" if results["C1"][0] == "ok" else "B"
        self.assertEqual(refs, {winner})
        # 另一份可基于最新修订重交
        loser = "c2" if winner == "A" else "c1"
        loser_path = p2 if winner == "A" else p1
        self.fx.apply(1, reports=[self.fx.report(
            "SYS", "B" if winner == "A" else "A",
            1005 if winner == "A" else 1000, 200 if winner == "A" else 100)],
            batch_no="C3", name="c3.json")
        self.assertEqual(workspace_info(self.fx.ws_dir)["revision"], 2)

    def test_ordering_inside_batch_does_not_matter(self):
        reports = [self.fx.report("SYS", "A", 1000, 100),
                   self.fx.report("SYS", "B", 1001, 100),
                   self.fx.report("SYS", "C", 1002, 100)]
        adjs = [self.fx.adj(("SYS", "A"), ("SYS", "B"), "same"),
                self.fx.adj(("SYS", "B"), ("SYS", "C"), "same")]
        self.fx.apply(0, reports=reports, adjudications=adjs)
        full1 = self.fx.full()
        # 另一工作区，行序与裁定序颠倒
        self.tmp2 = tempfile.TemporaryDirectory()
        fx2 = (WorkspaceFixture(self.tmp2.name)
               .axis("A1", 100)
               .calib_row("OLD", "A1", "S", 0, 60, 1000, +1, row_id="S1")
               .calib_row("NEW", "A1", "G", 0, 60, 2000, +1, row_id="G1")
               .reg("OLD", "NEW", 1000, 1060, reg_id="R1")
               .build().workspace())
        fx2.apply(0, reports=list(reversed(reports)),
                  adjudications=list(reversed(adjs)))
        full2 = fx2.full()
        self.assertEqual(full1["defect_groups"], full2["defect_groups"])
        self.assertEqual(full1["defect_members"], full2["defect_members"])
        self.tmp2.cleanup()

    def test_correction_of_report_content_changes_member_row(self):
        self.fx.apply(0, reports=[self.fx.report("SYS", "A", 1000, 100)])
        before = self.fx.full()
        self.fx.apply(1, reports=[self.fx.report("SYS", "A", 1010, 100)])
        after = self.fx.full()
        self.assertEqual(before["defect_groups"], after["defect_groups"])
        self.assertNotEqual(before["defect_members"], after["defect_members"])
        inc = fetch_changes(self.fx.ws_dir, 1)["batches"][0]["changes"]
        self.assertEqual([(c["table"], c["op"]) for c in inc],
                         [("defect_member", "~")])
        self.assertEqual(inc[0]["before"]["registration"]["reg_n"], 1000)
        self.assertEqual(inc[0]["after"]["registration"]["reg_n"], 1010)


# --------------------------------------------------------------------------- #
# 增量重放与独立参考程序
# --------------------------------------------------------------------------- #
def _apply_changes(state, batch):
    groups, members = state
    for c in batch["changes"]:
        table = groups if c["table"] == "defect_group" else members
        if c["op"] == "-":
            table.pop(tuple(c["key"]) if isinstance(c["key"], list)
                      else c["key"], None)
        else:
            key = c["key"] if isinstance(c["key"], str) else tuple(c["key"])
            table[key] = c["after"]


class IncrementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 100)
                   .calib_row("OLD", "A1", "S", 0, 60, 1000, +1,
                              row_id="S1")
                   .calib_row("NEW", "A1", "G", 0, 60, 2000, +1,
                              row_id="G1")
                   .reg("OLD", "NEW", 1000, 1060, reg_id="R1")
                   .build()
                   .workspace(spatial=3, time=100))

    def tearDown(self):
        self.tmp.cleanup()

    def test_replay_from_empty_and_from_full_snapshot(self):
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "A", 1000, 100),
                               self.fx.report("SYS", "B", 1000, 200),
                               self.fx.report("SYS", "C", 1005, 900)],
                      adjudications=[
                          self.fx.adj(("SYS", "A"), ("SYS", "B"), "same"),
                          self.fx.adj(("SYS", "B"), ("SYS", "C"), "same"),
                          self.fx.adj(("SYS", "A"), ("SYS", "C"), "different")])
        self.fx.apply(1, adjudications=[
            self.fx.adj(("SYS", "B"), ("SYS", "C"), "clear")])
        self.fx.apply(2, void=[{"source": "SYS", "source_ref": "A"}])
        self.fx.apply(3, reports=[self.fx.report("SYS", "D", 1006, 950)])

        full = self.fx.full()
        expected_groups = {g["group_key"]: g for g in full["defect_groups"]}
        expected_members = {(m["source"], m["source_ref"]): m
                            for m in full["defect_members"]}

        # 从空表重放全部增量
        state = ({}, {})
        for batch in fetch_changes(self.fx.ws_dir, 0)["batches"]:
            _apply_changes(state, batch)
        self.assertEqual(state[0], expected_groups)
        self.assertEqual(state[1], expected_members)

        # 从某次已读全量继续
        changes = fetch_changes(self.fx.ws_dir, 0)["batches"]
        mid = changes[1]["revision"]          # 修订 2 的全量
        # 重建到修订 2
        state2 = ({}, {})
        for batch in changes[:2]:
            _apply_changes(state2, batch)
        for batch in fetch_changes(self.fx.ws_dir, mid)["batches"]:
            _apply_changes(state2, batch)
        self.assertEqual(state2[0], expected_groups)
        self.assertEqual(state2[1], expected_members)

    def test_no_table_change_still_reports_batch(self):
        self.fx.apply(0, reports=[self.fx.report("SYS", "A", 1000, 100)])
        self.fx.apply(1, adjudications=[])      # 空批：无表变化
        batches = fetch_changes(self.fx.ws_dir, 1)["batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["revision"], 2)
        self.assertEqual(batches[0]["changes"], [])
        # 修订号已推进，空批可被确认
        self.assertEqual(fetch_changes(self.fx.ws_dir, 2)["batches"], [])

    def test_group_key_is_smallest_member_key_not_a_serial(self):
        # 录入顺序 C、B、A，但组键必须是 A
        self.fx.apply(0,
                      reports=[self.fx.report("SYS", "C", 1005, 900),
                               self.fx.report("SYS", "B", 1000, 200),
                               self.fx.report("SYS", "A", 1000, 100)],
                      adjudications=[
                          self.fx.adj(("SYS", "C"), ("SYS", "B"), "same"),
                          self.fx.adj(("SYS", "B"), ("SYS", "A"), "same")])
        groups = self.fx.full()["defect_groups"]
        self.assertEqual(groups[0]["group_key"], _key("SYS", "A"))


class OracleCrosscheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 100)
                   .calib_row("OLD", "A1", "S", 0, 60, 1000, +1,
                              row_id="S1")
                   .calib_row("NEW", "A1", "G", 0, 60, 2000, +1,
                              row_id="G1")
                   .reg("OLD", "NEW", 1000, 1060, reg_id="R1")
                   .build()
                   .workspace(spatial=4, time=80))

    def tearDown(self):
        self.tmp.cleanup()

    def test_random_batches_against_oracle(self):
        rnd = random.Random(20261120)
        sources = ["SRC", "MAP", "FLD"]
        keys = [(sources[i % 3], f"K{i:02d}") for i in range(10)]
        # 随机在册/撤下/录入：先全部录入
        init = [self.fx.report(k[0], k[1], 1000 + i * 2, 100 + i * 10)
                for i, k in enumerate(keys)]
        self.fx.apply(0, reports=init)
        rnd.shuffle(init)
        ora = oracle.Oracle(4, 80)
        batch0 = {"reports": [
            {"source": r["source"], "source_ref": r["source_ref"],
             "category": r["category"], "lane": r["lane"],
             "found_at": r["found_at"], "reg_id": r["reg_id"],
             "reg_n": r["reg_n"]} for r in init], "void": [],
            "adjudications": []}
        ora.apply(batch0)
        for i in range(10):
            ora.set_location(keys[i], "localizable", "A1", i * 2)
        for rev in range(1, 9):
            reps = []
            void = []
            adjs = []
            present = [k for k in keys
                       if ora.reports[k].present]
            # 随机撤下一两条
            for k in rnd.sample(present, min(len(present), rnd.randint(0, 2))):
                void.append({"source": k[0], "source_ref": k[1]})
            # 随机重录一条撤下的
            gone = [k for k in keys if not ora.reports[k].present]
            if gone:
                k = rnd.choice(gone)
                idx = keys.index(k)
                reps.append(self.fx.report(k[0], k[1], 1000 + idx * 2,
                                           100 + idx * 10))
            # 随机一组无序的不同报件对
            choices = rnd.sample(keys, min(len(keys), rnd.randint(2, 5)))
            for j in range(len(choices) - 1):
                a, b = choices[j], choices[j + 1]
                if rnd.random() < 0.5:
                    a, b = b, a
                decision = rnd.choice(["same", "different", "clear"])
                adjs.append(self.fx.adj(a, b, decision))
            path, batch = self.fx.write_batch(
                rev,
                reports=reps, void=void, adjudications=adjs,
                batch_no=f"O{rev:02d}")
            out = apply_batch(self.fx.ws_dir, path)
            self.assertEqual(out["receipt"]["revision"], rev + 1)

            # oracle 应用同一批（含重录报件保持定位）
            obatch = {"reports": [
                {"source": r["source"], "source_ref": r["source_ref"],
                 "category": r["category"], "lane": r["lane"],
                 "found_at": r["found_at"], "reg_id": r["reg_id"],
                 "reg_n": r["reg_n"]} for r in reps],
                "void": void, "adjudications": adjs}
            for r in reps:
                k = (r["source"], r["source_ref"])
                idx = keys.index(k)
                ora.set_location(k, "localizable", "A1", idx * 2)
            ora.apply(obatch)
            # oracle 的定位状态与产品 X 坐标一致：都用 n = 1000 + i*2
            for i, k in enumerate(keys):
                ora.set_location(k, "localizable", "A1", i * 2)

            full = self.fx.full()
            got_groups = _group_map(full)
            exp_groups = ora.group_rows()
            self.assertEqual(set(got_groups), set(exp_groups),
                             f"rev {rev+1} 组键不同")
            for gk, g in exp_groups.items():
                self.assertEqual(got_groups[gk]["isolated"], g["isolated"],
                                 f"rev {rev+1} 组 {gk} 隔离状态不同")
                self.assertEqual(got_groups[gk]["member_count"],
                                 g["member_count"])
                self.assertEqual(got_groups[gk]["first_found"],
                                 g["first_found"])
                self.assertEqual(got_groups[gk]["last_found"],
                                 g["last_found"])
            got_members = _member_map(full)
            exp_members = ora.member_rows()
            self.assertEqual(set(got_members), set(exp_members))
            for k, m in exp_members.items():
                self.assertEqual(got_members[k]["group_key"], m["group_key"])
                self.assertEqual(got_members[k]["found_at"], m["found_at"])

            # 增量重放到当前修订必须等于全量
            state = ({}, {})
            for b in fetch_changes(self.fx.ws_dir, 0)["batches"]:
                _apply_changes(state, b)
            self.assertEqual(state[0], got_groups)
            self.assertEqual(state[1], got_members)

            # 候选集合与 oracle 全量枚举一致（从每条在册可定位报件出发）
            for k in keys:
                if not ora.reports[k].present:
                    continue
                got = {(c["other"]["source"], c["other"]["source_ref"])
                       for c in candidate_pairs(
                           self.fx.ws_dir, k[0], k[1])["candidates"]}
                self.assertEqual(got, set(ora.candidates(k)),
                                 f"rev {rev+1} 起点 {k} 候选不同")

    def test_isolation_evidence_paths_are_valid(self):
        self.fx.apply(0,
                      reports=[self.fx.report("SRC", f"K{i}", 1000 + i,
                                              100 + i)
                               for i in range(6)],
                      adjudications=[
                          self.fx.adj(("SRC", "K0"), ("SRC", "K1"), "same"),
                          self.fx.adj(("SRC", "K1"), ("SRC", "K2"), "same"),
                          self.fx.adj(("SRC", "K3"), ("SRC", "K2"), "same"),
                          self.fx.adj(("SRC", "K4"), ("SRC", "K3"), "same"),
                          self.fx.adj(("SRC", "K5"), ("SRC", "K4"), "same"),
                          self.fx.adj(("SRC", "K0"), ("SRC", "K5"),
                                      "different")])
        gk = _key("SRC", "K0")
        ev = isolation_evidence(self.fx.ws_dir, gk)
        self.assertTrue(ev["isolated"])
        self.assertEqual(len(ev["different_edges"]), 1)
        edge = ev["different_edges"][0]
        path = [tuple(p) for p in edge["same_path"]]
        self.assertEqual(path[0], tuple(edge["between"][0]))
        self.assertEqual(path[-1], tuple(edge["between"][1]))
        # 路径每一步都必须是当前生效的 same 边
        adjs = {(tuple(a["a"]), tuple(a["b"])): a
                for a in list_adjudications(self.fx.ws_dir)["adjudications"]}
        for u, v in zip(path, path[1:]):
            pair = (u, v) if u < v else (v, u)
            self.assertIn(pair, adjs)
            self.assertEqual(adjs[pair]["kind"], "same")
            self.assertTrue(adjs[pair]["effective"])

    def test_isolation_evidence_ignores_voided_reports(self):
        self.fx.apply(0,
                      reports=[self.fx.report("SRC", f"K{i}", 1000 + i,
                                              100 + i)
                               for i in range(4)],
                      adjudications=[
                          self.fx.adj(("SRC", "K0"), ("SRC", "K1"), "same"),
                          self.fx.adj(("SRC", "K1"), ("SRC", "K2"), "same"),
                          self.fx.adj(("SRC", "K2"), ("SRC", "K3"), "same"),
                          self.fx.adj(("SRC", "K0"), ("SRC", "K3"),
                                      "different")])
        # 撤下 K1：K0/K2 的路径只剩 K0-K1-K2（K1 已不在册），无法用撤下报件解释
        self.fx.apply(1, void=[{"source": "SRC", "source_ref": "K1"}])
        groups = self.fx.full()["defect_groups"]
        # K0 单件、K2-K3 两件：没有同时含 K0 与 K3 的组
        sets = []
        for g in groups:
            sets.append({(m["source"], m["source_ref"])
                         for m in self.fx.full()["defect_members"]
                         if m["group_key"] == g["group_key"]})
        self.assertFalse(any({("SRC", "K0"), ("SRC", "K3")} <= s
                             for s in sets))


# --------------------------------------------------------------------------- #
# 局部重算范围
# --------------------------------------------------------------------------- #
class LocalityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 20000)
                   .calib_row("OLD", "A1", "S", 0, 19990, 1000, +1,
                              row_id="S1")
                   .calib_row("NEW", "A1", "G", 0, 19990, 2000, +1,
                              row_id="G1")
                   .reg("OLD", "NEW", 1000, 20990, reg_id="R1")
                   .build()
                   .workspace(spatial=5, time=100))

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_many(self):
        reps = []
        adjs = []
        # 200 个互不相干的单件组
        for i in range(200):
            n = 1000 + i * 10
            ref = f"S{i:03d}"
            reps.append(self.fx.report("SRC", ref, n, 1000 + i))
        # 一条有桥接、环与隔离证据的大组
        base = 15000
        names = [f"L{i:02d}" for i in range(12)]
        for i, nm in enumerate(names):
            reps.append(self.fx.report("SRC", nm, 1000 + base + i, 5000 + i))
        for i in range(len(names) - 1):
            adjs.append(self.fx.adj(("SRC", names[i]), ("SRC", names[i + 1]),
                                    "same"))
        adjs.append(self.fx.adj(("SRC", names[0]), ("SRC", names[5]), "same"))
        adjs.append(self.fx.adj(("SRC", names[10]), ("SRC", names[0]),
                                "different"))
        self.fx.apply(0, reports=reps, adjudications=adjs)
        return names

    def test_local_correction_touches_only_related_group(self):
        self._seed_many()
        full = self.fx.full()
        self.assertEqual(len(full["defect_groups"]), 201)
        before_other = {
            g["group_key"]: g for g in full["defect_groups"]
            if not any(m["group_key"] == g["group_key"] and
                       m["source_ref"].startswith("L")
                       for m in full["defect_members"])}
        # 只在大组里撤一条边，组不拆（有环/其他路径）
        self.fx.apply(1, adjudications=[
            self.fx.adj(("SRC", "L00"), ("SRC", "L01"), "clear")])
        from chainage.defects import last_batch_scope
        scope = last_batch_scope(self.fx.ws_dir)
        touched = set(scope["groups"])
        # 只触及含 L 报件的组，不触及 200 个无关组
        for gk in touched:
            members = [m for m in self.fx.full()["defect_members"]
                       if m["group_key"] == gk]
            self.assertTrue(all(m["source_ref"].startswith("L")
                                for m in members),
                            f"无关组被触及: {gk}")
        self.assertLessEqual(len(touched), 2)
        self.assertGreater(scope["current_totals"]["groups"], 200)

    def test_stateless_group_computation_does_not_rebuild_everything(self):
        self._seed_many()
        # 扩大无关数据，受影响组仍不变（组键与成员数不变）
        big = [self.fx.report("SRC", f"BIG{i:04d}", 1000 + (i % 18000),
                              10000 + i) for i in range(3000)]
        self.fx.apply(1, reports=big)
        before = {g["group_key"]: g for g in
                  self.fx.full()["defect_groups"]}
        self.fx.apply(2, adjudications=[
            self.fx.adj(("SRC", "L02"), ("SRC", "L03"), "clear")])
        from chainage.defects import last_batch_scope
        scope = last_batch_scope(self.fx.ws_dir)
        # 本批只触及 L 大组（大组键为最小键 L00）
        touched = set(scope["groups"])
        self.assertEqual(touched, {_key("SRC", "L00")})
        # 大组没拆：成员数、组键都不变，其余组也没动
        after = {g["group_key"]: g for g in self.fx.full()["defect_groups"]}
        self.assertEqual(before, after)

    def test_candidate_prefilter_ignores_unrelated_bulk(self):
        reps = [self.fx.report("SRC", f"S{i:04d}", 1000 + (i % 18000),
                               1000 + i) for i in range(3000)]
        reps.append(self.fx.report("SRC", "TARGET", 1000 + 9000, 500))
        reps.append(self.fx.report("OTHER", "NEAR", 1000 + 9002, 520))
        self.fx.apply(0, reports=reps)
        out = candidate_pairs(self.fx.ws_dir, "SRC", "TARGET",
                              explain_scan=True)
        sc = out["scan"]
        self.assertGreater(sc["bucket_rows"], 2000)
        self.assertLess(sc["space_time_preselected"], 20)
        self.assertEqual(sc["pairs_examined"],
                         len([c for c in out["candidates"]]))


# --------------------------------------------------------------------------- #
# 安全与 CLI
# --------------------------------------------------------------------------- #
class SafetyAndCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 100)
                   .calib_row("OLD", "A1", "S", 0, 60, 1000, +1,
                              row_id="S1")
                   .calib_row("NEW", "A1", "G", 0, 60, 2000, +1,
                              row_id="G1")
                   .reg("OLD", "NEW", 1000, 1060, reg_id="R1")
                   .build()
                   .workspace(spatial=3, time=100))

    def tearDown(self):
        self.tmp.cleanup()

    def test_replacing_chainage_file_does_not_change_positions(self):
        self.fx.apply(0, reports=[self.fx.report("SYS", "A", 1000, 100)])
        before = self.fx.full()
        # 替换工作区目录外的那份换算库：再建一份不同的
        pkg2 = os.path.join(self.tmp.name, "pkg2")
        os.makedirs(pkg2, exist_ok=True)
        fx2 = WorkspaceFixture(pkg2)
        (fx2.axis("A1", 100)
         .calib_row("OLD", "A1", "S", 0, 60, 990, +1, row_id="S9")
         .calib_row("NEW", "A1", "G", 0, 60, 2000, +1, row_id="G9")
         .reg("OLD", "NEW", 990, 1050, reg_id="R1")
         .build())
        shutil_target = os.path.join(self.tmp.name, "chainage.db")
        os.replace(os.path.join(pkg2, "chainage.db"), shutil_target)
        self.assertEqual(self.fx.full(), before)
        # 新录入仍按工作区绑定的那份标定
        self.fx.apply(1, reports=[self.fx.report("SYS", "B", 1005, 200)])
        members = _member_map(self.fx.full())
        self.assertEqual(members[("SYS", "B")]["registration"]["reg_n"], 1005)

    def test_tampered_workspace_chainage_is_refused(self):
        self.fx.apply(0, reports=[self.fx.report("SYS", "A", 1000, 100)])
        _, ch_path = os.path.join(self.fx.ws_dir, "workspace.db"), \
            os.path.join(self.fx.ws_dir, "chainage.db")
        conn = sqlite3.connect(ch_path)
        conn.execute("UPDATE registration SET n_from=999 WHERE reg_id='R1'")
        conn.commit()
        conn.close()
        from chainage.errors import DatabaseError
        with self.assertRaises(DatabaseError):
            list_reports(self.fx.ws_dir)

    def test_cli_end_to_end(self):
        self.fx.apply(0, reports=[self.fx.report("SYS", "A", 1000, 100),
                                  self.fx.report("SYS", "B", 1005, 200)],
                      adjudications=[
                          self.fx.adj(("SYS", "A"), ("SYS", "B"), "same")])
        ws = self.fx.ws_dir
        env = dict(os.environ)
        cli = os.path.join(PROJECT_ROOT, "chainage", "cli.py")
        full = subprocess.run(
            [sys.executable, "-I", cli, "defect-full", ws],
            capture_output=True, text=True, env=env, cwd="/tmp")
        self.assertEqual(full.returncode, 0, full.stderr)
        obj = json.loads(full.stdout)
        self.assertEqual(obj["revision"], 1)
        self.assertEqual(len(obj["defect_members"]), 2)
        text = subprocess.run(
            [sys.executable, "-I", cli, "defect-groups", ws, "--text"],
            capture_output=True, text=True, env=env, cwd="/tmp")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("病害组", text.stdout)


import shutil  # noqa: E402  (放在末尾：只为安全测试替换文件)

if __name__ == "__main__":
    unittest.main()


# 追加：目标标牌缺失时仍可找候选、定位输出、Unicode 组键、读者隔离
class ExtraSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = (WorkspaceFixture(self.tmp.name)
                   .axis("A1", 200)
                   .calib_row("OLD", "A1", "S", 0, 100, 1000, +1,
                              row_id="S1")
                   # 目标标牌只覆盖物理 0..10：物理 50 处没有目标标牌
                   .calib_row("NEW", "A1", "G", 0, 10, 2000, +1,
                              row_id="G1")
                   .reg("OLD", "NEW", 1000, 1100, reg_id="R1")
                   .build()
                   .workspace(spatial=10, time=100))

    def tearDown(self):
        self.tmp.cleanup()

    def test_unique_position_without_target_can_still_find_candidates(self):
        # 登记 1050 在物理 50：唯一位置但无目标标牌 -> NO_TARGET 业务状态，
        # 仍算唯一物理位置，参与找候选。
        self.fx.apply(0,
                      reports=[self.fx.report("SRC", "A", 1050, 1000),
                               self.fx.report("SRC", "B", 1052, 1000)],
                      adjudications=[
                          self.fx.adj(("SRC", "A"), ("SRC", "B"), "same")])
        reps = {r["source_ref"]: r for r in
                list_reports(self.fx.ws_dir)["reports"]}
        self.assertEqual(reps["A"]["location"]["status"], LOC_LOCALIZABLE)
        self.assertEqual(reps["A"]["evidence"]["segment_status"], "NO_TARGET")
        out = candidate_pairs(self.fx.ws_dir, "SRC", "A")
        self.assertTrue(out["eligible"])
        # 同来源不出现；换一个跨来源同样位置的报件才能看到候选
        self.fx.apply(1, reports=[self.fx.report("MAP", "C", 1051, 1000)])
        out = candidate_pairs(self.fx.ws_dir, "SRC", "A")
        self.assertEqual([c["other"]["source_ref"] for c in out["candidates"]],
                         ["C"])
        # 输出能看出定位状态、原引用、轴与刻度
        c = out["candidates"][0]
        self.assertEqual(c["other"]["location"]["axis_id"], "A1")
        self.assertEqual(c["other"]["location"]["lo"], 51)
        self.assertEqual(c["other"]["registration"]["reg_id"], "R1")
        self.assertEqual(c["other"]["registration"]["reg_n"], 1051)
        self.assertEqual(out["start"]["registration"]["reg_n"], 1050)

    def test_member_row_keeps_location_status_and_original_reference(self):
        self.fx.apply(0,
                      reports=[self.fx.report("SRC", "A", 1050, 1000)])
        members = _member_map(self.fx.full())
        m = members[("SRC", "A")]
        self.assertEqual(m["location"]["status"], LOC_LOCALIZABLE)
        self.assertEqual(m["location"]["axis_id"], "A1")
        self.assertEqual(m["registration"]["reg_id"], "R1")
        self.assertEqual(m["registration"]["reg_n"], 1050)
        ev = m["evidence"]
        self.assertEqual(ev["segment_status"], "NO_TARGET")
        self.assertEqual(ev["reg_id"], "R1")
        self.assertEqual(ev["reg_n"], 1050)

    def test_group_key_uses_unicode_code_point_order(self):
        # 码点序：'A' < 'Z' < 'a' < '中'
        reports = [self.fx.report(src, ref, 1050, 1000)
                   for src, ref in [("中源", "甲"), ("a", "z"),
                                    ("Z", "A"), ("A", "b")]]
        adjs = [self.fx.adj(("中源", "甲"), ("a", "z"), "same"),
                self.fx.adj(("a", "z"), ("Z", "A"), "same"),
                self.fx.adj(("Z", "A"), ("A", "b"), "same")]
        self.fx.apply(0, reports=reports, adjudications=adjs)
        groups = self.fx.full()["defect_groups"]
        self.assertEqual(len(groups), 1)
        # 最小的 Unicode 码点键是 ("A","b")
        self.assertEqual(groups[0]["group_key"], _key("A", "b"))

    def test_reader_never_sees_half_written_tables(self):
        self.fx.apply(0, reports=[self.fx.report("SRC", "A", 1050, 1000)])
        db_path = os.path.join(self.fx.ws_dir, "workspace.db")
        writer = sqlite3.connect(db_path)
        reader = sqlite3.connect(db_path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            # 模拟"新成员 + 旧组"这种半写状态
            writer.execute("UPDATE defect_group SET member_count=99")
            writer.execute(
                "INSERT INTO defect_member(source, source_ref, group_key, "
                "category, lane, found_at, reg_id, reg_n, location_status, "
                "position_count) VALUES ('SRC','NEW','[]','x','L1',0,'R1',1,"
                "'localizable',1)")
            rev = reader.execute(
                "SELECT value FROM wmeta WHERE key='revision'").fetchone()[0]
            count = reader.execute(
                "SELECT COUNT(*) FROM defect_member").fetchone()[0]
            self.assertEqual(rev, "1")
            self.assertEqual(count, 1)     # 看不到未提交的新成员
            writer.rollback()
        finally:
            writer.close()
            reader.close()
