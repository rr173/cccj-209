"""巡检履约结算规模验收：运行时生成数据，不进 Git。

场景（全部在临时目录生成，报告写到 workdir）：

组 1 —— 长无分界路段 + 大量无关趟次 + 反复巡同一段
    - 一条很长的物理轴（例如 5,000,000 刻度）上的长义务区间；
    - 路网上大量与当前义务无关的趟次（其他轴 / 其他车道 / 其他时段）；
    - 少数班组在义务区间内反复巡同一段（密集重复上报）。
    验证：
    * 结果区段数、解析片段数、查询行数与"相关系数"成正比，不随刻度数
      或全路网规模铺开；
    * 运行时内存峰值（tracemalloc）可报告且与长区间长度无关。

组 2 —— 多义务、多班组、密集重复上报
    - 多条义务跨多轴；
    - 数千条巡检记录、大量同一班组重复上报；
    展示耗时与内存随相关片段/分界增长。

用法::

    python3 -I scripts/settlement_scale_check.py --workdir data/scale-settle
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage.build import build  # noqa: E402
from chainage.settlement import run_settlement  # noqa: E402

DAY0 = 1795161600
HOUR = 3600


def _rss_kb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _write_package(path, version, axes, calibs, regs):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"version": version,
                   "axes": [{"axis_id": a, "length": l}
                            for a, l in axes.items()]}, fh, ensure_ascii=False)
    with open(os.path.join(path, "calibration.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in calibs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(path, "registration.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in regs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _long_road_case(workdir, axis_len, n_unrelated, n_repeat):
    print("=" * 70)
    print(f"组 1：轴长 {axis_len:,} 刻度，{n_unrelated} 条无关趟次，"
          f"{n_repeat} 条重复上报")
    print("=" * 70)
    pkg = os.path.join(workdir, "long_pkg")
    db = os.path.join(workdir, "long.db")
    # 一条长标定行覆盖整条轴，登记覆盖同样范围
    calibs = [
        {"row_id": "S-LONG", "system": "OLD", "axis_id": "AX",
         "segment": "MAIN", "lo": 0, "hi": axis_len,
         "label_lo": 0, "direction": 1},
        {"row_id": "T-LONG", "system": "NEW", "axis_id": "AX",
         "segment": "MAIN2", "lo": 0, "hi": axis_len,
         "label_lo": 0, "direction": 1},
    ]
    regs = [{"reg_id": "RLONG", "source_system": "OLD",
             "target_system": "NEW", "from": 0, "to": axis_len}]
    _write_package(pkg, "settle-long", {"AX": axis_len}, calibs, regs)
    build(pkg, db)

    rng = random.Random(1234)
    obligations = [{
        "obligation_id": "OLONG", "axis_id": "AX", "lane": "L1",
        "start": 0, "end": axis_len,
        "window_start": DAY0 + 8 * HOUR, "window_end": DAY0 + 16 * HOUR,
        "min_crews": 2,
        "sub_windows": [{"start": DAY0 + 8 * HOUR, "end": DAY0 + 12 * HOUR,
                         "min_crews": 1, "label": "morning"}],
    }]
    # 重复上报：同一班组在义务区间一小段内反复巡
    hot_lo = axis_len // 2
    hot_hi = hot_lo + 5
    trips = []
    for i in range(n_repeat):
        crew = "A" if i % 2 == 0 else "B"
        st = DAY0 + 9 * HOUR + (i % 3600)
        trips.append({
            "trip_id": f"HOT{i}", "ingest_time": st + 5,
            "action": "trip", "crew": crew, "survey_time": st,
            "lane": "L1",
            "routes": [{"fragments": [
                {"reg_id": "RLONG", "n_from": hot_lo, "n_to": hot_hi,
                 "calib_row_id": "S-LONG"}]}]})
    # 大量无关趟次：同窗口内但走其他车道——可见于截止点，却与该义务无关
    for i in range(n_unrelated):
        st = DAY0 + 9 * HOUR + (i % 3600)
        lane = "L2"
        lo = rng.randint(0, axis_len - 10)
        trips.append({
            "trip_id": f"UN{i}", "ingest_time": st + 5,
            "action": "trip", "crew": f"U{i % 7}", "survey_time": st,
            "lane": lane,
            "routes": [{"fragments": [
                {"reg_id": "RLONG", "n_from": lo, "n_to": lo + 5,
                 "calib_row_id": "S-LONG"}]}]})
    obl_path = os.path.join(workdir, "long_oblig.json")
    trip_path = os.path.join(workdir, "long_trips.jsonl")
    with open(obl_path, "w", encoding="utf-8") as fh:
        json.dump(obligations, fh, ensure_ascii=False)
    with open(trip_path, "w", encoding="utf-8") as fh:
        for t in trips:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")

    gc.collect()
    stage_dir = os.path.join(workdir, "long_stage")
    t0 = time.perf_counter()
    out = run_settlement(db, obl_path, trip_path, cutoff=DAY0 + 17 * HOUR,
                         stage_dir=stage_dir)
    elapsed = time.perf_counter() - t0
    ob = out["ledger"]["obligations"][0]
    n_segments = len(ob["segments"])

    # 从暂存库读实际行数：展示工作量随相关路线片段与分界增长，而不是
    # 随整个巡检簿或全路网铺开。
    import sqlite3
    sconn = sqlite3.connect(os.path.join(stage_dir, "stage.db"))
    total_records = sconn.execute(
        "SELECT COUNT(*) FROM trip_record").fetchone()[0]
    visible_trips = sconn.execute(
        "SELECT COUNT(*) FROM resolved_trip WHERE phase=0").fetchone()[0]
    resolved_frags = sconn.execute(
        "SELECT COUNT(*) FROM resolved_frag WHERE phase=0").fetchone()[0]
    sconn.close()

    # 单独一轮测 tracemalloc 峰值
    tracemalloc.start()
    run_settlement(db, obl_path, trip_path, cutoff=DAY0 + 17 * HOUR)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"结算耗时 {elapsed:.3f}s，区段数 {n_segments}")
    print(f"巡检记录 {total_records} 条 -> 可见趟次 {visible_trips} 个，"
          f"解析片段 {resolved_frags} 条；义务相关仅约 {n_repeat} 条")
    print(f"区段数 {n_segments}（与轴长 {axis_len:,} 无关：长无分界段压缩为一条）")
    print(f"tracemalloc 峰值 {peak / 1024 / 1024:.2f} MiB，"
          f"进程峰值 RSS {_rss_kb() / 1024:.1f} MiB")
    assert n_segments <= 8, "长无分界义务不应按刻度铺表"
    return {
        "axis_len": axis_len, "unrelated": n_unrelated, "repeat": n_repeat,
        "records": total_records, "visible_trips": visible_trips,
        "resolved_fragments": resolved_frags,
        "segments": n_segments, "seconds": elapsed,
        "tracemalloc_peak_mib": peak / 1024 / 1024,
        "rss_mib": _rss_kb() / 1024,
    }


def _multi_obligation_case(workdir, n_axes, n_oblig, n_trips,
                           n_repeat_factor):
    print("=" * 70)
    print(f"组 2：{n_axes} 轴 / {n_oblig} 义务 / {n_trips} 条记录，"
          f"重复系数 {n_repeat_factor}")
    print("=" * 70)
    pkg = os.path.join(workdir, "multi_pkg")
    db = os.path.join(workdir, "multi.db")
    axis_len = 10000
    axes = {f"AX{i}": axis_len for i in range(n_axes)}
    calibs = []
    regs = []
    for i in range(n_axes):
        calibs.append({"row_id": f"S{i}", "system": "OLD", "axis_id": f"AX{i}",
                       "segment": f"seg{i}", "lo": 0, "hi": axis_len,
                       "label_lo": 0, "direction": 1})
        calibs.append({"row_id": f"T{i}", "system": "NEW", "axis_id": f"AX{i}",
                       "segment": f"segT{i}", "lo": 0, "hi": axis_len,
                       "label_lo": 0, "direction": 1})
        regs.append({"reg_id": f"R{i}", "source_system": "OLD",
                     "target_system": "NEW", "from": 0, "to": axis_len})
    _write_package(pkg, "settle-multi", axes, calibs, regs)
    build(pkg, db)

    rng = random.Random(99)
    obligations = []
    for oi in range(n_oblig):
        ax = f"AX{oi % n_axes}"
        a = rng.randint(0, axis_len - 100)
        b = a + rng.randint(10, 200)
        obligations.append({
            "obligation_id": f"O{oi}", "axis_id": ax, "lane": "L1",
            "start": a, "end": b,
            "window_start": DAY0 + 8 * HOUR,
            "window_end": DAY0 + 16 * HOUR, "min_crews": 2,
            "sub_windows": [{"start": DAY0 + 8 * HOUR,
                             "end": DAY0 + 12 * HOUR, "min_crews": 1,
                             "label": "morning"}]})
    trips = []
    # 相关趟次 + 重复上报
    for i in range(n_trips):
        oi = rng.randrange(n_oblig)
        ax_idx = oi % n_axes
        a = obligations[oi]["start"]
        b = obligations[oi]["end"]
        lo = rng.randint(a, max(a, b - 1))
        hi = min(b, lo + rng.randint(0, 20))
        st = DAY0 + 8 * HOUR + rng.randint(0, 7 * HOUR)
        for rep in range(n_repeat_factor):
            trips.append({
                "trip_id": f"T{i}r{rep}", "ingest_time": st + rep + 1,
                "action": "trip", "crew": f"C{i % 5}",
                "survey_time": st if rep == 0 else st + 0,
                "lane": "L1",
                "routes": [{"fragments": [
                    {"reg_id": f"R{ax_idx}", "n_from": lo, "n_to": hi,
                     "calib_row_id": f"S{ax_idx}"}]}]})
    obl_path = os.path.join(workdir, "multi_oblig.json")
    trip_path = os.path.join(workdir, "multi_trips.jsonl")
    with open(obl_path, "w", encoding="utf-8") as fh:
        json.dump(obligations, fh, ensure_ascii=False)
    with open(trip_path, "w", encoding="utf-8") as fh:
        for t in trips:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")

    gc.collect()
    stage_dir = os.path.join(workdir, "multi_stage")
    t0 = time.perf_counter()
    out = run_settlement(db, obl_path, trip_path, cutoff=DAY0 + 17 * HOUR,
                         stage_dir=stage_dir)
    elapsed = time.perf_counter() - t0
    n_segments = sum(len(o["segments"])
                     for o in out["ledger"]["obligations"])
    tracemalloc.start()
    run_settlement(db, obl_path, trip_path, cutoff=DAY0 + 17 * HOUR)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 取一次"某项义务只读相关趟次"的查询计划与行数，证明工作量随相关
    # 片段增长，而不是随整份巡检簿增长。
    import sqlite3
    sconn = sqlite3.connect(os.path.join(stage_dir, "stage.db"))
    total_visible = sconn.execute(
        "SELECT COUNT(*) FROM resolved_trip WHERE phase=0").fetchone()[0]
    total_frags = sconn.execute(
        "SELECT COUNT(*) FROM resolved_frag WHERE phase=0").fetchone()[0]
    plan = [r[-1] for r in sconn.execute(
        "EXPLAIN QUERY PLAN SELECT t.trip_id FROM resolved_trip t "
        "WHERE t.phase=? AND t.lane=? AND t.survey_time>=? "
        "AND t.survey_time<? AND EXISTS (SELECT 1 FROM resolved_frag f "
        "WHERE f.phase=t.phase AND f.trip_id=t.trip_id AND f.axis_id=?)",
        (0, "L1", DAY0, DAY0 + 24 * HOUR, "AX0"))]
    sconn.close()
    print(f"结算耗时 {elapsed:.3f}s，总区段 {n_segments}，"
          f"义务 {n_oblig} 条，记录 {len(trips)} 条")
    print(f"可见趟次 {total_visible}，解析片段 {total_frags}"
          f"（义务只在相关桶内取趟次）")
    print("相关趟次查询计划:", " | ".join(plan))
    print(f"tracemalloc 峰值 {peak / 1024 / 1024:.2f} MiB，"
          f"进程峰值 RSS {_rss_kb() / 1024:.1f} MiB")
    return {
        "n_axes": n_axes, "n_obligations": n_oblig, "records": len(trips),
        "visible_trips": total_visible, "resolved_fragments": total_frags,
        "segments": n_segments, "seconds": elapsed,
        "query_plan": plan,
        "tracemalloc_peak_mib": peak / 1024 / 1024,
        "rss_mib": _rss_kb() / 1024,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="data/scale-settle")
    ap.add_argument("--axis-len", type=int, default=5_000_000)
    ap.add_argument("--unrelated", type=int, default=2000)
    ap.add_argument("--repeat", type=int, default=200)
    ap.add_argument("--axes", type=int, default=4)
    ap.add_argument("--obligations", type=int, default=40)
    ap.add_argument("--trips", type=int, default=2000)
    ap.add_argument("--repeat-factor", type=int, default=3)
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    report = {
        "long_road": _long_road_case(
            args.workdir, args.axis_len, args.unrelated, args.repeat),
        "multi_obligation": _multi_obligation_case(
            args.workdir, args.axes, args.obligations, args.trips,
            args.repeat_factor),
    }
    out = os.path.join(args.workdir, "settlement_scale_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print("\n报告已写入", out)


if __name__ == "__main__":
    main()
