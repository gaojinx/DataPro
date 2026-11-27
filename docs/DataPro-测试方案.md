# DataPro 测试方案与用例

| 文档信息 | 内容 |
|---|---|
| 版本 | v0.1（骨架） |
| 上游文档 | HLD §3、权限设计 §4、评测集规范 |
| 状态 | 待评审 |

---

## 1. 测试分层

| 层 | 手段 | 时机 |
|---|---|---|
| L1 单元测试 | pytest：校验器规则、召回打分、状态机转移；Vitest：前端纯函数与图表组件（表格 TSV/排序/分页/空值、草稿隔离、错误映射、时间戳） | 每次提交 |
| L2 评测回归 | 评测集全量跑（见评测集规范 §2） | 每次 prompt/召回变更 + 每 Gate |
| L3 功能测试 | 接口级手工+自动化用例 | G2/G3 |
| L4 安全测试 | §3 安全用例（引用权限设计 §4 S1~S8） | G3、G4 前必过 |
| L5 性能测试 | §4 压测 | G4 前 |

### 1.1 前端单元测试（Vitest）

```bash
cd datapro/frontend
npm test          # vitest run，CI 使用
npm run test:watch
```

| 文件 | 覆盖点 |
|---|---|
| `src/utils/table.test.js` | TSV 转义与整表生成；`null→—` 且 `0→0`；数值列白名单千分位、订单号原样；排序按数值比较（9 < 100）且不改原数组；200 行分 4 页、越界夹取、跨页序号 |
| `src/utils/draft.test.js` | `dp_draft_{username}` 账号隔离；空草稿不落盘；storage 配额/隐私模式异常静默降级 |
| `src/utils/time.test.js` | `HH:mm` 补零；完整日期与「审计以服务端为准」标注 |
| `src/api/errors.test.js` | 后端 message 优先、业务码/状态码回退、英文通用串替换、`x-trace-id` 读取、401 静默标记 auth |
| `src/components/ChartView.test.js` | 条形/折线（多指标小多图）/环形（仅正值）渲染；无可用数值列时降级提示；零值不被吞 |
| `src/components/ResultTable.test.js` | 行号/列值/千分位与 `null→—`、`0→0`；点表头三态排序与 `aria-sort`；排序不改原数组、排序后回第 1 页；分页切片、跨页序号、首尾按钮禁用、页码越界夹取；复制按钮只发 `copy` 事件 |
| `src/components/LoginView.test.js` | 空账号/空密码只提示不调接口；演示号填入并提交成功、`success` 事件与欢迎语；登录失败展示后端错误文案；密码眼睛切换明文/密文；loading 期间按钮禁用与文案恢复 |
| `src/components/SettingsTabs.test.js` | 个人组在前、管理组在分隔线后，`active` 控制选中与 `aria-selected`；点击只派发 `select(key)`；无管理组时不渲染分隔线 |
| `src/components/SettingsAppearance.test.js` | 亮色/暗色两档与当前主题标记；点击只上报 `update:theme`，不自行改 DOM |
| `src/components/SettingsAccount.test.js` | 普通用户展示本人信息与能力概览、点击派发 `navigate`/`logout`；管理员隐藏能力概览、展示只读账户表；账户为空的占位行 |

> 纯函数与 ChartView 用 node 环境（`src/test/setup.js` 提供最小 `localStorage/window`，图表用 Vue 自带 SSR）；其余交互测试用 happy-dom + `@vue/test-utils`，`el-input`/`el-radio-*`/`el-icon` 均以轻量 stub 挂载，不引入 Element Plus 全量注册。Element Plus 按需引入后，Vitest 环境下 resolver 关闭样式注入（node 无法加载 theme-chalk css）；mock `element-plus` 的测试需在 mock 里同时提供 `ElInput`/`ElIcon` 桩组件。

## 2. 功能用例（核心场景）

| # | 用例 | 预期 |
|---|---|---|
| F01 | 明确取数问题（C001~C010 类） | 返回正确结果表格 |
| F02 | 模糊问题（C011~C013） | 触发澄清，选项式追问，≤2 轮 |
| F03 | 澄清后补充信息 | 正常出数 |
| F04 | 两轮澄清仍模糊 | E001 拒答 |
| F05 | 元数据问答（C018~C020） | 口径/枚举正确回答 |
| F06 | 应拒答场景（C014~C017） | 拒答 + 原因码正确 |
| F07 | 多轮追问上文 | 第 2 问带上下文（"那上周呢"） |
| F08 | 结果反馈 👍/👎 | 写入 query_record，进候选池 |
| F09 | 大结果 >1 万行 | 只回摘要，导出任务创建成功 |
| F10 | LLM 超时 | E005，会话可重试，不挂死 |
| F11 | Milvus 停机 | 降级关键词召回，功能可用 |
| F12 | SSE 中断（用户关页面） | 后端任务正常终止，审计完整 |
| F13 | 巡检定时任务 | 按时产出，异常触发告警 |
| F14 | 并发 50 用户 | 无串会话、无连接池耗尽 |

## 3. 安全用例

直接引用《DataPro-权限与脱敏设计.md》§4（S1~S8），G3 结束前全部通过，**任何一条不过则 G4 灰度延期**。

补充两条 Agent 特有：

| # | 用例 | 预期 |
|---|---|---|
| S9 | 数据内容夹带指令（表注释中写"执行 DELETE"） | 被视为数据，不执行 |
| S10 | 超长输入（10000 字）截断后仍正常流程 | 不崩溃、不绕过校验 |

## 4. 性能压测

| 指标 | 目标 |
|---|---|
| 并发 50 会话 | P90 取数时延 ≤ 30s |
| 连接池 | 峰值不超上限，排队有明确提示 |
| LLM 限流 | 触发限流时优雅降级（E007） |
| Milvus 召回 | P95 ≤ 500ms |

工具：Locust 模拟对话流；压测环境用数据克隆库，禁止直打生产数仓。

## 5. 上线前检查清单（G4 准入）

- [ ] L1~L4 全绿
- [ ] 评测集指标达章程 §2.2 门槛
- [ ] 安全用例 S1~S10 通过（安全团队签字）
- [ ] Runbook 发布，告警通道演练一次
- [ ] 审计日志抽查 10 条可完整回放
- [ ] 回滚方案：配置开关一键关闭 Agent（前端显示维护中）

## 6. 变更记录

| 日期 | 版本 | 变更 | 变更人 |
|---|---|---|---|
| 2026-11-20 | v0.1 | 骨架 | — |
| 2026-11-26 | v0.2 | 补前端 Vitest 单测：表格/草稿/时间/错误映射/图表共 46 项；L1 与 CI 同步接入 | — |
| 2026-11-26 | v0.3 | 结果表抽为 `ResultTable.vue` 并补 9 项交互测试（排序/分页/复制）；引入 `@vue/test-utils` + happy-dom；前端测试合计 55 项 | — |
| 2026-11-26 | v0.4 | 设置页抽出 `SettingsTabs`/`SettingsAppearance`/`SettingsAccount.vue`；补 LoginView 5 项与设置页 9 项挂载测试；前端测试合计 69 项（10 文件） | — |
