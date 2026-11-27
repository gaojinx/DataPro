# DataPro 页面体验小功能 — 后端

| 文档信息 | 内容 |
|---|---|
| 拆分来源 | 《DataPro-页面体验小功能清单》（产品经理 许清楚）+《DataPro-页面体验小功能-技术评审》（架构师 高见远） |
| 性质 | 将原「产品清单 + 技术评审合一」的产出**按端拆分**，本文件只讲后端；前端 18 条完整实现见《DataPro-页面体验小功能-前端.md》 |
| 覆盖范围 | 18 条中**涉及后端的 4 条**：X-L03 / X-R04 / X-I03 / X-C01，外加 1 条明确否决的 X-R06 与 1 条移出的 X-A01 |
| 核对依据 | `routes_chat.py`（247 行）、`validator.py`（125 行）、`executor.py`（192 行）、`statemachine.py`、`masking.py`、`models.py`、`config.py`、`nginx.conf` 逐行抽查 |
| 状态 | 供排期与拍板 |

> **一句话结论**：18 条体验小功能里**只有 4 条真正涉及后端，且改动都很小**——X-L03 加 1 个 SSE 字段、X-R04 可选加类型白名单、X-C01 可选加权威时间、X-I03 若要硬上限才需联动。**X-R06 分页明确否决后端改动**（200 行硬帽是设计约束，加 offset/limit 属提权风险）。其余 13 条纯前端，后端零工作。

---

## 1. 后端相关条目总览

| ID | 功能 | 后端是否需要 | 改动性质 | 工作量 | 优先级建议 |
|---|---|---|---|---|---|
| **X-L03** | 扫描量预估提示 | 需要（1 个字段） | result 事件透传已有 `estimated` | S | 条件项，排在纯前端批次之后 |
| **X-R04** | 数值千分位 | 可选（推荐） | result 事件附 `numeric_columns` 类型白名单 | S | 待用户拍板：降级 vs 白名单 |
| **X-I03** | 字符计数 | 仅硬上限时需要 | `Field(max_length)` + 422（超出小功能） | S | 待用户拍板：是否设建议值；硬上限不属小功能 |
| **X-C01** | 消息时间戳 | 可选（一般不需要） | result 事件补服务端 `ts` | S | 建议不做，用前端 `Date.now()` |
| **X-R06** | 结果集分页 | ❌ **明确否决** | 后端 offset/limit 重查 | — | 纯前端切片即可 |
| **X-A01** | 字号缩放 | ❌ 不涉及 | — | — | 移出单独立项 |
| 其余 13 条 | 纯前端小功能 | ❌ 无需改动 | — | — | 见前端文档 |

---

## 2. 逐条后端改动点

### 2.1 X-L03 扫描量预估提示 — 需要，1 个字段

**现状事实**：后端在 EXECUTE 前已调用 `estimate_scan`，结果存在 `estimated` 变量中，目前**仅在超过阈值时用于 E009 拒绝**，未下发给前端。

| 层 | 文件:位置 | 改动 |
|---|---|---|
| 状态机 | `app/core/statemachine.py`（`Event` 是 `dict` 子类，`:99`） | 在 `result` 事件加 `est_scan_rows=estimated`，**复用已有变量，零成本**；禁止新增 EXPLAIN 请求 |
| 网关 | `app/api/routes_chat.py:133-166` | result 事件为直接透传（`yield sse(event)`），**无需改代码** |
| 前端 | `App.vue:611-657`（`handleEvent` 读取）、`:2406` 附近 chip | 见前端文档 |

**约束与降级**：
- `estimate_scan` 在 `SCAN_GUARD_ENABLED=false` 或 `MAX_SCAN_ROWS=0` 时返回 None（`executor.py:86-90`），此时前端无值可显、静默降级；
- 不改变 `validator` + 只读账号执行路径，只透传展示；
- E009 分支（超阈值走 escalate）行为不变，`App.vue:640-645` 已展示后端 message。

**验收**：后端单测断言 result 事件含 `est_scan_rows`；有值时前端 chip 显示、None 时不显示；确认未新增 EXPLAIN 调用。

### 2.2 X-R04 数值千分位 — 可选，推荐加类型白名单

**现状事实（关键）**：

| 检查点 | 结果 | 证据 |
|---|---|---|
| 执行结果类型 | `columns = list(result.keys())`、`rows = [dict(zip(columns, r))…]`，值为 pymysql 原生类型（int / float / **Decimal** / str / date） | `executor.py:153-154` |
| SSE 序列化 | `json.dumps(event, ensure_ascii=False, default=str)`——**Decimal 与 date 被转成字符串**，int/float 保持 number | `routes_chat.py:46-47` |
| 脱敏 | 只处理字符串值，数值列不改（`:108` `not isinstance(val, str)` 跳过） | `masking.py:74-121` |
| 后果 | 金额 `72470.55`（DECIMAL）到前端是 **string**；`order_no` / `trade_no` 也是 string，**前端无从区分** | 同上 |

**技术裁决**：**禁止**对字符串型值做启发式千分位——会把 `order_no` `202611220001` 变成 `202,611,220,001`，改坏编码语义，触碰「不改数值/编码」约束。

| 方案 | 做法 | 效果 | 工作量 |
|---|---|---|---|
| A 纯前端降级 | 只格式化 `typeof v === 'number'` 的列 | INT/FLOAT 生效，**金额列（DECIMAL）不生效**，须向用户说明 | XS |
| **B 后端白名单（推荐）** | 在 `default=str` 之前按 `isinstance(v, (int, float, Decimal))` 推断每列类型，result 事件附 `numeric_columns: list[str]` | 前端按列名白名单格式化，金额列生效，编码列保持原样 | S |

**需用户拍板**：接受 A 降级，还是投少量后端做 B（推荐）。

**验收**：按选定方案——A 则 number 列格式化且金额列限制已说明；B 则 `numeric_columns` 生效、`order_no` 等编码字段保持原样；两者均 display-only，不写入结果数据。

### 2.3 X-I03 字符计数 — 后端**没有**上限

**现状事实（清单的「N / 上限」形态不成立）**：

| 检查点 | 结果 | 证据 |
|---|---|---|
| 后端请求模型 | `message: str`，**无** `Field(max_length=…)` | `routes_chat.py:36-39` |
| 后端配置 | 无 message/input 字数配置（仅 `exec_row_limit`、`rate_limit_per_minute`、`vector_max_length` 等） | `config.py` 全文 |
| 网关 | 未设 `client_max_body_size`，仅 nginx 默认 body 上限（约 1MB，body 级非字段级） | `nginx.conf:8-19` |
| 前端 | textarea 无 `maxlength` | `App.vue:2638-2640` |

**技术裁决**：不虚构上限。X-I03 二选一——(a) 纯计数、不显示分母；(b) 定义一个**建议长度**（如 1000 字），UI 标「建议 1000 字以内，超出可能影响回答质量」，仍不硬截断。

**若要硬上限**（不属小功能）：后端 `ChatRequest.message` 加 `Field(max_length=…)` 返回 422，前端加 `maxlength`，两端联动。

**需用户拍板**：是否引入一个「建议数值」。

### 2.4 X-C01 消息级时间戳 — 一般**不需要**后端

**现状事实**：权威时间已存在——`message.created_at`、`query_record.created_at`（`server_default=func.now()`），每轮异步落库（`models.py:37` / `:64`；`routes_chat.py:103-104` / `:178-179`）；SSE 事件无 `ts` 字段（`routes_chat.py:110`、`:137-152`）。

**技术裁决**：X-C01 保持纯前端——push 时写 `ts: Date.now()`，显示 `HH:mm`，tooltip 完整日期，标注「本地时间，审计以服务端为准」。客户端时钟偏差不影响审计。

**仅当用户坚持「界面时间必须与审计逐秒一致」时**，才在 result 事件补 `ts`（S 档）——这不是当前「回溯有参照」诉求的必要条件。

---

## 3. 明确否决 / 移出的后端改动

### 3.1 X-R06 分页 — ❌ 否决后端 offset/limit

| 检查点 | 结果 | 证据 |
|---|---|---|
| 校验器强制 LIMIT | `validator.py:91-94` 强制 `LIMIT 200` | — |
| 执行器硬帽 | `fetchmany(exec_row_limit=200)` | `executor.py:154` |
| 配置 | `exec_row_limit=200` | `config.py:26` |
| 截断标记 | `truncated=true` 已随结果返回 | `executor.py:155` |
| 导出路径 | 另有 `fetch_all` + `EXPORT_MAX_ROWS`，走 B13 审批 | `executor.py:162-189` |

**裁决**：对话内结果最多 200 行，「几百行卡顿」前提不成立；**纯前端分页即可**。后端 offset/limit 重查需重过 `validator` + 会话身份 + 只读账号，且突破 200 行硬帽，属提权风险面，**明确否决**。大结果走导出审批，不走对话内表格。

### 3.2 X-A01 字号缩放 — ❌ 不涉及后端，移出单独立项

字阶变量存在但值为 px（`style.css:14-24`），全文 135 处硬编码 `font-size:Npx` vs 仅 13 处 `var(--fs-*)`；改 root `font-size` 对 px 无效，`zoom`/`transform: scale` 破坏 sticky 表头、滚动容器与 4px 网格；本机浏览器自动化不可用，视觉回归受限。属「设计令牌体系改造 + 全量视觉回归」，M 档（≈1 周），不算小功能。降级版「大字号模式」只改 2~3 个选择器，届时重评。

---

## 4. 后端红线核查

| 红线 | 涉及条目 | 核查结果 | 约束 |
|---|---|---|---|
| 不绕过 validator + 只读账号 | X-R06、X-L03 | ✅ 通过 | X-R06 仅对已返回行切片、不重查；X-L03 仅透传已算出的 `estimated`，不新增 EXPLAIN 请求。若 X-R06 改后端 offset/limit 重查 → 明确否决 |
| 不改数值/编码字段语义 | X-R04 | ⚠️ 需约束 | 禁止正则格式化字符串（改坏 `order_no`）；推荐 `numeric_columns` 白名单，display-only |
| 不引入越权缓存 | X-I01、X-C01 | ✅ 通过 | 草稿按 username 分 key（`dp_draft_{username}`）并随账号切换重置，时间戳随现有会话快照；不新增服务端缓存、不存结果行/附件 |
| 「0 单 ≠ 无数据」语义（§4.2-11） | X-R03（前端） | ✅ 通过 | 后端探针与结果语义自检不动，`0` 原样返回 |
| 旁路不阻塞主链路（§4.2-8） | X-L03、X-R04 | ✅ 通过 | 新增字段为可选，序列化/脱敏失败只 `logger.warning`，不影响取数结果 |
| 功能先登记再开发（§4.2-9 / §8） | 涉及后端的条目 | ⚠️ 待办 | 落地前先更新 `docs/DataPro-功能清单.md`（含变更登记）与 `AGENTS.md` 对应章节 |

---

## 5. 后端验收方式汇总

| 条目 | 验收方式 |
|---|---|
| X-L03 | 后端单测断言 result 事件含 `est_scan_rows`；`SCAN_GUARD_ENABLED=false` 时字段为 None 且不报错；确认未新增 EXPLAIN 调用（静态检查 + 日志） |
| X-R04（方案 B） | 单测构造 `Decimal('72470.55')`、`order_no='202611220001'`，断言 `numeric_columns` 只含金额列不含编码列；SSE 序列化后前端按白名单格式化 |
| X-I03（若做硬上限） | 超长请求返回 422 且错误信息明确；前端 `maxlength` 一致；**默认不做** |
| X-C01（若做权威 ts） | result 事件含 `ts`；与 `created_at` 误差在可接受范围；**默认不做** |
| X-R06 | 静态检查确认无新增网络请求（DevTools Network）；`truncated` 提示正确；**禁止**出现 offset/limit 参数 |

---

## 6. 与前端文档的衔接

| 条目 | 本文件（后端） | 前端文档 |
|---|---|---|
| X-L03 | result 事件加 `est_scan_rows=estimated` | `handleEvent` 读取 + chip 展示 |
| X-R04 | 可选 `numeric_columns` 白名单 | 按白名单 display-only 格式化 |
| X-C01 | 默认不做（权威时间已在 DB） | `Date.now()` 打点 + 本地时间标注 |
| X-I03 | 默认无上限，不虚构 | 纯计数或「建议值」提示 |
| X-R06 | **明确否决后端改动** | 纯前端 50 行分页 |
| 其余 13 条 | 零改动 | 全部纯前端实现 |

> **建议节奏**：先做前端文档里的周末 6 条（零后端依赖）；后端改动只在 X-L03 或 X-R04(方案 B) 进入迭代时同步进行，不要提前为分页加接口。
