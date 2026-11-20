# 病害报件归并工作区（离线，可纠错的病害主数据）

这一层建立在既有 ready 换算库之上，只回答一件事：**哪些病害报件应当归并
成同一个可下发的病害**。它不重算桩号、不做巡检结算、不做工单分派，也不
自动判同——候选清单只是给审核员的线索，病害身份只由人工裁定改变。

## 1. 它不是巡检趟次

- 报件主键是 `(source, source_ref)`，内容是病害类别、车道、实际发现时间
  （UTC 整数秒）以及**一处现有登记的源编号点** `(reg_id, reg_n)`；
- 换算库只负责把这个点定位到物理轴，输出 `localizable` /
  `no_source` / `multiple_source`；
- 同一处坑槽可能有多条报件，两条相邻坑槽也可能被派成一张工单——归并结果
  是**病害组**，不是巡检趟次、也不是工单。

## 2. 建工作区（绑定标定）

```bash
python3 -m chainage defect-init data/defects \
    data/demo.db --spatial-threshold 3 --time-threshold 600
```

- 建区时把 `data/demo.db` **复制**进工作区（`data/defects/chainage.db`），
  记下内容指纹与 `workspace_id`；
- 之后替换外部 `data/demo.db` 不会改写已入仓位置；
- 批文件必须声明 `based_on_workspace`，换一份标定不能混进同一工作区；
- 工作区自检时若发现绑定的换算库副本被改写，直接拒绝打开（不悄悄改写）。

输出示例：

```json
{
  "format": "chainage-defect-workspace",
  "workspace_id": "…",
  "revision": 0,
  "spatial_threshold": 3,
  "time_threshold": 600,
  "chainage_fingerprint": "…",
  "reports_present": 0,
  "groups": 0,
  "groups_isolated": 0,
  "members": 0
}
```

## 3. 定位状态

沿用既有正查语义，一个登记点有四种逐刻度状态；本层把它们压成三个定位
状态，并保留完整原引用与轴/刻度：

| 正查状态 | 定位状态 | 是否参与按位置找候选 |
|---|---|---|
| `RESOLVED`（唯一位置 + 有目标标牌） | `localizable` | 是 |
| `NO_TARGET`（唯一位置，无目标标牌） | `localizable` | 是（不要求有目标标牌） |
| `NO_SOURCE`（没有源位置） | `no_source` | 否 |
| `MULTIPLE_SOURCE`（多个物理位置） | `multiple_source` | 否 |

- 登记不存在 → `UNKNOWN_REGISTRATION`（输入错误，整批拒绝）；
- 编号超出登记范围 → `N_OUT_OF_REGISTRATION`（输入错误，整批拒绝）；
- 合法但无法唯一定位 → 照常入仓、照常可人工裁定，只是不参加候选检索。

成员表的 `evidence` 里保留原 `reg_id`/`reg_n`、登记区间、物理位置数、
参与定位的标定行，以及唯一位置时采用的轴与刻度。

## 4. 批文件

一份批文件是一个 JSON 对象，整批在一次事务中生效：

```json
{
  "format": "chainage-defect-batch",
  "batch_no": "2026-11-20-01",
  "based_on_workspace": "…",
  "based_on_revision": 0,
  "reports": [
    {"source": "巡查", "source_ref": "P-001", "category": "坑槽",
     "lane": "L1", "found_at": 1795161600, "reg_id": "R103", "reg_n": 104},
    {"source": "承包商", "source_ref": "C-77", "category": "坑槽",
     "lane": "L1", "found_at": 1795161660, "reg_id": "R103", "reg_n": 104}
  ],
  "void": [
    {"source": "承包商", "source_ref": "C-99"}
  ],
  "adjudications": [
    {"a": {"source": "巡查", "source_ref": "P-001"},
     "b": {"source": "承包商", "source_ref": "C-77"},
     "decision": "same"},
    {"a": {"source": "巡查", "source_ref": "P-001"},
     "b": {"source": "承包商", "source_ref": "C-98"},
     "decision": "different"},
    {"a": {"source": "巡查", "source_ref": "P-001"},
     "b": {"source": "承包商", "source_ref": "C-97"},
     "decision": "clear"}
  ]
}
```

规则：

- 报件录入或**整条更正**用 `reports`；只按主键替换，不做字段级合并。
- 同一键或同一对在一批内只能出现一次；同一键不能既录入又撤下。
- 裁定对无序：`a`/`b` 可以颠倒（也兼容平铺字段 `a_source/a_ref`、
  `b_source/b_ref`）。
- 操作行顺序不影响结果：内部先合并录入与撤下，再合并裁定，最后一次性
  重算组。
- 裁定只能引用仓内已知报件（含本批新录入及此前撤下的报件）；引用未知
  报件、自反对、批内重复都整批拒绝。
- 允许人工明确认定超出候选条件或未定位的报件——候选过滤不参与裁定。

提交与查看：

```bash
python3 -m chainage defect-batch data/defects batch-01.json --text
python3 -m chainage defect-reports data/defects --text
python3 -m chainage defect-adjudications data/defects --text
```

## 5. 候选（只是线索）

```bash
python3 -m chainage defect-candidates data/defects 巡查 P-001 \
    --explain-scan --text
```

条件（全部同时满足，**端点算命中**）：

- 对方也在册、`source` 与起点不同；
- 同病害类别、同车道、同轴；
- 双方都恰好一个物理位置（不看目标标牌有无）；
- 刻度差 `|x1-x2| <= spatial_threshold`；
- 发现时间差 `|t1-t2| <= time_threshold`；
- 该对当前没有生效的直接裁定（同一/不同都算）；撤去裁定后重新成为候选。

列表给出成对的原引用、物理位置与两个差值。**同桩号数值不是匹配依据**，
真正的匹配靠轴 + 空间窗口 + 时间窗口。即使两条报件当前处在同一病害里，
也不代表有直接裁定；候选出现或消失本身不改变病害身份。

`--explain-scan` 报告预筛读取量，与命中数分开：

```json
"scan": {
  "bucket_rows": 2400,             // 同 (类别,车道,轴) 分桶总行数
  "space_time_preselected": 12,     // 空间+时间窗口内、逐对精确判断的行数
  "pairs_examined": 12,
  "candidates": 2
}
```

索引先按分桶与空间/时间窗口剔除大量无关报件，命中条数不是扫描量。

## 6. 组与隔离

从每条在册报件出发，沿当前生效的「同一」裁定能互相到达的报件归为一组，
包括没有连边的单件。组内若有**任意**一条生效的「不同」裁定，整组进入
隔离，不能作为可下发病害。

组键 = 组内按 `(source, source_ref)` 逐项 Unicode 码点序最小的报件键，
不分配流水号、不依赖导入顺序。

```bash
python3 -m chainage defect-groups data/defects --text
python3 -m chainage defect-groups data/defects --isolated --text
python3 -m chainage defect-group data/defects '["巡查","P-001"]' --text
python3 -m chainage defect-evidence data/defects '["巡查","P-001"]' --text
```

隔离组不可下发，但成员仍可复核。`defect-evidence` 对每条导致隔离的
「不同」裁定给出一条由**当前生效**「同一」裁定串起来的可核实路径：

```json
{
  "group_key": "[\"巡查\",\"P-001\"]",
  "isolated": true,
  "different_edges": [
    {"kind": "different",
     "between": [["巡查","P-001"], ["承包商","C-98"]],
     "same_path": [["巡查","P-001"], ["承包商","C-77"], ["承包商","C-98"]]}
  ]
}
```

路径不使用已撤下报件或已撤去裁定做解释；只给一条路径，不枚举所有路径。

## 7. 修订、全量与增量

- 仓库从空表修订 0 开始，每次成功批提交修订号加一；
- 全量同时取得两张表与它们所属的修订号（同一读事务）；
- 增量从给定修订之后**按批**返回；每批以该批前后的两张表为准，按表名
  与行键给出新增 `+`、删除 `-`、变更 `~`，带前像与后像；
- 未变行不发；批内运算的临时拆组/合组不会出现在增量里；即使没有表变化
  也返回一个空批，确认该修订号已处理；
- 下游按修订整批应用，从空表或任一已读全量继续，都得到同一份当前表。

```bash
python3 -m chainage defect-full data/defects
python3 -m chainage defect-changes data/defects --since 2
```

增量样例：

```json
{
  "from_revision": 2,
  "revision": 4,
  "batches": [
    {"revision": 3, "batch_no": "2026-11-20-03", "changes": [
      {"table": "defect_group", "key": "[\"巡查\",\"P-001\"]", "op": "~",
       "before": {"isolated": true, "member_count": 3, "...": "..."},
       "after":  {"isolated": false, "member_count": 2, "...": "..."}},
      {"table": "defect_member", "key": ["承包商","C-98"], "op": "~",
       "before": {"group_key": "[\"巡查\",\"P-001\"]", "...": "..."},
       "after":  {"group_key": "[\"承包商\",\"C-98\"]", "...": "..."}}
    ]}
  ]
}
```

`defect_group` 的行键是组键字符串；`defect_member` 的行键是
`[source, source_ref]` 数组。

### 7.1 收据、重试与拒绝

- 重试**原基准修订号 + 完全相同的操作内容**（录入/撤下/裁定排序后比较，
  裁定两端无序、字段次序与行序都不影响）：只回放原收据，不重新执行，
  即使仓库已处理后续批；
- 同批号不同内容 → `BATCH_NO_REUSED`；
- 未见批号却引用过期基准修订 → `STALE_REVISION`；
- 引用晚于当前修订的未知修订 → `UNKNOWN_REVISION`；
- 批内输入错误（坏字段、重复键/对、未知报件、登记不存在、编号越界）
  → 整批拒绝，修订不变，不留半张表。

重试返回 `{"receipt": {...}, "replayed": true}`。

## 8. 局部重算（生产数据）

一批更正只重算**受影响的组**：本批录入/撤下/裁定涉及的报件所在组（含
被连接、拆开或改变裁定的组，以及新录入报件可能接回的历史组），其余组
整行沿用。允许相关大组整组落盘重算，但不会扫所有报件、不会重建其余
病害，也不预存全体两两可达关系或预生成所有候选对。

`defect-scope` 打印某批实际触及的组与成员行，以及工作区总量，便于核对
局部性：

```bash
python3 -m chainage defect-scope data/defects --batch 2026-11-20-03 --text
```

## 9. 演示一个审核过程

仓库自带一个很小的示例交接包与三份批文件；批文件里的
`based_on_workspace` 是占位符，建区后替换成实际 `workspace_id` 再提交。
`scripts/make_defect_example.py` 会一次做完建包、建区、替换与提交，并写出
`demo_summary.json`：其中 `stages` 逐批记录当时的组表、成员换组与 A 组的
隔离证据（A 到 C 的同一路径经过 B），可直接对照：

```bash
# 一键生成并提交三批（产物都在 examples/defect/，除批文件外均被忽略）
python3 -I scripts/make_defect_example.py examples/defect

# 或手工按步走：手工流程用 examples/defect_batches 里的批文件
python3 -m chainage build examples/defect_pkg data/defect-demo.db
python3 -m chainage defect-init data/defect-demo/ws \
    data/defect-demo.db --spatial-threshold 3 --time-threshold 600
# 把三份批文件的 based_on_workspace 换成上一步输出的 workspace_id
for b in examples/defect_batches/batch-*.json; do
  python3 -m chainage defect-batch data/defect-demo/ws "$b" --text
done
python3 -m chainage defect-groups data/defect-demo/ws --text
python3 -m chainage defect-full data/defect-demo/ws
```

示例覆盖：

1. 录入 A、B、C（同为巡查来源的三个报件键按码点序排列），裁定 A 同 B、
   B 同 C、A 不同 C：组 A 三件、被隔离，A 到 C 的证据须经过 B；
2. 撤去 B 同 C：组 A 只剩 A、B 且可下发，新增单件组 C；A 不同 C 仍在，
   但不隔离任何组；只有 C 的成员行因换组而变；
3. 加回 B 同 C 并同时撤去 A 不同 C：组 C 撤出，组 A 纳回 C，任何对外
   增量里都没有临时隔离；
4. 换算出现复号（多位置）时不发候选；旧批重试、坏批回滚、并发写入被拒。

## 10. 数据规模

工作区库只存报件、裁定、两张表和增量，不存逐刻度、不存候选对、
不存可达矩阵。候选检索走
`pair_bucket(category, lane, axis_id, loc_lo, loc_hi)` 与时间索引；
组重算只沿本批相关报件的「同一」边实时走图。规模验收：

```bash
python3 -I scripts/defect_scale_check.py --workdir data/defect-scale
```

脚本会生成 5000 个互不相干的病害组、1000 条无关第二来源报件与一个含
桥接、环、隔离证据的 40 件大组，再补 2000 条无关单件组，然后：

- 报告一次只改大组一条裁定的批实际触及的组数与成员行数（局部重算范围），
  以及扩大无关数据前后该范围是否不变（两次都只触及 1 个大组、40 个成员行，
  而工作区有 8000+ 组、8000+ 成员）；
- 在单一 2 万条报件的分桶里查一条起点，分别报告同桶总行数、空间+时间窗
  内行数、逐对判断数与命中数，并给出 `EXPLAIN QUERY PLAN`（走
  `pair_bucket` 的范围索引，不是全表扫描）；
- 报告全量与增量读取的耗时与行数、`tracemalloc` 峰值。

生成的大数据、库与报告都落在 `data/`，已被 `.gitignore` 忽略。
