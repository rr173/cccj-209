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
