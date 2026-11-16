# 离线“收货凭证—供应商发票”对账与过账数据契约（v1）

仅依赖 Python >=3.11 标准库与 SQLite，位于独立包 `ap_ledger`。采购数据独立输入，
**不读取**切割排样/库存计划仓；`cutting-stock` 与 `stock-*` 的行为与存储完全不变。

## 命令

```sh
ap-check   --source JSON --ledger DB --output JSON
ap-post    --source JSON --ledger DB --proposal JSON --request-id ID
ap-reverse --ledger DB --posting-id ID --request-id ID
ap-report  --ledger DB --output JSON
```

- `ap-check` 只读：账本不存在时按**空账**读取，绝不创建文件（以 `mode=ro` 打开）。
- 账本由**第一次成功的 `ap-post` 原子建立**；失败的首次过账连空文件也不保留。
- 复用 `common` 的严格 JSON（拒绝重复键、`NaN`/`Infinity`）、`ident` 标识规则和
  原子安全发布（临时文件 + `os.replace`，拒绝符号链接）。
- 源路径与输出路径相同、账本与输出路径相同均拒绝；输出为符号链接拒绝。
- 一切契约冲突以 `<code>: <message>` 写 stderr 并以**退出码 2** 结束。

## source 结构（顶层严格只有两个数组）

```json
{
  "receipts": [
    {"id","supplier","po_line","sku","accepted_qty","price_num","price_den"}
  ],
  "invoices": [
    {"id","supplier","lines":[
      {"id","po_line","sku","qty","net_cents","tax_bps","tax_cents"}
    ]}
  ]
}
```

- 未知字段、布尔冒充整数、超界数值、重复键/标识一律拒绝。
- 标识遵循 `ident`：非空白、去首尾空白不变、<=128 字符、无控制字符。
- receipt id 各自唯一；invoice id 各自唯一；行 id 在所属发票内唯一。
- 数量均为 `0..12` 整数；发票行 `qty` 必须为正（`1..12`）。
- 金额（`net_cents`、`tax_cents`）为 `0..10^12` 整数；
  `price_num` 为 `1..10^9`、`price_den` 为 `1..10^6`、`tax_bps` 为 `0..10000`。
- 单价是“分”的有理数 `price_num/price_den`，全程用 `fractions.Fraction`，
  **禁止任何浮点近似**。
- 每份 source 至多 6 个 receipt、6 张 invoice；其中**未过账**发票总行数至多 6、
  总 qty 至多 12；已过账历史不计上限。数组排列顺序不影响结果。

## 配对规则（check 的判定）

对全部未过账发票求非负整数分配矩阵 `x[发票ID,行ID,receiptID]`：

1. 每行的分配之和恰等于该行 `qty`，不得拆单位、不得漏掉任何一张发票/行。
2. 只有 `supplier`、`po_line`、`sku` 三者全等的凭证之间才存在合法边。
3. 每个 receipt 的总占用（账本中**未冲销**过账的占用 + 本次新分配）不得超过其
   当前 `accepted_qty`。
4. 金额规则（缺一不可，全部精确分数计算）：
   - `net_cents` 必须等于对 `Σ(x * price_num/price_den)` **仅在行末做一次**
     银行家舍入（四舍六入、半取偶）所得整数；逐凭证先舍入再求和属非法。
   - `tax_cents` 必须等于对 `net_cents * tax_bps / 10000` 做一次同样的
     银行家舍入所得整数。
5. 数量与两项金额**同时**成立才是可行解；金额不符必须回退寻找其它数量配对，
   不得四舍五入成“差不多”。

输出三态 `infeasible / unique / ambiguous`：

- 所有合法边（含取 0 的边）按 `(发票ID,行ID,receiptID)` 的 **UTF-8 字节序**排列，
  形成见证向量；按向量字典序输出最小的 1 个（unique）或 2 个（ambiguous）见证。
- **不因金额相同而合并不同配对**：两个矩阵即使舍入金额一致，只要向量不同即视为
  不同方案（ambiguous）。
- 每个见证返回逐行精确分数 `net_fraction`、舍入后的 `net_cents/tax_cents`、
  逐边数量与冻结单价证据，以及每个 receipt 的历史占用、本次占用与剩余量。
- `infeasible` 不要求输出最小冲突核。

## 过账与冲销（post / reverse）

- `ap-post` 的 `--proposal` 是 check 输出中的唯一方案，绑定**源原始字节 SHA-256**
  与账本 `sequence`；post 在单个 `BEGIN IMMEDIATE` 写事务内重新核对绑定、唯一性、
  源与账本一致性，然后原子过账**全部**未过账发票，冻结当时发票内容、配对数量与
  单价证据。
- 逐发票生成平衡分录：借采购费用（net 合计）、借进项税（tax 合计）、
  贷应付账款（net+tax）；每张发票借贷各自相等，批次合计也相等。
- 空批（无未过账发票）与歧义（ambiguous/infeasible）拒绝过账。
- 每个 `request-id` 跨 post/reverse **全局幂等**：规范命令重试返回首次结果；
  同 id 异参冲突（`idempotent_conflict`）。幂等判断先于过期（stale）检查，重试
  不会再次占用或冲销。
- 每次新成功操作 `sequence` 恰增 1，失败不增；并发争抢同一 sequence 时只有一个
  提交成功（SQLite 立即事务 + busy_timeout 串行化）。
- `ap-reverse` 必须引用完整存在且未冲销的 posting，追加**逐项相反分录**
  （sign=-1，金额取负参与合计）并释放原数量；不删除、不改写任何旧证据；
  同一 posting 不得二次冲销。

## source 与账本的一致性（每次 check/post 重核）

- 当前 source 必须包含每个**未冲销**过账用到的 receipt 和 invoice。
- 已过账 invoice 的内容必须与冻结值**语义一致**（规范化 JSON 比较，忽略格式）。
- receipt 的 `supplier/po_line/sku` 一旦被有效占用即不可变；`accepted_qty`
  不得低于历史有效占用；单价可更正，但**只对尚未占用的数量**生效。
- 未过账发票可随 source 自由更正。
- 冲销后可用更正后的单价重新配对、过账；旧 posting 永远显示原分数/金额，
  不用当前 source 重算历史。

## 报告（ap-report）

- 在一致只读快照上计算；按 sequence 展示过账与冲销关系、有效凭证占用
  （只统计未冲销配对）以及借贷合计（含按账户拆分；gross 为含冲销分录的有符号
  合计，active 为当前有效分录合计）。
- 全部列表按 UTF-8 字节序排序，JSON `sort_keys`、`ensure_ascii:false`、
  `allow_nan:false`，同状态字节一致；原子发布，发布失败不损坏旧报告。

## 错误码

| code | 触发 |
| --- | --- |
| `invalid_request` | 结构/字段/取值/标识/容量/金额规则、空批、歧义拒绝等 |
| `stale_proposal` | proposal 绑定的 source SHA 或 sequence 与账本现状不符 |
| `idempotent_conflict` | request-id 以不同参数复用 |
| `conflict` | 未知 posting-id、posting 已冲销等 |
| `ledger_state` | 账本缺失/无 AP schema/文件不是数据库 |
