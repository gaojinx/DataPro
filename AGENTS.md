# AGENTS.md — DataPro 项目长期上下文

> 本文件是项目的长期上下文与协作约定。**每次改完代码请顺手更新对应章节**（见文末「维护约定」）。

## 1. 项目定位与阶段

- **项目**：DataPro（数据管家 Agent），面向业务人员的自然语言取数助手：Chat2SQL、元数据/口径问答、数据巡检。
- **核心约束**：全程只读；先口径后取数；召回不到不臆造；全程留痕可审计。
- **当前阶段**：MVP 可运行骨架，核心链路已打通并接本机 MySQL 数仓；未上线。
- **LLM 状态**：默认无真实 Key，`LLM_MOCK=true`，走「口径模板降级」；无口径则转人工（escalate）。真实 Key 联调与评测仍待办。

## 2. 技术栈与端口

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | Python 3.14 + FastAPI + SQLAlchemy 2 + pymysql + sqlglot | 自研 9 节点状态机，**不使用 LangChain** |
| 前端 | Vue 3 + Vite 6 + Element Plus + Univer（开源版，按需加载） + SheetJS | Codex 风格三栏对话页 |
| 后端端口 | `127.0.0.1:8100` | `uvicorn app.main:app` |
| 前端端口 | `5175` | Vite dev，代理 `/api`、`/admin` 到 8100 |
| 数仓 | MySQL 8.0.32 `dwd_shop` | root/123456；只读账号 `ro/ro` |
| 业务库 | MySQL `datapro` | 账号 `datapro/pw`；存 query_record / audit_log 等 |
| 上传临时库 | MySQL `dp_uploads` | 会话文件解析成 `f_{file_id}` 表（多 sheet 为 `_s2`…） |
| LLM | openai 兼容 SDK | 支持 DeepSeek/Qwen 等；注册表 JSON 持久化 |
| 认证 | PBKDF2 密码哈希 + HMAC 签名 Token | B12 基础版；角色 admin/user；Token 默认 12h |

## 3. 目录结构

```
AgentProject/
├─ AGENTS.md                   # 本文件：长期上下文
├─ .gitignore                  # 已排除 node_modules/dist/运行数据/密钥
├─ docs/                       # 12 份项目文档（章程/HLD/接口/权限/评测/灰度/Runbook…）
└─ datapro/
   ├─ app/
   │  ├─ main.py               # FastAPI 入口 + trace 中间件 + 启动建表
   │  ├─ config.py             # pydantic-settings，全部配置走环境变量
   │  ├─ prompts.py            # Prompt 加载
   │  ├─ api/                  # routes_chat（SSE）/ routes_admin / routes_files / routes_auth
   │  ├─ core/                 # statemachine（9 节点）/ errors（E001~E010）/ trace
   │  ├─ db/                   # models（session/message/query_record/audit_log/metric_caliber）/ session
   │  ├─ llm/                  # client（超时/JSON 容错/备用模型）/ tools
   │  └─ services/             # intent / recall / generator / validator / executor /
   │                           # answer / file_store / file_ingest / db_identity /
   │                           # session_registry / session_janitor / llm_registry / audit / persist / auth
   ├─ worker/inspection_worker.py  # 每日 06:00 巡检 + 每小时会话回收
   ├─ eval/                    # runner（分类指标）+ datasets/eval_v1_seed.jsonl
   ├─ prompts/                 # intent / clarify / sql_gen / sql_fix / answer / meta_qa / reject
   ├─ tests/                   # 5 个测试文件，29 项
   ├─ frontend/                # Vue3 + Vite
   │  └─ src/{App.vue, main.js, style.css, api/{chat,admin,files,auth}.js, components/LoginView.vue, editor/UniverSheet.vue}
   ├─ data/                    # 运行数据（上传文件、llm_apis.json），已 gitignore
   ├─ .tmp/                    # 构建/临时目录，已 gitignore
   └─ pyproject.toml / docker-compose.yml / .env.example
```

> `univer-demo/` 是独立演示项目，已从本仓库忽略。GitHub 远端：`git@github.com:gaojinx/DataPro.git`（分支 `main`）。

## 4. 已实现功能与关键设计决策

### 4.1 已实现（与 `docs/DataPro-功能清单.md` 对应）

- **取数链路**：意图识别 → 澄清 → 召回 → 生成 → 校验 → 执行 → 结果呈现；失败重试/升级；统一错误码。
- **只读执行**：sqlglot 校验（写操作/表白名单/危险函数/LIMIT）+ 只读账号 + 会话身份透传 + 超时 + 行数截断 + 日期占位符 `{date}`/`{start_date}`/`{end_date}`。
- **文件能力（F27）**：CSV/TSV/JSON/XLSX 上传 → 解析（多 sheet）→ 临时表 → 可查询；2 万行 × 50 列；列名 `c1..cn` + 原始表头注入 prompt。
- **会话隔离安全**：每会话一个 MySQL 只读账号，按表授权；共享 `ro` 的 `dp_uploads` 整库权限已回收；删除/过期 DROP 表 + REVOKE + DROP USER。
- **管理能力**：LLM API 注册表（增删改/测试/设默认/Key 脱敏），仅管理员（Token 角色校验）。
- **登录与鉴权（B12 基础版）**：独立登录页；PBKDF2 密码哈希；HMAC 签名 Token（默认 12h）；管理接口校验管理员角色；聊天/反馈/会话接口要求登录；会话按创建者归属。演示账号 `admin/admin123`、`analyst/user123`。
- **前端体验**：Codex 式侧栏（日期分组/折叠/重命名/删除确认）、无弹窗主区视图、右侧文件与搜索面板、文件来源筛选、消息复制/重生成、导出 MD/CSV、Ctrl+K、暗色主题。
- **二级设置页（F37）**：横向分组标签（数据源 / 模型与AI / 外观风格 / 权限与角色 / 账户与统计）；模型表格列为名称/Base URL/模型/API Key/操作，支持设为当前、测试连接、编辑、删除；添加/编辑为内联表单（无弹窗）。
- **文件预览（F34）**：文件面板或消息附件点击进入主区预览；表格按工作表切换（前 200 行，数据来自解析临时表），图片/PDF 内联渲染；接口 `GET /api/v1/files/{id}/preview` 与 `download?inline=1`。
- **文件在线编辑（F35/F36）**：CSV/TSV/XLSX 可在主区用 Univer 开源版编辑，保存为新版本文件（`origin=edited`，原件保留）并自动重新解析临时表；XLSX 用 SheetJS 在浏览器读写（保留数据与多工作表名，样式/公式/图表有损）；单表 ≤5000 行、总计 ≤10000 行；打开时加 5 分钟进程内锁。
- **结果呈现**：answer-first——KPI 大数字（单行聚合）→ 图表/表格 → 「依据」折叠（SQL/口径/行数/耗时）→ 回到底部按钮。
- **反馈与审计（B08/S03）**：结果事件回传 `query_id`；👍👎 与原因写 `query_record`；节点级审计异步写 `audit_log`；业务库不可用时降级跳过。
- **评测**：20 条种子集；分类指标（executable_rate / table_hit_rate / failure_breakdown）；无 Key 基线 11/20。

### 4.2 关键设计决策

1. **自研状态机**而非 LangChain，节点可替换、可观测、可测试。
2. **召回 0 命中直接拒答**（E002/E003），禁止模型自由发挥。
3. **降级不造假**：无 LLM 时仅用口径模板生成；无口径一律 escalate，不返回看似正确的错误数据。
4. **双重只读防线**：模型输出先过 sqlglot 白名单校验，再由只读账号兜底。
5. **会话级数据库身份**：有解析文件的会话用独立账号；无文件会话回退共享只读账号（共享账号已无 `dp_uploads` 权限，安全等价）。
6. **UI 约定**：功能不使用弹窗，一律主区视图；不以常驻小字解释功能，用图标/徽标/图形/tooltip 表达。
7. **长对话可扫读**：答案在上、依据在下、过程可折叠。
8. **旁路不阻塞**：审计/记录写入失败只记日志，60 秒内不再重试，不影响主链路。
9. **功能先登记再开发**：新功能先写入 `docs/DataPro-功能清单.md`（含变更登记），再实现。

## 5. 环境与启动/验证命令

本机特殊路径（Git / Python 均不在 PATH，需用全路径）：

- Python：`C:\Program Files\Python314\python.exe`
- Git：`C:\Program Files\Git\cmd\git.exe`
- `gh` CLI：未安装

```powershell
# 后端（cwd: datapro）
& "C:\Program Files\Python314\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8100

# 前端开发（cwd: datapro/frontend）
npm run dev

# 后端单测（pyproject 已固定 --basetemp=.pytest_tmp）
& "C:\Program Files\Python314\python.exe" -m pytest -q

# 前端构建（系统 Temp 无写权限，必须改 TEMP）
$env:TEMP='e:\Project\AgentProject\datapro\.tmp'; $env:TMP=$env:TEMP; npm run build

# 评测
& "C:\Program Files\Python314\python.exe" -m eval.runner eval_v1_seed --verbose

# 巡检 worker
& "C:\Program Files\Python314\python.exe" -m worker.inspection_worker

# 健康检查
curl.exe -s http://127.0.0.1:8100/health
```

数据库初始化（一次性）：

- 数仓 `dwd_shop` 与只读账号 `ro` 已就绪。
- 业务库需存在 `datapro` 库与 `datapro/pw` 账号；后端启动时 `init_db()` 自动建表（失败降级为仅日志）。
- 上传库 `dp_uploads` 首次上传时自动建库/建表。

## 6. 已知约束与坑（含本机特殊处理）

- **系统 Temp 无写权限**：pytest 用 `--basetemp=.pytest_tmp`；前端构建必须把 `TEMP/TMP` 指到 `datapro\.tmp`，否则 esbuild 失败。
- **Git/gh 不在 PATH**：用全路径调用 `git.exe`；`gh` 未安装。GitHub 已配置 SSH（`~/.ssh/id_ed25519`，账号 `gaojinx`）。
- **后端未开 `--reload`**：改后端代码后必须重启进程才生效。重启前先停旧进程，避免 8100 端口冲突。
- **浏览器自动化不可用**：TRAE Chrome 扩展连接失败（os error 10061），无法做浏览器实测；验证以 `npm run build` + 接口实测 + 源码静态检查为准。
- **PowerShell 5 限制**：不支持 `&&`、`||`、三元运算符；`Invoke-WebRequest` 在非交互模式可能因 IE 引擎失败，联调优先用 `curl.exe` 或 Python `urllib`。含中文 JSON 建议写文件后 `curl --data-binary @file`。
- **种子数据日期会漂移**：`dwd_order_detail_di` 与 `dwd_user_di` 的日期列已滚动到 T-1（当前 2026-11-13 ~ 2026-11-22）。隔天会再次偏离，需要时重跑日期对齐（或后续接入日终任务）。
- **无真实 LLM Key**：默认降级模式；评测基线 11/20。`data/llm_apis.json` 含明文 Key，已 gitignore，禁止提交。
- **前端构建包体告警**：主包约 1.1 MB（Element Plus 全量引入）；Univer 编辑器为独立异步 chunk（约 6.8 MB / gzip 1.7 MB），只在打开 CSV/TSV 编辑时加载，属预期。
- **在线编辑限制**：CSV/TSV/XLSX；单表 ≤ 5000 行、总计 ≤ 10000 行；XLSX 用 SheetJS 转换，样式/公式/图表会丢失；Univer 官方 xlsx 导入导出属 Pro，不使用；编辑锁为进程内实现，仅适合单实例部署（多实例需 Redis，见 B11）。
- **SheetJS 版本**：npm 社区版 0.18.5 存在已知安全告警，仅用于处理当前会话的用户文件；后续可评估升级到官方 CDN 版本或服务端转换。
- **登录为演示账号**：`admin/admin123`、`analyst/user123` 仅首次启动写入，生产必须改密并替换 `AUTH_SECRET`；Token 无黑名单，退出只清客户端；文件预览/下载接口尚未接入 Token。
- **上传表生命周期**：会话 TTL 默认 24h，每小时 janitor 回收账号与临时表；删除文件同步 DROP/REVOKE。
- **Python 3.14 兼容**：pymysql 需 `cryptography`（已在依赖中）。

## 7. 待办

- **P0**：真实 LLM Key 联调与回归评测（B09/E04）；元数据字典实填（D02，依赖业务方）；评测集扩充到 50 条（E03）。
- **P1**：`sql_fix` 接真实 LLM（B10）；Redis 会话上下文（B11）；B12 剩余（用户管理、权限矩阵、Token 刷新/黑名单、文件接口接入 Token）；限流 E007（B14）；列级存在性校验（A03）；向量召回 + Rerank（A05）；小基数 GROUP BY 防泄露（A07）。
- **P2**：巡检结果页（F16）；导出任务队列与审批（B13/F11）；返回层脱敏拦截（S04）；前端水印（S06）；日终种子日期自动对齐。
- **文件编辑路线（不使用 Univer Pro）**：P1 只读预览、P2 CSV/TSV 编辑、P3 XLSX 多表编辑均已完成；图片/PDF 只读预览已在 P1 覆盖。后续可选：版本链/回滚、编辑历史对比。

## 8. 禁止事项

- 禁止提交密钥、`.env`、`data/`（上传文件与 `llm_apis.json`）、`node_modules`、构建产物。
- 禁止绕过 `docs/DataPro-功能清单.md` 直接开发新功能；完成后同步状态与变更登记。
- 禁止用弹窗承载功能页；禁止用大段常驻小字解释功能。
- 禁止开放写操作、导出敏感字段（手机号/身份证/银行卡等）。
- 禁止在召回 0 命中或无口径时让模型自由生成 SQL。
- 禁止把会话数据库账号权限扩大到整库；禁止恢复共享账号对 `dp_uploads` 的整库授权。
- 禁止 force push / 改写远端历史（除非用户明确要求）。
- 禁止在本机使用 `python` 短命令（WindowsApps 别名可能指向无依赖环境），统一用全路径解释器。

## 9. 维护约定

每次改完代码，按以下顺序收尾：

1. 更新 `docs/DataPro-功能清单.md`（功能状态 + 变更登记行）。
2. 更新本文件：涉及技术栈/目录/命令/约束/待办的变化必须同步。
3. 运行验证：后端 `pytest -q`；前端改动执行 `npm run build`（带 TEMP 覆盖）。
4. Git 提交与推送仅在用户明确要求时执行。
