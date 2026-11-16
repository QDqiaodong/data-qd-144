# data-qd-144 · 定尺材料切割排样

Deterministic offline cutting plans and waste conservation for a bounded,
SQLite-backed material warehouse.

## 安装与运行

仅依赖 Python >=3.11 标准库与 SQLite：

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

### 原有单次 FFD 契约（保持不变）

```sh
cutting-stock --source fixtures/input.json --output run/report.json
```

### 离线有限库存切割计划仓

```sh
stock-init   --db warehouse.db --source fixtures/stock_init.json
stock-apply  --db warehouse.db --request fixtures/stock_request.json
stock-report --db warehouse.db --output run/stock_report.json
```

- `stock-init` 只建新库，保存输入副本（输入文件之后可删除/替换，不影响库）。
- `stock-apply` 应用一个幂等批次；`start`/`finish` 是独占操作。
- `stock-report` 读取单一 revision 并原子发布确定性 JSON。

### 离线应付“收货凭证—供应商发票”对账与过账（独立采购数据，不读库存仓）

```sh
ap-check   --source fixtures/ap_source.json --ledger run/ap.db --output run/ap_check.json
ap-post    --source fixtures/ap_source.json --ledger run/ap.db \
           --proposal run/ap_check.json --request-id REQ-0001
ap-reverse --ledger run/ap.db --posting-id 1 --request-id REQ-0002
ap-report  --ledger run/ap.db --output run/ap_report.json
```

- `ap-check` 只读：账本不存在按空账读取且不建库；输出 infeasible/unique/ambiguous
  及字典序最小的 1/2 个精确整数矩阵见证（行末单次银行家舍入，无浮点）。
- 账本由首次成功的 `ap-post` 在单个立即事务内原子建立；proposal 绑定源字节
  SHA-256 与 sequence；request-id 跨 post/reverse 全局幂等。
- `ap-reverse` 追加相反分录并释放数量，旧证据冻结不删改；详见
  [docs/ap_contract.md](docs/ap_contract.md) 与
  [docs/ap_ledger_design.md](docs/ap_ledger_design.md)。

## 关键边界

- 每库至多 12 根未消耗（可用或占用）棒，完工历史不计上限；
- 每批至多 8 个开放工单、18 个待切零件；目标棒/工单在一批中至多出现一次；
- `kerf` ∈ [0, 10^6] 整数，首件免锯缝，其后每件一次锯缝；
- 长度 ∈ [1, 10^6]，棒/件等级 ∈ [0, 9]，棒材等级须不低于零件最低等级；
- 规划器是**精确全局优化**（非 FFD），按合同五级字典序决策；
- 只有 `finish` 提交的合格余料进入库存，计划余料绝不提前可用。

## 测试

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 tests/perf_stock_planner.py
```

- `test_business.py`：原 FFD 单次契约回归。
- `test_stock_planner.py`：独立穷举逐项比对精确优化器（200+ 小实例）。
- `test_stock_warehouse.py`：校验、幂等、revision、start/finish、余料结转、回滚、CLI 全链路。
- `perf_stock_planner.py`：限定规模下可复现的耗时与内存记录。

## 文档

- [docs/contract.md](docs/contract.md)：原 FFD 数据契约。
- [docs/stock_warehouse_design.md](docs/stock_warehouse_design.md)：精确搜索、剪枝、事务快照边界与资源记录。
