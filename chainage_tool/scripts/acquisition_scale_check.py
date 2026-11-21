#!/usr/bin/env python3
"""采集包装配规模验收：运行时生成乱序分卷，不进 Git。

规模（可用 --rows/--windows 调整）：
    - 至少 100 万采集行（含约 0.5% 重复行）、10 万个同轮次窗口；
    - 40 台设备 x 2 次启动，回执约每 200 条一条；
    - 大量同一时段的曝光、很稠密的小窗口（带空档）、少量横跨许多
      窗口的长曝光；
    - 设备 D0 的计数模数 2^62、步进 2^51：展开坐标超过 int64，
      验证十进制文本存储不回卷；
    - 8 个分卷，行序打散（文件次序不等于现场时序）。

验证：
    1. 子进程构建（python -m chainage acq-build），峰值 RSS <= 256 MiB；
    2. 统计与生成清单一致（记录数、重复行、窗口数）；
    3. 归因实际检查的窗口行数 stat.window_rows_examined 与记录数、
       相交输出量相称，不做 记录数 x 窗口数 的候选扫描；
    4. 抽样独立复核：归属窗口、完全窗外、局部交叠的交叠区间与空档，
       用逐窗暴力求交 + 精确有理数重算比对；
    5. 同一批分卷换个处理顺序（清单倒序）再建一次：除 meta/inputs 外
       全部结果表逐行一致（顺序无关性）。

用法::

    python3 -I scripts/acquisition_scale_check.py --workdir data/acq-scale
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage.build import build as build_chainage  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_UTC = 1_700_000_000_000_000_000
RSS_LIMIT_KIB = 256 * 1024


# --------------------------------------------------------------------------- #
# 输入生成
# --------------------------------------------------------------------------- #
def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")


def make_chainage_pkg(pkg_dir, reg_ids):
    os.makedirs(pkg_dir, exist_ok=True)
    with open(os.path.join(pkg_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"version": "acq-scale-lib-1",
                   "axes": [{"axis_id": "A1", "length": 1000}],
                   "files": {"manifest": "manifest.json",
                             "calibration": "calibration.jsonl",
                             "registration": "registration.jsonl"}}, fh)
    _write_jsonl(os.path.join(pkg_dir, "calibration.jsonl"), [
        json.dumps({"row_id": "S1", "system": "OLD", "axis_id": "A1",
                    "segment": "S", "lo": 0, "hi": 1000, "label_lo": 0,
                    "direction": 1}),
        json.dumps({"row_id": "U1", "system": "NEW", "axis_id": "A1",
                    "segment": "U", "lo": 0, "hi": 1000, "label_lo": 0,
                    "direction": 1}),
    ])
    _write_jsonl(os.path.join(pkg_dir, "registration.jsonl"), [
        json.dumps({"reg_id": rid, "source_system": "OLD",
                    "target_system": "NEW", "from": 0, "to": 10})
        for rid in reg_ids
    ])


def generate_acq_pack(pkg_dir, rows, n_windows, n_devices, n_volumes,
                      boots_per_device, seed):
    """生成采集交接包；返回清单统计（期望记录数、重复行数等）。"""
    rng = random.Random(seed)
    os.makedirs(os.path.join(pkg_dir, "volumes"), exist_ok=True)

    # 设备配置：D0 用超大模数制造超 int64 的展开坐标
    devices = []
    for i in range(n_devices):
        dev = f"D{i}"
        if i == 0:
            M, G = 2 ** 62, 2 ** 51
        else:
            M, G = 2 ** 32, 2 ** 30
        devices.append({"device": dev, "modulus": M, "max_advance": G,
                        "channels": [["s0", "scalar"], ["s1", "scalar"],
                                     ["p0", "photo"]]})
    with open(os.path.join(pkg_dir, "devices.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"devices": devices}, fh, ensure_ascii=False)

    # 计划：同一轮次 10 万个稠密小窗口（带空档）
    n_dup = rows // 201            # 约 0.5% 重复行
    n_unique = rows - n_dup
    n_boots = n_devices * boots_per_device
    per_boot_base, rem = divmod(n_unique, n_boots)
    max_expanded_normal = (per_boot_base + 1) * (2 ** 30)
    utc_span = max_expanded_normal * 2 + 10 ** 9
    win_len = utc_span // n_windows * 3 // 4
    win_gap = utc_span // n_windows - win_len
    expected = [["D0", "s0"], ["D1", "s0"], ["D2", "p0"]]
    plan_lines = []
    x = BASE_UTC
    for i in range(n_windows):
        plan_lines.append(json.dumps({
            "round": "R1", "window": f"W{i}", "start_utc": x,
            "end_utc": x + win_len,
            "registration": f"REG-{i % 3 + 1}",
            "expected_channels": expected}, ensure_ascii=False))
        x += win_len + win_gap
    _write_jsonl(os.path.join(pkg_dir, "plan.jsonl"), plan_lines)

    # 逐启动生成记录（JSON 文本行），按 hash 分到各卷
    vol_lines = [[] for _ in range(n_volumes)]
    pulse_lines = []
    n_readings = n_photos = n_receipts = 0
    for di in range(n_devices):
        dev = f"D{di}"
        M, G = (2 ** 62, 2 ** 51) if di == 0 else (2 ** 32, 2 ** 30)
        for bi in range(boots_per_device):
            boot = f"{dev}-B{bi}"
            boot_index = di * boots_per_device + bi
            per_boot = per_boot_base + (1 if boot_index < rem else 0)
            count = rng.randint(0, M - 1)
            expanded = 0
            receipt_idx = 0
            for seq in range(per_boot):
                is_receipt = (seq % 200 == 0)
                step = rng.randint(1 if is_receipt else 0, G)
                count = (count + step) % M
                expanded += step
                if is_receipt:
                    pulse = f"{boot}-P{receipt_idx}"
                    receipt_idx += 1
                    if di == 0:
                        utc = BASE_UTC + receipt_idx  # 亚纳秒级斜率
                    else:
                        utc = BASE_UTC + expanded * 2
                    pulse_lines.append(json.dumps(
                        {"pulse": pulse, "utc": utc}))
                    rec = {"device": dev, "boot": boot, "seq": seq,
                           "kind": "receipt", "count": count,
                           "pulse": pulse}
                    n_receipts += 1
                else:
                    kind_roll = rng.random()
                    if kind_roll < 0.75:
                        rec = {"device": dev, "boot": boot, "seq": seq,
                               "kind": "reading", "count": count,
                               "round": "R1",
                               "channel": "s0" if seq % 2 else "s1",
                               "value": rng.randint(-10 ** 6, 10 ** 6)}
                        n_readings += 1
                    else:
                        d_roll = rng.random()
                        if d_roll < 0.90:
                            d = rng.randint(1, 40)
                        elif d_roll < 0.98:
                            d = rng.randint(1000, 5000)
                        else:
                            # 横跨许多窗口的长曝光
                            d = rng.randint(2_000_000_000, 3_000_000_000)
                        rec = {"device": dev, "boot": boot, "seq": seq,
                               "kind": "photo", "count": count,
                               "round": "R1", "channel": "p0",
                               "resource": f"frame/{boot}/{seq}.jpg",
                               "exposure": d}
                        n_photos += 1
                line = json.dumps(rec, ensure_ascii=False)
                vol_lines[hash((dev, boot, seq)) % n_volumes].append(line)
    _write_jsonl(os.path.join(pkg_dir, "pulses.jsonl"), pulse_lines)

    # 每卷：加重复行后整体打散（文件次序不等于现场时序）
    all_lines = [ln for vol in vol_lines for ln in vol]
    for _ in range(n_dup):
        ln = rng.choice(all_lines)
        vol_lines[rng.randrange(n_volumes)].append(ln)
    vol_names = []
    for vi, lines in enumerate(vol_lines):
        rng.shuffle(lines)
        name = f"volumes/vol-{vi}.jsonl"
        _write_jsonl(os.path.join(pkg_dir, name), lines)
        vol_names.append(name)

    manifest = {"version": "acq-scale-1",
                "files": {"plan": "plan.jsonl", "pulses": "pulses.jsonl",
                          "devices": "devices.json", "volumes": vol_names}}
    with open(os.path.join(pkg_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
    return {
        "rows": rows, "unique_records": n_unique, "duplicate_lines": n_dup,
        "windows": n_windows, "readings": n_readings,
        "photos": n_photos, "receipts": n_receipts,
        "volumes": vol_names,
    }


# --------------------------------------------------------------------------- #
# 构建与量测
# --------------------------------------------------------------------------- #
def run_build(pkg_dir, lib_db, out_db, reversed_volumes=False):
    """子进程构建；返回 (耗时秒, 构建进程自报峰值 RSS KiB, 返回码, 输出)。

    量测说明：父进程若持有大量内存，fork/vfork 记账会把父进程足迹算进
    子进程 RSS（本脚本因此把输入生成也放在独立子进程里，保持父进程轻量）。
    这里让构建进程退出前自报 RUSAGE_SELF 峰值，是最直接的证据。
    """
    if reversed_volumes:
        # 同一批分卷换个处理顺序：另起一个只有清单的包目录，
        # 全部输入文件指回原包，仅 volumes 顺序倒过来
        with open(os.path.join(pkg_dir, "manifest.json"),
                  encoding="utf-8") as fh:
            manifest = json.load(fh)
        rev_dir = pkg_dir + "-reversed"
        os.makedirs(rev_dir, exist_ok=True)
        base = os.path.join("..", os.path.basename(pkg_dir))
        files = dict(manifest["files"])
        for key in ("plan", "pulses", "devices"):
            files[key] = os.path.join(base, files[key])
        files["volumes"] = [os.path.join(base, v)
                            for v in reversed(files["volumes"])]
        manifest = dict(manifest)
        manifest["files"] = files
        with open(os.path.join(rev_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)
        pkg_dir = rev_dir
    wrapper = (
        "import resource, sys\n"
        "from chainage.cli import main\n"
        "rc = main()\n"
        "print('SELF_RSS_KB=' + str(resource.getrusage("
        "resource.RUSAGE_SELF).ru_maxrss), file=sys.stderr)\n"
        "sys.exit(rc)\n")
    argv = [sys.executable, "-c", wrapper, "acq-build",
            pkg_dir, lib_db, out_db]
    t0 = time.monotonic()
    proc = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True,
                          text=True)
    elapsed = time.monotonic() - t0
    rss_kib = 0
    for line in proc.stderr.splitlines():
        if line.startswith("SELF_RSS_KB="):
            rss_kib = int(line.split("=", 1)[1])
    out = proc.stderr + proc.stdout
    return elapsed, rss_kib, proc.returncode, out


# --------------------------------------------------------------------------- #
# 独立复核
# --------------------------------------------------------------------------- #
def load_windows(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, round_key, window_key, start_utc, end_utc "
        "FROM plan_window ORDER BY start_utc").fetchall()
    conn.close()
    return rows


def spot_check(db, windows, rng, n_read=30, n_photo=15):
    """逐窗暴力求交 + 精确有理数，独立复核归属与交叠明细。"""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    checks = {"reading_attr": 0, "reading_outside": 0, "photo_partial": 0}
    try:
        # 已归属读数：暴力重算应落同一窗口
        ids = [r[0] for r in conn.execute(
            "SELECT a.record_id FROM attribution a JOIN sample_record r "
            "ON r.id = a.record_id WHERE r.kind='reading'")]
        for rid in rng.sample(ids, min(n_read, len(ids))):
            row = conn.execute(
                "SELECT a.window_id, al.utc_num, al.utc_den "
                "FROM attribution a JOIN alignment al "
                "ON al.record_id = a.record_id WHERE a.record_id=?",
                (rid,)).fetchone()
            t = Fraction(int(row["utc_num"]), int(row["utc_den"]))
            hits = [w for w in windows
                    if w[3] <= t < w[4]]
            assert len(hits) == 1 and hits[0][0] == row["window_id"], \
                f"读数 #{rid} 归属复核失败"
            checks["reading_attr"] += 1
        # 完全窗外读数：暴力确认没有任何窗口包含它
        ids = [r[0] for r in conn.execute(
            "SELECT u.record_id FROM unattributed u JOIN sample_record r "
            "ON r.id = u.record_id "
            "WHERE u.category='outside_windows' AND r.kind='reading'")]
        for rid in rng.sample(ids, min(n_read, len(ids))):
            row = conn.execute(
                "SELECT utc_num, utc_den FROM alignment WHERE record_id=?",
                (rid,)).fetchone()
            t = Fraction(int(row["utc_num"]), int(row["utc_den"]))
            assert not any(w[3] <= t < w[4] for w in windows), \
                f"读数 #{rid} 窗外复核失败"
            checks["reading_outside"] += 1
        # 局部交叠照片：重算交叠与空档，逐行比对
        ids = [r[0] for r in conn.execute(
            "SELECT record_id FROM unattributed "
            "WHERE category='partial_overlap'")]
        for rid in rng.sample(ids, min(n_photo, len(ids))):
            row = conn.execute(
                "SELECT open_num, open_den, close_num, close_den "
                "FROM alignment WHERE record_id=?", (rid,)).fetchone()
            o = Fraction(int(row["open_num"]), int(row["open_den"]))
            c = Fraction(int(row["close_num"]), int(row["close_den"]))
            want_ov = []
            for w in windows:
                lo = max(o, Fraction(w[3]))
                hi = min(c, Fraction(w[4]))
                if lo < hi:
                    want_ov.append((w[0], lo, hi))
            got_ov = [(r["window_id"],
                       Fraction(int(r["lo_num"]), int(r["lo_den"])),
                       Fraction(int(r["hi_num"]), int(r["hi_den"])))
                      for r in conn.execute(
                          "SELECT * FROM photo_overlap WHERE record_id=? "
                          "ORDER BY window_id", (rid,))]
            assert sorted(got_ov) == sorted(want_ov), \
                f"照片 #{rid} 交叠复核失败"
            want_gaps = []
            prev = o
            for _w, lo, hi in want_ov:
                if prev < lo:
                    want_gaps.append((prev, lo))
                prev = hi
            if prev < c:
                want_gaps.append((prev, c))
            got_gaps = [(Fraction(int(r["lo_num"]), int(r["lo_den"])),
                         Fraction(int(r["hi_num"]), int(r["hi_den"])))
                        for r in conn.execute(
                            "SELECT * FROM photo_gap WHERE record_id=? "
                            "ORDER BY seq_no", (rid,))]
            assert got_gaps == want_gaps, f"照片 #{rid} 空档复核失败"
            checks["photo_partial"] += 1
    finally:
        conn.close()
    return checks


def dump_hash(db, tables):
    """结果表逐行转储的指纹（排除 meta/inputs）。"""
    conn = sqlite3.connect(db)
    import hashlib
    h = hashlib.sha256()
    try:
        for t in tables:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            for row in conn.execute(
                    f"SELECT * FROM {t} ORDER BY {', '.join(cols)}"):
                h.update(repr(tuple(row)).encode() + b"\n")
    finally:
        conn.close()
    return h.hexdigest()


RESULT_TABLES = [
    "sample_record", "placement", "expand_block", "alignment",
    "attribution", "unattributed", "photo_overlap", "photo_gap",
    "window_channel_count", "plan_window", "evidence",
]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", default="data/acq-scale")
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--windows", type=int, default=100_000)
    ap.add_argument("--devices", type=int, default=40)
    ap.add_argument("--volumes", type=int, default=8)
    ap.add_argument("--boots-per-device", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20261121)
    ap.add_argument("--skip-determinism", action="store_true")
    ap.add_argument("--generate-only", action="store_true",
                    help="只在独立进程中生成输入（父进程保持轻量，"
                         "构建量测才准确）")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    manifest_path = os.path.join(args.workdir, "generated_manifest.json")

    if args.generate_only:
        pkg_dir = os.path.join(args.workdir, "pkg")
        manifest = generate_acq_pack(pkg_dir, args.rows, args.windows,
                                     args.devices, args.volumes,
                                     args.boots_per_device, args.seed)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)
        return

    report = {"params": {k: v for k, v in vars(args).items()
                         if k != "generate_only"}}

    print("=" * 70)
    print(f"生成输入（独立子进程）：{args.rows:,} 行 / {args.windows:,} 窗口 / "
          f"{args.devices} 设备 / {args.volumes} 卷")
    print("=" * 70)
    lib_pkg = os.path.join(args.workdir, "chainage_pkg")
    make_chainage_pkg(lib_pkg, ["REG-1", "REG-2", "REG-3"])
    lib_db = os.path.join(args.workdir, "lib.db")
    if os.path.exists(lib_db):
        os.remove(lib_db)
    build_chainage(lib_pkg, lib_db)

    t0 = time.monotonic()
    gen_argv = [sys.executable, os.path.abspath(__file__),
                "--workdir", args.workdir, "--rows", str(args.rows),
                "--windows", str(args.windows), "--devices", str(args.devices),
                "--volumes", str(args.volumes),
                "--boots-per-device", str(args.boots_per_device),
                "--seed", str(args.seed), "--generate-only"]
    subprocess.run(gen_argv, check=True, cwd=REPO_ROOT)
    gen_s = time.monotonic() - t0
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    pkg_dir = os.path.join(args.workdir, "pkg")
    print(f"输入生成 {gen_s:.1f}s：唯一记录 {manifest['unique_records']:,}，"
          f"重复行 {manifest['duplicate_lines']:,}，"
          f"读数 {manifest['readings']:,}，照片 {manifest['photos']:,}，"
          f"回执 {manifest['receipts']:,}")
    report["manifest"] = manifest

    out_db = os.path.join(args.workdir, "acq.db")
    for stale in (out_db, out_db + ".tmp"):
        if os.path.exists(stale):
            os.remove(stale)
    print("子进程构建中（构建进程自报峰值 RSS）...")
    elapsed, rss_kib, rc, out = run_build(pkg_dir, lib_db, out_db)
    print(f"构建 {elapsed:.1f}s，构建进程峰值 RSS "
          f"{rss_kib / 1024:.1f} MiB（上限 256 MiB）")
    if rc != 0:
        print(out)
        raise SystemExit(f"构建失败 rc={rc}")
    report["build"] = {"elapsed_s": round(elapsed, 2),
                       "rss_mib": round(rss_kib / 1024, 1)}
    assert rss_kib <= RSS_LIMIT_KIB, \
        f"峰值 RSS {rss_kib / 1024:.1f} MiB 超过 256 MiB"

    conn = sqlite3.connect(out_db)
    meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    conn.close()
    stats = {k[5:]: int(v) for k, v in meta.items()
             if k.startswith("stat.")}
    report["stats"] = stats
    assert stats["records"] == manifest["unique_records"], \
        (stats["records"], manifest["unique_records"])
    assert stats["duplicate_lines"] == manifest["duplicate_lines"]
    assert stats["windows"] == manifest["windows"]
    print(f"记录 {stats['records']:,}（重复行 {stats['duplicate_lines']:,}），"
          f"块 {stats['blocks']:,}，已归属读数 "
          f"{stats['attributed_readings']:,} + 照片 "
          f"{stats['attributed_photos']:,}，未归属 "
          f"{stats['unattributed']:,}（交叠行 "
          f"{stats['photo_overlap_rows']:,}，空档行 "
          f"{stats['photo_gap_rows']:,}）")

    # 候选扫描量：实际检查窗口行 vs 记录数 x 窗口数
    examined = stats["window_rows_examined"]
    naive = stats["records"] * stats["windows"]
    bound = (40 * (stats["readings"] + stats["photos"])
             + 3 * stats["photo_overlap_rows"] + 4 * stats["windows"])
    print(f"归因实际检查窗口行 {examined:,}；相交输出 "
          f"{stats['photo_overlap_rows']:,} 行；"
          f"若按 记录x窗口 扫描需 {naive:,}（比值 {naive / examined:.0f}x）")
    assert examined <= bound, (examined, bound)
    report["scan"] = {"window_rows_examined": examined,
                      "naive_product": naive,
                      "bound": bound}

    windows = load_windows(out_db)
    rng = random.Random(args.seed + 1)
    checks = spot_check(out_db, windows, rng)
    print(f"独立复核：已归属读数 {checks['reading_attr']} 条、"
          f"窗外读数 {checks['reading_outside']} 条、"
          f"局部交叠照片 {checks['photo_partial']} 张，全部一致")
    report["spot_checks"] = checks

    if not args.skip_determinism:
        out_db2 = os.path.join(args.workdir, "acq-reversed.db")
        for stale in (out_db2, out_db2 + ".tmp"):
            if os.path.exists(stale):
                os.remove(stale)
        print("同一批分卷按相反顺序再建一次（顺序无关性）...")
        elapsed2, rss2, rc2, out2 = run_build(
            pkg_dir, lib_db, out_db2, reversed_volumes=True)
        if rc2 != 0:
            print(out2)
            raise SystemExit(f"第二次构建失败 rc={rc2}")
        h1 = dump_hash(out_db, RESULT_TABLES)
        h2 = dump_hash(out_db2, RESULT_TABLES)
        same = h1 == h2
        print(f"结果表指纹 {'一致' if same else '不一致！'}"
              f"（第二次构建 {elapsed2:.1f}s，峰值 RSS "
              f"{rss2 / 1024:.1f} MiB）")
        assert same, "分卷处理顺序改变了结果"
        report["determinism"] = {"equal": same,
                                 "elapsed_s": round(elapsed2, 2),
                                 "rss_mib": round(rss2 / 1024, 1)}

    with open(os.path.join(args.workdir, "scale_report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"报告已写入 {os.path.join(args.workdir, 'scale_report.json')}")
    print("规模验收通过")


if __name__ == "__main__":
    main()
