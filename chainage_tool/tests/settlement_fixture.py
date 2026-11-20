"""结算测试夹具：构建 ready 换算库 + 义务表 + 巡检簿，并调用产品/参考程序。"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage.build import build  # noqa: E402
from chainage.settlement import run_settlement  # noqa: E402
import settlement_oracle as oracle  # noqa: E402

# 统一基准：2026-11-20 08:00 UTC
DAY0 = 1795161600


def at(h: int, m: int = 0, day: int = 0) -> int:
    return DAY0 + day * 86400 + h * 3600 + m * 60


class SettlementFixture:
    """一个 ready 库 + 义务 + 巡检簿集合。"""

    def __init__(self, root: str):
        self.root = root
        self.pkg_dir = os.path.join(root, "pkg")
        os.makedirs(self.pkg_dir, exist_ok=True)
        self.db_path = os.path.join(root, "chainage.db")
        self.axes = {}
        self.calib = []
        self.regs = []
        self._cseq = 0
        self._rseq = 0

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
        return row_id

    def reg(self, src, tgt, n_from, n_to, axis_id=None, segment=None,
            reg_id=None):
        self._rseq += 1
        reg_id = reg_id or f"R{self._rseq:03d}"
        row = {"reg_id": reg_id, "source_system": src, "target_system": tgt,
               "from": n_from, "to": n_to}
        if axis_id is not None:
            row["axis_id"] = axis_id
        if segment is not None:
            row["segment"] = segment
        self.regs.append(row)
        return reg_id

    def build(self):
        manifest = {"version": "settle-fixture",
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

    # ---------------- 输入文件 ----------------
    def write_obligations(self, obligations, name="obligations.json"):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obligations, fh, ensure_ascii=False)
        return path

    def write_trips(self, trips, name="trips.jsonl"):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            for t in trips:
                fh.write(json.dumps(t, ensure_ascii=False) + "\n")
        return path

    def settle(self, obligations_path, trips_path, cutoff,
               cutoff_before=None, explain=None):
        return run_settlement(self.db_path, obligations_path, trips_path,
                              cutoff=cutoff, cutoff_before=cutoff_before,
                              explain=explain)


# --------------------------------------------------------------------------- #
# 把同一份输入转成参考程序的数据结构
# --------------------------------------------------------------------------- #
def oracle_calibs(fixture: SettlementFixture):
    return {r["row_id"]: oracle.OCalib(
        row_id=r["row_id"], system=r["system"], axis_id=r["axis_id"],
        segment=r["segment"], lo=r["lo"], hi=r["hi"],
        label_lo=r["label_lo"], direction=r["direction"])
        for r in fixture.calib}


def oracle_regs(fixture: SettlementFixture):
    return {r["reg_id"]: oracle.OReg(
        reg_id=r["reg_id"], source_system=r["source_system"],
        n_from=r["from"], n_to=r["to"], axis_id=r.get("axis_id"),
        segment=r.get("segment"))
        for r in fixture.regs}


def oracle_obligation(ob: dict) -> oracle.OObligation:
    thresholds = [oracle.OThreshold("full", ob["window_start"],
                                    ob["window_end"], ob["min_crews"])]
    for i, sw in enumerate(ob.get("sub_windows", [])):
        thresholds.append(oracle.OThreshold(
            sw.get("label") or f"sub{i}", sw["start"], sw["end"],
            sw["min_crews"]))
    return oracle.OObligation(
        obligation_id=ob["obligation_id"], axis_id=ob["axis_id"],
        lane=ob["lane"], start=ob["start"], end=ob["end"],
        thresholds=tuple(thresholds))


def oracle_trips(records, cutoff):
    """按截止点选可见版本（并支持 void），转成参考程序的 OTrip。"""
    latest = {}
    for r in records:
        if r["ingest_time"] <= cutoff:
            latest[r["trip_id"]] = r
    out = []
    for trip_id, r in latest.items():
        if r.get("action", "trip") == "void":
            continue
        routes = []
        for rd in r["routes"]:
            frags = tuple(oracle.OFrag(
                reg_id=f["reg_id"], n_from=f["n_from"], n_to=f["n_to"],
                calib_row_id=f["calib_row_id"]) for f in rd["fragments"])
            routes.append(oracle.ORoute(fragments=frags))
        out.append(oracle.OTrip(
            trip_id=trip_id, crew=r["crew"], survey_time=r["survey_time"],
            lane=r["lane"], ingest_time=r["ingest_time"],
            routes=tuple(routes)))
    return out
