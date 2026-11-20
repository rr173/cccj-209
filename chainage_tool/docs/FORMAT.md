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
