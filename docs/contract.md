# Bounded cutting plan v1

为材料计划者将显式零件切到同长度原料。stock_length为1..10^9整数长度，kerf为0..stock_length整数锯缝，pieces最多5000个唯一id及1..stock_length长度。每行恰一零件，不隐含数量展开。
按长度降序、同长度id字典序，放入首个可容纳的现有棒，否则开启新棒。假定首零件无锯缝、每增加一个零件消耗一条kerf；没有首尾修边。每棒used=sum(piece_lengths)+kerf*(piece_count-1)，余料stock-used，所有零件恰一次且不超长。输出明确每棒零件及用量、全局零件/锯缝/余料守恒和忽略锯缝的ceil(total_piece_length/stock_length)下界。
样例2棒：a6+c3+锯缝1恰10；b4+d3+锯缝1用8余2。该FFD启发式可行且确定，不保证最优棒数、切割次序机械可执行或最佳余料价值。无有限原棒库存、多材料、二维套料或真实设备控制。
