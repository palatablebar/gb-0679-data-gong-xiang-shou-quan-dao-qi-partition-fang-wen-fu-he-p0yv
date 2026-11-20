# 授权定义

资源orders与订单等价，customers与客户等价。principal为主体文本，不支持模糊主体。partition为YYYY-MM；分区授权[start,end)不含end月。授权时间也为[start,end)，到期时刻立即失效。

每条授权包含主体、资源、生效窗、分区窗和allow/deny。全部条件必须同时匹配。拒绝优先，多个允许不能覆盖任何显式拒绝；无匹配默认拒绝。匹配规则ID按字典序稳定排序，冲突显示全部允许与拒绝证据。

observed为真实访问执行结果allowed或blocked；reported为上游0/1判定，留作证据。expected为实际授权复算，分区模块重建reported为执行结果便于二值对账。越权放行和误拦截分别计数，禁止用相互抵消的总数代替逐条核查。

业务ID和超大整数按文本保形；Decimal参与数量金额计算，对外序列化为字符串。哨兵集合定义在app/values.py，缺失值不能当零。所有时间带UTC偏移，先转UTC比较，业务日按指定地区划分。区间统一[start,end)。重复业务键保留第一条、完全相同行计重复；值冲突行隔离且保留原值。空文件、全空白、仅表头分开拒绝。

运行清单包括输入sha256、规则版本、参数、生成时刻与耗时。源行编号以CSV解析器的物理结束行号表示，引号内换行不增加记录数。输入超过4MB返回413，非法表头和类型返回422，失效版本复核返回409。

# 查询准入输入（M8）

`sample_data/admissions.csv` 表头固定为 `query_id,principal,resource,partition,submitted_at,rows,note`：

- `query_id`：联合查询业务键，按文本保形；同一 `query_id` 的多行是同一查询的多个分区。
- `principal`/`resource`/`partition`/`submitted_at`：与访问采信同义，复用同一套授权与半开区间规则；时间必须带 UTC 偏移。
- `rows`：该分区参与聚合的行数，Decimal 解析，留空/非数字隔离，不得为负。
- 同一查询内重复 `(query_id,partition)` 隔离，不覆盖。

输出按查询聚合：`conclusion` 为整单放行/部分放行/整单拒绝三选一；`released_partitions` 与 `blocked_partitions` 互斥，`taint` 为两者分区交集（正常恒为 0）；`basis_sha256` 是该查询全部分区证据的规范摘要，对账合计与证据 CSV 均引用同一摘要。`sealable` 要求至少一个放行分区、`taint=0` 且行数对平；否则封存被拒绝。