# 应付对账账本 — 设计与取舍说明

## 1. 模块与职责

| 模块 | 职责 |
| --- | --- |
| `common.py`（既有，复用） | 严格 JSON（拒绝重复键、`NaN`/`Infinity`）、`ident`、原子安全发布（临时文件 + `os.replace`，拒绝符号链接）。AP 包不复制这些规则，只 import。 |
| `ap_ledger/validate.py` | 严格结构与取值校验：顶层恰为 `receipts/invoices` 两数组、字段精确匹配、严格整数（`type is int` 且排除 `bool`）、范围、唯一标识、容量上限；单价构造为 `Fraction`。 |
| `ap_ledger/matcher.py` | 精确整数分配搜索与行末单次银行家舍入；输出三态与字典序最小的 1/2 个见证。 |
| `ap_ledger/store.py` | SQLite schema、只读/读写连接、快照读取、append-only 历史与事务内 DDL。 |
| `ap_ledger/api.py` | 一致性核对、proposal 绑定重核、全局幂等、sequence 推进、平衡分录、报告组装。 |
| `ap_ledger/cli.py` | `ap-check/ap-post/ap-reverse/ap-report` 四个命令；错误统一退出码 2。 |

## 2. 精确配对的搜索设计

### 2.1 为什么不用线性规划/启发式

问题是带容量约束的非负整数分配（整数运输问题），且可行性同时取决于数量与两项
舍入金额。浮点 LP 既不能表达“行末单次银行家舍入”这种离散判定，也无法保证枚举
到字典序最小的两个**不同整数矩阵**。因此采用精确 DFS，配合 `Fraction` 全程精确
分数，杜绝浮点近似。

### 2.2 规范化与见证排序

- receipt 先按 id 的 UTF-8 字节序排序；发票/行按字节序展开，保证输入数组顺序
  不影响结果。
- 合法边集合包含取 0 的边，按 `(发票,行,receipt)` 字节序固定为向量坐标。
- DFS 按行依次填充，每行对候选 receipt 从 0 开始递增枚举拆分，因此可行矩阵按
  见证向量**字典序升序**出现，只保留前两个不同矩阵。

### 2.3 金额判定与回退

每行在数量拆分确定后，用精确分数累加 `units * price`，在行末做**一次**
半偶舍入得 `net`，再对 `net*tax_bps/10000` 做一次半偶舍入得 `tax`；与发票声称值
不等则该拆分立即失败并回退到其它数量配对。逐凭证先舍入再求和的路径在结构上
不可能通过，因为系统根本不提供逐凭证舍入。

### 2.4 后缀记忆化（关键取舍）

朴素 DFS 在最坏情形（6 行、每行 qty 2、6 个全兼容 receipt、金额恒成立）会枚举
约 8.5×10^7 个矩阵。注意到一个后缀是否可行只取决于“剩余行 + 各 receipt 剩余
容量”，与到达该状态的前缀无关；因此以 `(行索引, 剩余容量元组)` 为键记忆每个
后缀**字典序最小的至多两个补全**。不同前缀仍产生不同完整矩阵（在前缀坐标上
不同），所以两见证语义不变；记忆化只剪掉“对当前后缀不可能进入前两名”的重复
探索。合同上限（<=6 行、<=12 单位、<=6 receipt）下最坏耗时亚毫秒级。

### 2.5 为什么“同金额不同矩阵”仍是歧义

记忆化返回的是矩阵（逐边数量），不是金额。两个拆分即使精确分数不同但舍入后
净额相同，也被当作两个不同见证（见 `fixtures/ap_source.json`：28/5 与 57/10
都舍入为 6 分）。系统从不按金额合并配对，完全符合“不以相同金额合并不同配对”。

## 3. 账本表示与历史不变性

- `receipt_records` 保存被引用过的 receipt 键与当前 `accepted_qty`；
  `postings` 冻结发票语义 JSON，`posting_lines` 冻结逐行数量、声称金额与精确
  分数，`posting_allocations` 冻结逐边数量与**当时单价** `price_num/price_den`。
- 单价证据随配对行冻结，所以后续 source 改价不会污染旧金额；冲销后按新价
  重新过账生成的是新 posting，旧 posting 原样保留。
- 有效占用由 `posting_allocations JOIN postings` 且 `reversed_by IS NULL`
  聚合得到；冲销只在 posting 上打标记并追加 sign=-1 分录，不删行。这天然保证
  “冲销恰好释放旧数量、残余数量使用新价格、旧分数不被重算”。

## 4. 事务、原子建库与故障恢复

- 写操作全程在单个 `BEGIN IMMEDIATE` 事务内。schema 用**逐条 DDL** 创建而不用
  `executescript`（后者会隐式 COMMIT，使 DDL 在随后回滚时仍落盘）；默认
  rollback journal 模式让建表参与同一事务，任何异常整体回滚。
- 写连接在打开时就会创建文件，因此“是否首次建库”的标记在连接之前捕获；首次
  过账失败回滚后，若留下的是零长度/无表文件则删除，保证失败的首次 post 不留
  任何账本痕迹。
- 幂等表 `requests` 以 `(操作指纹)` 记录请求：post 指纹覆盖 source SHA 与
  proposal 规范化字节，reverse 指纹覆盖 posting-id。同 id 同指纹返回首次响应，
  异指纹 `idempotent_conflict`；幂等查询在 stale 判定之前，过期 proposal 的
  规范重试依旧返回原结果。
- 并发：立即事务在写入前取保留锁，配合 `busy_timeout` 串行化；同一 sequence 的
  竞争只有一个提交，其余要么在推进后的 sequence 上重核为 `stale_proposal`，
  要么因发票已全部过账而以空批拒绝，要么锁等待失败——三者都不产生第二次
  sequence 推进。
- 故障注入测试在 `set_sequence` 处抛错，重开库验证无半套表/占用/幂等记录，且
  request-id 可被复用。

## 5. 一致性规则如何落地

- 每次 check/post 都在快照上重核：未冲销过账引用的 receipt/invoice 必须仍在
  source；已过账 invoice 与冻结 JSON 语义相等；receipt 三键不可变；
  `accepted_qty` 不低于历史有效占用。
- 单价更正不进一致性禁区：历史占用按冻结价、剩余数量按 source 现价，由
  matcher 在“剩余容量 = accepted_qty − 有效占用”上自然实现。
- check 经只读连接（`mode=ro`、`query_only`）在单一快照上计算；账本缺失按空账
  处理且不建文件。非数据库文件在 API 边界转成 `ledger_state`，CLI 捕获
  `sqlite3.Error` 后统一退出码 2。

## 6. 报告确定性

列表一律 UTF-8 字节序排序；`journal_totals_gross` 含冲销的有符号合计（全部冲销
后归零且仍平衡），`journal_totals_active` 只计有效分录；JSON 以
`sort_keys=True`、`ensure_ascii=False`、`allow_nan=False` 序列化，经 `common.save`
原子发布，同状态字节一致，发布失败不破坏旧报告。

## 7. 测试与可复现性

- `tests/test_ap_matcher.py`：银行家舍入边界、逐凭证舍入非法性、税额半偶边界、
  两发票竞争、数量可行而金额无解、同金额两矩阵歧义，并以**独立朴素全枚举**
  （无记忆化、独立重算精确分数与舍入）在 150+ 随机小实例上比对三态与最小两见证。
- `tests/test_ap_ledger.py`：严格校验、空账不建库、proposal 绑定、平衡分录、
  全局幂等与异参冲突、source 一致性、改价只影响剩余量、冲销释放与证据冻结、
  并发单赢家、故障注入无半套状态、CLI 全链路与符号链接/同路径/损坏库处理、
  以及贯穿更正—过账—冲销—按新价重过账的联动用例。
- `fixtures/ap_source.json`：自制样例，展示同键不同价的两个 receipt 产生
  精确分数不同但舍入净额相同的两个矩阵（ambiguous）。
