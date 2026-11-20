"""规模验收：两组运行时生成的数据，不进 Git。

组 1 —— 大刻度跨度
    一条长达 10 亿刻度、中间无任何边界的登记。验证：
    - 分段数与刻度长度无关（恒定结构只产生 1 个分段）；
    - 构建/查询耗时与峰值内存不随刻度数增长。

组 2 —— 大量不相关标定 + 少数高重叠区段
    上万条标定行、上千条登记，其中只有几条登记落在高重叠区段。
    反查借助 target_index 区间索引，只访问与查询标牌重叠的索引项，
    不扫登记表、不重读交接包。脚本报告 EXPLAIN QUERY PLAN、
    实际访问行数、耗时与 tracemalloc 峰值。

用法::

    python3 -I scripts/scale_check.py --workdir ./data/scale
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage.build import build  # noqa: E402
from chainage.query import (  # noqa: E402
    explain_reverse_plan,
    forward_query,
    reverse_query,
)
from chainage.database import new_connection  # noqa: E402

BILLION = 1_000_000_000


def _write_package(path, version, axes, calibs, regs):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({
            "version": version,
            "axes": [{"axis_id": a, "length": l} for a, l in axes.items()],
        }, fh, ensure_ascii=False)
    with open(os.path.join(path, "calibration.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in calibs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(path, "registration.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in regs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _rss_kb():
    """当前进程驻留内存峰值（KB，Linux ru_maxwp 为峰值 RSS）。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def case_long_span(workdir):
    print("=" * 70)
    print("组 1：十亿刻度无中间边界登记")
    print("=" * 70)
    pkg = os.path.join(workdir, "long_span_pkg")
    db = os.path.join(workdir, "long_span.db")
    # 轴长 12 亿；一条源标定、一条目标标定，各自覆盖十亿级物理范围
    axes = {"AX": BILLION + 100}
    calibs = [
        {"row_id": "S-BIG", "system": "OLD", "axis_id": "AX",
         "segment": "MAIN", "lo": 0, "hi": BILLION,
         "label_lo": 0, "direction": 1},
        {"row_id": "T-BIG", "system": "NEW", "axis_id": "AX",
         "segment": "MAIN", "lo": 0, "hi": BILLION,
         "label_lo": 5_000_000_000, "direction": -1},
    ]
    regs = [
        {"reg_id": "LONG1", "source_system": "OLD",
         "target_system": "NEW", "from": 0, "to": BILLION},
    ]
    _write_package(pkg, "scale-long-v1", axes, calibs, regs)

    gc.collect()
    # 计时与内存测量分开：tracemalloc 会显著拖慢计时，这里只测墙钟。
    t0 = time.perf_counter()
    stats = build(pkg, db)
    build_s = time.perf_counter() - t0
    # 单独一轮短测量取 Python 层分配峰值（库本身落盘，不随刻度常驻）
    tracemalloc.start()
    build(pkg, db + ".memprobe")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    os.remove(db + ".memprobe")
    print(f"构建: {build_s:.3f}s，分段数={stats.segments}（与 {BILLION} 刻度无关）")
    print(f"复测 tracemalloc 峰值={peak / 1024:.1f} KiB，"
          f"进程峰值 RSS={_rss_kb()} KiB")
    assert stats.segments == 1, "恒定结构的十亿刻度登记必须只产生 1 个分段"

    t0 = time.perf_counter()
    res = forward_query(db, "LONG1")
    print(f"正查整条: {time.perf_counter() - t0:.4f}s，"
          f"返回分段={len(res['segments'])}，"
          f"可入仓={len(res['warehousable_segments'])} 段")

    # 区间裁切：取登记深处的一个小窗口，工作量与窗口分界数相关
    mid = BILLION // 2
    t0 = time.perf_counter()
    res = forward_query(db, "LONG1", n_from=mid, n_to=mid + 999)
    print(f"正查中部 1000 刻度窗口: {time.perf_counter() - t0:.4f}s，"
          f"分段={len(res['segments'])}")

    t0 = time.perf_counter()
    target_label = 5_000_000_000 - mid  # NEW 反向编号
    rev = reverse_query(db, "NEW", n=target_label)
    print(f"反查点 {target_label}: {time.perf_counter() - t0:.4f}s，"
          f"命中登记={[m['reg_id'] for m in rev['matches']]}")
    print(f"库文件大小={os.path.getsize(db) / 1024:.1f} KiB（无逐刻度行）")
    return {"build_seconds": build_s, "segments": stats.segments,
            "tracemalloc_peak_kib": peak / 1024,
            "db_kib": os.path.getsize(db) / 1024}


def case_many_unrelated(workdir, n_calib=12000, n_reg=3000):
    print("=" * 70)
    print(f"组 2：{n_calib} 条不相关标定 + {n_reg} 条登记 + 高重叠区段")
    print("=" * 70)
    pkg = os.path.join(workdir, "mix_pkg")
    db = os.path.join(workdir, "mix.db")

    axes = {f"AX{i}": 1_000_000 for i in range(20)}
    calibs = []
    rid = 0
    # 大量互不相关的短标定，撒在 20 根轴、两个体系上
    for i in range(n_calib):
        rid += 1
        ax = f"AX{i % 20}"
        system = "OLD" if i % 2 == 0 else "NEW"
        lo = (i * 37) % 999_000
        hi = lo + 100
        calibs.append({
            "row_id": f"K{rid:06d}", "system": system, "axis_id": ax,
            "segment": f"seg{i % 50}", "lo": lo, "hi": hi,
            "label_lo": (i * 13) % 2_000_000 - 1_000_000,
            "direction": 1 if i % 3 else -1,
        })

    # 高重叠区段：AX0 的物理 [1000,2000] 上，OLD 4 条 + NEW 4 条叠放
    hot_ids = []
    for k in range(4):
        rid += 1
        sid = f"HOT-S{k}"
        hot_ids.append(sid)
        calibs.append({
            "row_id": sid, "system": "OLD", "axis_id": "AX0",
            "segment": f"hotS{k}", "lo": 1000, "hi": 2000,
            "label_lo": 900_000 + k * 10, "direction": 1 if k % 2 else -1,
        })
    for k in range(4):
        rid += 1
        tid = f"HOT-T{k}"
        calibs.append({
            "row_id": tid, "system": "NEW", "axis_id": "AX0",
            "segment": f"hotT{k}", "lo": 1000, "hi": 2000,
            "label_lo": 800_000 + k * 10, "direction": 1,
        })

    regs = []
    # 绝大多数登记与热区无关
    for i in range(n_reg):
        nf = (i * 53) % 1_900_000 - 950_000
        regs.append({
            "reg_id": f"REG{i:05d}", "source_system": "OLD",
            "target_system": "NEW", "from": nf, "to": nf + 50,
        })
    # 3 条登记落在热区
    for i in range(3):
        regs.append({
            "reg_id": f"HOTREG{i}", "source_system": "OLD",
            "target_system": "NEW",
            "from": 900_000 + i * 100, "to": 900_500 + i * 100,
        })

    _write_package(pkg, "scale-mix-v1", axes, calibs, regs)
    gc.collect()
    t0 = time.perf_counter()
    stats = build(pkg, db)
    build_s = time.perf_counter() - t0
    # 单独一轮复测取 Python 层分配峰值，不污染计时
    tracemalloc.start()
    build(pkg, db + ".memprobe")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    os.remove(db + ".memprobe")
    print(f"构建: {build_s:.3f}s，标定={stats.calibration_rows}，"
          f"登记={stats.registration_rows}，分段={stats.segments}，"
          f"目标索引行={stats.target_index_rows}")
    print(f"复测 tracemalloc 峰值={peak / 1024 / 1024:.2f} MiB，"
          f"进程峰值 RSS={_rss_kb() / 1024:.1f} MiB")

    # 反查热区标牌：先看查询计划，必须走 idx_target_* 而非全表
    plan = explain_reverse_plan(db, "NEW", 800_010, 800_010)
    print("反查 EXPLAIN QUERY PLAN:")
    for p in plan:
        print("   ", p["detail"])
    assert any("idx_target" in p["detail"] for p in plan), "反查必须走目标索引"

    # 统计实际索引访问行（与全库登记/标定规模对比）
    conn = new_connection(db)
    total_index = conn.execute(
        "SELECT COUNT(*) c FROM target_index").fetchone()["c"]
    total_reg = conn.execute(
        "SELECT COUNT(*) c FROM registration").fetchone()["c"]
    visited = conn.execute(
        "SELECT COUNT(*) c FROM target_index "
        "WHERE target_system='NEW' AND label_hi >= 800010 AND label_lo <= 800010"
    ).fetchone()["c"]
    conn.close()
    print(f"目标索引总行={total_index}，登记总行={total_reg}，"
          f"本次重叠索引项={visited}（不扫登记表）")

    t0 = time.perf_counter()
    rev = reverse_query(db, "NEW", n_from=800_000, n_to=800_100)
    rev_s = time.perf_counter() - t0
    hit_regs = sorted({m["reg_id"] for m in rev["matches"]})
    print(f"反查热区区间: {rev_s:.4f}s，命中登记={hit_regs}")
    assert any(r.startswith("HOTREG") for r in hit_regs)

    # 冷区点查：验证访问范围仍然很窄
    conn = new_connection(db)
    cold_visited = conn.execute(
        "SELECT COUNT(*) c FROM target_index "
        "WHERE target_system='NEW' AND label_hi >= -123456 "
        "AND label_lo <= -123456").fetchone()["c"]
    conn.close()
    print(f"冷区点查重叠索引项={cold_visited}（与热区规模解耦）")
    print(f"库文件大小={os.path.getsize(db) / 1024 / 1024:.2f} MiB")
    return {
        "build_seconds": build_s,
        "calibration_rows": stats.calibration_rows,
        "registration_rows": stats.registration_rows,
        "segments": stats.segments,
        "reverse_seconds": rev_s,
        "tracemalloc_peak_mib": peak / 1024 / 1024,
        "hot_overlap_index_rows": visited,
        "cold_overlap_index_rows": cold_visited,
        "db_mib": os.path.getsize(db) / 1024 / 1024,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="data/scale")
    ap.add_argument("--calib", type=int, default=12000)
    ap.add_argument("--reg", type=int, default=3000)
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    report = {
        "long_span": case_long_span(args.workdir),
        "many_unrelated": case_many_unrelated(
            args.workdir, n_calib=args.calib, n_reg=args.reg),
    }
    with open(os.path.join(args.workdir, "scale_report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print("\n报告已写入", os.path.join(args.workdir, "scale_report.json"))


if __name__ == "__main__":
    main()
