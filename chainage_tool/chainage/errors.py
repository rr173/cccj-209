"""错误类型。

所有校验错误都带 ``location``，定位到交接包内的具体文件与行：
- ``calibration.jsonl:12`` 表示标定表第 12 行（物理行号，从 1 起，含坏行）
- ``registration.jsonl:3`` 表示登记表第 3 行
- ``manifest.json`` 表示清单文件本身

构建阶段收集到的全部错误通过 :class:`PackageValidationError` 一次性抛出，
调用方（CLI / 脚本）可逐条展示每次拒绝自动入仓的测量依据。
"""

from __future__ import annotations

from typing import List, Optional


class ChainageError(Exception):
    """本工具所有自定义异常的基类。"""


class PackageValidationError(ChainageError):
    """交接包校验失败：聚合所有定位错误，而不是遇到第一条就停。"""

    def __init__(self, errors: List["ValidationError"]):
        self.errors: List[ValidationError] = list(errors)
        summary = "; ".join(str(e) for e in self.errors[:10])
        if len(self.errors) > 10:
            summary += f"; ...（共 {len(self.errors)} 条）"
        super().__init__(f"交接包校验失败，{len(self.errors)} 条错误: {summary}")


class ValidationError:
    """单条校验错误（值对象，可序列化）。"""

    __slots__ = ("location", "code", "message", "row_id")

    def __init__(
        self,
        location: str,
        code: str,
        message: str,
        row_id: Optional[str] = None,
    ) -> None:
        self.location = location
        self.code = code
        self.message = message
        # 出错行自报的行标识（若该行连 id 都是坏的，则为 None）
        self.row_id = row_id

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        rid = f" [id={self.row_id}]" if self.row_id is not None else ""
        return f"{self.location}{rid}: {self.code} {self.message}"

    def to_dict(self) -> dict:
        return {
            "location": self.location,
            "code": self.code,
            "message": self.message,
            "row_id": self.row_id,
        }


class DatabaseError(ChainageError):
    """库文件缺失、版本不符或查询阶段的结构问题。"""


class BuildInProgressError(ChainageError):
    """旧库仍处于未完成的构建状态（正常流程下临时库会被清理）。"""


class SettlementIssue:
    """巡检履约结算的一条可定位问题。

    与交接包校验不同，巡检簿的问题必须能直接指回 **趟次 / 入簿时刻 /
    片段**，班组才能把更正记录改对；义务/结构问题则带文件物理行号。
    """

    __slots__ = ("location", "code", "message", "trip_id", "ingest_time",
                 "route_index", "fragment_index", "obligation_id")

    def __init__(self, location: str, code: str, message: str,
                 trip_id: Optional[str] = None,
                 ingest_time: Optional[int] = None,
                 route_index: Optional[int] = None,
                 fragment_index: Optional[int] = None,
                 obligation_id: Optional[str] = None) -> None:
        self.location = location
        self.code = code
        self.message = message
        self.trip_id = trip_id
        self.ingest_time = ingest_time
        self.route_index = route_index
        self.fragment_index = fragment_index
        self.obligation_id = obligation_id

    def __str__(self) -> str:
        parts = [self.location, self.code]
        if self.trip_id is not None:
            parts.append(f"trip={self.trip_id}")
        if self.ingest_time is not None:
            parts.append(f"ingest={self.ingest_time}")
        if self.route_index is not None:
            parts.append(f"route={self.route_index}")
        if self.fragment_index is not None:
            parts.append(f"fragment={self.fragment_index}")
        return f"{' '.join(parts)}: {self.message}"

    def to_dict(self) -> dict:
        out = {
            "location": self.location,
            "code": self.code,
            "message": self.message,
        }
        if self.trip_id is not None:
            out["trip_id"] = self.trip_id
        if self.ingest_time is not None:
            out["ingest_time"] = self.ingest_time
        if self.route_index is not None:
            out["route_index"] = self.route_index
        if self.fragment_index is not None:
            out["fragment_index"] = self.fragment_index
        if self.obligation_id is not None:
            out["obligation_id"] = self.obligation_id
        return out


class SettlementValidationError(ChainageError):
    """巡检义务表或巡检簿校验失败：聚合全部问题，绝不留下部分清单。"""

    def __init__(self, issues: List[SettlementIssue]):
        self.issues: List[SettlementIssue] = list(issues)
        summary = "; ".join(str(i) for i in self.issues[:10])
        if len(self.issues) > 10:
            summary += f"; ...（共 {len(self.issues)} 条）"
        super().__init__(f"巡检履约结算校验失败，{len(self.issues)} 条问题: "
                         f"{summary}")


class DefectIssue:
    """病害归并工作区的一条可定位问题（结构、引用或批文件冲突）。"""

    __slots__ = ("location", "code", "message", "source", "source_ref",
                 "other_source", "other_ref", "batch_no")

    def __init__(self, location: str, code: str, message: str,
                 source: Optional[str] = None,
                 source_ref: Optional[str] = None,
                 other_source: Optional[str] = None,
                 other_ref: Optional[str] = None,
                 batch_no: Optional[str] = None) -> None:
        self.location = location
        self.code = code
        self.message = message
        self.source = source
        self.source_ref = source_ref
        self.other_source = other_source
        self.other_ref = other_ref
        self.batch_no = batch_no

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        parts = [self.location, self.code]
        if self.source is not None:
            parts.append(f"report={self.source}/{self.source_ref}")
        if self.other_source is not None:
            parts.append(f"other={self.other_source}/{self.other_ref}")
        return f"{' '.join(parts)}: {self.message}"

    def to_dict(self) -> dict:
        out = {"location": self.location, "code": self.code,
               "message": self.message}
        if self.source is not None:
            out["source"] = self.source
            out["source_ref"] = self.source_ref
        if self.other_source is not None:
            out["other_source"] = self.other_source
            out["other_ref"] = self.other_ref
        if self.batch_no is not None:
            out["batch_no"] = self.batch_no
        return out


class DefectValidationError(ChainageError):
    """自带问题列表的病害工作区错误（结构/引用/批冲突），整批拒绝。"""

    def __init__(self, issues: List[DefectIssue]):
        self.issues: List[DefectIssue] = list(issues)
        summary = "; ".join(str(i) for i in self.issues[:10])
        if len(self.issues) > 10:
            summary += f"; ...（共 {len(self.issues)} 条）"
        super().__init__(f"病害工作区校验失败，{len(self.issues)} 条问题: "
                         f"{summary}")


class BatchRejected(DefectValidationError):
    """批文件被拒绝（重号不同内容、过期基准修订、输入错误等）。"""

    def __init__(self, issues: List[DefectIssue], code: str = "BATCH_REJECTED"):
        self.code = code
        super().__init__(issues)
