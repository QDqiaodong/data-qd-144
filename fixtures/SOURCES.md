本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

- `input.json`：原 FFD 单次切割契约样例。
- `stock_init.json` / `stock_request.json`：有限库存切割计划仓的建库与批次样例。
- `ap_source.json`：应付对账源样例。
- `energy_source.json`：用电核算源样例。配套仓库由以下步骤现场构建
  （plan id 确定性为 `plan-r1-1`）：
  1. `stock-init`：`kerf=1, reuse_min=4`，棒 `bar-alpha` 长 10 等级 0；
  2. 批次 `b1`（rev 0）：工单 `wo-frame`（零件 `f1` 长 2）与
     `wo-brace`（零件 `b1` 长 4）；
  3. 批次 `b2`（rev 1）`start`；批次 `b3`（rev 2）`finish`。
  核算结果：区间用电 d=[6,4]，run [2,8) 跨读数边界 t=6 与电价边界 t=5，
  权重 wo-frame:wo-brace = 1:2（长度 2:4），工单能耗 2/4、费用 5/10，
  idle 能耗 4、费用 10，合计能耗 10、费用 25。
- `activity_request.json` / `activity_close.json`：客户活动账本的关账样例。
  一个批次写入 4 笔 t=9/10/29/30 的交易和一条 `acct-A`–`acct-B` 于
  `[10,30)` 生效的关系；关账 generation 1、报告期 `[0,100)`。
