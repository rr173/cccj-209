# 交接包与库格式说明

全部文件均为 UTF-8、无 BOM。整数一律为 JSON 整数（不接受小数、布尔或字符串），
且必须落在有符号 64 位整数范围 `[-2^63, 2^63-1]`；标定两端的实际编号也须
可表示，任何溢出/舍入风险都在校验期拒绝。

## 1. 目录结构

```
<package_dir>/
├── manifest.json        清单（文档化 JSON）
├── calibration.jsonl    标定段表（每行一个 JSON 对象）
└── registration.jsonl   待换算登记表（每行一个 JSON 对象）
```

## 2. manifest.json

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | string | 交接版本标识，原样写入库并随每条查询结果返回 |
| `axes` | array | 物理基准轴列表，非空，`axis_id` 不可重复 |
| `axes[].axis_id` | string | 轴标识 |
| `axes[].length` | int ≥1 | 轴的整数长度；物理坐标为闭区间 `[0, length]` 上的整数刻度 |
| `files` | object? | 可选，声明三张表的文件名（默认即下列名称） |

刻度单位全包一致，本工具不做任何单位换算。

## 3. calibration.jsonl（标定段表）

| 字段 | 类型 | 说明 |
|---|---|---|
| `row_id` | string | 稳定行标识，全包唯一；登记证据与库内引用均使用它 |
| `system` | string | 编号体系（如旧线 `OLD`、新线 `NEW`） |
| `axis_id` | string | 所属轴，必须在 manifest 中声明 |
| `segment` | string | 断链段名；同体系可在不同轴复用 |
| `lo`, `hi` | int | 轴上的物理闭区间 `[lo, hi]`，要求 `lo < hi` 且 `0 ≤ lo ≤ hi ≤ length` |
| `label_lo` | int | 物理位置 `lo` 处的编号 |
| `direction` | 1 或 -1 | 编号方向 |

仿射关系（方向 ±1，无舍入）：

```
编号 n(x) = label_lo + direction · (x - lo)
反解 x(n) = lo + direction · (n - label_lo)
```

合法性规则：

- 同一 `(axis_id, system, segment)` 的两条标定行，其物理**闭区间**不得重叠，
  共享端点（`prev.hi == cur.lo`）也算非法重叠，错误定位到后一行；
- **不同段名**之间可以任意重叠、共享端点、留空，断链复号是合法形态，不拒绝；
- 同一物理位置允许出现多条标牌（多标定行读出同号或异号），证据全部保留。

## 4. registration.jsonl（登记表）

| 字段 | 类型 | 说明 |
|---|---|---|
| `reg_id` | string | 唯一登记 ID |
| `source_system` / `target_system` | string | 源/目标编号体系，必须不同且都有标定行 |
| `n` | int | 点号；与 `from`/`to` 二选一，点号等价于 `from = to = n` |
| `from`, `to` | int | 源编号闭区间，要求 `from ≤ to` |
| `axis_id` | string? | 可选轴限定；**缺省表示未知，不是默认选某个轴** |
| `segment` | string? | 可选源断链段名限定；缺省表示未知 |

未填 `axis_id`/`segment` 时，引擎在所有满足其余限定的标定行上求解；
填了就严格限定。`备注` 等非定位字段不参与消歧，本工具也不读取。

## 5. 逐刻度业务定义与四种状态

对登记区间内每个整数源刻度 `n`：

1. 取满足登记限定、且编号区间覆盖 `n` 的全部源标定行，逐行反解物理位置，
   合并相同 `(axis_id, x)`，但保留每个位置的全部源标定行证据；
2. 在每个位置上取覆盖该物理位置的全部目标标定行，按
   `(target_segment, target_label)` 合并重复来源，标定行标识不消失；
3. 按下表判状态：

| 状态常量 | 中文 | 含义 | 可否自动入仓 |
|---|---|---|---|
| `NO_SOURCE` | 无源位置 | 没有任何源标定行能解释该刻度 | 否 |
| `MULTIPLE_SOURCE` | 多个源位置 | 独立物理位置多于一个（即使换成同一目标编号也算） | 否 |
| `NO_TARGET` | 缺目标标牌 | 源位置唯一，但该位置上没有目标标牌 | 否 |
| `RESOLVED` | 可入仓 | 源位置唯一且有 ≥1 个目标标牌（多标牌互为别名） | 是 |

多位置刻度上，每个位置的可得目标证据都保留，不因某位置缺目标而删除该候选。

## 6. 分段压缩（库内不按刻度存行）

库表 `segment` 存的是状态与整套依据恒定的源编号闭区间。分段边界只可能来自：

1. 源标定行编号区间的端点（行开始/停止覆盖）；
2. 两条同轴源行的唯一**整数**交点（该刻度单列；有理非整数交点不产生合并）；
3. 源行与目标行物理交叠的端点（目标证据出现/消失）。

相邻分段只有在状态、位置集合、每位置的源行集合与目标行集合完全一致时才合并。
因此一条十亿刻度、中间无边界的登记在库内只有一行分段。

## 7. SQLite 库表

| 表 | 内容 |
|---|---|
| `meta` | 构建状态（`building`/`ready`）、schema 版本、交接版本 |
| `axis` | 轴及长度 |
| `calibration` | 全部标定行及预计算仿射参数 `a,b,c` |
| `registration` | 登记头 |
| `segment` | 压缩分段（登记、源编号闭区间、状态、位置数） |
| `position` | 分段内每个独立物理位置及其源标定行证据（JSON 行标识） |
| `target_evidence` | 位置上的目标标牌证据、目标编号关于源编号的仿射关系 |
| `target_index` | 目标侧反查索引（体系、段、轴、可检索目标闭区间） |

精确区间不靠存刻度：`position` 存 `x(n) = x_slope·n + x_intercept`，
`target_evidence` 存 `label(n) = label_slope·n + label_intercept`，
查询时按实际命中的源刻度现算，正反向定位都精确且无舍入。

## 8. 查询输出

- `forward <db> <reg_id> [--from N --to M]`：登记信息、覆盖的压缩分段、
  每段全部位置与证据，以及 `warehousable_segments`（仅 RESOLVED 子区间）。
- `reverse <db> <target_system> (--n N | --from A --to B) [--axis X] [--segment S]`：
  走 `target_index` 区间索引的命中登记，含实际命中源刻度闭区间、该范围原状态
  （不改写）、全部候选位置；命中证据带 `"matched": true`，其余证据保留。
- 默认输出 JSON；`--text` 输出班组可读紧凑文本。错误以非零退出码与
  定位 JSON 返回（校验失败退出码 2，其余链具错误退出码 1）。

## 9. 巡检履约结算

结算只读一个既有 ready 换算库，另需一张**巡检义务表**与一份**巡检簿**。
它不产生新的桩号换算结果，只把巡检记录经换算库定位到物理轴后，按义务
统计每个物理刻度上的班组覆盖。

### 9.1 义务表（JSON 数组或 JSONL）

| 字段 | 类型 | 说明 |
|---|---|---|
| `obligation_id` | string | 唯一义务 ID |
| `axis_id` | string | 物理轴（须存在于换算库） |
| `lane` | string | 义务车道；只有车道相同且实测时刻在窗口内的趟次计数 |
| `start`, `end` | int | 物理**闭区间** `[start, end]`，要求 `start ≤ end` |
| `window_start`, `window_end` | int | 主巡检窗口 `[start, end)`，要求 `start < end` |
| `min_crews` | int ≥ 1 | 主窗口最低独立班组数 |
| `sub_windows` | array? | 子窗口，可相交；每个有各自的 `start`、`end`、`min_crews`，可选 `label` |

每个门槛（主窗 + 各子窗）独立统计相应时间内不同的班组；同一班组可以分别
满足不同门槛，不做班组指派。重叠义务分别结算。

### 9.2 巡检簿（JSONL，可乱序送达的更正记录）

| 字段 | 类型 | 说明 |
|---|---|---|
| `trip_id` | string | 趟次 ID，可多版本 |
| `ingest_time` | int | 入簿时刻（UTC 整数秒） |
| `action` | `"trip"` / `"void"` | 缺省 `trip`；`void` 撤销该趟 |
| `crew` | string | 班组（`trip` 必填） |
| `survey_time` | int | 实际巡检时刻，必须 `≤ ingest_time` |
| `lane` | string | 车道（`trip` 必填） |
| `routes` | array | 一个或多个**互斥完整路线版本**（`trip` 必填） |
| `routes[].fragments` | array | 一个片段引用对象的数组，非空 |
| `fragments[].reg_id` | string | 换算库中的登记 ID |
| `fragments[].n_from`, `n_to` | int | 登记内的源编号子区间，要求 `n_from ≤ n_to` |
| `fragments[].calib_row_id` | string | 选定的源标定行 ID |

规则：

- 同一 `(trip_id, ingest_time)` 只能有一条记录，重复即 `DUPLICATE_INGEST_KEY`；
- 结算截止 `cutoff` 取 `ingest_time ≤ cutoff` 的最后一条记录，整条替换此前
  版本；只有被选中的记录才解析引用，旧版本/未来版本的坏引用不解析；
- 班组与实测时刻适用于该记录列出的**所有**路线版本；路线版本由班组列好，
  工具不猜测、不补全；
- 每个片段必须能被选定行完整解释：登记存在、标定行属于登记源体系且满足
  轴/段限定、覆盖整个子区间、子区间落在登记内、反解出的物理范围落在标定行
  物理范围内。任一引用不可解释即拒绝整份结算，并定位到
  **趟次 / 入簿时刻 / 路线 / 片段**，不删掉该路线继续算。

### 9.3 逐刻度证据与状态

对同一趟次：

- 任一**完整**路线版本到过刻度 `x` -> "可能巡过"；
- **每个**完整路线版本都到过刻度 `x` -> "确定巡过"。

趟次的路线选择相互独立；不输出推测的单一真实路线，也不假设某个刻度的
判定能拼成一段可同时满足门槛的路线组合。

每个门槛按班组去重：同一班组有任一趟确定巡过就计入**下界**，有任一趟
（确定或仅可能）可能巡过就计入**上界**。

| 状态 | 条件 |
|---|---|
| `MET`（已达标） | 所有门槛下界 ≥ 各自门槛 |
| `UNMET`（确定缺巡） | 某一门槛上界 < 门槛 |
| `REVIEW`（需核实） | 其余（下界不足但上界足够） |

### 9.4 输出

- `settle`：完整台账 `ledger.obligations[].segments`（每个区间一条，含每个
  门槛 `lower`/`upper`/`min_crews`）、状态汇总与两张清单
  `ledger.dispatch`（缺巡）、`ledger.review`（待核实）。`--list dispatch|review`
  可直接只取一张清单。相邻刻度若状态与全部门槛人数都不变则合并为一条记录。
- `settle-diff`：两个截止点 `--before` / `--after`，只列**状态改变**的区段，
  按两边实际分界对齐，附前后各门槛人数；只改人数不改状态的路段不出现。
- `settle-explain`：某义务在某物理刻度的逐门槛取证，列出确定班组与仅可能
  班组及贡献趟次，每趟标注入簿版本、各路线版本是否命中、命中片段引用的
  登记与源标定行。相邻刻度人数相同也只用本点证据，不沿用邻点。

结构错误、重复入簿键、引用不可解释都会聚合为 `SETTLEMENT_VALIDATION_FAILED`
（退出码 2），不输出任何部分清单；义务 ID 不存在等查询错误退出码 1。

## 10. 病害归并工作区

病害归并工作区是既有 ready 换算库之上的独立工作区（`chainage/defects.py`，
CLI `defect-*`），不改变第 1–9 节的交接包、库与巡检结算格式。工作区目录：

```
<workspace_dir>/
├── workspace.db     报件、裁定、病害组/成员表、批收据、增量（SQLite）
└── chainage.db      建区时复制的 ready 换算库（之后不读外部库）
```

### 10.1 工作区元数据与绑定

`workspace.db` 的 `wmeta` 保存：`format`、`workspace_schema`、
`workspace_id`、`chainage_fingerprint`、`spatial_threshold`、
`time_threshold`、`revision`。`chainage_fingerprint` 由轴、标定行、登记
与交接版本计算；打开工作区时重算并与副本比对，被替换即拒绝。批文件必须
声明同一 `workspace_id`。

阈值在建区时固定：空间阈值是物理刻度差的**闭区间**上限，时间阈值是
实际发现时间差（秒）的**闭区间**上限，端点算命中。候选查询可临时覆盖
阈值，但不改写工作区阈值。

### 10.2 报件定位

报件用 `(reg_id, reg_n)` 复用第 8 节正查，在查询点合并该登记的全部分段
位置：

| 位置数 | `location_status` | 可找候选 |
|---|---|---|
| 1 | `localizable` | 是（`RESOLVED` 与 `NO_TARGET` 都算） |
| 0 | `no_source` | 否 |
| ≥2 | `multiple_source` | 否 |

登记不存在（`UNKNOWN_REGISTRATION`）与编号超出登记范围
（`N_OUT_OF_REGISTRATION`）是输入错误，整批拒绝；无位置/多位置是业务
状态，照常入仓、照常可人工裁定。

### 10.3 批文件（JSON 对象）

| 字段 | 类型 | 说明 |
|---|---|---|
| `format` | string | 固定 `chainage-defect-batch` |
| `batch_no` | string | 批号，全局唯一；重试原基准修订 + 相同操作内容只回放收据 |
| `based_on_workspace` | string | 工作区 `workspace_id` |
| `based_on_revision` | int | 基准修订，必须等于当前修订（过期/未知拒绝） |
| `reports` | array | 录入或整条更正；每项含 `source`、`source_ref`、`category`、`lane`、`found_at`、`reg_id`、`reg_n` |
| `void` | array | 撤下 `{source, source_ref}` |
| `adjudications` | array | `{"a": {source, source_ref}, "b": {...}, "decision": "same" \| "different" \| "clear"}` |

裁定两端无序，也可用平铺字段 `a_source/a_ref`、`b_source/b_ref`。同一
报件键或同一报件对在一批内只能出现一次；同一键不能既录入又撤下；裁定
只能引用仓内已知报件（含本批新录入和此前撤下的报件）。整批在一个事务
里生效，行序不影响结果。

### 10.4 病害组与成员表

- 组 = 当前在册报件沿生效「同一」裁定的连通分量（单件也算一组）；
- 组内有任意生效「不同」裁定 -> `isolated = 1`，不可下发；
- 组键 = 组内按 `(source, source_ref)` 逐项 Unicode 码点序最小的报件键
  的规范 JSON（例 `["巡查","A"]`），不是流水号；
- `defect_group` 行：`group_key`、`isolated`、`member_count`、
  `first_found`、`last_found`；
- `defect_member` 行：`source`、`source_ref`、`group_key`、类别、车道、
  发现时间、登记原引用、定位状态/轴/刻度、完整证据；
- 撤下报件不删除其裁定；裁定只有两端都在册时才参与分组；重新录入同键
  后旧裁定自动恢复生效。

### 10.5 修订与增量

- 空表修订 0，每批成功后 +1；
- 全量（`defect-full`）在同一读事务里返回 `revision` 与两张表的全部行；
- 增量（`defect-changes --since N`）按批返回修订 N 之后的 `change` 行：
  `table` 为 `defect_group` 或 `defect_member`，`key` 为组键字符串或
  `[source, source_ref]`，`op` 为 `+`/`-`/`~`，带 `before`/`after`；
- 增量的 `before`/`after` 与全量的行形状完全一致，可逐行替换；
- 未变行不发；无表变化的批返回空 `changes`，确认该修订号已处理；
- 批内临时拆组/合组不进入增量，只比较批前表与批后表。

### 10.6 局部重算范围

每批只读取并重算本批涉及的报件所在组（含被连接、拆开、改变裁定的组
以及新录入报件可能接回的历史组），其余组整行沿用。`defect-scope`
输出该批实际触及的组键与成员键。实现不预存全体两两可达关系，也不预
生成候选对；候选检索只走 `pair_bucket(category, lane, axis_id,
loc_lo, loc_hi)` 及时间索引，并按分桶、空间窗口、时间窗口预筛。
