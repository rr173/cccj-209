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
chainage/            产品代码
  models/engine/database/build/query   既有桩号换算库
  intervals.py                         整数闭区间集合运算
  settlement_input.py                  义务表/巡检簿解析、校验、暂存
  settlement.py                        巡检履约结算（可见版本、片段定位、分界扫描）
  cli.py                               命令行入口
tests/               验收测试 + 独立逐刻度参考程序
  reference_oracle.py                  桩号换算的逐刻度参考程序
  settlement_oracle.py                 巡检结算的独立逐刻度参考程序
  settlement_fixture.py                结算测试夹具
scripts/             规模验收与示例生成
docs/FORMAT.md       交接包、库格式与结算输入/输出说明
examples/demo_pkg/   文档化示例交接包
examples/settlement_pkg/ , examples/settlement/
                     文档化结算示例（义务表 + 巡检簿）
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

## 巡检履约结算（离线）

在既有 ready 换算库之上，按一张**巡检义务表**和一份**巡检簿**（可乱序送达
的更正记录）结算每个物理刻度上的履约状态。它**不是**新的桩号换算结果：
换算库只负责把巡检记录定位到物理轴。

```bash
# 0) 生成文档化示例（交接包 + 义务表 + 巡检簿）
python3 -I scripts/make_settlement_example.py \
    examples/settlement_pkg examples/settlement
python3 -m chainage build examples/settlement_pkg data/settlement.db

# 1) 单截止点：按义务、轴、物理闭区间出完整台账，并可直接取两张清单
python3 -m chainage settle data/settlement.db \
    examples/settlement/obligations.json examples/settlement/trips.jsonl \
    --cutoff 1795217400 --text
python3 -m chainage settle data/settlement.db \
    examples/settlement/obligations.json examples/settlement/trips.jsonl \
    --cutoff 1795217400 --list dispatch --text   # 可直接派人补巡
python3 -m chainage settle data/settlement.db \
    examples/settlement/obligations.json examples/settlement/trips.jsonl \
    --cutoff 1795217400 --list review  --text    # 先核对行车记录

# 2) 两个截止点：列出状态改变的区段、前后状态及各门槛人数
python3 -m chainage settle-diff data/settlement.db \
    examples/settlement/obligations.json examples/settlement/trips.jsonl \
    --before 1795214700 --after 1795217400 --text

# 3) 单点取证：某义务在某物理位置的逐门槛解释（可追到登记与源标定行）
python3 -m chainage settle-explain data/settlement.db \
    examples/settlement/obligations.json examples/settlement/trips.jsonl \
    --cutoff 1795217400 --obligation O1 --x 11 --text
```

三张输出如何对应：

- `settle` 的 `ledger.obligations[].segments` 是**完整台账**（每个区间一条，
  带各门槛上下界）；`ledger.dispatch` / `ledger.review` 是从同一份台账按状态
  切出的两张清单，分别就是"派工"和"先核对"两张纸，不另算一遍；
- `settle-diff` 只列 `状态改变` 的区段，按两个截止点各自的实际分界对齐，
  不把只改了人数的路段误报成换了派工状态；各段附前后门槛人数；
- `settle-explain` 针对某一刻度、某一截止点，逐门槛列出确定班组与仅可能
  班组及其贡献趟次，标明每趟采用的入簿版本、每条路线版本是否命中、命中
  片段引用的登记与源标定行。计数与 `settle` 在该刻度的结果逐项一致。

结算语义（与 `docs/FORMAT.md` 第 9 节一致）：

- **可见版本**：`ingest_time <= cutoff` 的最后一条记录整条替换旧版；`void`
  使该趟不可用；只有可见版本才解析引用（旧版/未来版的坏引用不解析）。
- **路线版本**：任一完整版本到过刻度 -> "可能巡过"；每个完整版本都到过 ->
  "确定巡过"。不会拼出单一真实路线，也不假设各刻度的路线可同时成立。
- **门槛**：主窗与各子窗口各自统计；趟次须车道相符、实测时刻落在半开窗口。
  同一班组可分别满足不同门槛；按班组去重，确定计下界、可能计上界。
  全部门槛下界达标 -> `MET`；任一门槛上界不足 -> `UNMET`（确定缺巡）；
  否则 -> `REVIEW`（需核实）。
- **范围与内存**：不按刻度铺表、不枚举路网路线组合、不为每个区段复制整套
  班组证据；长无分界段压缩为一条。校验失败（结构错误、重复入簿键、引用
  无法解释）聚合并抛错，**不输出任何部分清单**。


## 验收测试

测试含题目指定的断链轴（S/T 源段、U/V 目标段）、四态、别名、反查不改写、
两位置同号、缺目标、跨轴复用段名、非法相接重叠、整数/非整数交点、坏数据定位、
失败不覆盖旧库，并用**独立的逐刻度参考程序**（不调用产品分段/索引逻辑）
在随机交接包上全量对照正查展开结果与反查命中刻度集合及证据：

```bash
python3 -I tests/test_acceptance.py
```

巡检结算另有独立验收：

```bash
python3 -I tests/test_settlement.py
```

覆盖题目主样例（派工 10、14；核实 11、13；12 达标）、更正复算（11 变缺巡、
13 变达标，且不污染先前截止点）、16:00 边界、撤销、另轴同数值、相邻刻度
同人数不同班组、不可解释引用的定位与拒绝、重复入簿键与结构错误、旧版/
未来版坏引用不解析，并用**独立的逐刻度结算参考程序** `settlement_oracle.py`
在短道路随机数据上全量核对每项义务各刻度各窗的班组上下界、状态与取证。

## 规模验收

```bash
python3 -I scripts/scale_check.py --workdir data/scale
```

- 组 1：一条 10 亿刻度、无中间边界的登记——分段数恒为 1，库约 80 KiB；
- 组 2：1.2 万条不相关标定 + 3003 条登记 + 高重叠热区——报告 EXPLAIN QUERY PLAN
  （命中 `idx_target_lookup`）、实际重叠索引项、墙钟耗时与峰值内存。

参考机器上的实测数量级见 `data/scale/scale_report.json`（运行后生成）：
热区反查毫秒级，访问的索引项与全库登记规模解耦；构建峰值 Python 堆约十几 MiB。

巡检结算规模验收：

```bash
python3 -I scripts/settlement_scale_check.py --workdir data/scale-settle
```

- 组 1：500 万刻度的长无分界义务 + 数千条无关趟次 + 反复巡同一段——区段数
  恒定、与轴长无关；报告巡检记录数、可见趟次、解析片段数、耗时与峰值内存；
- 组 2：多轴、多义务、密集重复上报——展示工作量随相关片段与状态分界增长，
  并实际报告 `tracemalloc` 峰值与进程峰值 RSS。

## 工作内存边界

- 标定工作集按 `(体系, 轴)` 驻留，规模随**标定行数**线性增长（每行是很小的
  固定字段对象，1.2 万行约数 MiB），与刻度跨度无关；
- 登记处理逐条进行，单条登记的候选行、分段与证据随迭代释放；
- 分段、证据、反查索引全部落在 SQLite 文件；连接设 `temp_store=FILE`、
  有界 page cache，大查询的排序/临时表走磁盘；
- 若标定行数超过物理内存预算，可在 `build._load_calib_workset` 层替换为
  SQLite 临时表 + 键集分页（接口不变），无需改动引擎与查询。
- 巡检结算同样落盘：义务与巡检簿先入暂存库，可见版本用窗口函数在
  SQLite 内完成"取最后一条"，逐趟解析后直接写 `resolved_*` 索引表；
  每项义务按 `(轴, 车道, 时刻)` 索引只取相关趟次，不对义务全扫巡检簿，
  也不为输出区段复制整套班组证据。分界扫描只随相关片段与状态分界增长。

## 不入库的产物

`data/` 下的原始大数据、生成的库与规模报告均被 `.gitignore` 忽略；
示例小包 `examples/demo_pkg`、`examples/settlement_pkg`、
`examples/settlement` 随仓库提供（都很小，不含生成的大数据）。
