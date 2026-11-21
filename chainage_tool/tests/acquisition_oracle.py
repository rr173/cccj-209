"""采集包验收的独立参考程序。

不调用产品任何模块：只用 fractions.Fraction 做精确有理数计算，
展开/对齐逐块直接模拟，归属与交叠/空档逐窗暴力求交。测试用它
在随机小输入上全量对照产品库的每条记录结果。
"""

from __future__ import annotations

from fractions import Fraction


def expand_boot(seq_records, M, G):
    """同一启动、已按 seq 排序的记录 -> [(rec, block_idx, expanded)]。

    rec 为 dict（含 seq/kind/count 等）。序号断开切新块；块内相邻序号
    取 [0,M) 非负模差，差 > G 抛 AssertionError（测试只生成合法输入）。
    """
    out = []
    prev_seq = prev_count = prev_exp = None
    block = -1
    for rec in seq_records:
        if prev_seq is None or rec["seq"] != prev_seq + 1:
            block += 1
            expanded = 0
        else:
            diff = (rec["count"] - prev_count) % M
            assert diff <= G, f"模差 {diff} > G={G}"
            expanded = prev_exp + diff
        out.append((rec, block, expanded))
        prev_seq, prev_count, prev_exp = rec["seq"], rec["count"], expanded
    return out


def _at(receipts, x):
    """receipts: [(expanded, utc)]（展开坐标严格递增）。
    返回 Fraction，或 'start' / 'end' / None（无回执）。"""
    if not receipts:
        return None
    if x < receipts[0][0]:
        return "start"
    if x > receipts[-1][0]:
        return "end"
    for e, u in receipts:
        if e == x:
            return Fraction(u)
    for (e1, u1), (e2, u2) in zip(receipts, receipts[1:]):
        if e1 < x < e2:
            return Fraction(u1) + Fraction(u2 - u1) * (x - e1) / (e2 - e1)
    raise AssertionError("unreachable")


def align_block(block_records, pulses):
    """block_records: [(rec, block, expanded)]（同一块）。
    返回 {id(rec): (status, utc | (open, close) | None)}。"""
    receipts = [(exp, pulses[rec["pulse"]])
                for rec, _, exp in block_records if rec["kind"] == "receipt"]
    out = {}
    for rec, _, x in block_records:
        if rec["kind"] == "receipt":
            continue
        if rec["kind"] == "reading":
            u = _at(receipts, x)
            if u is None:
                out[id(rec)] = ("no_receipts", None)
            elif u == "start":
                out[id(rec)] = ("start_unconstrained", None)
            elif u == "end":
                out[id(rec)] = ("end_unconstrained", None)
            else:
                out[id(rec)] = ("ok", u)
        else:  # photo
            d = rec["exposure"]
            if not receipts:
                out[id(rec)] = ("no_receipts", None)
            elif x < receipts[0][0]:
                out[id(rec)] = ("start_unconstrained", None)
            elif x + d > receipts[-1][0]:
                out[id(rec)] = ("end_unconstrained", None)
            else:
                out[id(rec)] = ("ok", (_at(receipts, x),
                                       _at(receipts, x + d)))
    return out


def attribute_reading(t, windows):
    """windows: 该轮次窗口 dict 列表（start/end 可比较）。返回窗口或 None。"""
    hits = [w for w in windows if w["start"] <= t < w["end"]]
    assert len(hits) <= 1
    return hits[0] if hits else None


def attribute_photo(o, c, windows):
    """返回 ('attributed', window) | ('outside',) |
    ('partial', overlaps=[(window, lo, hi)], gaps=[(lo, hi)])。"""
    for w in windows:
        if w["start"] <= o and c <= w["end"]:
            return ("attributed", w)
    overlaps = []
    for w in windows:
        lo = max(o, Fraction(w["start"]))
        hi = min(c, Fraction(w["end"]))
        if lo < hi:
            overlaps.append((w, lo, hi))
    if not overlaps:
        return ("outside",)
    gaps = []
    prev = o
    for _w, lo, hi in overlaps:
        if prev < lo:
            gaps.append((prev, lo))
        prev = hi
    if prev < c:
        gaps.append((prev, c))
    return ("partial", overlaps, gaps)


def oracle_run(records_by_boot, pulses, devices, windows_by_round):
    """对整包输入跑参考计算。

    records_by_boot: {(device, boot): [rec, ...]}（未排序，函数内按 seq 排）
    pulses: {pulse: utc}; devices: {device: (M, G)}
    windows_by_round: {round: [window dict]}（window 含 start/end/key）
    返回 {id(rec): {"status":..., "utc"/"open"/"close": Fraction,
                    "result": ("attributed", window) / (category, ...)}}。
    """
    results = {}
    for (device, _boot), recs in records_by_boot.items():
        M, G = devices[device]
        ordered = sorted(recs, key=lambda r: r["seq"])
        expanded = expand_boot(ordered, M, G)
        blocks = {}
        for rec, blk, exp in expanded:
            blocks.setdefault(blk, []).append((rec, blk, exp))
        for blk, block_records in blocks.items():
            aligned = align_block(block_records, pulses)
            for rec, _, x in block_records:
                if rec["kind"] == "receipt":
                    continue
                status, val = aligned[id(rec)]
                entry = {"status": status}
                if status == "ok":
                    if rec["kind"] == "reading":
                        entry["utc"] = val
                        w = attribute_reading(
                            val, windows_by_round[rec["round"]])
                        entry["result"] = ("attributed", w) if w else \
                            ("outside",)
                    else:
                        entry["open"], entry["close"] = val
                        entry["result"] = attribute_photo(
                            val[0], val[1], windows_by_round[rec["round"]])
                else:
                    entry["result"] = (status,)
                results[id(rec)] = entry
    return results
