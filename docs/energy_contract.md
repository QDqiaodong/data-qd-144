# 离线“完工工单用电归因与计量不确定性核算”数据契约（v1）

`energy-audit --warehouse DB --source JSON --output JSON` 在现有切割计划仓
（`stock-*`）之上提供只读核算：把会回绕的电表读数翻译成一组可能的真实用电
解释，按完工布局把 run 时窗用电摊到工单，并在全部工单预算的合取下给出联合
可行解释的计数与逐指标精确范围。本命令不做规划、不改库存、不过账、不配对；
对仓库严格只读。

## source 输入

顶层严格为四个数组，多一个少一个字段都拒绝：

```json
{
  "meters":  [{"id": "m1", "modulus": 100, "max_power": 20,
               "readings": [{"t": 0, "value": 10}, {"t": 10, "value": 20}]}],
  "runs":    [{"id": "r1", "meter_id": "m1", "plan_id": "plan-r1-1",
               "bar_id": "bar-alpha", "start": 2, "end": 8}],
  "tariffs": [{"meter_id": "m1", "start": 0, "end": 10,
               "price_num": 2, "price_den": 1}],
  "budgets": [{"work_order": "wo-frame", "min_energy": 0, "max_energy": 50}]
}
```

### meters

- `id`：标识符（现有 ident 规则），全输入唯一。
- `modulus`：整数 2..10^6，表计回绕模数。
- `max_power`：整数 0..10^6，能量单位/秒。
- `readings`：每项严格 `{"t","value"}`；`t` 为 0..10^9 整数秒，`value` 为
  0..modulus-1 整数。按时间排序后至少两条，时间不得重复。
- 相邻读数形成左闭右开区间 `[t0,t1)`，长度 `dt` 须满足
  `max_power*dt <= 2*modulus`（最多两次回绕），违反是格式错误。
  全输入区间总数最多 10 个、至少 1 个。
- 区间真实用电 `d` 是满足 `0 <= d <= max_power*dt` 且
  `d ≡ value1 - value0 (mod modulus)` 的任一整数：零、一或两次回绕都可能，
  实现不得预选最小值。区间内功率恒为 `d/dt`。表之间独立，无跨表守恒。
- 候选集为空（功率上界不允许任何回绕解释）不是格式错误，而是全局无解。

### runs

- 每项严格 `{"id","meter_id","plan_id","bar_id","start","end"}`；最多 20 个。
- `id` 唯一；`meter_id` 必须存在；`start < end` 且落在该表首末读数时刻内
  （触边允许）。
- 同一表的 run 时窗不得重叠，触边允许。
- `(plan_id, bar_id)` 二元组全输入至多出现一次。
- `plan_id` 必须是仓库 `plan_history` 中的**已完工** plan；started（active）
  与 open 快照、未知 plan/bar 一律拒绝。布局永远取自该完工 plan 的历史
  `plan_json.layouts`，绝不使用当前 open 快照或当前棒材库存替代。

### tariffs

- 每项严格 `{"meter_id","start","end","price_num","price_den"}`。
- `price_num` 0..10^6 整数，`price_den` 1..10^6 整数，单位分/能量单位。
- 每台表的 tariff 段对其整个读数时域 `[首读数, 末读数]` 恰好无重叠无缺口
  覆盖（首段起于首读数、末段止于末读数、相邻端点相接）。

### budgets

- 每项严格 `{"work_order","min_energy","max_energy"}`；可为空数组。
- `min_energy`/`max_energy` 为 0..10^12 整数且 `min <= max`；工单不得重复。
- 工单必须出现在本次 run 引用的布局中。含义：该工单跨全部 run 的总用电
  （精确有理数）必须落在闭区间内。所有预算同时成立，解释才全局可行。

## 核算语义

- **归因权重**：run 内按布局中各工单零件长度之和占全布局零件长度的比例
  分摊该时窗用电；锯缝与余料不进权重；同一工单跨 run 合并；run 以外的
  用电记该表 idle，不摊给工单。
- **解释**：按 (meter_id UTF-8 字节序, 区间开始时间) 排列的完整 d 向量。
  不同 d 向量即不同解释，即使工单分摊结果相同也分别计数。
- **联合可行**：全部预算同时满足的解释集合。不做逐工单拼接，也不把边际
  上下界的笛卡尔积当作可行集合。
- **积分**：按表读数、run、电价的全部时间交点切分，用精确有理数
  （`Fraction`）计算能耗与费用；禁止先舍入再分配；报告不含浮点数。
- **守恒**：任一可行解释下，全部工单能耗 + 各表 idle 能耗 = 全表积分
  （= `sum(d)`）；费用同理。

## 输出

确定性 JSON（`sort_keys`、`ensure_ascii:false`、`allow_nan:false`、原子
发布、拒绝符号链接输出）：

- `status`：`feasible` / `infeasible`；`interpretations`：全局可行解释数
  （无解为 0，且不输出伪范围：`work_orders`/`idle`/`total` 为 `null`）。
- `warehouse_revision`、`source_sha256`（源文件原始字节 SHA-256）。
- `intervals`：规范序区间及各自候选 d 列表（见证向量按此顺序对齐）。
- `plans`：引用的 `plan_id`、`started_revision`、`finished_revision`、`bars`。
- `runs`：run 参照及逐工单归因权重 `{num,den}`（最简分数）。
- `work_orders` / `idle` / `total`：逐工单、逐表 idle、全表合计的
  `energy`/`cost` 范围。每个指标含 `min`/`max`（最简分数 `{num,den}`，
  `den>0`）及 `min_witness`/`max_witness`——达到该极值的一套完整可行
  d 向量；平局取字典序最小向量。不同指标可有不同见证，不声称全部极值
  能同时达到；每个见证本身都是满足全部预算的完整解释。

## 只读与失败语义

- 仓库以 `mode=ro` + `PRAGMA query_only=ON` 打开，revision 与全部历史布局
  在**同一个 SQLite 读事务**内读取；并发 finish 不会撕裂快照。不建不存在
  的库、不改 schema、不写任何业务表。
- 输出不得覆盖源文件或仓库；输出不得为符号链接。任何失败以退出码 2 打印
  `<code>: <message>`，并保留旧报告字节不变。同一仓库状态与同一源字节
  产生字节一致的报告。
