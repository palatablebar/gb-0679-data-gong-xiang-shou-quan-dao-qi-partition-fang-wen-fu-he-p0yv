# 深度登记 数据共享授权到期与分区访问复核

规模：中型。使用者：企业数据共享审计专员。核心规则：拒绝规则优先于允许，授权必须同时命中资源、分区和生效窗；显式拒绝不得被宽授权覆盖。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 访问采信 | 查看并运行访问采信 | `/api/accesses` | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py` |
| M2 资源映射 | 查看并运行资源映射 | `/api/resources` | `app/modules/m2_resources/rules.py` | `tests/test_m2.py` |
| M3 授权定版 | 查看并运行授权定版 | `/api/grants` | `app/modules/m3_grants/rules.py` | `tests/test_m3.py` |
| M4 冲突裁决 | 查看并运行冲突裁决 | `/api/conflicts` | `app/modules/m4_conflicts/rules.py` | `tests/test_m4.py` |
| M5 分区判定 | 查看并运行分区判定 | `/api/partitions` | `app/modules/m5_partitions/rules.py` | `tests/test_m5.py` |
| M6 访问对账 | 查看并运行访问对账 | `/api/audit` | `app/modules/m6_audit/rules.py` | `tests/test_m6.py` |
| M7 审计归档 | 查看并运行审计归档 | `/api/seals` | `app/modules/m7_seals/rules.py` | `tests/test_m7.py` |
| M8 查询准入 | 联合查询分区裁决与封存闸门 | `/api/admissions` | `app/modules/m8_admissions/rules.py`、`app/admission_store.py` | `tests/test_m8.py`、`tests/test_m8_api.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码 | 默认解码失败 | 回退GB18030 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_legacy_encoding_real_sample` |
| TRAP-02 | BOM | 首列名多字符 | utf-8-sig | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_bom_real_sample` |
| TRAP-03 | 带引号换行 | 拆成两条 | csv逻辑行 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_quoted_newline_is_one_record` |
| TRAP-05 | 列数不匹配 | 静默错列 | 隔离原行 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_bad_columns_real_sample` |
| TRAP-08 | 前导零ID | 编码变形 | 全程字符串 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_identifier_preserved_real_sample` |
| TRAP-14 | 重复键冲突 | 后写覆盖 | 保留首条并隔离冲突行 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_duplicate_conflict_real_sample` |
| TRAP-18 | 空态三态 | 一律成功 | 分别错误码 | `app/modules/m1_accesses/rules.py` | `tests/test_m1.py::test_three_empty_states` |
| TRAP-26 | 旧版本提交 | 覆盖新结果 | 版本条件更新 | `app/store.py` | `tests/test_e2e.py::test_stale_review_409` |
| TRAP-07 | 上游结果空值 | 误报拒绝 | 隔离缺失 | `app/values.py` | `tests/test_m3.py::test_real_missing_decision_not_zero` |
| TRAP-19 | 允许与拒绝重叠 | 后写覆盖 | 拒绝优先且排序证据 | `app/modules/m4_conflicts/rules.py` | `tests/test_m4.py::test_deny_priority_counterexample` |
| TRAP-23 | 授权到期点 | 多放行一次 | 半开生效窗 | `app/modules/m3_grants/rules.py` | `tests/test_m3.py::test_expiry_endpoint_excluded` |
| TRAP-31 | 联合查询整单二选一 | 整单放行泄露越期，或整单拒绝断档 | 逐分区裁决，部分放行只出可独立证明授权范围的结果 | `app/modules/m8_admissions/rules.py` | `tests/test_m8.py::test_mixed_query_partial_release_keeps_authorized_partition` |
| TRAP-32 | 同查询重试新增结论 | 结论漂移、依据被覆盖 | query_key 幂等，重试只追加 RETRY 流水 | `app/admission_store.py` | `tests/test_m8.py::test_retry_same_query_creates_no_new_conclusion` |
| TRAP-33 | 切换失败丢旧结论 | 失败回滚把留痕和原依据一起清掉 | 失败留痕同连接先提交，旧结论与原分区依据不动 | `app/admission_store.py` | `tests/test_m8.py::test_stale_switch_keeps_old_conclusion_and_partition_basis` |
| TRAP-34 | 未证明无到期混入即封存 | 越期数据进入封存证据 | taint/对平闸门，未过闸返回 NOT_SEALABLE | `app/admission_store.py` | `tests/test_m8.py::test_non_sealable_run_refuses_seal` |

## 对账规则

输入原始逻辑行=最终采纳来源行+隔离来源行+重复行+空白行+分段表头。派生记录用_sources保存完整来源，因此分组不丢行。业务预期合计与上游申报合计之差等于逐行差异之和。结果按输入摘要、规则版本及参数产生唯一批次，事务写入；同源重跑只复用已有批次。

## 已知不覆盖

4MB以上文件需预先按业务键分批；不模拟生产支付与外部回调。人工复核只变更批次状态，不改写原始记录。
