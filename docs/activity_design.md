# activity-ledger 设计说明与取舍

## 职责切分

- `activity_ledger.validate`：纯结构与值域校验，不碰数据库。布尔冒充整数、
  未知字段、重复键、重复 ID、越界数值、自连边、非法区间、delete 的业务字段
  非 null、空批与超 5000 条批在此拒绝。
- `activity_ledger.store`：SQLite schema、连接（读写 / `mode=ro`）与访问器。
  DDL 逐条执行并包在调用方的 `BEGIN IMMEDIATE` 内，**不用**
  `executescript`（它会隐式 COMMIT，可能发布半建成 schema）。
- `activity_ledger.identity`：双时态身份解析。先按 generation 选择每个
  `link_id`/`event_id` 的最高已知 revision（delete 撤回），再按 `event_time`
  构图（并查集）求 `canonical_account`。
- `activity_ledger.projection`：报告期过滤、按 canonical account 聚合、
  行摘要与 `restated - as_booked` 差异。`as_booked` 与 `restated` 共用同一
  投影函数，仅 generation 不同，避免两套逻辑漂移。
- `activity_ledger.api`：init / apply / close / report 的事务边界、幂等与
  generation 规则；可测试 API 为 `initialize`、`apply_request`、
  `close_period`、`build_report`。
- `activity_ledger.cli`：路径冲突拒绝、严格 JSON 读取、原子发布、退出码 2。

## 关键取舍

### 1. 修订带 apply generation，历史永不重写

`events` / `links` 都是 append-only，每行记录其 `request_id` 与被写入时的
`generation`。历史选择按 `generation <= G` 的最高 `revision` 完成，与插入
顺序无关——同一 revision 不可能被写两次，该记录同时就是知识时间戳。这样
`close_generation` 与当前 generation 可以各自独立解析，`as_booked` 一旦
冻结，之后任何插入都不会改变其字节：`closes.as_booked_json` 是关闭时算出的
规范 JSON 原文，报告只回读、不重算。

### 2. 两个时间轴分开消费

- 有效时间只在**构图**时使用：`canonical_accounts` 对每个不同的
  `event_time` 建一张新图，只并入 `valid_from <= t < valid_to` 的边。不同
  时刻的分量可能不同，绝不使用单一「期末图」。
- 知识时间只在**选修订**时使用：`select_revisions` 取不晚于 generation 的
  最高 revision；若为 `delete` 则整体跳过。删除只在知识轴上撤回，旧 `put`
  仍留在表里，因此早先的 close 仍能解析出旧关系。
- 两者正交：先选修订，再按时刻构图。测试用「关系从 `[10,30)` 收窄为
  `[20,30)`」验证 `t=10..19` 重新归属、`t=29/30` 不受影响。

### 3. 每个 event_time 独立并查集（离线动态连通性）

`canonical_accounts` 先按 `event_time` 收集事件并排序，把每条 link 的
`[valid_from, valid_to)` 映射到事件时刻上的下标区间，插入一棵迭代式线段树；
随后 DFS 线段树，用**可回滚并查集**（按大小合并、无路径压缩、显式 undo 栈）
在进入子树时 union、离开时 rollback。每个叶子（时刻）看到的边集恰是区间
包含该时刻的全部 link，并且分量互不污染。并查集每个根额外维护 UTF-8 最小
成员，叶子处 O(1) 取 canonical ID。总复杂度
`O((L log T + T) α)`，而非逐时刻重建图的 `O(T·L)`；测试用独立 BFS 在 20 组
随机图上逐点比对结果。无任何边的账号保留自身 ID，不需要预建节点。

### 4. 差异是集合差，不是追加

`diff` 以账号为键比较 `restated` 与 `as_booked` 的成员列表与
`(count, amount)`。交易改金额、跨账号移动、删除或成员替换时，旧组出现负
增量、新组出现正增量，两侧同时存在；即使替换后笔数与金额恰好不变，只要
`event_ids` 不同该账号也会列出（行内含两侧 `event_ids`）。
`changes_totals` 恒等于 `restated_totals - as_booked_totals`（测试断言），
但**不**声称能捕捉纯成员替换的净额变化（净额确实为 0）。这保证「删除/移动
时旧账号差异必须出现」。

### 5. 幂等先于 generation

`apply_request` 与 `close_period` 都在事务开头先查回执表，命中则校验指纹并
原样返回响应；未命中才检查 `expected_generation`。因此同一请求在 generation
前移后重试仍返回**原** generation，而不同参数复用 ID 得到
`idempotent_conflict`。指纹基于规范请求（数组已排序），重排数组仍是重放。

### 6. 单事务写与失败无半态

apply 在 `BEGIN IMMEDIATE` 中写入全部修订、幂等回执与 `generation+1`，最后
`COMMIT`；任何异常 `rollback`。close 同样单事务写入冻结行与回执，但**不**
推进 generation。测试用替换 `store.set_generation` 注入异常，验证 generation、
修订数、回执数全部回退，且原 `request_id` 仍可成功使用；init 失败会删除刚
创建的半成品文件。

### 7. 报告只读单快照

`build_report` 用 `mode=ro` + `query_only` 打开，在一个 `BEGIN` 内读取 close
行、当前 generation 与全部修订历史，`ROLLBACK` 后关闭连接，再在内存里重述。
报告绝不建库、不写表；并发 apply 要么整体可见、要么整体不可见。

### 8. 复用而非新造

严格 JSON、`ident`、原子符号链接拒绝发布、`<code>: <message>` + 退出码 2
全部复用 `cutting_stock.common` 与现有 CLI 模式；四个新命令注册在
`pyproject.toml`，旧入口不动。

## 明确不做

- 不读库存仓、不配对采购、不核算用电；不跨库 JOIN。
- 不提供 close 撤销或重开；`close_id` 一次性且结果永久。
- 不做预测式 canonical 映射缓存：每次报告按快照重算，确定性优先。

## 测试覆盖

`tests/test_activity_ledger.py` 覆盖：

- 严格校验：未知/缺失字段、布尔、非法与重复 ID、自连边、非法区间、空批/
  超限批、非法 delete、关闭参数、缺失库不建文件。
- 有效时间边界：`[10,30)` 关系下 `t=9/10/29/30` 的处置，以及收窄为
  `[20,30)` 后仅 `10..19` 重述。
- 图拆分/合并：`A-B`、`B-C` 传递成组，撤回 `A-B` 后分成正确分量并重算
  canonical ID，恢复新关系后再合并；环视为单一分量。
- 投影联动：改金额、跨账号移动、删除事件时旧组撤减 + 新组增量，
  笔数/总额与独立 oracle 全量重算一致。
- 知识时间：close 后新增/撤回关系只改 `restated`，`as_booked` 字节不变
  （直接比对 `closes.as_booked_json`）。
- 幂等与并发：apply/close 重放、异参冲突、revision 跳号、未知 delete、
  generation 冲突、4 线程同 generation 仅一个成功。
- 事务故障回滚、空报告、未知 close、报告只读与确定性、CLI 原子发布/符号
  链接拒绝/失败保留旧字节，以及 30 步随机历史的独立 BFS oracle 比对。
