# 离线有限库存切割计划仓数据契约（v1）

## stock-init 输入

```json
{
  "kerf": 1,
  "reuse_min": 4,
  "bars": [
    {"id": "b1", "length": 10, "grade": 2}
  ]
}
```

- `kerf`：整数，0..1_000_000。每根棒首件不收锯缝，其后每加一件收一次锯缝。
- `reuse_min`：整数，1..1_000_000。余料长度达到该值才在完工后回库，否则记损耗。
- `bars`：初始棒，1..12 根（可为空数组）。每根 `id` 为非空白标识（去空白后不改变），
  `length` 1..1_000_000，`grade` 0..9，`id` 唯一。

## stock-apply 请求

```json
{
  "batch_id": "B1",
  "expected_revision": 0,
  "operations": [
    {"op": "order", "id": "o1", "priority": 50,
     "pieces": [{"id": "p1", "length": 6, "min_grade": 2}]}
  ]
}
```

- `batch_id`：非空标识；同 id 同规范载荷返回首次结果，同 id 异载荷冲突。
- `expected_revision`：必须等于当前 revision，否则整批失败且不改变事实。
- 每批最多 8 个开放工单、18 个待切零件；目标工单/棒在一批中至多出现一次。
- 操作类型：
  - `receive`：`{"op":"receive","bar":{"id":...,"length":...,"grade":...}}`，
    新棒 id 不得与任何现存（含已消耗/占用）棒 id 冲突。
  - `order`：`priority` 1..100；至少一个零件，零件 `length` 1..1_000_000，
    `min_grade` 0..9，全局 id 唯一。
  - `amend` / `withdraw`：仅作用于未开工工单；修订替换其全部零件，撤回禁止再用其 id。
  - `start` / `finish`：独占操作，不得与其他操作混批。

## 规划目标的严格顺序

1. 最大化已接受工单 priority 总和；
2. 最大化接受工单数；
3. 最小化消耗棒数；
4. 最小化总余料（实现为最小化消耗棒长度和）；
5. 字典序最小的有序对：
   （按 UTF-8 字节序排序的已接受工单 id 元组，
    按（工单 id、零件 id）字节序排序后对应的棒 id 元组）。

工单不可部分接受：库存不足或等级不兼容时整单延期，延期原因取
`quantity`（库存数量/碎片化）、`length`（可用长度不足）、`grade`（等级不兼容），
可多值并存，按 `quantity < length < grade` 规范顺序返回。

## 库存生命周期

棒状态：`available` → `occupied`（start 冻结）→ `consumed`（finish）。
收料棒为 `initial` / `received`；完工合格余料为 `remainder`，带来源棒
（`parent_bar_id`）与继承等级，并以稳定 id `rem-<plan>-<seq>` 入库；
不合格余料写入 `waste`。每库未消耗棒（available+occupied）至多 12 根，
完工历史不计上限。计划余料只在报告中标记 `planned_reusable`，不入库。

## stock-report 输出要点

- 单一 `revision`；工单 `accepted` / `deferred` / `started` / `finished` /
  `withdrawn` 状态与延期原因；
- 逐棒原料/计划余料与零件分配（含件序、用量、锯缝、余料、可否回用）；
- 总 priority、棒占用计数、已回库余料与损耗；
- 全部列表按 UTF-8 字节序排序，JSON `sort_keys`、`ensure_ascii:false`、
  `allow_nan:false`，同状态字节一致，原子发布且拒绝符号链接输出。

## 错误处理

校验失败、未知字段、布尔冒充整数、超界数值、重复键、非有限数等均被拒绝；
CLI 以退出码 2 输出 `<code>: <message>`。任何批次失败都回滚，revision 不变。
