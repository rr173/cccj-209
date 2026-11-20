"""病害归并工作区的独立参考程序（验收对照，不调用产品实现）。

它故意写得直白、慢、全量重建：

1. 维护一份报件内容表 ``reports``（含在册/撤下）和一份裁定表 ``adj``；
2. **每批结束后**都从空表重建全图：所有在册报件各自为点，把当前生效
   的「同一」边做并查集，组键取组内最小报件键（按 Unicode 码点序）；
3. 组内有任一生效「不同」边 -> 隔离；
4. 两组表的行（组键、隔离、成员数、首次/末次发现；成员的组键与内容）
   全量算出，不做任何增量/局部优化；
5. 候选配对逐对枚举全部在册报件（不预筛），用阈值逐项判断。

参考程序不读 SQLite、不共享产品的定位/候选/组逻辑，用来在小数据上
逐批核对产品的两张表、增量重放、候选集合与隔离证据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

Key = Tuple[str, str]


@dataclass
class OReport:
    source: str
    source_ref: str
    category: str
    lane: str
    found_at: int
    reg_id: str
    reg_n: int
    present: bool = True
    # 由调用方提供（产品定位结果不参与组/候选的图判断；候选用 axis/x）
    location_status: str = "localizable"
    axis_id: Optional[str] = None
    x: Optional[int] = None

    @property
    def key(self) -> Key:
        return (self.source, self.source_ref)


def key_json(key: Key) -> str:
    return json.dumps([key[0], key[1]], ensure_ascii=False,
                      separators=(",", ":"))


def norm_pair(a: Key, b: Key) -> Optional[Tuple[Key, Key]]:
    if a == b:
        return None
    return (a, b) if a < b else (b, a)


class Oracle:
    def __init__(self, spatial_threshold: int, time_threshold: int):
        self.st = spatial_threshold
        self.tt = time_threshold
        self.reports: Dict[Key, OReport] = {}
        # 每对至多一条裁定；kind = same / different；present = 生效与否
        self.adj: Dict[Tuple[Key, Key], dict] = {}
        self.revision = 0

    # ---------------- 批次 ----------------
    def apply(self, batch: dict) -> None:
        for r in batch.get("reports", []):
            key = (r["source"], r["source_ref"])
            old = self.reports.get(key)
            rep = OReport(r["source"], r["source_ref"], r["category"],
                          r["lane"], r["found_at"], r["reg_id"], r["reg_n"])
            if old is not None:
                rep.location_status = old.location_status
                rep.axis_id = old.axis_id
                rep.x = old.x
            self.reports[key] = rep
        for v in batch.get("void", []):
            self.reports[(v["source"], v["source_ref"])].present = False
        for a in batch.get("adjudications", []):
            ka = (a["a"]["source"], a["a"]["source_ref"])
            kb = (a["b"]["source"], a["b"]["source_ref"])
            pair = norm_pair(ka, kb)
            decision = a["decision"]
            if decision == "clear":
                if pair in self.adj:
                    self.adj[pair]["present"] = False
            else:
                self.adj[pair] = {"kind": decision, "present": True}
        self.revision += 1

    def set_location(self, key: Key, status: str, axis_id: Optional[str],
                     x: Optional[int]) -> None:
        self.reports[key].location_status = status
        self.reports[key].axis_id = axis_id
        self.reports[key].x = x

    # ---------------- 组 ----------------
    def present_keys(self) -> List[Key]:
        return sorted(k for k, r in self.reports.items() if r.present)

    def _parent_map(self) -> Dict[Key, Key]:
        keys = self.present_keys()
        parent = {k: k for k in keys}

        def find(k: Key) -> Key:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        def union(a: Key, b: Key) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                if ra < rb:
                    parent[rb] = ra
                else:
                    parent[ra] = rb

        for (a, b), info in self.adj.items():
            if info["present"] and info["kind"] == "same":
                if a in parent and b in parent:
                    union(a, b)
        return {k: find(k) for k in keys}

    def groups(self) -> Dict[str, dict]:
        parent = self._parent_map()
        comps: Dict[Key, List[Key]] = {}
        for k, root in parent.items():
            comps.setdefault(root, []).append(k)
        out: Dict[str, dict] = {}
        for keys in comps.values():
            keys.sort()
            gk = key_json(keys[0])
            keyset = set(keys)
            isolated = any(
                info["present"] and info["kind"] == "different"
                and a in keyset and b in keyset
                for (a, b), info in self.adj.items())
            times = [self.reports[k].found_at for k in keys]
            out[gk] = {"group_key": gk, "isolated": isolated,
                       "member_count": len(keys),
                       "first_found": min(times), "last_found": max(times),
                       "keys": keys}
        return out

    def member_rows(self) -> Dict[Key, dict]:
        groups = self.groups()
        out: Dict[Key, dict] = {}
        for gk, g in groups.items():
            for k in g["keys"]:
                r = self.reports[k]
                out[k] = {
                    "source": k[0], "source_ref": k[1], "group_key": gk,
                    "category": r.category, "lane": r.lane,
                    "found_at": r.found_at,
                }
        return out

    def group_rows(self) -> Dict[str, dict]:
        return {gk: {kk: g[kk] for kk in
                     ("group_key", "isolated", "member_count",
                      "first_found", "last_found")}
                for gk, g in self.groups().items()}

    # ---------------- 候选 ----------------
    def candidates(self, start: Key) -> List[Key]:
        r0 = self.reports[start]
        if not r0.present or r0.location_status != "localizable":
            return []
        adjudicated = {pair for pair, info in self.adj.items()
                       if info["present"]
                       and (pair[0] == start or pair[1] == start)}
        out: List[Key] = []
        for k, r in sorted(self.reports.items()):
            if k == start or not r.present:
                continue
            if r.source == r0.source:
                continue
            if r.location_status != "localizable":
                continue
            if (r.category, r.lane, r.axis_id) != (r0.category, r0.lane,
                                                   r0.axis_id):
                continue
            if abs(r.x - r0.x) > self.st:
                continue
            if abs(r.found_at - r0.found_at) > self.tt:
                continue
            # "还没有直接裁定"按当前生效的裁定计：撤去后重新成为候选。
            if norm_pair(start, k) in adjudicated:
                continue
            out.append(k)
        return out

    # ---------------- 隔离证据 ----------------
    def same_path(self, start: Key, goal: Key,
                  allowed: Set[Key]) -> Optional[List[Key]]:
        prev: Dict[Key, Optional[Key]] = {start: None}
        queue = [start]
        while queue:
            cur = queue.pop(0)
            if cur == goal:
                break
            for (a, b), info in self.adj.items():
                if not (info["present"] and info["kind"] == "same"):
                    continue
                if a == cur:
                    nb = b
                elif b == cur:
                    nb = a
                else:
                    continue
                if nb in prev or nb not in allowed:
                    continue
                if not self.reports[nb].present:
                    continue
                prev[nb] = cur
                queue.append(nb)
        if goal not in prev:
            return None
        path: List[Key] = []
        cur: Optional[Key] = goal
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    def isolation_edges(self, group_key: str) -> List[Tuple[Key, Key]]:
        g = self.groups().get(group_key)
        if g is None or not g["isolated"]:
            return []
        keys = set(g["keys"])
        return [(a, b) for (a, b), info in sorted(self.adj.items())
                if info["present"] and info["kind"] == "different"
                and a in keys and b in keys]
