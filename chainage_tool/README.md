# 道路桩号断链离线入仓工具

道路养护与测量承包方用两套桩号登记同一批设施。旧线接入新线处会断链
（跳号、回退复号），部分测量区段沿基准轴反向编号。本工具在**完全离线**的
前提下，用 Python 标准库 + SQLite 完成登记点/登记区间的换算入仓，并让班组
能查回每次拒绝自动入仓的测量依据。

明确**不做**：GPS 匹配、线几何处理、巡检覆盖评估。

## 特性对照需求

- 严格校验并定位原行：重复行标识、坏字段、未知轴、未知体系/段、int64 溢出、
  同段闭区间重叠（含端点相接）；合法断链复号不误拒。
- 四种逐刻度状态 `NO_SOURCE / MULTIPLE_SOURCE / NO_TARGET / RESOLVED`，
  仅 `RESOLVED` 可自动入仓；多位置即使换成同一目标编号仍判多位置。
- 同位置多标牌按别名处理；同 `(段,编号)` 的多标定行合并但证据不消失。
- **分段压缩**：库存恒定结构区间而非逐刻度行；十亿刻度无边界登记只占一行。
  唯一整数交点准确单列，有理非整数交点不合并。
- 反查走目标侧区间索引，按实际命中裁切并还原方向，返回登记 ID、实际命中源
  刻度范围、候选位置与**原状态**（命中争议位置也不改写为唯一），不扫登记表、
  不重读交接包。
- 构建走临时库 + 单事务 + 原子替换：校验和索引全部完成后才可供查询，
  失败不覆盖已有库、不留半个结果。
- 结构化记录含交接版本、正反向精确实时区间（仿射参数现算）、登记与标定行标识。

## 目录

```
chainage/            产品代码（models 校验 / engine 分段 / database / build / query / cli）
tests/               验收测试 + 独立逐刻度参考程序 reference_oracle.py
scripts/             规模验收 scale_check.py、示例生成 make_example.py
docs/FORMAT.md       交接包与库格式说明
examples/demo_pkg/   文档化示例交接包
data/                运行产物（Git 忽略）
```

## 快速开始

```bash
# 1) 只校验
python3 -m chainage validate examples/demo_pkg

# 2) 构建离线库（失败不覆盖旧库）
python3 -m chainage build examples/demo_pkg data/demo.db

# 3) 正查：按登记 ID 取完整换算明细与可入仓区段
python3 -m chainage forward data/demo.db R103
python3 -m chainage forward data/demo.db R103 --from 104 --to 104 --text

# 4) 反查：按目标体系标牌点/区间选查，可再限轴或目标段
python3 -m chainage reverse data/demo.db NEW --n 899 --text
python3 -m chainage reverse data/demo.db NEW --from 899 --to 900 --segment V

# 5) 复核某条标定依据 / 查看反查查询计划 / 列登记
python3 -m chainage calib data/demo.db T1
python3 -m chainage plan  data/demo.db NEW --n 899
python3 -m chainage regs  data/demo.db
```

读取不可信交接包时用隔离解释器（不加载 CWD 下被植入的模块）。`-I` 会忽略
`PYTHONPATH`，因此对模块入口用正常模式但把工作目录切到仓库外、显式传路径；
对脚本/测试则直接以文件路径运行：

```bash
# 模块入口：在仓库目录外调用，路径全部显式给出
cd /tmp && python3 /workspace/chainage_tool/chainage/cli.py build \
    /path/untrusted/pkg /path/out.db

# 脚本与测试（文件路径模式，-I 安全）
python3 -I /workspace/chainage_tool/tests/test_acceptance.py
python3 -I /workspace/chainage_tool/scripts/scale_check.py --workdir data/scale
```

## 验收测试

测试含题目指定的断链轴（S/T 源段、U/V 目标段）、四态、别名、反查不改写、
两位置同号、缺目标、跨轴复用段名、非法相接重叠、整数/非整数交点、坏数据定位、
失败不覆盖旧库，并用**独立的逐刻度参考程序**（不调用产品分段/索引逻辑）
在随机交接包上全量对照正查展开结果与反查命中刻度集合及证据：

```bash
python3 -I tests/test_acceptance.py
```

## 规模验收

```bash
python3 -I scripts/scale_check.py --workdir data/scale
```

- 组 1：一条 10 亿刻度、无中间边界的登记——分段数恒为 1，库约 80 KiB；
- 组 2：1.2 万条不相关标定 + 3003 条登记 + 高重叠热区——报告 EXPLAIN QUERY PLAN
  （命中 `idx_target_lookup`）、实际重叠索引项、墙钟耗时与峰值内存。

参考机器上的实测数量级见 `data/scale/scale_report.json`（运行后生成）：
热区反查毫秒级，访问的索引项与全库登记规模解耦；构建峰值 Python 堆约十几 MiB。

## 工作内存边界

- 标定工作集按 `(体系, 轴)` 驻留，规模随**标定行数**线性增长（每行是很小的
  固定字段对象，1.2 万行约数 MiB），与刻度跨度无关；
- 登记处理逐条进行，单条登记的候选行、分段与证据随迭代释放；
- 分段、证据、反查索引全部落在 SQLite 文件；连接设 `temp_store=FILE`、
  有界 page cache，大查询的排序/临时表走磁盘；
- 若标定行数超过物理内存预算，可在 `build._load_calib_workset` 层替换为
  SQLite 临时表 + 键集分页（接口不变），无需改动引擎与查询。

## 不入库的产物

`data/` 下的原始大数据、生成的库与规模报告均被 `.gitignore` 忽略；
示例小包 `examples/demo_pkg` 随仓库提供。
