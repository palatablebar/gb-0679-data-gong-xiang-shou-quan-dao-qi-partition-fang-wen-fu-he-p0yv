# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## M8 查询准入

- `GET /api/admissions`：用 `sample_data/admissions.csv` 即时演算，不落账。
- `POST /api/admissions`：body 为 `{sample}` 或 `{csv,parameters?}`，裁决并幂等落账；同输入重试不新增结论。
- `POST /api/admissions/upload`：multipart 文件 `file`。
- `GET /api/admissions/runs` / `GET /api/admissions/runs/<id>`：批次历史与详情（含 version、review、sealed）。
- `POST /api/admissions/runs/<id>/review`：`{version,decision}`，decision 仅 `封存`/`退回补证`；陈旧版本 409，未证明未混入到期数据时封存返回 422 NOT_SEALABLE。
- `GET /api/admissions/runs/<id>/journal`：追加式操作流水（ADMIT/RETRY/REVIEW/SWITCH_FAILED/SEAL_BLOCKED/INGEST）。
- `GET /api/admissions/runs/<id>/evidence`：证据 CSV，逐分区携带与裁决一致的 `basis_sha256`，隔离行单列。
- `GET /api/admissions/queries/<query_key>`：单个查询的唯一准入结论与原分区依据。

每个查询返回唯一 `conclusion`（整单放行/部分放行/整单拒绝）、`released_partitions`、`blocked_partitions`、`taint`、`balanced`、`sealable` 与 `basis_sha256`；查询裁决、对账、证据导出共用同一依据摘要。
