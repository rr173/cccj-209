# 巡测车采集包装配（离线）——交付说明

巡测车交回的现场数据缺一张能真正交给质检员的采样表：车载计划写的是
UTC，路面传感器和相机各记各的循环计数器；照片晚一点关快门可能已跨出
采样窗口；设备掉电重启后计数又可能从同一个值重新开始。本工具把计划表、
参考脉冲表、设备配置和各设备 JSONL 分卷在**完全离线**的条件下装配成
一份 SQLite 采集包，交付：

- 每处计划窗口的已归属读数/照片索引与各通道数量（空窗口也列出，并
  逐一标出没有可用证据的期望通道）；
- 不能归属的原始记录单列（缺时钟 / 完全窗外 / 局部交叠互不混淆）；
- 任一采样行或未归属记录的回看入口：原始计数、卷行、展开块，以及
  确定该时刻（或曝光两个端点）所用的回执与其参考时刻。

明确**不做**：

- 不读取照片二进制（`resource` 只是资源标识，从不打开）；
- 不从采样数据推测行驶路线（计划表里的登记 ID 只核实它在 ready
  换算库中存在，并在结果里带上登记引用与换算库交接版本，不做桩号
  换算，也不改动换算库）。

## 1. 输入

采集交接包是一个目录，含 `manifest.json` 与其引用的文件：

```json
{
  "version": "acq-demo-2026-11-21",
  "files": {
    "plan": "plan.jsonl",
    "pulses": "pulses.jsonl",
    "devices": "devices.json",
    "volumes": ["volumes/vol-a.jsonl", "volumes/vol-b.jsonl"]
  }
}
```

`files.volumes` 的数组顺序就是分卷处理顺序；调整它不会改变归属、
诊断类别和窗口数量（见第 8 节）。

### 1.1 计划表 plan.jsonl（完全以参考时钟记载）

每行一个窗口：

```json
{"round": "R-7", "window": "W-b", "start_utc": 0, "end_utc": 8,
 "registration": "R104",
 "expected_channels": [["survey-cam", "still"], ["survey-cam", "probe"]]}
```

- `round` / `window`：巡测轮次 / 窗口号，合起来是窗口键；同一轮次内
  窗口互不重叠（可以紧邻或留空），不同轮次可同时发生，轮次由计划表
  列出；
- `start_utc` / `end_utc`：UTC 纳秒**半开**区间 `[start, end)`，
  有符号 64 位整数；
- `registration`：既有 ready 换算库里的登记 ID。装配时只核实它存在，
  并在结果里带上登记引用和换算库的交接版本；
- `expected_channels`：该窗口期望的采集通道名单。通道以
  `[设备号, 通道号]` 共同标识，必须出现在设备配置的合法通道里。

### 1.2 参考脉冲表 pulses.jsonl

```json
{"pulse": "P-14", "utc": -10}
```

唯一的脉冲号对应到 UTC 纳秒（int64）。脉冲号重复是输入错误。

### 1.3 设备配置 devices.json

```json
{"devices": [
  {"device": "survey-cam", "modulus": 16, "max_advance": 6,
   "channels": [["still", "photo"], ["probe", "scalar"]]}
]}
```

每台设备声明：计数模数 `M`、正常连续记录间允许的最大计数前进量 `G`
（`0 < G < M`）、合法通道及其类型（`scalar` 标量读数 / `photo` 照片）。

### 1.4 分卷 volumes/*.jsonl

同一台设备每次启动都有独立的启动号；卷中混有三种记录：

```json
{"device": "survey-cam", "boot": "B-1", "seq": 0, "kind": "receipt", "count": 14, "pulse": "P-14"}
{"device": "survey-cam", "boot": "B-1", "seq": 1, "kind": "photo", "count": 0, "round": "R-7", "channel": "still", "resource": "frame/0001.jpg", "exposure": 4}
{"device": "survey-cam", "boot": "B-1", "seq": 2, "kind": "reading", "count": 1, "round": "R-7", "channel": "probe", "value": 42}
```

- 所有记录带设备号、启动号、非负序号和 `[0, M)` 内的原始计数；
- 脉冲回执引用参考脉冲号；其余记录还带轮次和通道；
- 读数有整数值（int64）；照片有资源标识和从开快门开始的正整数曝光
  计数 `d < M`，当前记录的计数是**开快门**坐标；
- **文件送达次序和行次序都不能当成现场时序**；时序只由
  （设备, 启动号, 序号）与计数展开决定。

### 1.5 标识符与整数规则

- 轮次、窗口号、设备号、启动号、通道号、脉冲号是 JSON 字符串或整数
  （库内按 JSON 编码存储，字符串 `"1"` 与整数 `1` 不混淆）；资源标识
  必须是非空字符串；登记 ID 是换算库 `reg_id` 原样字符串；
- UTC 纳秒、序号、计数、读数值、曝光计数只接受有符号 64 位 JSON
  整数，**不接受浮点数**（`1.0` 也会被拒）；
- 记录字段严格：多余字段、缺字段都是坏字段。

## 2. 从零建库

```bash
# 0) 既有 ready 换算库（若还没有）：本仓库示例
python3 -m chainage build examples/demo_pkg data/demo.db

# 1) 生成文档化示例采集交接包（题目"现场核一卷"那组数字）
python3 -I scripts/make_acquisition_example.py examples/acquisition

# 2) 装配采集包（输出库已存在则拒绝，不覆盖上一份交付）
python3 -m chainage acq-build examples/acquisition data/demo.db data/acq.db
```

构建在临时库 `<out>.tmp` 上进行，全部校验、索引与统计完成后才原子
发布为正式库；任一步失败都不留看似完成的库，也不动已有库。

读取不可信输入时按仓库惯例用文件路径入口：

```bash
cd /tmp && python3 /workspace/chainage_tool/chainage/cli.py acq-build \
    /path/pkg /path/ready.db /path/out.db
```

## 3. 按轮次读窗口采样表

```bash
python3 -m chainage acq-windows data/acq.db --round R-7            # 整轮（分页）
python3 -m chainage acq-windows data/acq.db --round R-7 --window W-b
python3 -m chainage acq-windows data/acq.db --round R-7 --no-records
```

- 标识符参数按 JSON 字面量解析（整数轮次传 `--round 7`，字符串轮次
  传 `--round R-7` 或 `--round '"R-7"'`）；默认 JSON 输出，`--text`
  给紧凑表格；`--limit/--offset` 翻页；
- 每个窗口给出：区间、登记引用与换算库交接版本、期望通道及
  `has_evidence`、各通道已归属数量（含非期望通道，标
  `expected: false`，**不代替缺失通道**）、已归属记录明细（读数的
  精确 UTC 与值 / 照片的完整曝光区间与资源标识）。

**怎样看出一个空窗口是缺读数而不是没处理**：采集包要么整体交付要么
整体不存在（`acq-summary` 里 `status` 为 `ready` 才允许查询），所以
窗口行在就是已处理；`records` 为空、`totals.records` 为 0 且期望通道
全部出现在 `missing_expected_channels` 里，就是该窗口缺读数。

示例包中：读数取 UTC `0` 归 `W-b`；`W-a`、`W-c` 没有任何已归属记录，
两个期望通道都标缺。

## 4. 查未归属

```bash
python3 -m chainage acq-unattributed data/acq.db
python3 -m chainage acq-unattributed data/acq.db --category partial_overlap
python3 -m chainage acq-unattributed data/acq.db --round R-7 --kind photo
```

类别（质量结果，不是输入错误，记录全部保留）：

| 类别 | 含义 |
| --- | --- |
| `no_receipts` | 展开块内没有任何回执 |
| `start_unconstrained` | 记录/开快门在块内首个回执之前 |
| `end_unconstrained` | 记录/闭快门在块内末个回执之后 |
| `outside_windows` | 已对齐，但完全落在所有窗口之外 |
| `partial_overlap` | 照片与窗口相交，但整段曝光不在单一窗口内 |

`partial_overlap` 的 `detail` 里有两个独立标志：`spans_multiple`
（跨越多扇窗）与 `has_uncovered`（曝光有离开窗口的部分），一张照片
可以同时跨窗且有空档。`overlaps` 给出**所有**正长度相交窗口及其精确
交叠区间，`gaps` 给出尚未被任何窗口覆盖的子区间 `[lo, hi)`。

示例包的照片：曝光 `[-10/3, 10]`，与 `W-a` `W-b` `W-c` 三窗都相交
（交叠 `[-10/3,0]`、`[0,8]`、`[9,10]`），留下 `[8,9)` 未被覆盖——
三扇窗都不能把它计作可用照片，它也不会被计两次。

## 5. 回看一条记录

```bash
python3 -m chainage acq-trace data/acq.db 2
```

从任一采样行或未归属记录回看：

- `record`：设备、启动号、序号、原始计数、业务字段；
- `evidence`：该记录出现的全部卷名与原行号（重复记录证据不消失）；
- `block`：展开块编号、序号范围、块内回执数、该记录的展开坐标
  （十进制文本，大坐标不回卷）；
- `clock`：对齐状态；已对齐时给出精确 UTC（或曝光两个端点）和
  **确定该时刻所用的回执**（回执记录号、角色、脉冲号、脉冲 UTC、
  展开坐标）；
- `attribution` 或 `unattributed`：归属的窗口，或未归属类别与
  交叠/空档明细。

建库完成后，取数和回看都只读采集包，不再读取原始分卷。

## 6. 精确数表示

- 库内 UTC 纳秒、窗口边界是有符号 64 位整数；展开坐标是十进制文本
  （可超过 int64，不因数据库整数列回卷）；
- 插值结果用精确有理数：JSON 输出里整数写 `"123"`，非整数写约分后
  的 `"p/q"`（如 `"-10/3"`）；归属判定先用精确值比较，**不先四舍五入
  再选窗口**（大 UTC 值下小于一纳秒的差也能正确落窗）。

## 7. 装配规则（与实现一一对应）

1. **拼接**：按 `(设备, 启动号, 序号)` 拼接；业务字段一致的重复记录
   只算一次，但保留所有卷名和原行号；同键内容冲突、未知引用（设备/
   通道/轮次/脉冲/登记 ID）、坏字段、重复窗口键、非法窗口（区间非
   半开、轮次内重叠）都定位输入并拒绝交付，不按"最后收到"覆盖。
2. **展开**：每次启动内序号每断开一次切出新的连续块，不用丢失记录
   两侧的计数猜测卷绕次数；仅对同块相邻序号把两计数之差取 `[0, M)`
   非负模差，差大于 `G` 是结构错误，差为零合法；累计差得到块内展开
   坐标。
3. **回执校验**：同一启动不重复回执同一脉冲；回执 UTC 按序号严格
   向前；块内相邻回执的展开坐标严格向前。违反即不可能的时钟记录，
   拒绝交付。
4. **对齐**：块内相邻回执间线性插值；读数落在某个回执坐标上就采用
   该回执的 UTC（即使那是全块唯一回执），其余读数必须被同块回执
   夹住；照片的开/闭快门都必须在同块回执围住的范围内，闭快门坐标
   是展开后的开快门坐标加 `d`，中间可以跨越回执。不把别的块、别的
   启动或对齐范围之外的时钟借来补齐。回执不足是质量结果
   （`no_receipts` / `start_unconstrained` / `end_unconstrained`），
   不是输入错误。
5. **归属**：读数按它在同轮次所落的一个窗口归档（`start <= t < end`）；
   已对时的照片保留完整 UTC 曝光区间，只有整段曝光落在一个窗口里
   才归档（闭快门正好等于窗口右端算在内）；不满足时不选重叠最长的
   一扇，也不把同一张照片计作两次可用证据，而是给出全部正长度相交
   窗口的精确交叠区间与未覆盖子区间。

## 8. 失败行为与顺序无关性

- 输入冲突时命令失败（退出码 2，JSON 列出全部定位错误），不留下
  看似完成的库；输出路径已存在时直接拒绝，不覆盖上一份交付；
- 相同业务输入调整分卷处理顺序后，除明确反映物理行址的证据
  （卷名:行号）外，归属、诊断类别、窗口数量与全部结果表逐行一致；
- 记录号、块号、窗口号都按规范顺序分配，与分卷到达顺序无关。

## 9. 性能与资源边界

- 分卷逐行流入暂存表，拼接/展开/对齐/归因全部走 SQLite 游标分批
  落盘；不在内存积压全包、所有曝光或所有相交明细；
- 进程内只常驻单块回执列表与单轮次窗口数组；大排序走磁盘
  （`temp_store=FILE`），页缓存有界；
- 归属按轮次二分 + 顺序扫描，不给每条记录扫描该轮次的全部窗口；
  实际检查的窗口行数记入 `stat.window_rows_examined`（见
  `acq-summary`），规模验收会把它与相交输出量一起核对，确保不按
  记录数 x 窗口数 做候选扫描；
- 常规 64 位 Linux 上，100 万采集行 + 10 万同轮次窗口的构建进程
  峰值 RSS 不超过 256 MiB；磁盘随真实记录、证据和相交输出增长。

## 10. 验收

```bash
# 验收测试（39 例）：题目现场核卷精确数字、打散/重复/断块/另一启动、
# 触边/单脉冲点/跨斜率/大 UTC 亚纳秒/超 int64 展开坐标、各类拒绝、
# 顺序无关性，以及与独立精确有理数参考程序在 150 个随机包上全量对照
python3 -I tests/test_acquisition.py

# 规模验收（默认 100 万行 / 10 万窗口，产物只落 data/）
python3 -I scripts/acquisition_scale_check.py --workdir data/acq-scale
```

## 11. 采集包库结构（查询约定）

查询一律经 `acq-*` 子命令或 `chainage.acquisition_query` 进行；库未
处于 `ready` 状态会被拒绝。主要表：

- `plan_window`：计划窗口（含登记引用与期望通道 JSON）；
- `sample_record` / `evidence`：拼接后的记录与全部卷行证据；
- `expand_block` / `placement`：展开块与每条记录的展开坐标（文本）；
- `alignment`：对齐状态、精确 UTC/曝光端点（分子/分母文本）、所用
  回执记录号；
- `attribution` / `unattributed`：归属与未归属类别；
- `photo_overlap` / `photo_gap`：未归属照片的精确交叠区间与空档；
- `window_channel_count`：每窗口每通道已归属数量；
- `meta` / `inputs`：状态、交接版本、统计与全部输入的 sha256。
