# DataPro 对「数据库字段存 JSON、内含多条数据」的取数/分析可行性评审

> 评审人：高见远（Gao，架构师） · 团队 software-datapro-json
> 评审对象：用户提供的「网页采集配置 JSON」（来源疑似"先知"投放监控平台）
> 依据：以最新 `AGENTS.md` 与源码为准，结论附 `文件:行号`；实测为只读探测，不改动项目。
> 说明：用户样例中 `login` 段含**明文真实账号密码**，已随对话进入历史、视为已泄露，本文一律以 `***` 代替，且不给任何"把它整段交给 DataPro/LLM"的方案。

---

## ① 结论速览

| 问题 | 结论 |
|---|---|
| **能不能**（JSON_EXTRACT / `->` / JSON_TABLE 在当前链路能否走通） | **部分可行**。MySQL 8.0 的 JSON 函数在执行层能跑（只读账号 + `dwd_shop` 支持），但**召回层不认识 JSON 内层字段、生成层没约束也没字段信息、校验层"两不通"**——手写/模板提取外层已登记的表勉强能通；自然语言 Text2SQL 自动分析 JSON 内层字段**不可靠**。 |
| **该不该**（把这类 JSON 当分析对象） | **不该**。用户样例本质是**爬虫/采集配置**（选择器、翻页、登录、字段抽取规则），不是业务事实数据。真正要分析的大概率是它采集出来的"展示/点击/账面消耗/报名人数/报名成本"等**结果数据**，而不是配置本身。配置应作元数据/配置对象管理。 |
| **单个数据能不能分析** | 单条记录适合**查询/展示/问答**（"这条配置抓哪些字段"），**谈不上统计分析**——统计需要多条记录或它产出的业务数据。 |
| **推荐方案** | **中期规范化（M）为主**：把 JSON 展开/抽取成规范化事实表 `ods_xianzhi_*`，登记元数据后走既有 Text2SQL 链路；**短期生成列（S）作过渡**。分析对象优先选"采集结果"而非"配置"。 |

**一句话**：技术上"能查但不能稳查"，业务上"不该查配置、该查采集结果"，架构上"回到 AGENTS.md §24 的既定决策——一数据集一真实表，不做单表 JSON 堆叠"（`AGENTS.md:24`）。

---

## ② 逐环节技术核对表（含源码证据）

主链路：意图 → 召回 → 解析(IR) → 生成 → 校验 → 执行 → 呈现（`AGENTS.md:95,98`）。JSON 字段要穿透的是"召回 → 生成 → 校验 → 执行"四环。

| 环节 | 判定 | 具体表现 | 源码/实测证据 |
|---|---|---|---|
| **召回** | ❌ **卡点** | 召回靠 `meta_table/meta_column` 组装的**扁平 `columns` 列表**和关键词匹配。JSON 内层字段（如 `$.展示`、`$.点击`）**没有独立列条目，无法被召回**；若这张 JSON 表根本没登记进 meta，则召回 0 → E002/E003 拒答，链路走不到生成。 | `recall.py:174-203`（`recall()` 基于 `metadata_store.tables()`）；`recall.py:154-160`（表名/关键词匹配）；`metadata_store.py:92-102`（`"columns": [c.col_name for c in col_rows]`，无 JSON 路径概念）；`AGENTS.md:149`（0 命中不编造） |
| **生成** | ⚠️ **半通** | `sql_gen.txt` v7 对 JSON 函数**无任何条款**（既未禁止也未允许）。但 prompt 强约束"只使用可用表结构中列出的表与字段，未列出的视为不存在，不得臆造"——JSON 内层路径没被注入 `_schema_block`，模型即便想用也没有字段名，倾向于不用或编造。few-shot 形状示例里也没有 JSON 查询样例。 | `sql_gen.txt:26-34`（要求第 6 条）、`sql_gen.txt:40-43`（方言约束只列日期函数，**无 JSON 条款**）；`generator.py:24-43`（`_schema_block` 只注入 `cols`/`labels`/`enums`）；`generator.py:119-131` |
| **校验** | ⚠️ **两不通（实测）** | ① **误放**：`JSON_EXTRACT(cfg,'$.nmae')` 路径拼错仍 `passed=True`——字段存在性检查只遍历 `exp.Column`，JSON 路径是**字符串字面量**，穿透不了；写错路径不报错，执行返回 NULL，属"看似成功"的错答。<br>② **误拦**：`JSON_TABLE(...) AS j` 的展开列 `j.dim` 被解析为 `exp.Column`，未登记到 `column_map` 时直接报"引用了不存在的字段: dim"。<br>③ 危险函数黑名单与"非 MySQL 方言函数"表**都不含 JSON 函数**，JSON_EXTRACT/JSON_TABLE 本身不会因函数名被拦。 | `validator.py:47-56`（危险函数 + `UNSUPPORTED_FUNCS`）、`validator.py:77-89`（字段存在性：`parsed.find_all(exp.Column)`）、`statemachine.py:420-428`（`_column_map` 仅由召回表 `columns` 构造）。<br>**实测**（只读调用 `validate_sql`，`column_map={"t":{cfg,dt,id,name}}`）：`JSON_EXTRACT(cfg,"$.nmae")` → `passed=True`；`j.dim`（JSON_TABLE 展开列）→ `passed=False, "引用了不存在的字段: dim"`；`cfg->>"$.login.username"` → `passed=True`。 |
| **执行** | ✅ **可通（有前提）** | 只读账号 `ro/ro` 对 `dwd_shop`，MySQL 8.0.32 原生支持 JSON 函数，只读 `SELECT` 无障碍；`inject_dates`/超时/行数截断均不干扰。**前提**：JSON 表必须在 `dwd_shop` 且已进表白名单——`executor` 只连 `data_source_url` 一个库，不连业务库 `datapro`、也不连数据源库 `datapro_source`。 | `executor.py:44-51,138-160`（单 engine = `data_source_url`）；`config.py:16`（`data_source_url=...ro:ro@.../dwd_shop`）；`AGENTS.md:20`；表白名单 `validator.py:62-66` |

**判定汇总**：执行层可通；召回层是**硬卡点**；生成层缺字段信息；校验层既会**误放错路径**又会**误拦合法展开列**。因此"技术上完全可行"不成立，"完全不可行"也不准确——结论为 **部分可行 / 需建模改造**。

### 关于"多数据源"旧判断的纠正（重要）

- 此前 `docs/DataPro-缺口分析-前后端技术评审.md:223,288` 将 **U-B02 多数据源**判为 **P2 / L**。
- 以最新 `AGENTS.md` 为准：**基础设施已落地**——独立数据源库 `datapro_source`、5 张 `ingest_*` 登记表、独立 engine（`source_session.py`）与独立迁移链（`alembic_source`），head 分别为 `0012_split_ingest_db` / `0001_ingest_layer`（`AGENTS.md:22-24`；`models_source.py:1-9`）。
- **但要区分两层**：落地的是"**导入承接/登记与血缘**"，**不是"取数链路的多源查询"**。`models_source.py:8` 明确"只放登记与血缘，不放业务数据本身；一个数据集对应一张真实物理表"；`executor.py:44-51` 仍是单 `engine`；`metadata_store.py:76-102` 仍只读业务库 `meta_*`。故 U-B02 的"**多源取数**"能力实质上未打通。
- **对本例的影响**：本次 JSON 数据若落在 `dwd_shop` 的规范化表，**不需要**动多源取数，旧 P2 判定不影响本方案；只有当数据必须留在另一个物理库、且要跨源 JOIN 时，才真正触发 U-B02（仍属二期 L）。

---

## ③ 两种数据形态的处理方案（含 MySQL 8.0 SQL 示例）

> 命名沿用 AGENTS.md 建议 `ods_{source}_{dataset}`（`AGENTS.md:24`）。示例表名/字段名为占位，需按实际 schema 调整；**示例不含任何真实凭据**。

### 形态 A：一个 JSON 字段里存的是**数组**（一条记录 = 多条配置对象）

假设 `ods_xianzhi_config.cfg` 是 JSON 数组。

**方案 A1（推荐）— 用 `JSON_TABLE` 展开成行再落表**：

```sql
-- 一次性规范化：把数组配置展开成"一配置一行"
CREATE TABLE ods_xianzhi_config_item AS
SELECT
    c.id                         AS config_id,
    j.ord                        AS item_ord,
    j.cfg_name                   AS cfg_name,
    j.list_url                   AS list_url,
    j.item_selector              AS item_selector,
    j.max_pages                  AS max_pages
    -- 注意：login.username / login.password 一律不落地、不入表
FROM ods_xianzhi_config c,
     JSON_TABLE(
       c.cfg,
       '$[*]' COLUMNS (
         ord           FOR ORDINALITY,
         cfg_name      VARCHAR(128) PATH '$.name',
         list_url      VARCHAR(512) PATH '$.list_url',
         item_selector VARCHAR(255) PATH '$.item_selector',
         max_pages     INT          PATH '$.pagination.max_pages'
       )
     ) AS j;
```

- `JSON_TABLE` 的 `PATH '$.pagination.max_pages'` 演示**嵌套对象**取值；`FOR ORDINALITY` 保留数组顺序。
- 列表字段（`list_fields` 是内层数组）若也要下钻，可再对 `j` 的结果用第二层 `JSON_TABLE`，或单列一张 `ods_xianzhi_config_field(config_id, field_name, selector, attr)`。

**方案 A2 — 分析时即时展开（不落表，仅适合已登记的表 + 手写 SQL）**：
```sql
SELECT j.cfg_name, j.max_pages
FROM ods_xianzhi_config c,
     JSON_TABLE(c.cfg, '$[*]' COLUMNS (
       cfg_name  VARCHAR(128) PATH '$.name',
       max_pages INT          PATH '$.pagination.max_pages')) AS j
WHERE c.dt BETWEEN '{date_start}' AND '{date_end}'
LIMIT 200;
```
- ⚠️ 当前链路下 `j.cfg_name` 会被校验器**误拦**（见 ② 实测），除非把 `cfg_name` 登记进 meta_column 使 `column_map` 含它。故 A2 只作为过渡或调试用，**不作为自然语言 Text2SQL 的稳定路径**。

### 形态 B：表里很多行、**每行一个 JSON 对象**

**方案 B1（推荐）— 提取高频字段为生成列（Generated Column）**：

```sql
-- MySQL 8.0：生成列可像普通列一样被索引、被元数据登记、被 Text2SQL 引用
ALTER TABLE ods_xianzhi_config
  ADD COLUMN cfg_name   VARCHAR(128)
      GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(cfg, '$.name'))) STORED,
  ADD COLUMN list_url   VARCHAR(512)
      GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(cfg, '$.list_url'))) STORED,
  ADD COLUMN cost_yuan  DECIMAL(18,2)
      GENERATED ALWAYS AS (CAST(JSON_EXTRACT(cfg, '$.cost') AS DECIMAL(18,2))) STORED,
  ADD COLUMN signup_cnt INT
      GENERATED ALWAYS AS (CAST(JSON_EXTRACT(cfg, '$.signup_count') AS UNSIGNED)) STORED,
  ADD INDEX idx_cfg_name (cfg_name);
```
- 生成列的关键价值：它**把 JSON 内层字段"抬升"成真正的列**，于是自动获得召回（`meta_column` 可登记）、字段校验（`column_map` 可含它）、Text2SQL 可引用——**绕开 ② 中 JSON 路径的全部卡点**。
- 若字段多/结构常变，改为**方案 B2 规范化列**：ETL 时把高频字段直接写成普通列（与应用层 ORM 对应），JSON 原文仅在需要时留存（且必须脱敏后再留）。

### 元数据登记：让 Text2SQL 能识别

无论 A/B，展开后的列必须登记，否则召回不到、校验会拦：

1. **业务库 `meta_table` / `meta_column`（D05，`AGENTS.md:137`）**：把规范化/生成列作为普通列登记（`table_name` / `col_name` / `cn_name` / `enum_meaning`）。这是生成 prompt `_schema_block`（`generator.py:24-43`）与校验 `column_map`（`statemachine.py:420-428`）的**唯一来源**，登记后模型能看到、校验能放行。示例：在填报模板中登记 `ods_xianzhi_config_item` 表及 `cfg_name`（配置名）/`max_pages`（最大翻页）等列，`python -m importer.import_metadata --file 模板.xlsx --dry-run` 校验后正式导入（`AGENTS.md:207-209`）。
2. **口径库 `metric_caliber`**：若用户问的是采集结果（如"报名成本"），按口径模板登记 `logic`（如 `SUM(signup_cost)/NULLIF(SUM(signup_cnt),0)`），走"先口径后取数"（`AGENTS.md:8,98`）。
3. **数据源库 `ingest_*`（`models_source.py`）**：把这条采集通道登记为 `ingest_source`（`kind='scraper'`，`code` 参与物理表命名）；一个数据集登记一行 `ingest_dataset`（`db_name='dwd_shop'`、`table_name='ods_xianzhi_*'`、`grain`、`primary_key`、`meta_registered=True`）；`ingest_field_map` 记录 `src_field`（如 `$.name` 或 `list_fields[].selector`）→ `col_name`（如 `cfg_name`）的映射，`transform`/`enum_map` 放清洗与枚举归一；`ingest_batch` 记录批次血缘（`file_sha256`/`rows_*`）。**注意 `ingest_*` 是登记表，物理数据另放 `dwd_shop`**（`models_source.py:8,38-62,65-79`）。

### 用户样例的定位（关键提示）

样例里的 `item_selector`、`.el-table__row`、`td:nth-child(N)`、`pagination` 是**怎么抓**的规则；`username`/`password`（`***`）是**用什么身份抓**；`block_urls`（如 `**/internal-sentry.baijia.com/**`）是**不抓哪些内网/监控地址**。这些是**采集配置/元数据**，不是可统计的业务事实。真正的"数据"是抓下来的"展示、点击、账面消耗、报名人数、报名成本"等指标——**这些才应该落表成为分析对象**。

---

## ④ "单个数据"能不能分析——诚实回答

- **能做的**：把这一条配置当作**对象**查询/展示/问答。例如"这条配置的 `list_url` 是什么""抓了哪些字段""是否翻页""登录方式配置"。这属于 `meta_qa`/查询展示，走检索与呈现即可，不是统计分析。
- **不能做的**：**统计意义上的分析**——均值、趋势、同比环比、分布、TopN、对比等，对**一条**记录没有统计意义（样本量=1）。用户样例中的"维度/展示/点击/账面消耗/报名人数"若只在一条 JSON 里各出现一次，无法做聚合分析。
- **要分析的两种正确姿势**：
  1. 有**多条配置/多行 JSON** → 先按 ③ 展开成多行，再做统计；
  2. 只有**这一条配置** → 分析它**每次采集产出的结果数据**（按天/按维度的消耗、报名人数），这些结果才是时间序列、才可分析。
- 结论：**单条配置 = 查询/问答；统计分析需要"多条记录"或"它产出的业务数据"。** 不夸大能力，避免让用户以为贴一条 JSON 就能出报表。

---

## ⑤ 安全与合规硬约束

> 明文密码已进入对话历史，视为泄露。**轮换是第一优先级**，任何建模方案都不能替代轮换。

1. **凭据已在对话暴露 → 必须立即轮换**该账号密码，并检查该平台登录日志是否有异常。
2. **绝不入库原样**：落表/落地时剔除 `login.username`/`login.password`（见 ③ 示例注释），或写入前统一替换为 `***`。不要出现 `password` 这样的列存真值。
3. **绝不入 prompt**：`generator._question(ctx)` 会把用户原句**完整**注入生成 prompt（`generator.py:176-194`）；用户若把整段 JSON（含密码）贴进问题，密码直达 LLM 请求与上游服务商。**禁止把整段 JSON 塞给 LLM**。
4. **绝不入审计/日志/trace**：
   - `audit.py:12-23` 记录 `question_digest = ctx.question[:200]` 与 `sql_text`；
   - `executor.py:156-159` 失败时 `logger.warning("SQL 执行失败：%s | SQL=%s", detail, final_sql)` **记完整 SQL**；
   - `AGENTS.md:276` 明确 Langfuse trace 含"用户问题、prompt 与 SQL"，接外部服务前须确认合规。
   → 因此问题文本与 SQL 中都**不得出现明文凭据**；否则进 `audit_log`、日志与 Langfuse。
5. **返回层脱敏不覆盖 JSON 内密码**：`masking.py:13-21` 的列名规则只针对 `phone/id_card/bank_card/email/address/real_name` 等，**不含 password/token/secret**；`SELECT cfg` 会把 JSON 内明文密码原样返回前端。故必须在**采集/入库前**剔除，不能指望返回层打码。
6. **密钥管理建议**：真实凭据放密钥管理系统（Vault / 云 KMS / 密钥文件），通过环境变量或运行时注入引用，绝不写死在 JSON、代码、`data/*.json` 或提交历史；参照项目对 LLM Key 的做法（`data/llm_apis.json` 已 gitignore，`AGENTS.md:10,257`）；采集侧用**最小权限专用账号**，不要复用高权限账号。
7. **`block_urls` 提示**：`**/internal-sentry.baijia.com/**` 是**内网监控/哨兵域名**的屏蔽规则，说明该采集面向内网系统。提示用户：内网地址、账号体系属敏感资产，配置与日志不要外发；`block_urls` 本身也应在入库前做**白名单校验**，避免被改写成任意内网地址造成 SSRF 式风险（DataPro 本身不执行爬虫，但配置保管方需注意）。
8. **遵守项目铁律**：全程只读、先口径后取数、召回不到不臆造、禁止开放写/导出敏感字段、禁止模型自由生成绕过校验的 SQL（`AGENTS.md:8,296-303`）。本评审不 propose 任何写库/旁路执行方案。

---

## ⑥ 推荐落地路径与工作量

| 方案 | 做法 | 工作量 | 前提 | 风险 | 适用 |
|---|---|---|---|---|---|
| **方案一：生成列过渡（短期）** | 对形态 B 的高频字段加 `GENERATED ALWAYS AS (JSON_EXTRACT(...)) STORED` 生成列 + 登记 `meta_column`；不碰状态机（③ B1） | **S** | 表在 `dwd_shop`、可 ALTER；字段集合相对稳定；能停机/低峰加列 | 大表 ALTER 可能锁表（MySQL 8.0 多数 DDL 可 Online，仍需评估）；字段变更要改列；JSON 内密码需先剔除 | 快速验证/"就想先查起来" |
| **方案二：规范化事实表（中期）⭐ 推荐** | 用 `JSON_TABLE`/ETL 把配置或采集结果展开成 `ods_xianzhi_*` 真实表；登记 `meta_table/meta_column/metric_caliber` + `ingest_*` 承接层；走既有 Text2SQL（③ A1/B2） | **M** | 明确数据形态（数组 or 多行）与业务主键/粒度；确认分析对象是"结果数据" | ETL 与口径对齐工作量；需回填历史；配置结构变更需维护映射 | 用户真正想分析采集结果 |
| **方案三：走 ingest 数据源层（二期）** | 数据留在独立源库，扩展 `executor` 多 engine 路由 + 召回跨源；即 U-B02 原判定 | **L** | 交付形态确定、源侧只读账号就绪、限定同构 MySQL 起步（`缺口分析…:223`） | 触及"只读 + 不绕过 validator"红线，需防多源旁路（`缺口分析…:238`）；工作量数周 | 多源且必须跨源 JOIN |

**推荐**：先确认分析对象。**若分析"采集结果"（大概率）→ 方案二（M）为主、方案一（S）作过渡**；配置本身只作元数据登记，不进分析。只有当数据必须留在另一物理库且需跨源 JOIN，才升级方案三（L，二期）。安全侧动作（轮换凭据、脱敏入库）**先于任何方案执行**。

---

## ⑦ 待用户澄清（不确定项，不臆造）

1. **数据形态**：JSON 字段里到底是**一个数组（多条配置）**，还是**表里很多行、每行一个 JSON 对象**？——决定用 ③ 的 A 还是 B。
2. **分析对象**：想分析的是**这些采集配置本身**（几条配置、翻页设置等），还是**它们采集出来的结果数据**（消耗/报名人数/成本）？——后者才适合统计分析。
3. **数据位置与规模**：这张表在哪个库、哪张表？是否在 `dwd_shop`？数据量多大（决定 ALTER 可行性）？是否已有 `dt` 分区/时间维度？
4. **字段语义**：样例里的"展示/点击/账面消耗/报名人数/成本"是**采集结果指标**还是**配置里的阈值**？若要按天分析，是否有对应的日期字段？
5. **凭据处置**：已暴露账号是否已完成轮换？（未轮换前不建议把数据接入任何环境。）
6. **权限边界**：这批数据谁能看？是否包含其他账号/内网信息需要脱敏或行级隔离？
7. **待核实（源码无法确认）**：`JSON_TABLE` 展开列在真实 MySQL 8.0 上的执行计划/性能、大 JSON 的扫描成本——需在样例库上 `EXPLAIN` 实测（A09 扫描量守卫 `executor.py:86-110` 会兜底拦截大查询）。

---

### 附：本文证据索引（便于复核）

- `AGENTS.md:8,20,22-24,95,98,137,149,207-209,257,276,296-303`
- `datapro/app/db/models_source.py:1-9,38-62,65-79`
- `datapro/app/services/validator.py:24-26,47-56,62-66,77-89,91-94`
- `datapro/app/core/statemachine.py:340-401,420-428`
- `datapro/prompts/sql_gen.txt:26-34,40-43`
- `datapro/app/services/generator.py:24-43,119-131,176-194`
- `datapro/app/services/recall.py:154-160,174-203`
- `datapro/app/services/metadata_store.py:76-102`
- `datapro/app/services/executor.py:44-51,86-110,138-160`
- `datapro/app/config.py:16`
- `datapro/app/services/audit.py:12-23`
- `datapro/app/services/masking.py:13-21`
- `datapro/app/db/session.py:21-41`（`init_db` 先数据源库后业务库）、`datapro/app/db/source_session.py:84-105`
- `docs/DataPro-缺口分析-前后端技术评审.md:223,238,288`（U-B02 旧判定，本文已据最新 AGENTS.md 纠正）
- 实测：只读调用 `validate_sql` 验证 JSON 路径误放 / JSON_TABLE 展开列误拦（结果见 ②）。
