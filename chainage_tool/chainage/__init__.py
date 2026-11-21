"""道路桩号断链离线入仓工具。

模块划分
========
- errors:   校验/构建错误类型
- models:   交接包（清单、标定段表、登记表）的加载与严格校验
- engine:   仿射标定关系与登记区间的分段压缩换算引擎
- database: SQLite 库结构（落盘索引，不常驻进程内存）
- build:    全量校验 -> 临时库构建 -> 原子替换的入仓流程
- query:    正查（按登记 ID）与反查（按目标体系标牌范围）
- intervals: 整数闭区间集合运算
- settlement_input: 巡检义务表/巡检簿的解析、校验与暂存
- settlement: 巡检履约结算（可见版本、片段定位、分界扫描、取证）
- defects:  病害报件归并工作区（批、裁定、病害组、候选、修订与增量）
- acquisition: 巡测车采集包装配（计划/脉冲/设备/分卷 -> SQLite 采集包）
- acquisition_query: 采集包查询（窗口采样表、未归属、回看、摘要）
- cli:      命令行入口

本包只处理编号体系之间的整数刻度换算；在此之上提供离线的巡检履约结算、
病害报件归并工作区与巡测车采集包装配，不做 GPS 匹配、线几何或路线规划。
"""

__version__ = "1.0.0"

# 四种逐刻度业务状态（英文标识用于库内稳定存储，中文说明见 FORMAT.md）
STATUS_NO_SOURCE = "NO_SOURCE"        # 没有源位置
STATUS_MULTI = "MULTIPLE_SOURCE"      # 多个源物理位置
STATUS_NO_TARGET = "NO_TARGET"        # 源位置唯一但没有目标标牌
STATUS_RESOLVED = "RESOLVED"          # 源位置唯一且有至少一个目标标牌

ALL_STATUSES = (
    STATUS_NO_SOURCE,
    STATUS_MULTI,
    STATUS_NO_TARGET,
    STATUS_RESOLVED,
)

# 仅 RESOLVED 段可自动入仓
WAREHOUSABLE_STATUSES = (STATUS_RESOLVED,)
