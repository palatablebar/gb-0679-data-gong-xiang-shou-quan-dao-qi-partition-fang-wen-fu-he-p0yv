# 数据共享授权到期与分区访问复核

供企业数据共享审计专员核对原始记录、处理差异并封存证据。

## 启动

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

打开 http://localhost:18248 。首次进入自动运行仓内脏样例。工具条可重跑、切换样例或上传CSV。SQLite保存在命名卷records中，重建服务保留批次。

## 模块

- 1. 访问采信：`/accesses`，`/api/accesses`。
- 2. 资源映射：`/resources`，`/api/resources`。
- 3. 授权定版：`/grants`，`/api/grants`。
- 4. 冲突裁决：`/conflicts`，`/api/conflicts`。
- 5. 分区判定：`/partitions`，`/api/partitions`。
- 6. 访问对账：`/audit`，`/api/audit`。
- 7. 审计归档：`/seals`，`/api/seals`。
- 8. 查询准入：`/admissions`，`/api/admissions`（联合查询按分区裁决，详见下文）。

## 查询准入（M8）

联合分析把多个分区合成一次聚合查询：已授权分区当天要出数，但另一个分区刚到期。M8 不做整单放行/整单拒绝的二选一，而是逐分区复算授权，为每个查询形成唯一准入结论：

- **整单放行**：所有分区都能独立证明在授权范围内；
- **部分放行**：只放行能独立证明授权范围的分区，到期/未授权分区拦截，合法分析不断档；
- **整单拒绝**：没有任何可放行分区。

裁决直接复用 M3 授权定版与 M4 冲突裁决（拒绝优先、半开区间 `[start,end)`），查询裁决、对账与证据导出引用同一组分区依据 `basis_sha256`。

准入账本只增不改：同一查询重试不新增结论，仅追加 `RETRY` 流水；复核切换失败（陈旧版本 409、不可封存 422）保留旧结论和原分区依据，失败也留痕。未证明放行结果未混入到期数据（`taint=0` 且行数对平）前，封存按钮禁用、封存接口返回 `NOT_SEALABLE`。

入口：`POST /api/admissions`（sample 或 csv，幂等落账）、`GET /api/admissions`（即时演算不落账）、`POST /api/admissions/upload`、`GET /api/admissions/runs`、`POST /api/admissions/runs/<id>/review`（`封存`/`退回补证`，需 version）、`GET /api/admissions/runs/<id>/journal`、`GET /api/admissions/runs/<id>/evidence`。样例为 `sample_data/admissions.csv`。

## 核心规则

拒绝规则优先于允许，授权必须同时命中资源、分区和生效窗；显式拒绝不得被宽授权覆盖。

## 演示

1. 打开首页，核对当次计算、差异和隔离原因。
2. 切换模块查看源行及裁决依据。
3. 重跑相同样例，批次号与行数保持一致。
4. 复核后导出一份证据CSV。版本已变化的复核提交返回409。

## 输入与输出

输入列见 `docs/DATA-DICTIONARY.md`。项目包含dirty.csv、clean.csv、legacy.csv，统一走实际导入路径。上传限制4MB，拒绝不带时区的时间。业务键按字符串保留，Decimal参与计算，空值不会当零。

`POST /api/runs` 接收sample或csv；`GET /api/runs`返回历史批次；`POST /api/runs/<id>/review`需要version与decision；`GET /api/runs/<id>/archive`返回唯一证据导出。

## 范围

样例均为虚构。单机复核工具不连接生产系统；不提供登录、远端数据库或自动支付。内存计算按4MB输入上限控制，SQLite事务保证结果完整写入。

## 目录

app为服务与规则，sample_data为输入，tests为测试，docs为定义与深度登记。
