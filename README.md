# data-qd-144 · 定尺材料切割排样

Deterministic first-fit-decreasing cutting plans and waste conservation。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/cutting-stock --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/cutting_stock/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 当前业务与样例
# Bounded cutting plan v1

为材料计划者将显式零件切到同长度原料。stock_length为1..10^9整数长度，kerf为0..stock_length整数锯缝，pieces最多5000个唯一id及1..stock_length长度。每行恰一零件，不隐含数量展开。
按长度降序、同长度id字典序，放入首个可容纳的现有棒，否则开启新棒。假定首零件无锯缝、每增加一个零件消耗一条kerf；没有首尾修边。每棒used=sum(piece_lengths)+kerf*(piece_count-1)，余料stock-used，所有零件恰一次且不超长。输出明确每棒零件及用量、全局零件/锯缝/余料守恒和忽略锯缝的ceil(total_piece_length/stock_length)下界。
样例2棒：a6+c3+锯缝1恰10；b4+d3+锯缝1用8余2。该FFD启发式可行且确定，不保证最优棒数、切割次序机械可执行或最佳余料价值。无有限原棒库存、多材料、二维套料或真实设备控制。

