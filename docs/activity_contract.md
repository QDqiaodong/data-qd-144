# 离线客户活动账本数据契约（v1）

`activity-*` 在独立 SQLite 库上提供可审计的身份归并与历史重述。它不读库存仓、
不碰应付账本、不参与用电核算；旧入口（`cutting-stock`、`stock-*`、`ap-*`、
`energy-audit`）零改动。严格 JSON（重复键/非有限数拒绝）、`ident` 规则与
原子、拒绝符号链接的报告发布全部复用 `cutting_stock.common`。

## 命令

```sh
activity-init   --db DB
activity-apply  --db DB --request JSON
activity-close  --db DB --request JSON
activity-report --db DB --close-id ID --output JSON
```

- `activity-init` 只建新库（已存在即失败），`generation` 从 0 开始。
- `activity-apply` 原子写入一批事件/关系修订，幂等，成功后 `generation` 恰 +1。
- `activity-close` 冻结 `[start,end)` 报告期的 `as_booked`，但不推进
  `generation`；`close_id` 不可复用。
- `activity-report` 在只读快照中同时输出冻结的 `as_booked` 与按当前
  `generation` 重述的 `restated`、两者差异和各自 generation。

失败一律打印 `<code>: <message>` 到 stderr 并以退出码 2 结束，且不改变已发布
报告字节。

## apply 请求

顶层严格为四个字段，多一个少一个都拒绝：

```json
{
  "request_id": "REQ-1",
  "expected_generation": 0,
  "events": [{"event_id","revision","op","account_id","event_time","amount_minor"}],
  "links":  [{"link_id","revision","op","left_account","right_account","valid_from","valid_to"}]
}
```

- `events` / `links` 各自可为空数组，但合计至少一条；单批合计至多 5000 条。
- 同一 `event_id`（或 `link_id`）在一批中至多出现一条修订。
- 所有 ID 使用 `ident`：非空字符串、无首尾空白、≤128 字符、无控制字符。
- `revision` 为 1..10^12 的严格整数；`event_time`/`valid_from`/`valid_to` 为
  0..10^12 的严格整数；`amount_minor` 为 0..10^12 的严格非负整数。
  布尔值冒充整数一律拒绝。
- `op` 为 `put` / `delete`。
  - `put` 必须给出全部业务字段。
  - `delete` 的业务字段（事件的 `account_id`、`event_time`、`amount_minor`；
    关系的四个字段）必须全为 `null`。
- `link` 的 `valid_from < valid_to`，即左闭右开区间 `[valid_from, valid_to)`。
  自连边（`left_account == right_account`）拒绝。
- 同一 ID 的 `revision` 必须从 1 起**连续递增**；`delete` 必须存在未删除的
  旧版本（最高版本须是 `put`），否则拒绝。

### 幂等与 generation

- 每个成功 apply 在**一笔** SQLite 事务内写入全部修订、幂等回执，并把
  `generation` 恰增 1；任一步失败整笔回滚，不留部分事实。
- 请求指纹取自**规范请求**（数组顺序无关）：同一规范请求重试返回原响应与
  原 generation，即使当前 generation 已前移。
- 重放检查**先于** `expected_generation` 校验，因此正常重试不会因 generation
  过期而被误判。
- 同一 `request_id` 携带不同参数是 `idempotent_conflict`。
- 首次 apply 必须 `expected_generation == 当前 generation`，否则
  `generation_conflict`。

## 身份解析语义（双时态）

`link` 是对两个账号在其有效时间内属于同一客户的无向等价断言。

- **知识时间**：在指定 generation 上，每个 `link_id` 取**不晚于该
  generation** 的最高 revision。最高版本若为 `delete`，则该 link 在此时点
  不生效；删除是知识时间上的撤回，**不擦除**其旧修订。
- **有效时间**：对每笔交易的 `event_time`，只使用其区间包含该时刻的生效
  link 构图，沿**任意长度**的连通分量归并；不同 link 可传递连接或成环。
- `canonical_account` 为该分量内 UTF-8 字节序最小的 account ID；无有效边时
  使用交易原 `account_id`。
- 每个不同的 `event_time` 分别求图；绝不能用报告末时刻的单一分量代替整段
  历史。

## close 请求

顶层严格为四个字段：

```json
{"close_id": "C-1", "expected_generation": 3, "start": 0, "end": 100}
```

- `start < end`；数据库当前 generation 必须等于 `expected_generation`。
- `close_id` 不可复用；完全相同请求重放返回原关闭结果，异参冲突。
- 关闭时固定 `[start,end)` 报告期与 `close_generation`，并计算、永久保存
  `as_booked`：仅采用该 generation 已知的每个 event/link 最高 revision，按
  各 `event_time` 归属，输出每 `canonical_account` 的 `event_ids`、笔数与
  `amount_minor` 总额。
- `event_ids` 按 UTF-8 字节序排列；`event_count` 等于列表长度；`amount_minor`
  等于所列事件金额之和。
- 关闭不推进 generation。后续任何事件或关系修订都不得改变 `as_booked` 字节。

## report 输出

在**同一只读 SQLite 快照**中读取：保存的 `as_booked`、当前 generation、当前
最新 event/link 版本，并输出：

- `as_booked` / `as_booked_totals` / `as_booked_generation`：冻结结果。
- `restated` / `restated_totals` / `restated_generation`：按当前 generation
  最新 event/link 版本对同一报告期重新归属；事件更正/删除可改变其成员、笔数
  与金额。
- `changes`：逐账号 `restated - as_booked` 差异，列出确有变化的账号
  （成员、笔数或金额任一变化即列出）。每行同时含两侧 `event_ids` 与
  `count`/`amount`，因此即便是「金额与笔数相同、仅交易成员被替换」也可见。
  出现「旧组撤减（负） + 新组增量（正）」两侧，绝非只追加新组。
- `changes_totals`：差异合计（应等于 `restated - as_booked` 的期间总量）。

所有账号行按 UTF-8 字节序稳定排序；同状态产出确定性 JSON（`sort_keys`、
`ensure_ascii:false`、`allow_nan:false`），原子发布且拒绝符号链接输出。

## 失败码

| code | 含义 |
| --- | --- |
| `invalid_request` | 结构/值域/标识符/修订连续性/未知 delete 等契约违反 |
| `generation_conflict` | `expected_generation` 与当前 generation 不符 |
| `idempotent_conflict` | 同一 `request_id`/`close_id` 携带不同参数 |
| `ledger_state` | 数据库缺失、不可读或无 activity schema |
| `unknown_close` | `activity-report` 引用了从未记录的 close |
