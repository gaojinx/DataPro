<script setup>
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowLeft, ArrowUp, ArrowUpBold, Bell, ChatDotSquare, CircleCheck, CircleClose, Close, CopyDocument, Cpu, DataAnalysis, DataLine, Delete, Document, Download, Edit, Expand, Fold, Grid, Histogram, InfoFilled, Key, Lock, MagicStick, Menu, Monitor, Moon, Paperclip, PieChart, Plus, Printer, QuestionFilled, Reading, Refresh, Search, Star, Setting, Sunny, SwitchButton, Timer, TrendCharts, Upload, User, WarningFilled } from '@element-plus/icons-vue'
import { fetchSessionArchive, fetchSessionMessages, sendFeedback, streamChat } from './api/chat'
import { createUser, deleteApi, deleteUser, fetchApis, fetchLlmStatus, fetchMetaTables, fetchPermissions, fetchRecords, fetchTableAccess, fetchUsers, saveApi, saveRolePermissions, saveTableAccess, setDefaultApi, testApi, updateUser, fetchRulings, saveRuling, deleteRuling, sweepSessions, fetchCacheStats, clearCache } from './api/admin'
import { deleteFile, downloadUrl, fetchFileDiff, fetchFileVersions, fetchPreview, inlineUrl, listFiles, lockFile, restoreFileVersion, unlockFile, uploadFile } from './api/files'
import { deleteDoc, deleteKnowledge, fetchDocs, fetchDocStats, fetchKnowledge,
         reindexDoc, saveKnowledge, searchDocs, uploadDoc } from './api/knowledge'
import { deleteLearnedSkill, deleteMemory, fetchLearnedSkills, fetchMemories, updateLearnedSkill, updateMemory } from './api/learning'
import { deleteSchedule, fetchSchedules, runSchedule, saveSchedule } from './api/schedules'
import { deleteSkill, fetchSkills, saveSkill } from './api/skills'
import { fetchTrace } from './api/trace'
import { fetchUsage } from './api/usage'
import { createAccessRequest, decideAccessRequest, fetchAccessCatalog, fetchAllAccessRequests, fetchMyAccessRequests } from './api/access'
import { fetchInspectionRules, fetchInspections, runInspection, saveInspectionRules } from './api/inspections'
import { approveExport, createExport, downloadExport, fetchExports, rejectExport } from './api/exports'
import { clearAuth, getUser, logout as apiLogout, onAuthExpired, refreshAuth, sessionExpired, tokenExpiresAt } from './api/auth'
import LoginView from './components/LoginView.vue'
import ChartView from './components/ChartView.vue'
import ResultTable from './components/ResultTable.vue'
import SettingsTabs from './components/SettingsTabs.vue'
import SettingsAppearance from './components/SettingsAppearance.vue'
import SettingsAccount from './components/SettingsAccount.vue'
import QueuedMessage from './components/QueuedMessage.vue'
import TrustCard from './components/TrustCard.vue'
import EmptyResultGuide from './components/EmptyResultGuide.vue'
import { buildTsv, toNumber } from './utils/table'
import { buildReportHtml } from './utils/report'
import { formatClock as fmtClock, formatDateTime as fmtDateTime } from './utils/time'
import { draftKeyFor, readDraft, writeDraft, removeDraft } from './utils/draft'

// Univer 体积较大，仅在打开编辑器时按需加载
const UniverSheet = defineAsyncComponent(() => import('./editor/UniverSheet.vue'))

// ---- 主题 ----
const theme = ref(localStorage.getItem('dp_theme') || 'light')
const applyTheme = () => document.documentElement.setAttribute('data-theme', theme.value)
const toggleTheme = () => {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  localStorage.setItem('dp_theme', theme.value)
  applyTheme()
}
// 设置页单选：直接设定主题，避免重复点击已选项时被 toggle 反转
function setTheme(t) {
  if (theme.value === t) return
  theme.value = t
  localStorage.setItem('dp_theme', t)
  applyTheme()
}

// ---- 会话管理（localStorage 持久化） ----
const STORAGE_KEY = 'dp_sessions_v1'
const sessions = ref([])            // [{id, title, ts, snapshot}]
const activeId = ref('')
const sessionId = ref('')
const sidebarOpen = ref(false)
const input = ref('')
const busy = ref(false)

// ---- X-I01 输入草稿（按账号隔离，只存文本，不存附件/结果） ----
function draftKey() { return draftKeyFor(authUser.value?.username) }
let draftTimer = 0
function saveDraftNow(v = input.value) { writeDraft(localStorage, draftKey(), v) }
function restoreDraft() {
  input.value = readDraft(localStorage, draftKey())
  nextTick(autoResize)
}
function clearDraft() {
  if (draftTimer) { clearTimeout(draftTimer); draftTimer = 0 }
  removeDraft(localStorage, draftKey())
}
watch(input, () => {
  if (draftTimer) clearTimeout(draftTimer)
  draftTimer = setTimeout(() => { saveDraftNow(); draftTimer = 0 }, 300)
})

function loadSessions() {
  try { sessions.value = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') } catch { sessions.value = [] }
}
function saveSessions() {
  // 最多保留 30 个会话；localStorage 配额溢出时逐步裁剪，避免抛错打断主流程
  for (let n = 30; n >= 1; n -= 5) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions.value.slice(0, n)))
      return
    } catch { /* 配额不足，缩小快照数量后重试 */ }
  }
}
function fmtTime(ts) {
  const d = new Date(ts)
  const now = new Date()
  if (d.toDateString() === now.toDateString()) {
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  }
  const y = new Date(now)
  y.setDate(now.getDate() - 1)
  if (d.toDateString() === y.toDateString()) return '昨天'
  return `${d.getMonth() + 1}/${d.getDate()}`
}

const messages = ref([])            // {role, text?, result?, reject?, steps?, running?, isClarify?, feedback?, stopped?}
const scrollRef = ref()
const expanded = ref({})            // 处理过程折叠
const evOpen = ref({})              // F30 依据折叠（SQL/口径/耗时）

// 侧栏折叠（Codex 式，桌面/移动统一，状态持久化）
const sidebarCollapsed = ref(localStorage.getItem('dp_sidebar') === 'collapsed')
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem('dp_sidebar', sidebarCollapsed.value ? 'collapsed' : 'open')
  sidebarOpen.value = !sidebarCollapsed.value
}

// 会话按日期分组（Codex 式）
const groupedSessions = computed(() => {
  const now = new Date()
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const bounds = [
    ['今天', startToday],
    ['昨天', startToday - 86400000],
    ['7 天内', startToday - 6 * 86400000],
  ]
  const groups = new Map([['今天', []], ['昨天', []], ['7 天内', []], ['更早', []]])
  for (const s of sessions.value) {
    const hit = bounds.find(([, t]) => s.ts >= t)
    const key = hit ? hit[0] : '更早'
    groups.get(key).push(s)
  }
  return [...groups.entries()].filter(([, v]) => v.length)
})

const NODE_LABELS = {
  INTENT: '理解意图', PARSE: '解析规格', TIME: '解析时间', CLARIFY: '澄清确认',
  RECALL: '检索数据资产', GENERATE: '生成查询', VALIDATE: '校验语句', EXECUTE: '执行查询',
  VISION: '识别图片',
  PRESENT: '整理结果', DONE: '完成',
}

// ---- 登录态（B12：Token + 角色由后端签发） ----
const authUser = ref(getUser())
const role = computed(() => authUser.value?.role === 'admin' ? 'admin' : 'user')
const roleName = computed(() => role.value === 'admin' ? '管理员' : '普通用户')
// 细粒度权限（B12）：管理员恒为全量；其余按令牌里的生效权限判断
const can = (code) => role.value === 'admin' || (authUser.value?.permissions || []).includes(code)
const displayName = computed(() => authUser.value?.display_name || authUser.value?.username || '未登录')

// ---- 前端水印（S06）：登录用户 + 当前时间，定时刷新 ----
const wmTime = ref('')
const watermarkText = computed(
  () => `${authUser.value?.username || 'datapro'} · ${wmTime.value}`)
let wmTimer = null
function tickWatermark() {
  wmTime.value = new Date().toLocaleString('zh-CN', { hour12: false })
}

function onLogin(user) {
  authUser.value = user
  // 换账号后回到对话首页，避免沿用上一个账号的高权限视图/设置分组
  activeView.value = 'chat'
  settingsTab.value = user.role === 'admin' ? 'model' : 'appearance'
  skillTab.value = 'skills'
  learningScope.value = 'mine'
  learningReady.value = false
  loadLlmStatus()
  loadSkills()
  loadLearning()
  if (user.role === 'admin') loadApis()
  loadFavorites()
  restoreDraft()               // X-I01：登录后恢复该账号上次未发送的输入
  favOpen.value = false
  loadNotices()
  if (!noticeTimer) noticeTimer = setInterval(() => loadNotices(), 60 * 1000)
  mergeServerSessions()        // F53b：登录后并入服务端会话归档（跨设备可见）
}

// 清空本地登录后的界面状态（退出登录 / 会话过期共用）
function resetClientSession() {
  controller.value?.abort()
  controller.value = null
  busy.value = false
  stopTicker()
  clearDraft()                 // X-I01：退出/会话过期清草稿，避免跨账号残留
  authUser.value = null
  sessions.value = []
  messages.value = []
  pendingFiles.value = []
  files.value = []
  previewFile.value = null
  previewData.value = null
  sessionId.value = ''
  activeId.value = ''
  queuedText.value = ''
  learnedSkills.value = []
  memories.value = []
  learningReady.value = false
  learningScope.value = 'mine'
  skillTab.value = 'skills'
  localStorage.removeItem(STORAGE_KEY)
  sidebarOpen.value = false
  noticeOpen.value = false
  notices.value = []
  stopExportPolling()
  if (noticeTimer) { clearInterval(noticeTimer); noticeTimer = 0 }
}

// 任意接口返回 401：清本地登录态并回到登录页（提示由具体请求的错误信息负责）
let offAuthExpired = null
function handleAuthExpired() {
  accountMenuOpen.value = false
  resetClientSession()
  // API 层对 401 静默，这里统一弹一次，避免并发请求弹出多条重复提示
  ElMessage.warning('登录已失效，请重新登录')
}

async function doLogout() {
  accountMenuOpen.value = false
  releaseEditLock()
  await apiLogout()
  resetClientSession()
}

const activeTitle = computed(() => {
  if (activeView.value === 'help') return '使用帮助'
  if (activeView.value === 'settings') return '设置'
  if (activeView.value === 'skills') return '技能'
  if (activeView.value === 'schedules') return '定时任务'
  if (activeView.value === 'knowledge') return '知识库'
  if (activeView.value === 'inspections') return '巡检'
  if (activeView.value === 'exports') return role.value === 'admin' ? '导出审批' : '导出记录'
  return sessions.value.find(s => s.id === activeId.value)?.title || '新对话'
})
const showHero = computed(() => !busy.value && messages.value.length === 0)

// ---- 品牌白标（S-F02）：public/branding.json 可覆盖，缺省即 DataPro ----
const branding = ref({ product_name: 'DataPro', logo_text: 'DP', version: 'v0.1', accent: '' })
async function loadBranding() {
  try {
    const base = import.meta.env.BASE_URL || '/'
    const resp = await fetch(`${base}branding.json`, { cache: 'no-cache' })
    if (!resp.ok) return
    const data = await resp.json()
    branding.value = { ...branding.value, ...data }
    if (data.accent) {
      document.documentElement.style.setProperty('--accent', data.accent)
      document.documentElement.style.setProperty('--accent-hover', data.accent)
    }
  } catch { /* 品牌配置缺失不阻塞启动 */ }
}

// ---- 新用户引导（U-F01）：非弹窗、可关闭、只出现一次 ----
const guideDismissed = ref(localStorage.getItem('dp_guide_dismissed') === '1')
const GUIDE_FORMULA = [
  { k: '指标', v: '支付订单数' },
  { k: '时间', v: '最近 7 天' },
  { k: '维度', v: '按品类' },
]
function dismissGuide() {
  guideDismissed.value = true
  localStorage.setItem('dp_guide_dismissed', '1')
}

// ---- 常用问法收藏（U-F02 本地版）：按账号存 localStorage ----
const favorites = ref([])
const favOpen = ref(false)
const favKey = () => `dp_favorites_v1_${authUser.value?.username || 'anonymous'}`
function loadFavorites() {
  try {
    const raw = JSON.parse(localStorage.getItem(favKey()) || '[]')
    favorites.value = Array.isArray(raw) ? raw.filter(x => typeof x === 'string').slice(0, 20) : []
  } catch { favorites.value = [] }
}
function persistFavorites() {
  try { localStorage.setItem(favKey(), JSON.stringify(favorites.value.slice(0, 20))) } catch { /* 存储不可用时忽略 */ }
}
function addFavorite(text) {
  const q = (text || '').trim()
  if (!q) return
  if (favorites.value.includes(q)) { ElMessage.info('已在常用问法中'); return }
  favorites.value = [q, ...favorites.value].slice(0, 20)
  persistFavorites()
  ElMessage.success('已加入常用问法')
}
function removeFavorite(text) {
  favorites.value = favorites.value.filter(x => x !== text)
  persistFavorites()
}
function toggleFav() {
  favOpen.value = !favOpen.value
  if (favOpen.value) loadFavorites()
}
// 常用问法一键重跑：回答中点则进待发送框，空闲时直接发出
function favRun(text) {
  favOpen.value = false
  if (busy.value) { input.value = text; insertQueued() }
  else send(text)
}

// ---- 追问建议（U-F07 静态版）：按本轮 spec 生成，点击仍走完整意图识别 ----
const DIM_SUGGESTIONS = [
  { keys: ['渠道', 'channel'], text: '按渠道拆一下' },
  { keys: ['品类', 'category'], text: '按品类拆一下' },
  { keys: ['区域', '省份', '城市', 'region', 'province', 'city'], text: '按区域拆一下' },
  { keys: ['商品', 'goods'], text: '按商品拆一下' },
  { keys: ['品牌', 'brand'], text: '按品牌拆一下' },
]
function suggestionsFor(msg) {
  const r = msg?.result
  if (!r || !r.rows?.length) return []
  const spec = r.spec || {}
  const dims = (spec.dimensions || []).map(x => String(x).toLowerCase())
  const out = []
  if ((spec.grain || 'total') !== 'time') out.push('按天看一下趋势')
  for (const s of DIM_SUGGESTIONS) {
    if (out.length >= 2) break
    if (!dims.some(d => s.keys.some(k => d.includes(k.toLowerCase())))) out.push(s.text)
  }
  out.push('最近 30 天是多少')
  return out.slice(0, 3)
}

// ---- 拒答下一步（U-F09）：把死胡同变成可自救路径，点击仍走完整链路 ----
function rejectAction(kind) {
  if (kind === 'range') return send('数据最新到几号')
  if (kind === 'example') return send(HERO_CARDS[0].t)
  return send('哪些问题可以问我')
}

// ---- 管理端运维处置（U-F10）：口径裁决 + 会话回收，后端接口已就绪 ----
const rulings = ref([])
const rulingForm = ref({ term: '', metric_id: '', note: '' })
const sweepResult = ref(null)
// 限定范围缓存（U-B03）：只缓存意图分类与口径问答，取数结果永不缓存
const cacheStats = ref(null)
async function loadCache() {
  try { cacheStats.value = await fetchCacheStats() } catch (e) { ElMessage.error(e.message) }
}
async function purgeCache(scope = '') {
  try {
    const r = await clearCache(scope)
    ElMessage.success(`已清空 ${r.removed} 条（${r.scope}）`)
    loadCache()
  } catch (e) { ElMessage.error(e.message) }
}
async function loadRulings() {
  try { rulings.value = await fetchRulings() } catch (e) { ElMessage.error(e.message) }
}
async function submitRuling() {
  const { term, metric_id, note } = rulingForm.value
  if (!term.trim() || !metric_id.trim()) { ElMessage.warning('请填写术语与指标 ID'); return }
  try {
    await saveRuling(term.trim(), { metric_id: metric_id.trim(), note: note.trim() })
    rulingForm.value = { term: '', metric_id: '', note: '' }
    await loadRulings()
    ElMessage.success('裁决已保存')
  } catch (e) { ElMessage.error(e.message) }
}
async function removeRuling(term) {
  try { await deleteRuling(term); await loadRulings() } catch (e) { ElMessage.error(e.message) }
}
async function runSweep() {
  try {
    sweepResult.value = await sweepSessions()
    ElMessage.success('回收完成')
  } catch (e) { ElMessage.error(e.message) }
}

// ---- 应用内帮助（U-F12 / S-F04）：静态 FAQ，不引入 md 渲染依赖 ----
const HELP_FAQ = [
  { q: '怎么问才能一次问对？', a: '按「指标 + 时间 + 维度」组织，例如「最近 7 天按品类看支付订单数」。指标尽量用口径库里的名字，时间用昨天/最近 7 天/本月这类明确说法。' },
  { q: '为什么提示需要补充信息？', a: '问题里缺少时间、指标口径或输出粒度时，系统不会猜，而是请你补充。同一个词有多种统计口径时，会列出选项让你确认。' },
  { q: '数据到几号？会不会是旧数据？', a: '直接问「数据最新到几号」。结果卡上标注「数据截至」时，表示系统已核对数仓真实分区；未标注时说明暂时无法探测，按默认窗口取数。' },
  { q: '结果能导出吗？', a: '结果下方可直接下载 CSV；超过行数上限时走「申请导出」，管理员审批后在导出页下载。' },
  { q: '为什么有些问题被拒答？', a: '系统只做只读查询，并且只回答已召回的表和口径。改写、删表、跨权限取数都会被拒绝，这是设计约束，不是故障。' },
  { q: '我的提问会被记住吗？', a: '成功的取数会沉淀为技能规范与个人记忆，可在「技能 → 学习」里查看、停用或删除；每条结果都能用溯源标记码查到当时的 SQL 与依据。' },
]

const HERO_CARDS = [
  { t: '查昨天的支付订单数', b: '口径 M003', icon: DataAnalysis },
  { t: '本周每个品类的销量', b: '分组汇总', icon: Grid },
  { t: '支付订单数的口径是什么', b: '元数据', icon: InfoFilled },
  { t: '把订单价格都改一下', b: '越界演示', icon: CircleClose, danger: true },
]
// S-F01 站内演示入口：固定走一条只读样例问法，不接站外后端
const DEMO_QUESTION = '最近 7 天按品类看支付订单数'
function startDemo() {
  if (busy.value) { ElMessage.warning('当前回答进行中，请稍后再试'); return }
  activeView.value = 'chat'
  send(DEMO_QUESTION)
}

// 智能粘底：用户上翻浏览时不强制打断
const nearBottom = () => {
  const wrap = scrollRef.value?.wrapRef
  return !wrap || wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 120
}
// F31：回到底部按钮的显隐
const atBottom = ref(true)
function onThreadScroll(payload) {
  const wrap = scrollRef.value?.wrapRef
  const top = payload?.scrollTop ?? wrap?.scrollTop ?? 0
  const sh = payload?.scrollHeight ?? wrap?.scrollHeight ?? 0
  const ch = payload?.clientHeight ?? wrap?.clientHeight ?? 0
  atBottom.value = sh - top - ch < 140
}
let lastAutoScroll = 0
const scrollToBottom = async (force = false) => {
  await nextTick()
  // 流式事件高频触发时做节流，避免反复重排造成的界面闪烁
  const now = performance.now()
  if (!force && now - lastAutoScroll < 120) return
  if (!force && !nearBottom()) return
  lastAutoScroll = now
  if (scrollRef.value) scrollRef.value.setScrollTop(999999)
  atBottom.value = true
}

// Token 自动续期（B12）：剩余不足 2 小时时用 /auth/refresh 换新令牌
let refreshTimer = null
async function ensureFreshToken() {
  if (!authUser.value) return
  const exp = tokenExpiresAt()
  if (!exp) { sessionExpired(); return }        // 无 Token / 无法解析：直接回登录页
  const left = exp - Date.now()
  if (left <= 0) { sessionExpired(); return }   // 已过期：回登录页，不再静默停留在主界面
  if (left < 2 * 3600 * 1000) {
    if (await refreshAuth()) authUser.value = getUser()
    else if (!getUser()) authUser.value = null
  }
}

onMounted(() => {
  applyTheme()
  loadBranding()
  loadSessions()
  loadFavorites()
  // 本地登录态已过期（或缺少 Token）：直接回登录页，避免打开页面先弹「登录已过期」
  if (authUser.value && tokenExpiresAt() <= Date.now()) {
    clearAuth()
    authUser.value = null
  }
  if (authUser.value) {
    restoreDraft()               // X-I01：刷新后恢复未发送的输入草稿
    loadLlmStatus(); loadSkills(); loadLearning()   // 顶栏模型 chip / 首页技能卡 / 学习基线
    loadNotices()
    noticeTimer = setInterval(() => loadNotices(), 60 * 1000)
    mergeServerSessions()        // F53b：跨设备会话归档并入侧栏
  }
  offAuthExpired = onAuthExpired(handleAuthExpired)
  tickWatermark()
  wmTimer = setInterval(tickWatermark, 60 * 1000)
  window.addEventListener('keydown', onGlobalKey)
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.account-wrap')) accountMenuOpen.value = false
    if (!e.target.closest('.fav-wrap')) favOpen.value = false
    if (!e.target.closest('.notice-wrap')) noticeOpen.value = false
  })
  refreshTimer = setInterval(ensureFreshToken, 10 * 60 * 1000)
  ensureFreshToken()
})

onUnmounted(() => {
  stopTicker()                 // X-L02：卸载时清理计时器
  if (refreshTimer) clearInterval(refreshTimer)
  if (wmTimer) clearInterval(wmTimer)
  stopExportPolling()
  if (noticeTimer) clearInterval(noticeTimer)
  if (offAuthExpired) offAuthExpired()
})

function newChat() {
  if (busy.value) { ElMessage.warning('当前回答进行中，请先停止再新建对话'); return }
  if (editing.value) { ElMessage.warning('文件编辑中，请先保存为新版本或取消'); return }
  snapshotSession()
  releaseEditLock()
  previewFile.value = null
  previewData.value = null
  // 切走时清空上一会话的临时交互态，避免残留面板/表单串到新会话
  exportReq.value = null
  reasonFor.value = null
  traceHit.value = null
  searchQ.value = ''
  flashIdx.value = -1
  expanded.value = {}
  evOpen.value = {}
  // 前端先生成会话 id，保证上传文件与会话一一对应（后端复用该 id）
  const id = 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
  sessionId.value = id
  activeId.value = id
  activeView.value = 'chat'
  messages.value = []
  pendingFiles.value = []
  files.value = []
  queuedText.value = ''
  sidebarOpen.value = false
}

function snapshotSession() {
  if (!sessionId.value || !messages.value.length) return
  const snap = JSON.parse(JSON.stringify(messages.value))
  const idx = sessions.value.findIndex(s => s.id === sessionId.value)
  if (idx >= 0) { sessions.value[idx].snapshot = snap; sessions.value[idx].ts = Date.now() }
  else sessions.value.unshift({
    id: sessionId.value,
    title: messages.value.find(m => m.role === 'user')?.text?.slice(0, 24) || '新对话',
    ts: Date.now(),
    snapshot: snap,
  })
  saveSessions()
}

function switchSession(id) {
  if (busy.value) { ElMessage.warning('当前回答进行中，请先停止再切换会话'); return }
  if (editing.value) { ElMessage.warning('文件编辑中，请先保存为新版本或取消'); return }
  snapshotSession()
  releaseEditLock()
  previewFile.value = null
  previewData.value = null
  exportReq.value = null
  reasonFor.value = null
  traceHit.value = null
  searchQ.value = ''
  flashIdx.value = -1
  expanded.value = {}
  evOpen.value = {}
  activeId.value = id
  sessionId.value = id
  activeView.value = 'chat'
  const s = sessions.value.find(x => x.id === id)
  messages.value = s ? JSON.parse(JSON.stringify(s.snapshot)) : []
  pendingFiles.value = []
  queuedText.value = ''        // 切换会话不把上一会话的待发送内容带过来
  sidebarOpen.value = false
  if (panelOpen.value) loadFiles()
  // 本地快照缺失（清缓存/换设备）时从服务端恢复消息流，附件引用可点击预览（F53）
  if (!messages.value.length) restoreHistoryFromServer(id)
  scrollToBottom()
}

// ---- F54 从终态载荷还原步骤时间轴：与 handleEvent('node') 的构建规则一致，
// retry>0 表示对上一步的第 N 次修正（覆盖而非追加） ----
function stepsFromPayload(list) {
  const steps = []
  for (const s of list || []) {
    const label = NODE_LABELS[s.node] || s.node
    if (s.retry > 0) steps[steps.length - 1] = `${label}（第${s.retry + 1}次修正）`
    else steps.push(label)
  }
  return steps
}

// ---- F53 服务端消息历史恢复：localStorage 快照是富状态（步骤/结果），优先本地；
// 快照为空才拉服务端。终态载荷（0019 payload）可还原富结果：表格/KPI/澄清卡片 ----
async function restoreHistoryFromServer(id) {
  try {
    const rows = await fetchSessionMessages(id)
    if (!rows.length) return
    messages.value = rows.map(r => {
      const ts = Date.parse(r.created_at) || Date.now()
      if (r.role === 'user') {
        return { role: 'user', text: r.content, attachments: r.attachments || [], ts, restored: true }
      }
      const p = r.payload
      const steps = stepsFromPayload(p?.steps)
      const base = { kind: r.kind, steps, ts, restored: true }
      if (steps.length) base.elapsedMs = p?.elapsed_ms || undefined
      if (p?.type === 'result') {
        // 富还原：与 handleEvent('result') 同构，含步骤时间轴与耗时（F54）
        return { role: 'assistant', result: p, text: p.summary || r.content,
                 estScanRows: p.est_scan_rows ?? null, queryId: p.query_id,
                 ...base }
      }
      if (p?.type === 'clarify') {
        return { role: 'assistant', isClarify: true, text: p.question || r.content,
                 clarify: { kind: p.kind || '', options: p.options || [] }, ...base }
      }
      if (p?.type === 'reject' || p?.type === 'escalate' || p?.type === 'error') {
        return { role: 'assistant',
                 reject: p.type === 'escalate'
                   ? { reason_code: p.reason === 'SCAN_TOO_LARGE' ? 'E009' : 'E010',
                       message: p.message || '本次请求已转人工处理' }
                   : { reason_code: p.reason_code || 'E010', message: p.message || r.content },
                 ...base }
      }
      return { role: 'assistant', text: r.content, ...base }
    })
    // 本地没有这个会话（在其它设备发起）：补进侧栏列表，后续可继续对话
    if (!sessions.value.some(s => s.id === id)) {
      const first = messages.value.find(m => m.role === 'user')
      sessions.value.unshift({
        id, title: first?.text?.slice(0, 24) || '历史会话',
        ts: Date.now(), snapshot: JSON.parse(JSON.stringify(messages.value)),
      })
      saveSessions()
    }
    await scrollToBottom(true)
  } catch (e) {
    ElMessage.warning(e.message || '历史消息读取失败')
  }
}

// ---- 服务端会话归档（F53b）：本地快照是单设备的，归档让历史会话跨设备可见 ----
async function mergeServerSessions() {
  if (!authUser.value) return
  try {
    const list = await fetchSessionArchive()
    const known = new Set(sessions.value.map(s => s.id))
    const fresh = list.filter(s => !known.has(s.session_id)).map(s => ({
      id: s.session_id, title: s.title,
      ts: Date.parse(s.last_at) || Date.now(),
      snapshot: [],                                   // 空 ⇒ 点击时走服务端恢复
      server: true,
    }))
    if (!fresh.length) return
    // 插在本地会话之后：saveSessions 截前 30 个时优先保住本地富快照
    const localCount = sessions.value.length
    sessions.value.splice(localCount, 0, ...fresh)
    saveSessions()
  } catch { /* 归档是锦上添花，失败静默 */ }
}

function deleteSession(id, e) {
  e?.stopPropagation()
  const wasActive = id === activeId.value
  if (wasActive && busy.value) {
    ElMessage.warning('当前回答进行中，请先停止再删除会话')
    return
  }
  if (wasActive && editing.value) {
    ElMessage.warning('文件编辑中，请先保存为新版本或取消')
    return
  }
  // 两步确认，避免误删（3 秒后复位）
  if (confirmDelId.value !== id) {
    confirmDelId.value = id
    setTimeout(() => { if (confirmDelId.value === id) confirmDelId.value = '' }, 3000)
    return
  }
  confirmDelId.value = ''
  sessions.value = sessions.value.filter(s => s.id !== id)
  saveSessions()
  if (wasActive) {
    sessionId.value = ''   // 避免 newChat 的 snapshotSession 把已删会话写回
    newChat()
  }
  ElMessage.success('会话已删除')
}

// ---- F12 会话重命名（内联编辑，无弹窗） ----
const renamingId = ref('')
const renameText = ref('')
const confirmDelId = ref('')
const pendingDelKey = ref('')       // 通用内联两步删除：列表项 key，3 秒复位

// 返回 true 表示本次点击只是进入确认态，调用方应直接 return
function needConfirm(key) {
  if (pendingDelKey.value === key) { pendingDelKey.value = ''; return false }
  pendingDelKey.value = key
  setTimeout(() => { if (pendingDelKey.value === key) pendingDelKey.value = '' }, 3000)
  return true
}

function startRename(s, e) {
  e?.stopPropagation()
  renamingId.value = s.id
  renameText.value = s.title
  nextTick(() => document.querySelector('.rename-input')?.focus())
}
function commitRename() {
  const id = renamingId.value
  if (!id) return
  const s = sessions.value.find(x => x.id === id)
  const t = renameText.value.trim()
  if (s && t) { s.title = t.slice(0, 40); saveSessions() }
  renamingId.value = ''
}
function cancelRename() { renamingId.value = '' }

// ---- 主流程（支持中断 + 运行中插入排队） ----
const controller = ref(null)
const queuedText = ref('')

// 运行中「插入」：本轮结束后自动发送
function insertQueued() {
  const t = input.value.trim()
  if (!t) return
  const replaced = !!queuedText.value
  queuedText.value = t
  input.value = ''
  clearDraft()                 // X-I01：插入后草稿同样清空
  autoResize()
  ElMessage.success(replaced ? '已替换待发送内容' : '已加入待发送，本轮结束后自动发出')
}

// 待发送小框：取回输入框，让用户继续编辑或手动发送
function takeQueued() {
  const t = queuedText.value.trim()
  if (!t) return
  queuedText.value = ''
  input.value = t
  saveDraftNow(t)              // X-I01：取回后恢复草稿，刷新不丢
  nextTick(() => { autoResize(); taRef.value?.focus() })
}
function removeQueued() {
  queuedText.value = ''
  ElMessage.info('已删除待发送内容')
}

async function send(preset) {
  const content = (preset ?? input.value).trim()
  if (!content || busy.value) return
  const atts = pendingFiles.value.splice(0)
  input.value = ''
  clearDraft()                 // X-I01：发送成功后不再保留草稿（预设问法同样清理）
  busy.value = true
  startTicker()                // X-L02：全局唯一秒级 tick，仅 busy 期间运行
  messages.value.push({ role: 'user', text: content, attachments: atts, ts: Date.now() })

  const msg = { role: 'assistant', steps: [], running: true, asked: content,
                startedAt: Date.now(), ts: Date.now() }
  messages.value.push(msg)
  // 关键：取回响应式代理引用，直接用 msg 修改属性会绕过 Proxy 导致 DOM 不更新
  const tracked = messages.value[messages.value.length - 1]
  const learnBefore = learnSnapshot()      // A11：本轮结束后据此判断是否学到新东西
  expanded.value[messages.value.length - 1] = false
  evOpen.value[messages.value.length - 1] = false
  await scrollToBottom(true)

  // 附件随提问带给后端（当前仅传文件名作上下文提示，内容解析见后续功能）
  const payload = atts.length
    ? `${content}\n\n[本会话附件] ${atts.map(a => a.name).join('、')}`
    : content

  controller.value = new AbortController()
  const t0 = performance.now()
  try {
    await streamChat({
      sessionId: sessionId.value,
      message: payload,
      // 附件引用快照随请求落库：历史消息恢复后文件仍可点击预览
      attachments: atts.map(a => ({ file_id: a.file_id, name: a.name, ext: a.ext,
                                    size: a.size, origin: a.origin })),
      signal: controller.value.signal,
      onEvent: (ev) => handleEvent(ev, tracked, t0),
    })
  } catch (err) {
    if (err.name === 'AbortError') tracked.stopped = true
    else tracked.error = err.message || '网络异常，请重试'
  } finally {
    tracked.running = false
    busy.value = false
    stopTicker()                 // X-L02：卸载/停止后不留定时器
    controller.value = null
    autoResize()
    snapshotSession()
    if (panelOpen.value && panelTab.value === 'files') loadFiles()
    await scrollToBottom()
    // 本轮正常完成时探测自动学习结果，命中则在回答下方提示
    if (!tracked.error && !tracked.reject && !tracked.stopped && (tracked.text || tracked.result)) {
      probeLearned(tracked, learnBefore)
    }
    // 运行中插入的内容：本轮结束后自动发出
    if (queuedText.value) {
      const next = queuedText.value
      queuedText.value = ''
      send(next)
    }
  }
}

function stop() {
  queuedText.value = ''
  controller.value?.abort()
}

function handleEvent(ev, msg, t0 = 0) {
  switch (ev.type) {
    case 'node': {
      if (ev.node === 'START') {
        sessionId.value = ev.session_id
        if (!activeId.value) activeId.value = ev.session_id
        return
      }
      if (ev.node === 'DONE') return
      const label = NODE_LABELS[ev.node] || ev.node
      if (ev.retry > 0) msg.steps[msg.steps.length - 1] = `${label}（第${ev.retry + 1}次修正）`
      else msg.steps.push(label)
      break
    }
    case 'clarify':
      msg.isClarify = true
      msg.text = ev.question
      // U-F03：后端已下发口径选项与定义摘要，前端只呈现、不回灌摘要
      msg.clarify = { kind: ev.kind || '', options: ev.options || [] }
      break
    case 'result':
      msg.result = ev
      msg.text = ev.summary
      msg.estScanRows = ev.est_scan_rows ?? null   // X-L03：复用后端已算出的预估，不另发 EXPLAIN
      msg.queryId = ev.query_id        // B08：反馈回写 query_record
      if (t0) msg.elapsedMs = Math.round(performance.now() - t0)   // F31 结果耗时
      break
    case 'reject':
      msg.reject = ev
      break
    case 'escalate':
      msg.reject = {
        reason_code: ev.reason === 'SCAN_TOO_LARGE' ? 'E009' : 'E010',
        message: ev.message || '本次请求已转人工处理，数据团队会跟进。',
      }
      break
    case 'error':
      if (ev.auth) {          // Token 失效：直接回登录页，不在对话里抛错误
        authUser.value = null
        messages.value = []
        return
      }
      msg.reject = { reason_code: ev.reason_code || 'E010', message: ev.message || '服务异常' }
      if (ev.session_id) sessionId.value = ev.session_id
      break
  }
  scrollToBottom()
}

// ---- 反馈（👎 需选原因，对应章程反馈闭环） ----
const REASONS = ['数据不对', '口径不对', '没听懂我的问题', '结果不完整']
const reasonFor = ref(null)   // 需要选原因的 msg 引用

// X-C02 反馈微动效：160ms 轻微回弹，无彩色阴影/粒子，尊重减少动效偏好
function feedbackPulse(e) {
  const el = e?.currentTarget
  if (!el || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  el.classList.remove('pop')
  void el.offsetWidth
  el.classList.add('pop')
  setTimeout(() => el.classList.remove('pop'), 180)
}
function clickFeedback(msg, rating, e) {
  feedbackPulse(e)
  if (rating === 'up') {
    msg.feedback = 'up'
    sendFeedback(msg.queryId || 'latest', 'up').then(ok => ok && ElMessage.success('感谢反馈，已记录'))
  } else {
    if (msg.feedback === 'down') { msg.feedback = null; reasonFor.value = null; return }
    reasonFor.value = msg      // 展开"选择原因"面板
  }
}

async function submitReason(reason) {
  const msg = reasonFor.value
  msg.feedback = 'down'
  reasonFor.value = null
  const ok = await sendFeedback(msg.queryId || 'latest', 'down', reason)
  if (ok) ElMessage.success('已记录，会有人跟进核实')
}

// ---- 一键分享报告（F71）：问题+可信信息+结果+SQL 打包成自包含 HTML，可打印/另存 PDF ----
function shareReport(msg) {
  try {
    const html = buildReportHtml(msg, { branding: branding.value, traceCode: traceCode(msg) })
    const url = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }))
    const win = window.open(url, '_blank')
    if (!win) { URL.revokeObjectURL(url); ElMessage.error('弹窗被拦截，请允许本站点打开新窗口'); return }
    ElMessage.success('已生成报告：可打印或另存为 PDF 后分享')
    setTimeout(() => URL.revokeObjectURL(url), 60000)
  } catch { ElMessage.error('生成报告失败，请重试') }
}

// ---- 导出 CSV（F11 简化版：本地下载 + 登记为会话生成文件） ----
async function exportCSV(msg) {
  const { columns, rows } = msg.result
  const esc = (v) => `"${String(v ?? '').replaceAll('"', '""')}"`
  const csv = '\ufeff' + [
    columns.map(esc).join(','),
    ...rows.map((r) => columns.map((c) => esc(r[c])).join(',')),
  ].join('\n')
  const filename = `datapro_${Date.now()}.csv`
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const a = Object.assign(document.createElement('a'), { href: url, download: filename })
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('已导出 CSV')
  // 后台登记到会话文件面板（失败不打断导出）
  try {
    await uploadFile({ sessionId: sessionId.value || 'default',
                       file: new File([csv], filename, { type: 'text/csv' }), origin: 'generated' })
    await loadFiles()
  } catch { /* 静默 */ }
}

// ---- 右侧面板（F22-F24）：文件 + 会话检索 ----
const panelOpen = ref(localStorage.getItem('dp_panel') === 'open')
const panelTab = ref('files')       // files | search
const files = ref([])
const uploading = ref(false)
const pendingFiles = ref([])        // 待随消息发送的附件芯片
const searchQ = ref('')
const traceHit = ref(null)          // 标记码溯源结果
const flashIdx = ref(-1)
const fileInput = ref()
const dragOver = ref(false)
const confirmFileId = ref('')       // 文件删除两步确认（3 秒复位）

// F32 文件面板：来源筛选 + 文件名搜索
const fileQ = ref('')
const fileScope = ref('all')        // all | upload | generated
const fileScopes = computed(() => {
  const gen = files.value.filter(f => f.origin === 'generated').length
  return [
    { k: 'all', label: '全部', n: files.value.length },
    { k: 'upload', label: '上传', n: files.value.length - gen },
    { k: 'generated', label: '生成', n: gen },
  ]
})
const shownFiles = computed(() => {
  const q = fileQ.value.trim().toLowerCase()
  return files.value.filter(f => {
    if (fileScope.value === 'upload' && f.origin === 'generated') return false
    if (fileScope.value === 'generated' && f.origin !== 'generated') return false
    return !q || (f.name || '').toLowerCase().includes(q)
  })
})

// ---- F34 文件预览（表格 / 图片 / PDF） ----
const previewFile = ref(null)
const previewData = ref(null)
const previewLoading = ref(false)
const previewSheet = ref(0)

// ---- 文件版本链与回滚（F49）----
const versions = ref([])
const versionsOpen = ref(false)
const versionsLoading = ref(false)

async function loadVersions() {
  if (!previewFile.value?.file_id) return
  versionsLoading.value = true
  try { versions.value = await fetchFileVersions(previewFile.value.file_id) }
  catch (e) { ElMessage.error(e.message) }
  finally { versionsLoading.value = false }
}
async function toggleVersions() {
  versionsOpen.value = !versionsOpen.value
  if (versionsOpen.value && !versions.value.length) await loadVersions()
}
async function restoreVersion(v) {
  try {
    const restored = await restoreFileVersion(v.file_id)
    await loadFiles()
    await openPreview(restored)
    ElMessage.success(`已从 V${v.version} 恢复为新版本`)
  } catch (e) { ElMessage.error(e.message) }
}

// ---- 版本对比（F49）：当前版本 vs 历史版本的单元格级 diff ----
const diffTarget = ref(null)
const diffData = ref(null)
const diffLoading = ref(false)

async function compareVersion(v) {
  if (!previewFile.value?.file_id) return
  diffTarget.value = v
  diffLoading.value = true
  diffData.value = null
  try {
    diffData.value = await fetchFileDiff(previewFile.value.file_id, v.file_id)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    diffLoading.value = false
  }
}
const DIFF_STATUS = { same: '无变化', modified: '有修改', added: '新增表', removed: '删除表' }

async function openPreview(f) {
  if (!f?.file_id) return
  if (editing.value) { ElMessage.warning('文件编辑中，请先保存为新版本或取消'); return }
  releaseEditLock()
  previewFile.value = f
  previewData.value = null
  previewSheet.value = 0
  versions.value = []
  versionsOpen.value = false
  diffTarget.value = null
  diffData.value = null
  activeView.value = 'file'
  previewLoading.value = true
  try { previewData.value = await fetchPreview(f.file_id) }
  catch (e) { ElMessage.error(e.message) }
  finally { previewLoading.value = false }
}
function closePreview() {
  releaseEditLock()
  previewFile.value = null
  previewData.value = null
  activeView.value = 'chat'
}

// ---- 侧边栏内文件预览（F52）：对话中点附件不打断对话，右侧栏内嵌紧凑预览 ----
const panelPreview = ref(null)
const panelPreviewData = ref(null)
const panelPreviewLoading = ref(false)
const panelPreviewSheet = ref(0)
const panelPreviewKind = computed(() => {
  const ext = (panelPreview.value?.ext || '').toLowerCase()
  if (['.png', '.jpg', '.jpeg', '.gif', '.webp'].includes(ext)) return 'image'
  if (ext === '.pdf') return 'pdf'
  if (panelPreviewData.value?.sheets?.length) return 'table'
  return 'none'
})
const activePanelSheet = computed(
  () => panelPreviewData.value?.sheets?.[panelPreviewSheet.value] || null)

async function openPanelPreview(f) {
  if (!f?.file_id) return
  panelPreview.value = f
  panelPreviewData.value = null
  panelPreviewSheet.value = 0
  panelPreviewLoading.value = true
  panelOpen.value = true                       // 侧栏没开时自动展开
  panelTab.value = 'preview'
  localStorage.setItem('dp_panel', 'open')
  try { panelPreviewData.value = await fetchPreview(f.file_id) }
  catch (e) {
    // 历史引用的文件可能已被删除：给可读的提示而不是裸 404
    ElMessage.error(/不存在|已删除|404/.test(e.message || '') ? '该文件已被删除，无法预览' : e.message)
  }
  finally { panelPreviewLoading.value = false }
}

function closePanelPreview() {
  panelPreview.value = null
  panelPreviewData.value = null
  panelTab.value = 'files'
}
const previewKind = computed(() => {
  const ext = (previewFile.value?.ext || '').toLowerCase()
  if (['.png', '.jpg', '.jpeg', '.gif', '.webp'].includes(ext)) return 'image'
  if (ext === '.pdf') return 'pdf'
  if (previewData.value?.sheets?.length) return 'table'
  return 'none'
})
const activePreviewSheet = computed(
  () => previewData.value?.sheets?.[previewSheet.value] || null)

// ---- 文件在线编辑（CSV/TSV/XLSX；Univer 开源版 + SheetJS；保存为新版本） ----
const EDITABLE_EXTS = ['.csv', '.tsv', '.xlsx']
const EDIT_MAX_ROWS_PER_SHEET = 5000
const EDIT_MAX_ROWS_TOTAL = 10000
const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const editing = ref(false)
const editorReady = ref(false)
const editorSheets = ref(null)
const editorRef = ref()
const saving = ref(false)
const canEdit = computed(
  () => can('files.upload')
    && EDITABLE_EXTS.includes((previewFile.value?.ext || '').toLowerCase())
    && previewKind.value === 'table')

const editorProps = computed(() => (editorSheets.value ? { sheets: editorSheets.value } : {}))

const lockOwner = () => sessionId.value || 'default'

async function startEdit() {
  const sheets = previewData.value?.sheets || []
  if (!sheets.length) return
  const oversized = sheets.find(s => (s.row_count || 0) > EDIT_MAX_ROWS_PER_SHEET)
  if (oversized) {
    ElMessage.warning(`工作表「${oversized.sheet || '未命名'}」共 ${oversized.row_count} 行，超过单表 ${EDIT_MAX_ROWS_PER_SHEET} 行上限，请下载后修改再上传`)
    return
  }
  const total = sheets.reduce((n, s) => n + (s.row_count || s.rows.length), 0)
  if (total > EDIT_MAX_ROWS_TOTAL) {
    ElMessage.warning(`文件共 ${total} 行，超过 ${EDIT_MAX_ROWS_TOTAL} 行上限，请下载后修改再上传`)
    return
  }
  try {
    const full = await fetchPreview(previewFile.value.file_id, EDIT_MAX_ROWS_PER_SHEET)
    await lockFile(previewFile.value.file_id, lockOwner())
    editorSheets.value = (full.sheets || []).map((s, i) => ({
      name: s.sheet || `工作表${i + 1}`,
      labels: s.labels,
      columns: s.columns,
      rows: s.rows,
    }))
    editorReady.value = false
    editing.value = true
    // 编辑期间收起版本/对比面板，避免误触切换文件导致未保存内容丢失
    versionsOpen.value = false
    diffData.value = null
    diffTarget.value = null
  } catch (e) {
    ElMessage.warning(e.message || '无法打开编辑器')
  }
}

function releaseEditLock() {
  if (!editing.value || !previewFile.value) return
  unlockFile(previewFile.value.file_id, lockOwner()).catch(() => {})
  editing.value = false
  editorSheets.value = null
}

async function cancelEdit() {
  releaseEditLock()
}

function toDelimited(rows, delim) {
  const cell = (v) => {
    const s = v == null ? '' : String(v)
    return /[",\n\r\t]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s
  }
  return rows.map(r => r.map(cell).join(delim)).join('\n')
}

function trimRows(values) {
  const rows = (values || []).map(r => r.map(v => (v == null ? '' : v)))
  while (rows.length && rows[rows.length - 1].every(v => v === '')) rows.pop()
  if (!rows.length) return []
  const width = Math.max(...rows.map(r => r.length))
  return rows.map(r => [...r, ...Array(width - r.length).fill('')])
}

function cleanSheetName(raw, used, index) {
  const name = String(raw || '').replace(/[\[\]:*?\/\\]/g, '_').trim().slice(0, 31) || `Sheet${index + 1}`
  let candidate = name
  let n = 2
  while (used.has(candidate)) candidate = `${name}(${n++})`.slice(0, 31)
  used.add(candidate)
  return candidate
}

async function buildXlsxFile(sheets, filename) {
  const XLSX = await import('xlsx')
  const book = XLSX.utils.book_new()
  const used = new Set()
  sheets.forEach((s, i) => {
    XLSX.utils.book_append_sheet(
      book, XLSX.utils.aoa_to_sheet(s.values), cleanSheetName(s.name, used, i))
  })
  const buf = XLSX.write(book, { bookType: 'xlsx', type: 'array' })
  return new File([buf], filename, { type: XLSX_MIME })
}

async function saveEdit() {
  if (saving.value) return
  const extracted = editorRef.value?.extract() || []
  const sheets = extracted
    .map((s, i) => ({ name: s.name || `工作表${i + 1}`, values: trimRows(s.values) }))
    .filter(s => s.values.length)
  if (!sheets.length) {
    ElMessage.warning('内容为空，未保存')
    return
  }
  // 编辑过程中可能新增行：保存前再校验一次上限，避免生成超限文件
  const oversized = sheets.find(s => s.values.length > EDIT_MAX_ROWS_PER_SHEET)
  if (oversized) {
    ElMessage.warning(`工作表「${oversized.name}」共 ${oversized.values.length} 行，超过单表 ${EDIT_MAX_ROWS_PER_SHEET} 行上限`)
    return
  }
  const totalRows = sheets.reduce((n, s) => n + s.values.length, 0)
  if (totalRows > EDIT_MAX_ROWS_TOTAL) {
    ElMessage.warning(`文件共 ${totalRows} 行，超过 ${EDIT_MAX_ROWS_TOTAL} 行上限`)
    return
  }
  // 扩展名统一小写：canEdit 按小写判断，保存分支也必须一致，否则 .XLSX 会被当成 CSV 写坏
  const ext = (previewFile.value.ext || '').toLowerCase()
  const base = previewFile.value.name.replace(/\.(csv|tsv|xlsx)$/i, '')
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-:T]/g, '')
  const name = `${base}_edited_${stamp}${ext}`
  saving.value = true
  try {
    let file
    if (ext === '.xlsx') {
      file = await buildXlsxFile(sheets, name)
    } else {
      const text = toDelimited(sheets[0].values, ext === '.tsv' ? '\t' : ',')
      file = new File([ext === '.csv' ? '\ufeff' + text : text], name,
        { type: ext === '.tsv' ? 'text/tab-separated-values' : 'text/csv' })
    }
    const meta = await uploadFile({
      sessionId: lockOwner(), file, origin: 'edited',
      rootId: previewFile.value.root_id || previewFile.value.file_id,
    })
    unlockFile(previewFile.value.file_id, lockOwner()).catch(() => {})
    editing.value = false
    editorSheets.value = null
    await loadFiles()
    await openPreview(meta)
    ElMessage.success('已保存为新版本')
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

function togglePanel(tab) {
  // 面板只在对话视图渲染：在其它视图点击顶栏入口时先回到对话
  if (activeView.value !== 'chat') {
    if (editing.value) { ElMessage.warning('文件编辑中，请先保存为新版本或取消'); return }
    activeView.value = 'chat'
  }
  if (tab) panelTab.value = tab
  panelOpen.value = !panelOpen.value
  localStorage.setItem('dp_panel', panelOpen.value ? 'open' : 'closed')
  if (panelOpen.value && panelTab.value === 'files') loadFiles()
}

async function loadFiles() {
  try { files.value = await listFiles(sessionId.value || 'default') }
  catch { files.value = [] }
}

function pickFile() { fileInput.value?.click() }

async function uploadPicked(fileList) {
  if (!can('files.upload')) { ElMessage.warning('当前账号无文件上传权限'); return }
  const items = [...(fileList || [])]
  if (!items.length) return
  uploading.value = true
  try {
    const metas = []
    for (const f of items) {
      metas.push(await uploadFile({ sessionId: sessionId.value || 'default', file: f }))
    }
    pendingFiles.value.push(...metas)
    const parsed = metas.filter(m => m.parsed)
    const failed = metas.filter(m => m.parse_error)
    const flagged = metas.filter(m => m.secret_hits?.length)
    if (parsed.length) {
      const first = parsed[0].parsed
      const sheets = first.tables?.length || 1
      ElMessage.success(`已上传并解析 ${parsed.length} 个文件` +
        (parsed.length === 1
          ? `（${first.row_count} 行 · ${sheets > 1 ? `${sheets} 个工作表` : `${first.col_count} 列`}）`
          : ''))
    } else {
      ElMessage.success(`已上传 ${items.length} 个文件`)
    }
    if (flagged.length) {
      const rules = flagged[0].secret_hits.map(h => h.rule).join('、')
      ElMessage({
        type: 'warning', duration: 8000, showClose: true,
        message: `「${flagged[0].name}」内容疑似包含密钥/凭据（${rules}）。` +
          '建议删除该文件并轮换对应密钥。',
      })
    }
    if (failed.length) ElMessage.warning(`未解析：${failed[0].parse_error}`)
    panelOpen.value = true
    panelTab.value = 'files'
    await loadFiles()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    uploading.value = false
  }
}

function onFileChange(e) {
  uploadPicked(e.target.files)
  e.target.value = ''
}

function onDrop(e) {
  uploadPicked(e.dataTransfer?.files)
}

function dropPending(idx) { pendingFiles.value.splice(idx, 1) }

async function removeFile(f) {
  if (!can('files.delete')) { ElMessage.warning('当前账号无文件删除权限'); return }
  // 两步确认，避免误删（3 秒后复位）
  if (confirmFileId.value !== f.file_id) {
    confirmFileId.value = f.file_id
    setTimeout(() => { if (confirmFileId.value === f.file_id) confirmFileId.value = '' }, 3000)
    return
  }
  confirmFileId.value = ''
  try {
    await deleteFile(f.file_id)
    await loadFiles()
    // 删除的是当前预览文件：退回对话，避免主区继续展示已删除内容
    if (previewFile.value?.file_id === f.file_id) closePreview()
  } catch (e) { ElMessage.error(e.message) }
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// ---- 图表可视化（F13）：自动识别标签列 + 数值列，纯 CSS 条形图 ----
const toNum = toNumber

function chartOf(msg) {
  const r = msg.result
  if (!r?.rows?.length || r.rows.length > 20 || r.rows.length < 2) return null
  const cols = r.columns || []
  const numeric = cols.filter(c => {
    const vals = r.rows.map(row => toNum(row[c]))
    return vals.every(v => v !== null) && new Set(vals).size > 1
  })
  // 值取最后一个数值列（常见"维度在前、指标在后"），标签优先第一个非数值列
  const valueCol = numeric[numeric.length - 1]
  if (!valueCol) return null
  const labelCol = cols.find(c => c !== valueCol && r.rows.every(row => toNum(row[c]) === null))
    || cols.find(c => c !== valueCol)
  if (!labelCol) return null
  const items = r.rows.map(row => ({
    label: String(row[labelCol] ?? '—'),
    value: toNum(row[valueCol]) ?? 0,
  }))
  const max = Math.max(...items.map(i => Math.abs(i.value)))
  if (!max) return null
  return {
    labelCol, valueCol,
    items: items.map(i => ({ ...i, pct: Math.max(2, Math.round(Math.abs(i.value) / max * 100)) })),
  }
}
// 环形图仅在存在正值时提供，避免把含负数/零值的数据画成误导性占比
function canDonut(msg) {
  return !!chartOf(msg) && chartOf(msg).items.some(i => i.value > 0)
}

// ---- F29 答案优先：单行聚合结果提取 KPI ----
function fmtNum(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return String(v ?? '—')
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
function fmtMs(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

// X-L02 运行中实时耗时：一个全局 tick 服务所有消息，避免每条一个 interval
const nowTick = ref(Date.now())
let tickTimer = 0
function startTicker() {
  if (tickTimer) return
  nowTick.value = Date.now()
  tickTimer = setInterval(() => { nowTick.value = Date.now() }, 1000)
}
function stopTicker() {
  if (tickTimer) { clearInterval(tickTimer); tickTimer = 0 }
}
function liveElapsed(msg) {
  if (!msg.running || !msg.startedAt) return ''
  const s = Math.max(0, Math.floor((nowTick.value - msg.startedAt) / 1000))
  return s >= 60 ? `${Math.floor(s / 60)}m${s % 60}s` : `${s}s`
}
function kpisOf(msg) {
  const r = msg.result
  if (!r?.rows?.length || r.rows.length !== 1) return []   // 仅单行聚合结果
  const row = r.rows[0]
  const out = []
  for (const c of r.columns || []) {
    const v = toNum(row[c])
    if (v === null) continue
    out.push({ label: c, value: fmtNum(v), raw: String(row[c]) })
    if (out.length >= 4) break
  }
  return out
}

// 是否有可折叠依据（SQL / 口径假设 / 命中资料）
const hasEvidence = (msg) => !!(msg.result?.sql || msg.result?.assumptions?.length || msg.result?.sources?.length || msg.result?.time_window || msg.result?.spec_display || msg.result?.explanation || msg.result?.data_as_of || msg.result?.memory_notes?.length)
const SOURCE_LABEL = { knowledge: '知识库', caliber: '口径', table: '表', doc: '文档', web: '网页' }

function fileIcon(f) {
  const e = (f.ext || '').toLowerCase()
  if (e === '.csv' || e === '.xlsx' || e === '.xls') return '▦'
  if (['.png', '.jpg', '.jpeg'].includes(e)) return '▣'
  if (e === '.pdf') return '▤'
  return '▢'
}

// 会话检索：匹配消息正文、结果摘要、SQL
const searchResults = computed(() => {
  const q = searchQ.value.trim().toLowerCase()
  if (!q) return []
  const out = []
  messages.value.forEach((m, i) => {
    const parts = [m.text, m.result?.summary, m.result?.sql,
                   ...(m.attachments || []).map(a => a.name)].filter(Boolean)
    const hit = parts.find(p => String(p).toLowerCase().includes(q))
    if (hit) out.push({ i, role: m.role, text: String(hit) })
  })
  return out
})

function highlight(text) {
  const q = searchQ.value.trim()
  const safe = text.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
  if (!q) return safe
  return safe.replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'),
                      (m) => `<mark>${m}</mark>`)
}

function jumpTo(i) {
  const el = document.getElementById(`msg-${i}`)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  flashIdx.value = i
  setTimeout(() => { if (flashIdx.value === i) flashIdx.value = -1 }, 1800)
}

// ---- 消息操作（F25/F26） ----
// 标记码：每条回答一个可复制的溯源码（DP-0001）
const traceCode = (msg) => msg.result?.trace_code || (msg.queryId ? 'DP-' + String(msg.queryId).padStart(4, '0') : '')

async function lookupTrace() {
  const code = (searchQ.value || '').trim()
  if (!/^(DP-?\d+|\d+)$/i.test(code)) { traceHit.value = null; return }
  try {
    traceHit.value = await fetchTrace(code)
  } catch (e) {
    traceHit.value = null
    ElMessage.warning(e.message)
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch { ElMessage.error('复制失败，请手动选择文本') }
}

async function copyAnswer(msg) {
  const text = [msg.text, msg.result?.sql].filter(Boolean).join('\n\n')
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch { ElMessage.error('复制失败，请手动选择文本') }
}

// ---- X-R01 复制整表 TSV（复制的是已脱敏的 msg.result.rows，不新增暴露面） ----
async function copyTable(msg) {
  const { columns, rows } = msg.result || {}
  const tsv = buildTsv(columns, rows)
  if (!tsv) return
  try {
    await navigator.clipboard.writeText(tsv)
    ElMessage.success(`已复制 ${rows.length} 行，可直接粘贴到 Excel`)
  } catch { ElMessage.error('复制失败，请手动选择表格') }
}

// X-R02/X-R03/X-R04/X-R05/X-R06 的排序、分页与单元格显示已收进 ResultTable.vue，
// 状态仍挂在 msg 上（随会话快照持久化），原数组只读。

async function regenerate(msg, i) {
  if (busy.value) return
  const prevIdx = messages.value.slice(0, i).map(m => m.role).lastIndexOf('user')
  if (prevIdx < 0) return
  const prevUser = messages.value[prevIdx]
  // 从该问题起重新生成：截断后续轮次，避免新旧回答交错
  messages.value.splice(prevIdx)
  await send(prevUser.text)
}

function exportMarkdown() {
  if (!messages.value.length) return
  const title = activeTitle.value
  const lines = [`# ${title}`, `> 导出于 ${new Date().toLocaleString()}`, '']
  for (const m of messages.value) {
    if (m.role === 'user') lines.push(`## 我`, '', m.text, '')
    else {
      lines.push(`## DataPro`, '')
      if (m.text) lines.push(m.text, '')
      if (m.result?.sql) lines.push('```sql', m.result.sql, '```', '')
      if (m.reject) lines.push(`> [${m.reject.reason_code}] ${m.reject.message}`, '')
    }
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = Object.assign(document.createElement('a'), { href: url, download: `${title}.md` })
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('已导出 Markdown')
}

function onGlobalKey(e) {
  const meta = e.ctrlKey || e.metaKey
  if (meta && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    if (activeView.value !== 'chat') {
      if (editing.value) { ElMessage.warning('文件编辑中，请先保存为新版本或取消'); return }
      activeView.value = 'chat'
    }
    panelOpen.value = true
    panelTab.value = 'search'
    localStorage.setItem('dp_panel', 'open')
  } else if (e.key === 'Escape' && accountMenuOpen.value) {
    accountMenuOpen.value = false
  } else if (e.key === 'Escape' && panelOpen.value) {
    panelOpen.value = false
    localStorage.setItem('dp_panel', 'closed')
  }
}

// ---- 输入框自动增高 ----
const taRef = ref()
function autoResize() {
  const el = taRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}
function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (busy.value) insertQueued()
    else send()
  }
}

// ---- 功能视图化（F21，Codex 式：无弹窗，功能在主区以页面视图呈现） ----
const activeView = ref('chat')   // chat | settings
const llmStatus = ref(null)
const apiList = ref([])
const showApiForm = ref(false)
const apiForm = ref({ id: null, name: '', base_url: '', model: '', api_key_ref: '', enabled: true })
const testingId = ref(null)
const deletingId = ref(null)     // 内联两步删除：第一次点变"确认删除"，再点确认

// 仅管理员专属的视图：知识库（技能/定时任务/巡检/导出对全员可见）
const ADMIN_VIEWS = new Set(['knowledge'])
function canOpenView(view) {
  return role.value === 'admin' || !ADMIN_VIEWS.has(view)
}

function openView(view) {
  if (editing.value) { ElMessage.warning('文件编辑中，请先保存为新版本或取消'); return }
  if (!canOpenView(view)) view = 'chat'
  if (view !== 'file') releaseEditLock()
  activeView.value = view
  if (view === 'settings') {
    // 设置分组按角色过滤：当前分组不可见时回落到第一个可见分组
    if (!settingsTabs.value.some(t => t.k === settingsTab.value)) {
      settingsTab.value = settingsTabs.value[0]?.k || 'account'
    }
    loadLlmStatus()
    if (role.value === 'admin') loadApis()
    if (settingsTab.value === 'role') { loadUsers(); loadPermissions(); loadMetaTables() }
    if (settingsTab.value === 'ops') { loadRulings(); loadCache() }
    if (settingsTab.value === 'usage') loadUsage()
    if (settingsTab.value === 'access') loadAccess()
    if (settingsTab.value === 'account' && role.value === 'admin') loadUsers()
  }
  if (view === 'skills') {
    if (!can('skills.manage') && skillTab.value !== 'skills') skillTab.value = 'skills'
    loadSkills(can('skills.manage'))
    loadLearning()
  }
  if (view === 'schedules') loadSchedules()
  if (view === 'knowledge') { loadKnowledge(); loadDocs() }
  if (view === 'inspections') { loadInspections(); loadInspectionRules() }
  if (view === 'exports') {
    loadExports().then(() => { if (hasPendingExport()) startExportPolling(); else stopExportPolling() })
  }
}

// ---- 二级设置页（参考 Codex/WorkBuddy 设置中心：横向分组 + 卡片式内容） ----
const settingsTab = ref('model')   // datasource | model | appearance | usage | access | role | records | ops | account
// S-F05 最小用量页：只展示 query_record 计数，套餐/计费不在内部工具形态下出现
const usage = ref(null)
const usageAll = ref(false)        // 管理员切换「看全部账号」
async function loadUsage() {
  try {
    const all = role.value === 'admin' && usageAll.value ? 1 : 0
    usage.value = await fetchUsage(30, all)
  } catch (e) { ElMessage.error(e.message) }
}

// ---- U-B05 数据表权限申请：越权 E003 后的自助开通入口 ----
const ACCESS_STATUS = {
  pending: { text: '待审批', cls: 'skip' },
  approved: { text: '已通过', cls: 'ok' },
  rejected: { text: '已驳回', cls: 'fail' },
  cancelled: { text: '已取消', cls: 'skip' },
}
const accessStatus = (s) => ACCESS_STATUS[s] || { text: s || '—', cls: 'skip' }
const accessCatalog = ref([])
const accessMine = ref([])
const accessPending = ref([])
const accessBusy = ref('')
const accessNotes = ref({})
const accessForm = ref({ table_name: '', reason: '', duration_days: 30 })

async function loadAccess() {
  try {
    if (!accessCatalog.value.length) {
      accessCatalog.value = (await fetchAccessCatalog()).items || []
    }
    accessMine.value = (await fetchMyAccessRequests()).items || []
    accessPending.value = role.value === 'admin'
      ? (await fetchAllAccessRequests('pending')).items || []
      : []
  } catch (e) { ElMessage.error(e.message) }
}

async function submitAccess() {
  if (!accessForm.value.table_name) { ElMessage.warning('请选择要申请的数据表'); return }
  // 空值/越界值在前端夹紧，避免后端 422 打断申请流程
  const n = Number(accessForm.value.duration_days)
  const duration_days = Number.isFinite(n) ? Math.min(365, Math.max(1, Math.round(n))) : 30
  accessBusy.value = 'submit'
  try {
    await createAccessRequest({ ...accessForm.value, duration_days })
    ElMessage.success('已提交申请，等待管理员审批')
    accessForm.value = { table_name: '', reason: '', duration_days: 30 }
    await loadAccess()
  } catch (e) { ElMessage.error(e.message) } finally { accessBusy.value = '' }
}

async function decideAccess(id, action) {
  accessBusy.value = id
  try {
    await decideAccessRequest(id, action, { note: accessNotes.value[id] || '' })
    ElMessage.success(action === 'approve' ? '已通过，按有效期授权' : '已驳回')
    delete accessNotes.value[id]
    await loadAccess()
  } catch (e) { ElMessage.error(e.message) } finally { accessBusy.value = '' }
}

// E003 拒答后一键到设置页并预填表名；openView 会按需加载目录与我的申请
function requestAccess(tableName = '') {
  if (!can('chat.use')) return
  settingsTab.value = 'access'
  accessForm.value.table_name = tableName || ''
  openView('settings')
}
const ALL_SETTINGS_TABS = [
  { k: 'datasource', label: '数据源', admin: true, icon: DataLine },
  { k: 'model', label: '模型与AI', admin: true, icon: Cpu },
  { k: 'appearance', label: '外观风格', icon: Sunny },
  { k: 'usage', label: '用量', icon: DataAnalysis },
  { k: 'access', label: '权限申请', icon: Key, perm: 'chat.use' },
  { k: 'role', label: '权限与角色', admin: true, icon: Lock },
  { k: 'records', label: '对话记录', admin: true, icon: Document },
  { k: 'ops', label: '运维处置', admin: true, icon: Monitor },
  { k: 'account', label: '账户与统计', icon: User },
]
// 普通用户只保留个人设置分组；管理分组不下发到前端菜单，另按权限点(perm)二次过滤
const settingsTabs = computed(
  () => ALL_SETTINGS_TABS.filter(t => (role.value === 'admin' || !t.admin)
    && (!t.perm || can(t.perm))))
const settingsTitle = computed(
  () => settingsTabs.value.find(t => t.k === settingsTab.value)?.label || '设置')
// X-N01 面包屑：设置页两级路径，其余视图「DataPro / 视图名」；对话页保持单标题
const breadcrumb = computed(() => {
  if (activeView.value === 'settings') {
    return settingsTabs.value.some(t => t.k === settingsTab.value)
      ? ['设置', settingsTitle.value] : ['设置']
  }
  if (activeView.value !== 'chat') return ['DataPro', activeTitle.value]
  return []
})
// 设置页按「个人 / 管理」分组呈现：图标即语义，管理组用锁形分隔标记
const personalTabs = computed(() => settingsTabs.value.filter(t => !t.admin))
const adminTabs = computed(() => settingsTabs.value.filter(t => t.admin))
// 普通用户的能力概览：仅列真实权限点覆盖的功能，可点击直达对应视图
const abilities = computed(() => {
  const list = [{ k: 'chat', label: '智能问数', icon: ChatDotSquare, view: 'chat' }]
  if (can('files.upload')) list.push({ k: 'files', label: '文件分析', icon: Paperclip, view: 'chat' })
  if (can('skills.manage')) list.push({ k: 'skills', label: '技能库', icon: MagicStick, view: 'skills' })
  if (can('schedules.manage')) list.push({ k: 'schedules', label: '定时任务', icon: Timer, view: 'schedules' })
  list.push({ k: 'exports', label: '导出记录', icon: Download, view: 'exports' })
  return list
})

// ---- 账户二级菜单：管理员专属入口在头像菜单内，点击后进入主区视图 ----
const accountMenuOpen = ref(false)
function toggleAccountMenu() {
  // 只有管理员点开头像二级菜单；普通用户点击无反应
  if (role.value !== 'admin') return
  accountMenuOpen.value = !accountMenuOpen.value
}

function openAccountView(tab) {
  accountMenuOpen.value = false
  if (!settingsTabs.value.some(t => t.k === tab)) return
  settingsTab.value = tab
  openView('settings')
}

// ---- 技能包（F38）：可复用问法，全员可见，管理员维护 ----
const skills = ref([])
const skillForm = ref(null)      // null=关闭表单；否则为编辑中的技能
const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

async function loadSkills(all = false) {
  try { skills.value = await fetchSkills(all) } catch (e) { ElMessage.error(e.message) }
}
function startSkillEdit(s = {}) {
  skillForm.value = {
    id: s.id || null, name: s.name || '', description: s.description || '',
    question: s.question || '', answer: s.answer || '', enabled: s.enabled ?? true,
  }
}
async function submitSkill() {
  const f = skillForm.value
  if (!f?.name?.trim() || !f?.question?.trim()) { ElMessage.warning('名称与问法必填'); return }
  try {
    await saveSkill(f)
    skillForm.value = null
    await loadSkills(can('skills.manage'))
    ElMessage.success('技能已保存')
  } catch (e) { ElMessage.error(e.message) }
}
async function toggleSkill(s) {
  try { await saveSkill({ ...s, enabled: !s.enabled }); await loadSkills(can('skills.manage')) }
  catch (e) { ElMessage.error(e.message) }
}
async function removeSkill(s) {
  if (needConfirm('skill:' + s.id)) return
  try { await deleteSkill(s.id); await loadSkills(can('skills.manage')); ElMessage.success('技能已删除') }
  catch (e) { ElMessage.error(e.message) }
}

// ---- 自动学习（A11）：技能规范 + 长期记忆（技能页内标签） ----
const skillTab = ref('skills')          // skills | learned | memory
const learnedSkills = ref([])
const memories = ref([])
const learningPending = ref(0)
const learningLoading = computed(() => learningPending.value > 0)
const learningScope = ref('mine')       // mine | all（仅管理员可切，默认只看自己）
const learnedQ = ref('')
const memoryQ = ref('')
const memoryType = ref('all')
const learnOpen = ref({})               // id -> 展开完整 SQL / 记忆全文
const learningReady = ref(false)        // 已成功加载过（对话内学习提示的基线快照）
const MEMORY_TYPES = { preference: '偏好', terminology: '术语', rule: '规则', caliber: '口径' }
const fmtConfidence = (v) => `${Math.round((Number(v) || 0) * 100)}%`

const learningAll = computed(() => role.value === 'admin' && learningScope.value === 'all')
const learningFlag = () => role.value === 'admin' && learningScope.value === 'all'

async function loadLearnedSkills() {
  if (!can('skills.manage')) return
  learningPending.value++
  try {
    learnedSkills.value = await fetchLearnedSkills(learningFlag())
    learningReady.value = true
  } catch (e) { ElMessage.error(e.message) }
  finally { learningPending.value-- }
}
async function loadMemories() {
  if (!can('skills.manage')) return
  learningPending.value++
  try {
    memories.value = await fetchMemories(learningFlag())
    learningReady.value = true
  } catch (e) { ElMessage.error(e.message) }
  finally { learningPending.value-- }
}
// 进入技能页 / 登录后加载一次，同时作为「本轮是否学到新东西」的基线
async function loadLearning() {
  if (!can('skills.manage')) return
  await Promise.all([loadLearnedSkills(), loadMemories()])
}
function switchSkillTab(tab) {
  if (tab !== 'skills' && !can('skills.manage')) tab = 'skills'
  skillTab.value = tab
  if (tab === 'learned') loadLearnedSkills()
  if (tab === 'memory') loadMemories()
}
function switchLearningScope(scope) {
  if (learningScope.value === scope) return
  learningScope.value = scope
  loadLearning()
}
function toggleLearnOpen(id) {
  learnOpen.value = { ...learnOpen.value, [id]: !learnOpen.value[id] }
}

// 搜索 / 类型筛选（纯前端，数据量有上限）
const _kwHit = (item, q, fields) => {
  const kw = (q || '').trim().toLowerCase()
  return !kw || fields.some(f => String(item[f] ?? '').toLowerCase().includes(kw))
}
const shownLearned = computed(() => learnedSkills.value.filter(
  s => _kwHit(s, learnedQ.value, ['name', 'question', 'notes', 'caliber', 'sql_template', 'username'])))
const shownMemories = computed(() => memories.value.filter(m =>
  (memoryType.value === 'all' || m.type === memoryType.value) &&
  _kwHit(m, memoryQ.value, ['content', 'source', 'username'])))
const learnedStat = computed(() => ({
  total: learnedSkills.value.length,
  on: learnedSkills.value.filter(s => s.enabled).length,
}))
const memoryStat = computed(() => ({
  total: memories.value.length,
  on: memories.value.filter(m => m.enabled).length,
}))
const memoryTypes = computed(() => [
  { k: 'all', label: '全部', n: memories.value.length },
  ...Object.entries(MEMORY_TYPES)
    .map(([k, label]) => ({ k, label, n: memories.value.filter(m => m.type === k).length }))
    .filter(t => t.n),
])

async function toggleLearned(item) {
  try { await updateLearnedSkill(item.id, !item.enabled); await loadLearnedSkills() }
  catch (e) { ElMessage.error(e.message) }
}
async function removeLearned(item) {
  if (needConfirm('learned:' + item.id)) return
  try {
    await deleteLearnedSkill(item.id)
    await loadLearnedSkills()
    ElMessage.success('规范已删除')
  } catch (e) { ElMessage.error(e.message) }
}
async function toggleMemory(item) {
  try { await updateMemory(item.id, !item.enabled); await loadMemories() }
  catch (e) { ElMessage.error(e.message) }
}
async function removeMemory(item) {
  if (needConfirm('memory:' + item.id)) return
  try {
    await deleteMemory(item.id)
    await loadMemories()
    ElMessage.success('记忆已删除')
  } catch (e) { ElMessage.error(e.message) }
}

// ---- 对话内学习反馈：后端异步学习晚于流结束，延迟探测本轮新增项 ----
function learnSnapshot() {
  if (!learningReady.value || !can('skills.manage') || learningScope.value !== 'mine') return null
  return {
    skills: new Set(learnedSkills.value.map(s => s.id)),
    memories: new Set(memories.value.map(m => m.id)),
  }
}
async function probeLearned(msg, before) {
  if (!before) return
  for (const wait of [2500, 4500]) {
    await new Promise(resolve => setTimeout(resolve, wait))
    if (!authUser.value || msg.learnedTip) return
    try {
      const [sk, mem] = await Promise.all([fetchLearnedSkills(false), fetchMemories(false)])
      const addSkills = sk.filter(s => !before.skills.has(s.id)).length
      const addMems = mem.filter(m => !before.memories.has(m.id)).length
      if (learningScope.value === 'mine') { learnedSkills.value = sk; memories.value = mem }
      if (addSkills || addMems) {
        msg.learnedTip = { skills: addSkills, memories: addMems }
        return
      }
    } catch { return }
  }
}
function viewLearned(tip) {
  skillTab.value = tip.memories && !tip.skills ? 'memory' : 'learned'
  openView('skills')
}

// 首页建议卡：有启用技能时优先展示技能问法
const heroCards = computed(() => {
  const list = skills.value.filter(s => s.enabled).slice(0, 4)
  if (!list.length) return HERO_CARDS
  return list.map(s => ({ t: s.question, b: s.name, icon: MagicStick }))
})
// X-I02 示例卡「填入可编辑」：只写输入框并聚焦，不发送
function fillInput(t) {
  input.value = t
  nextTick(() => { taRef.value?.focus(); autoResize() })
}

// ---- 定时任务（F39）：到点自动执行取数并保留运行记录 ----
const schedules = ref([])
const scheduleForm = ref(null)
const scheduleRunning = ref('')
const runOpen = ref('')

async function loadSchedules() {
  try { schedules.value = await fetchSchedules() } catch (e) { ElMessage.error(e.message) }
}
function startScheduleEdit(t = {}) {
  scheduleForm.value = {
    id: t.id || null, name: t.name || '', question: t.question || '',
    freq: t.freq || 'daily', time: t.time || '09:00',
    weekday: t.weekday || 1, enabled: t.enabled ?? true,
  }
}
async function submitSchedule() {
  const f = scheduleForm.value
  if (!f?.name?.trim() || !f?.question?.trim()) { ElMessage.warning('名称与问法必填'); return }
  try {
    await saveSchedule(f)
    scheduleForm.value = null
    await loadSchedules()
    ElMessage.success('任务已保存')
  } catch (e) { ElMessage.error(e.message) }
}
async function toggleSchedule(t) {
  try { await saveSchedule({ ...t, enabled: !t.enabled }); await loadSchedules() } catch (e) { ElMessage.error(e.message) }
}
async function removeSchedule(t) {
  if (needConfirm('sched:' + t.id)) return
  try { await deleteSchedule(t.id); await loadSchedules(); ElMessage.success('任务已删除') } catch (e) { ElMessage.error(e.message) }
}
async function runNow(t) {
  scheduleRunning.value = t.id
  try {
    const run = await runSchedule(t.id)
    ElMessage[run.status === 'ok' ? 'success' : 'warning'](
      run.status === 'ok' ? `执行成功，返回 ${run.row_count ?? 0} 行` : (run.error || '执行未返回结果'))
    await loadSchedules()
    runOpen.value = t.id
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    scheduleRunning.value = ''
  }
}
const statusText = (s) => ({ ok: '成功', rejected: '已拒答', failed: '失败' }[s] || '—')

// ---- 巡检（A08/F16）：规则 + 最近结果，管理员可见 ----
const inspectionHistory = ref([])
const inspectionRules = ref([])
const inspectionRunning = ref(false)
const inspectionOpen = ref('')

async function loadInspections() {
  try {
    const data = await fetchInspections()
    inspectionHistory.value = data.items || []
  } catch (e) { ElMessage.error(e.message) }
}
async function loadInspectionRules() {
  try { inspectionRules.value = await fetchInspectionRules() } catch (e) { ElMessage.error(e.message) }
}
async function runInspectionNow() {
  inspectionRunning.value = true
  try {
    const res = await runInspection()
    await loadInspections()
    ElMessage[res.failed ? 'warning' : 'success'](
      res.failed ? `巡检完成，${res.failed} 条规则未通过` : '巡检完成，全部通过')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    inspectionRunning.value = false
  }
}
async function toggleInspectionRule(rule) {
  const next = inspectionRules.value.map(r =>
    r.id === rule.id ? { ...r, enabled: !r.enabled } : r)
  try {
    inspectionRules.value = await saveInspectionRules(next)
  } catch (e) { ElMessage.error(e.message) }
}
const INSPECTION_STATUS = {
  pass: { text: '通过', cls: 'ok' },
  fail: { text: '异常', cls: 'fail' },
  error: { text: '执行失败', cls: 'fail' },
  skip: { text: '跳过', cls: 'skip' },
}
const inspectionStatus = (s) => INSPECTION_STATUS[s] || { text: s || '—', cls: 'skip' }
const CHECK_TEXT = { rowcount_drop: '行数骤降', null_ratio: '空值率', no_data: '断流' }

// ---- 大结果集导出申请与审批（B13/F11） ----
const exportsList = ref([])
const exportReq = ref(null)          // { msg, reason }：结果卡内联申请表单
const exportBusy = ref(false)
const exportDeciding = ref('')        // 正在审批的申请 id，防重复点击
const exportNotes = ref({})          // 每条申请的审批备注
const EXPORT_STATUS = {
  pending: { text: '待审批', cls: 'skip' },
  approved: { text: '已通过', cls: 'ok' },
  rejected: { text: '已驳回', cls: 'fail' },
  failed: { text: '导出失败', cls: 'fail' },
}
const exportStatus = (s) => EXPORT_STATUS[s] || { text: s || '—', cls: 'skip' }

async function loadExports(silent = false) {
  try {
    exportsList.value = await fetchExports({ all: role.value === 'admin' ? 1 : 0 })
  } catch (e) { if (!silent) ElMessage.error(e.message) }
}
function startExport(msg) {
  exportReq.value = { msg, reason: '' }
}
async function submitExport() {
  const req = exportReq.value
  if (!req?.msg?.result?.sql) { ElMessage.warning('本条结果没有可导出的 SQL'); return }
  exportBusy.value = true
  try {
    await createExport({
      question: req.msg.asked || '',
      sql: req.msg.result.sql,
      session_id: sessionId.value || '',
      row_count: req.msg.result.row_count,
      reason: req.reason || '',
      window_start: req.msg.result.time_window_start || null,
      window_end: req.msg.result.time_window_end || null,
    })
    exportReq.value = null
    ElMessage.success('已提交导出申请，等待管理员审批')
    startExportPolling()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    exportBusy.value = false
  }
}
async function decideExport(rec, action) {
  if (exportDeciding.value) return
  exportDeciding.value = rec.id
  const note = exportNotes.value[rec.id] || (action === 'approve' ? '同意导出' : '驳回')
  try {
    if (action === 'approve') await approveExport(rec.id, note)
    else await rejectExport(rec.id, note)
    await loadExports()
    ElMessage.success(action === 'approve' ? '已通过并生成文件' : '已驳回')
  } catch (e) { ElMessage.error(e.message) }
  finally { exportDeciding.value = '' }
}
async function saveExportedFile(rec) {
  try {
    const blob = await downloadExport(rec.id)
    const url = URL.createObjectURL(blob)
    const a = Object.assign(document.createElement('a'), { href: url, download: `${rec.id}.csv` })
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) { ElMessage.error(e.message) }
}

// ---- U-F08 导出进度轮询：只在存在待审批时轮询，避免多用户刷接口 ----
let exportPollTimer = 0
const hasPendingExport = () => exportsList.value.some(r => r.status === 'pending')
function stopExportPolling() {
  if (exportPollTimer) { clearInterval(exportPollTimer); exportPollTimer = 0 }
}
function startExportPolling() {
  if (exportPollTimer || !hasPendingExport()) return
  exportPollTimer = setInterval(async () => {
    if (!authUser.value) { stopExportPolling(); return }
    const before = new Map(exportsList.value.map(r => [r.id, r]))
    try {
      const list = await fetchExports({ all: role.value === 'admin' ? 1 : 0 })
      for (const rec of list) {
        const old = before.get(rec.id)
        if (old?.status === 'pending' && rec.status !== 'pending') {
          const kind = rec.status === 'approved' && rec.file ? 'success'
            : rec.status === 'rejected' ? 'warning' : 'error'
          ElMessage[kind](`导出 ${rec.id} ${exportStatus(rec.status).text}`)
        }
      }
      exportsList.value = list
      if (!hasPendingExport()) stopExportPolling()
    } catch (e) { /* 轮询失败静默，下轮再试 */ }
  }, 12 * 1000)
}

// ---- U-F11 通知中心（阶段一）：聚合现有巡检/导出/任务记录，零新后端表 ----
const noticeOpen = ref(false)
const notices = ref([])
const noticeLoading = ref(false)
const noticeReadAt = ref(Number(localStorage.getItem('dp_notice_read_v1') || 0))
let noticeTimer = 0
function noticeTs(n) {
  const t = Date.parse(String(n.ts || '').replace(' ', 'T'))
  return Number.isFinite(t) ? t : 0
}
const unreadNotices = computed(() => notices.value.filter(n => noticeTs(n) > noticeReadAt.value).length)
function markNoticesRead() {
  noticeReadAt.value = Date.now()
  localStorage.setItem('dp_notice_read_v1', String(noticeReadAt.value))
}
function shortTime(ts) {
  const t = noticeTs({ ts })
  if (!t) return '—'
  const d = new Date(t)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
async function loadNotices(silent = true) {
  if (!authUser.value) return
  noticeLoading.value = true
  try {
    const out = []
    if (can('inspections.manage')) {
      const r = await fetchInspections()
      const s = r?.summary || {}
      if (s.latest_failed) out.push({ id: 'insp-fail', view: 'inspections', icon: WarningFilled,
        title: '巡检异常', text: `最近巡检 ${s.latest_failed} 项未通过`, ts: s.latest_ts })
      if (s.latest_error) out.push({ id: 'insp-error', view: 'inspections', icon: WarningFilled,
        title: '巡检执行错误', text: `${s.latest_error} 项执行出错`, ts: s.latest_ts })
    }
    const ex = await fetchExports({ all: role.value === 'admin' ? 1 : 0 })
    for (const rec of ex || []) {
      if (role.value === 'admin' && rec.status === 'pending') {
        out.push({ id: `exp-${rec.id}`, view: 'exports', icon: Download,
          title: '待审批导出', text: `${rec.requester} · ${rec.id}`, ts: rec.created_at })
      } else if (rec.requester === authUser.value.username && ['rejected', 'failed'].includes(rec.status)) {
        out.push({ id: `exp-${rec.id}`, view: 'exports', icon: Download,
          title: `导出${exportStatus(rec.status).text}`, text: `${rec.id} ${rec.error || rec.review_note || ''}`.trim(), ts: rec.created_at })
      }
    }
    // U-B05：管理员看待审批，普通用户看本人被驳回的申请
    if (role.value === 'admin') {
      const ar = await fetchAllAccessRequests('pending', 20)
      for (const rec of ar?.items || []) {
        out.push({ id: `ar-${rec.id}`, view: 'settings', tab: 'access', icon: Key,
          title: '待审批权限申请', text: `${rec.requester} · ${rec.table_name}`, ts: rec.created_at })
      }
    } else {
      const ar = await fetchMyAccessRequests('rejected', 20)
      for (const rec of ar?.items || []) {
        out.push({ id: `ar-${rec.id}`, view: 'settings', tab: 'access', icon: Key,
          title: '权限申请已驳回',
          text: `${rec.table_name} ${rec.review_note || ''}`.trim(),
          ts: rec.reviewed_at || rec.created_at })
      }
    }
    if (can('schedules.manage')) {
      const tasks = await fetchSchedules()
      for (const t of (tasks || []).filter(x => x.last_status && x.last_status !== 'ok').slice(0, 8)) {
        out.push({ id: `sch-${t.id}`, view: 'schedules', icon: Timer,
          title: '定时任务未成功', text: `${t.name} · ${exportStatus(t.last_status).text || t.last_status}`, ts: t.last_run_at })
      }
    }
    notices.value = out.sort((a, b) => noticeTs(b) - noticeTs(a)).slice(0, 12)
  } catch (e) { if (!silent) ElMessage.error(e.message) }
  finally { noticeLoading.value = false }
}
function toggleNotices() {
  noticeOpen.value = !noticeOpen.value
  if (noticeOpen.value) loadNotices()
}
function openNotice(n) {
  noticeOpen.value = false
  markNoticesRead()
  if (n.tab) settingsTab.value = n.tab   // 权限申请落在设置页内，直接切到对应分组
  openView(n.view)
}

// ---- 新媒体行业知识库：术语/指标定义，用于口径问答 ----
const knowledge = ref([])
const kbQuery = ref('')
const knowledgeForm = ref(null)
const kbCategory = ref('')          // 当前分类筛选（空=全部）
const kbOpen = ref(new Set())       // 展开的词条 id，默认全部收起

const kbCategories = computed(() => {
  const map = new Map()
  for (const k of knowledge.value) {
    const c = k.category || '通用'
    map.set(c, (map.get(c) || 0) + 1)
  }
  return [...map.entries()].map(([name, count]) => ({ name, count }))
})
const kbVisible = computed(() => kbCategory.value
  ? knowledge.value.filter(k => (k.category || '通用') === kbCategory.value)
  : knowledge.value)
function toggleKb(id) {
  const next = new Set(kbOpen.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  kbOpen.value = next
}

async function loadKnowledge(q = '') {
  try {
    knowledge.value = await fetchKnowledge(q)
    // 搜索结果里已没有当前分类时退回「全部」
    if (kbCategory.value && !knowledge.value.some(k => (k.category || '通用') === kbCategory.value)) {
      kbCategory.value = ''
    }
  } catch (e) { ElMessage.error(e.message) }
}
function startKbEdit(item = {}) {
  knowledgeForm.value = {
    id: item.id || null, term: item.term || '', category: item.category || '',
    definition: item.definition || '', formula: item.formula || '',
    aliasesText: (item.aliases || []).join('、'),
  }
}
async function submitKb() {
  const f = knowledgeForm.value
  if (!f?.term?.trim()) { ElMessage.warning('术语必填'); return }
  try {
    const payload = {
      ...f,
      aliases: f.aliasesText.split(/[、,，\s]+/).filter(Boolean),
    }
    delete payload.aliasesText
    await saveKnowledge(payload)
    knowledgeForm.value = null
    await loadKnowledge(kbQuery.value)
    ElMessage.success('知识条目已保存')
  } catch (e) { ElMessage.error(e.message) }
}
async function removeKb(item) {
  if (needConfirm('kb:' + item.id)) return
  try { await deleteKnowledge(item.id); await loadKnowledge(kbQuery.value) } catch (e) { ElMessage.error(e.message) }
}

// ---- 文档知识库（RAG）：制度/规范/报告，按段落检索并给出引用 ----
const docList = ref([])
const docStats = ref(null)
const docBusy = ref(false)
const docOpen = ref(new Set())
const docQuery = ref('')
const docHits = ref(null)      // 检索预览结果：调阈值时看真实分数

function toggleDoc(id) {
  const next = new Set(docOpen.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  docOpen.value = next
}
async function loadDocs() {
  try {
    docList.value = await fetchDocs()
    docStats.value = await fetchDocStats()
  } catch (e) { ElMessage.error(e.message) }
}
async function handleDocUpload(ev) {
  const file = ev.target?.files?.[0]
  ev.target.value = ''            // 允许同一文件重传（内容改过但文件名没变）
  if (!file) return
  if (file.size > 2 * 1024 * 1024) { ElMessage.warning('单篇文档不超过 2 MB'); return }
  docBusy.value = true
  try {
    const r = await uploadDoc(file, file.name.replace(/\.[^.]+$/, ''))
    await loadDocs()
    // 没向量不报错但要明说：此时只能按字面命中，语义检索未生效
    if (r.embedded === 0 && r.chunk_count > 0) {
      ElMessage.warning(`已入库 ${r.chunk_count} 段，但未向量化（embedding 未配置），当前仅按字面检索`)
    } else {
      ElMessage.success(`已入库 ${r.chunk_count} 段，向量化 ${r.embedded} 段`)
    }
  } catch (e) { ElMessage.error(e.message) } finally { docBusy.value = false }
}
async function removeDoc(item) {
  if (needConfirm('doc:' + item.id)) return
  try { await deleteDoc(item.id); await loadDocs() } catch (e) { ElMessage.error(e.message) }
}
async function handleReindex(item) {
  docBusy.value = true
  try {
    const r = await reindexDoc(item.id)
    await loadDocs()
    ElMessage.success(r.embedded ? `已重建 ${r.chunk_count} 段并向量化` : '已重建切块，但未向量化（embedding 未配置）')
  } catch (e) { ElMessage.error(e.message) } finally { docBusy.value = false }
}
async function runDocSearch() {
  const q = docQuery.value.trim()
  if (!q) { docHits.value = null; return }
  try { docHits.value = await searchDocs(q, 5) } catch (e) { ElMessage.error(e.message) }
}

function hostOf(url) {
  try { return new URL(url).host } catch { return url || '—' }
}

async function loadLlmStatus() {
  try { llmStatus.value = await fetchLlmStatus() } catch { llmStatus.value = null }
}

async function loadApis() {
  try { apiList.value = await fetchApis() }
  catch (e) { ElMessage.error(e.message) }
}

function editApi(row) {
  apiForm.value = { id: row.id, name: row.name, base_url: row.base_url, model: row.model, api_key_ref: row.api_key_ref || '', enabled: row.enabled }
  showApiForm.value = true
}

function newApi() {
  apiForm.value = { id: null, name: '', base_url: '', model: '', api_key_ref: '', enabled: true }
  showApiForm.value = true
}

async function submitApi() {
  const f = apiForm.value
  if (!f.name || !f.base_url || !f.model || (!f.id && !f.api_key_ref)) {
    ElMessage.warning('请填写完整信息' + (f.id ? '（Key 引用留空表示清除）' : ''))
    return
  }
  try {
    await saveApi(f)
    ElMessage.success(f.id ? '已保存' : '已添加')
    showApiForm.value = false
    await loadApis()
    await loadLlmStatus()
  } catch (e) { ElMessage.error(e.message) }
}

async function removeApi(row) {
  if (deletingId.value !== row.id) {          // 第一步：进入确认态
    deletingId.value = row.id
    setTimeout(() => { if (deletingId.value === row.id) deletingId.value = null }, 3000)
    return
  }
  deletingId.value = null                     // 第二步：确认删除
  try {
    await deleteApi(row.id)
    ElMessage.success('已删除')
    await loadApis()
    await loadLlmStatus()
  } catch (e) { ElMessage.error(e.message) }
}

async function makeDefault(row) {
  try {
    await setDefaultApi(row.id)
    ElMessage.success(`已将「${row.name}」设为默认`)
    await loadApis()
    await loadLlmStatus()
  } catch (e) { ElMessage.error(e.message) }
}

async function runTest(row) {
  testingId.value = row.id
  try {
    const r = await testApi(row.id)
    row.lastTest = r.ok ? `✓ ${r.message}` : `✗ ${r.message}`
    r.ok ? ElMessage.success(r.message) : ElMessage.error(r.message)
  } catch (e) { ElMessage.error(e.message) }
  finally { testingId.value = null }
}

// ---- 用户管理（B12，仅管理员）：账号增删改 + 改密/禁用后旧令牌失效 ----
const userList = ref([])
const userForm = ref(null)
const userDeletingId = ref(null)
const userSaving = ref(false)

async function loadUsers() {
  if (role.value !== 'admin') return
  try { userList.value = (await fetchUsers()).users } catch (e) { ElMessage.error(e.message) }
}

function switchSettings(tab) {
  if (!settingsTabs.value.some(t => t.k === tab)) return
  settingsTab.value = tab
  if (tab === 'role') { loadUsers(); loadPermissions(); loadMetaTables() }
  if (tab === 'records') loadRecords()
  if (tab === 'usage') loadUsage()
  if (tab === 'access') loadAccess()
  if (tab === 'account' && role.value === 'admin') loadUsers()
}

// ---- 对话记录（管理后台，按用户/天数/反馈/关键词筛选） ----
const recordList = ref([])
const recordTotal = ref(0)
const recordPage = ref(1)
const recordPageSize = 20
const recordLoading = ref(false)
const recordFilters = ref({ username: '', days: 7, rating: '', keyword: '', outcome: '', error_code: '' })
// U-B01 结果分类：状态机每个终态都会写进 query_record.outcome，这里只做展示映射
const OUTCOME_OPTIONS = [
  ['', '全部'], ['result', '已出数'], ['escalate', '转人工'],
  ['clarify', '需澄清'], ['reject', '拒答'], ['error', '异常'], ['unknown', '未分类'],
]
const OUTCOME_LABEL = Object.fromEntries(OUTCOME_OPTIONS.map(([k, v]) => [k, v]))
const ERROR_OPTIONS = [
  ['', '全部'], ['E001', 'E001 信息不足'], ['E002', 'E002 无匹配'], ['E003', 'E003 越权'],
  ['E004', 'E004 校验未过'], ['E005', 'E005 模型超时'], ['E006', 'E006 数据源不可用'],
  ['E007', 'E007 限流'], ['E009', 'E009 扫描过大'], ['E010', 'E010 转人工'],
]
const recordDetail = ref(null)

async function loadRecords() {
  recordLoading.value = true
  try {
    const data = await fetchRecords({ ...recordFilters.value, page: recordPage.value,
                                      page_size: recordPageSize })
    recordList.value = data.items || []
    recordTotal.value = data.total || 0
  } catch (e) { ElMessage.error(e.message) } finally { recordLoading.value = false }
}

function searchRecords() {
  recordPage.value = 1
  recordDetail.value = null
  loadRecords()
}

function changeRecordPage(delta) {
  const next = recordPage.value + delta
  if (next < 1 || (next - 1) * recordPageSize >= recordTotal.value) return
  recordPage.value = next
  loadRecords()
}

function toggleRecordDetail(row) {
  recordDetail.value = recordDetail.value?.id === row.id ? null : row
}

// ---- 用户级数据表权限（A02/S1）：管理员可在用户表单里限定可访问表 ----
const metaTables = ref([])
const metaTablesLoaded = ref(false)
const tableAccess = ref({ inherit: true, tables: [], loading: false, failed: false })

async function loadMetaTables() {
  if (role.value !== 'admin' || metaTablesLoaded.value) return
  try {
    metaTables.value = await fetchMetaTables()
    metaTablesLoaded.value = true
  } catch (e) { ElMessage.error(e.message) }
}

async function loadTableAccess(userId) {
  tableAccess.value = { inherit: true, tables: [], loading: true, failed: false }
  try {
    const data = await fetchTableAccess(userId)
    tableAccess.value = { inherit: data.inherit, tables: data.tables || [],
                          loading: false, failed: false }
  } catch (e) {
    // 加载失败时标记 failed：保存用户时跳过写表权限，避免用默认值覆盖已有授权
    tableAccess.value = { inherit: true, tables: [], loading: false, failed: true }
    ElMessage.error(e.message)
  }
}

function toggleTableAccess(name) {
  const list = tableAccess.value.tables
  tableAccess.value.tables = list.includes(name) ? list.filter(t => t !== name) : [...list, name]
}

// 已授权但不在当前元数据中的表（如已下线）：仍需展示，否则无法通过界面取消
const extraAccessTables = computed(() => {
  const known = new Set(metaTables.value.map(t => t.table_name.toLowerCase()))
  return tableAccess.value.tables.filter(t => !known.has(t))
})

function startUserCreate() {
  userForm.value = { id: null, username: '', display_name: '', password: '', role: 'user', status: 'active',
                     permMode: 'inherit', permissions: [] }
  tableAccess.value = { inherit: true, tables: [], loading: false, failed: false }
}

function startUserEdit(u) {
  userForm.value = { id: u.id, username: u.username, display_name: u.display_name,
                     password: '', role: u.role, status: u.status,
                     permMode: u.permissions ? 'custom' : 'inherit',
                     permissions: [...(u.permissions || [])] }
  loadMetaTables()
  if (u.role !== 'admin') loadTableAccess(u.id)
}

async function submitUser() {
  const f = userForm.value
  if (!f) return
  if (!f.id && (!f.username.trim() || !f.password)) { ElMessage.warning('账号与密码必填'); return }
  if (f.password && f.password.length < 6) { ElMessage.warning('密码至少 6 位'); return }
  if (f.id && tableAccess.value.loading) { ElMessage.warning('数据表权限加载中，请稍候'); return }
  // 元数据表清单没加载成功时不允许切到「仅限勾选表」，避免把用户误配为空权限
  if (f.id && !tableAccess.value.inherit && !metaTables.value.length) {
    ElMessage.warning('元数据表清单未加载，暂不能保存数据表权限'); return
  }
  userSaving.value = true
  try {
    let uid = f.id
    if (f.id) {
      const patch = { display_name: f.display_name, role: f.role, status: f.status }
      if (f.password) patch.password = f.password
      if (f.permMode === 'inherit') patch.inherit_permissions = true
      else patch.permissions = f.permissions
      await updateUser(f.id, patch)
    } else {
      const created = await createUser({ username: f.username.trim(), display_name: f.display_name.trim(),
                                         password: f.password, role: f.role })
      uid = created?.id || null
    }
    // 数据表权限与账号一起保存：inherit=true 表示沿用全局白名单（清除覆盖）；
    // 读取失败时跳过，避免用默认值覆盖已有授权；管理员不受表权限限制，无需保存
    if (uid && !tableAccess.value.failed && f.role !== 'admin') {
      await saveTableAccess(uid, { inherit: tableAccess.value.inherit,
                                   tables: tableAccess.value.tables })
    }
    userForm.value = null
    await loadUsers()
    ElMessage.success(f.id ? '用户已保存' : '用户已创建，可编辑该用户配置数据表权限')
  } catch (e) { ElMessage.error(e.message) }
  finally { userSaving.value = false }
}

async function removeUser(u) {
  if (userDeletingId.value !== u.id) {
    userDeletingId.value = u.id
    setTimeout(() => { if (userDeletingId.value === u.id) userDeletingId.value = null }, 3000)
    return
  }
  userDeletingId.value = null
  try {
    await deleteUser(u.id)
    await loadUsers()
    ElMessage.success('用户已删除')
  } catch (e) { ElMessage.error(e.message) }
}

// ---- 权限矩阵（B12）：权限点 × 角色默认；用户级覆盖在用户表单里设置 ----
const permDefs = ref([])
const rolePerms = ref({ user: [] })
const permSaving = ref(false)

async function loadPermissions() {
  if (role.value !== 'admin') return
  try {
    const data = await fetchPermissions()
    permDefs.value = data.permissions || []
    rolePerms.value = { user: [...(data.roles?.user || [])] }
  } catch (e) { ElMessage.error(e.message) }
}

function toggleRolePerm(code) {
  const list = rolePerms.value.user
  const i = list.indexOf(code)
  if (i >= 0) list.splice(i, 1)
  else list.push(code)
}

async function saveRolePerms() {
  permSaving.value = true
  try {
    await saveRolePermissions('user', rolePerms.value.user)
    ElMessage.success('已保存普通用户默认权限，相关用户需重新登录生效')
    await loadPermissions()
  } catch (e) { ElMessage.error(e.message) }
  finally { permSaving.value = false }
}

// ---- 顶栏/composer 模型选择器（F20，联动 B16） ----
const modelMenuOpen = ref(false)
async function onModelMenu(visible) {
  modelMenuOpen.value = visible
  await loadLlmStatus()
  if (visible && role.value === 'admin') loadApis()
}
async function onPickModel(row) {
  try {
    await setDefaultApi(row.id)
    ElMessage.success(`已切换默认模型：${row.model}`)
    await Promise.all([loadLlmStatus(), loadApis()])
  } catch (e) { ElMessage.error(e.message) }
}
</script>

<template>
  <LoginView v-if="!authUser" @success="onLogin" />
  <div v-else class="shell" :class="{ 'sidebar-open': sidebarOpen, 'sidebar-collapsed': sidebarCollapsed }">
    <!-- S06 前端水印：用户 + 时间，仅视觉溯源，不拦截交互 -->
    <div class="watermark-layer" aria-hidden="true">
      <span v-for="i in 12" :key="i">{{ watermarkText }}</span>
    </div>
    <!-- 左侧会话栏（Codex 式：品牌/新对话/日期分组/账户区） -->
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">{{ branding.logo_text }}</div>
        <div class="name">{{ branding.product_name }}</div>
        <div class="ver">{{ branding.version }}</div>
        <button class="side-toggle" :title="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" @click="toggleSidebar">
          <el-icon><Expand v-if="sidebarCollapsed" /><Fold v-else /></el-icon>
        </button>
      </div>
      <div class="sidebar-body">
      <button class="new-chat-btn" title="新对话" @click="newChat">
        <el-icon><Plus /></el-icon><span class="ncb-label">新对话</span>
      </button>
      <div class="nav-list">
        <div class="nav-item" :class="{ active: activeView === 'chat' }" title="对话" @click="openView('chat')">
          <el-icon><ChatDotSquare /></el-icon><span class="nav-label">对话</span>
        </div>
        <!-- 技能 / 定时任务 / 巡检：由权限点控制入口，普通用户默认可用 -->
        <div v-if="can('skills.manage')" class="nav-item" :class="{ active: activeView === 'skills' }"
             title="技能" @click="openView('skills')">
          <el-icon><MagicStick /></el-icon><span class="nav-label">技能</span>
        </div>
        <div v-if="can('schedules.manage')" class="nav-item" :class="{ active: activeView === 'schedules' }"
             title="定时任务" @click="openView('schedules')">
          <el-icon><Timer /></el-icon><span class="nav-label">定时任务</span>
        </div>
        <div v-if="role === 'admin'" class="nav-item" :class="{ active: activeView === 'knowledge' }"
             title="知识库" @click="openView('knowledge')">
          <el-icon><Reading /></el-icon><span class="nav-label">知识库</span>
        </div>
        <div v-if="can('inspections.manage')" class="nav-item"
             :class="{ active: activeView === 'inspections' }"
             title="巡检" @click="openView('inspections')">
          <el-icon><Monitor /></el-icon><span class="nav-label">巡检</span>
        </div>
        <!-- 导出：管理员是审批台，普通用户只能看本人申请与下载 -->
        <div class="nav-item" :class="{ active: activeView === 'exports' }"
             :title="role === 'admin' ? '导出审批' : '导出记录'"
             @click="openView('exports')">
          <el-icon><Download /></el-icon>
          <span class="nav-label">{{ role === 'admin' ? '导出审批' : '导出记录' }}</span>
        </div>
        <div class="nav-item" :class="{ active: activeView === 'help' }"
             title="使用帮助" @click="openView('help')">
          <el-icon><QuestionFilled /></el-icon><span class="nav-label">使用帮助</span>
        </div>
        <!-- 个人设置：普通用户走侧栏；管理员的设置入口统一收进头像二级菜单 -->
        <div v-if="role !== 'admin'" class="nav-item" :class="{ active: activeView === 'settings' }"
             title="设置" @click="openView('settings')">
          <el-icon><Setting /></el-icon><span class="nav-label">设置</span>
        </div>
      </div>
      <div class="session-list">
        <div class="group-label">历史会话</div>
        <template v-for="([label, items]) in groupedSessions" :key="label">
          <div class="group-label">{{ label }}</div>
          <div v-for="s in items" :key="s.id" class="session-item"
               :class="{ active: s.id === activeId }" :title="s.title"
               @click="switchSession(s.id)">
            <input v-if="renamingId === s.id" v-model="renameText" class="rename-input" maxlength="40"
                   @click.stop @keydown.enter.prevent="commitRename"
                   @keydown.esc.prevent="cancelRename" @blur="commitRename" />
            <template v-else>
              <span class="session-title" @dblclick.stop="startRename(s)">{{ s.title }}</span>
              <span class="session-ts">{{ fmtTime(s.ts) }}</span>
            </template>
            <button v-if="renamingId !== s.id" class="session-act" title="重命名"
                    @click.stop="startRename(s)"><el-icon><Edit /></el-icon></button>
            <button class="del" :class="{ confirm: confirmDelId === s.id }"
                    :title="confirmDelId === s.id ? '再次点击确认' : '删除会话'"
                    @click.stop="deleteSession(s.id, $event)">
              <span v-if="confirmDelId === s.id">确认</span>
              <el-icon v-else><Close /></el-icon>
            </button>
          </div>
        </template>
        <div v-if="!sessions.length" class="empty-hint">暂无历史对话</div>
      </div>
      <div class="account-wrap">
        <transition name="menu-pop">
          <!-- 头像二级菜单仅管理员可用：普通用户点头像无反应 -->
          <div v-if="accountMenuOpen && role === 'admin'" class="account-menu" @click.stop>
            <div class="am-group">通用</div>
            <button class="am-item" @click="openAccountView('appearance')">
              <el-icon><Sunny /></el-icon>外观风格
            </button>
            <button class="am-item" @click="openAccountView('account')">
              <el-icon><User /></el-icon>账户与统计
            </button>
            <div class="am-sep" />
            <div class="am-group admin"><el-icon><Lock /></el-icon>管理员</div>
            <button class="am-item" @click="openAccountView('model')">
              <el-icon><Key /></el-icon>API 管理
            </button>
            <button class="am-item" @click="openAccountView('datasource')">
              <el-icon><DataLine /></el-icon>数据源
            </button>
            <button class="am-item" @click="openAccountView('role')">
              <el-icon><Lock /></el-icon>权限与角色
            </button>
            <button class="am-item" @click="openAccountView('access')">
              <el-icon><Key /></el-icon>权限申请审批
            </button>
            <button class="am-item" @click="openAccountView('records')">
              <el-icon><Document /></el-icon>对话记录
            </button>
          </div>
        </transition>
        <div class="account-bar" :class="{ open: accountMenuOpen }">
          <button class="account-main" @click.stop="toggleAccountMenu">
            <div class="account-info">
              <div class="account-avatar" :class="role" data-tip="账号与设置">
                <el-icon><Lock v-if="role === 'admin'" /><User v-else /></el-icon>
              </div>
              <div class="account-meta">
                <div class="account-name">{{ displayName }}</div>
                <div class="account-role" :class="role">{{ roleName }}</div>
              </div>
            </div>
            <el-icon v-if="role === 'admin'" class="account-chevron"><ArrowUp /></el-icon>
          </button>
          <button class="account-logout" title="退出登录" @click.stop="doLogout">
            <el-icon><SwitchButton /></el-icon>
          </button>
        </div>
      </div>
      </div>
    </aside>
    <div class="scrim" @click="sidebarOpen = false" />

    <!-- 主区 -->
    <main class="main">
      <div class="topbar">
        <div class="left">
          <button class="icon-btn menu-btn" @click="toggleSidebar">
            <el-icon><Menu /></el-icon>
          </button>
          <!-- 模型选择器（Codex 顶栏位） -->
          <el-dropdown trigger="click" @command="onPickModel" @visible-change="onModelMenu">
            <span class="model-chip" :class="{ user: role !== 'admin' }">
              <el-icon><Cpu /></el-icon> {{ llmStatus?.default_model || '未配置' }}
              <el-icon v-if="role === 'admin'"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <template v-if="role === 'admin'">
                  <div class="model-menu-hint">切换默认模型</div>
                  <el-dropdown-item v-for="a in apiList.filter(x => x.enabled)" :key="a.id"
                                    :command="a">
                    {{ a.model }}
                    <el-tag v-if="a.is_default" size="small" type="success" style="margin-left:6px">当前</el-tag>
                  </el-dropdown-item>
                  <el-dropdown-item v-if="!apiList.some(x => x.enabled)" disabled>暂无可用模型</el-dropdown-item>
                </template>
                <template v-else>
                  <div class="model-menu-hint"><el-icon><Lock /></el-icon> 由管理员配置</div>
                  <el-dropdown-item disabled>{{ llmStatus?.default_model || '未配置' }}（{{ llmStatus?.provider || '—' }}）</el-dropdown-item>
                </template>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <div class="title" :class="{ crumb: breadcrumb.length }">
            <template v-if="breadcrumb.length">
              <span v-for="(b, bi) in breadcrumb" :key="bi" class="crumb-seg"
                    :class="{ current: bi === breadcrumb.length - 1 }">{{ b }}<span v-if="bi < breadcrumb.length - 1" class="crumb-sep">/</span></span>
            </template>
            <template v-else>{{ activeTitle }}</template>
          </div>
        </div>
        <div class="status">
          <!-- 状态用图形反馈：锁=只读，脉冲点=运行态；文字说明收进 tooltip -->
          <span class="state-flag" data-tip="只读模式 · 所有查询均不改写数据">
            <el-icon><Lock /></el-icon>
          </span>
          <span class="state-live" :class="{ running: busy }"
                :data-tip="busy ? '正在处理' : '服务就绪'">
            <i></i>
          </span>
          <button class="icon-btn" :class="{ active: panelOpen }" title="文件与搜索（Ctrl+K）"
                  @click="togglePanel()">
            <el-icon><Document /></el-icon>
          </button>
          <button class="icon-btn" title="导出 Markdown" @click="exportMarkdown">
            <el-icon><Download /></el-icon>
          </button>
          <div class="notice-wrap">
            <button class="icon-btn notice-btn" :class="{ active: noticeOpen }" title="通知"
                    @click="toggleNotices">
              <el-icon><Bell /></el-icon>
              <i v-if="unreadNotices" class="notice-badge">{{ unreadNotices > 9 ? '9+' : unreadNotices }}</i>
            </button>
            <transition name="menu-pop">
              <div v-if="noticeOpen" class="notice-panel" @click.stop>
                <div class="notice-head" data-tip="聚合现有巡检 / 导出 / 任务记录，非实时推送">
                  <b>通知</b>
                  <button class="model-act" :disabled="!unreadNotices" @click="markNoticesRead">全部已读</button>
                </div>
                <button v-for="n in notices" :key="n.id" class="notice-item" @click="openNotice(n)">
                  <span class="notice-ico"><el-icon><component :is="n.icon" /></el-icon></span>
                  <span class="notice-main"><b>{{ n.title }}</b><span>{{ n.text }}</span></span>
                  <span class="notice-time">{{ shortTime(n.ts) }}</span>
                </button>
                <div v-if="!notices.length && !noticeLoading" class="empty-hint">暂无需要关注的动态</div>
              </div>
            </transition>
          </div>
          <button class="icon-btn" :title="theme === 'light' ? '切换暗色' : '切换亮色'" @click="toggleTheme">
            <el-icon><Sunny v-if="theme === 'dark'" /><Moon v-else /></el-icon>
          </button>
          <el-button text size="small" :icon="Refresh" @click="newChat" />
        </div>
      </div>

      <el-scrollbar v-if="activeView === 'chat'" ref="scrollRef" class="thread-scroll"
                    @scroll="onThreadScroll">
        <!-- 空会话 Hero -->
        <div v-if="showHero" class="hero">
          <div class="big-logo">{{ branding.logo_text }}</div>
          <h2>有什么<em>数据</em>可以帮你？</h2>
          <div class="hero-badges">
            <span class="hero-badge" data-tip="只读查询 · 不改写任何数据"><el-icon><Lock /></el-icon></span>
            <span class="hero-badge" data-tip="口径优先 · 先确认指标定义再取数"><el-icon><Reading /></el-icon></span>
            <span class="hero-badge" data-tip="全程留痕 · 每条结果可溯源"><el-icon><Timer /></el-icon></span>
            <button class="hero-badge demo-entry" data-tip="站内演示：运行一条只读样例问法"
                    @click="startDemo">
              <el-icon><MagicStick /></el-icon><span>演示</span>
            </button>
          </div>
          <div v-if="!guideDismissed" class="guide-card">
            <div class="guide-head">
              <span>这样问，更容易一次问对</span>
              <button class="guide-x" title="不再提示" @click="dismissGuide">
                <el-icon><Close /></el-icon>
              </button>
            </div>
            <div class="guide-formula">
              <template v-for="(g, gi) in GUIDE_FORMULA" :key="g.k">
                <span v-if="gi" class="guide-plus">+</span>
                <span class="guide-slot"><b>{{ g.k }}</b>{{ g.v }}</span>
              </template>
            </div>
            <button class="guide-example" @click="send('最近 7 天按品类看支付订单数')">
              例如：最近 7 天按品类看支付订单数
            </button>
          </div>

          <div class="hero-cards">
            <div v-for="c in heroCards" :key="c.t" class="hero-card" @click="send(c.t)">
              <div class="hero-card-head">
                <span class="hero-card-icon" :class="{ danger: c.danger }"><el-icon><component :is="c.icon" /></el-icon></span>
                <span class="hero-card-badge" :class="{ danger: c.danger }">{{ c.b }}</span>
                <button class="hero-fill" data-tip="填入输入框，可修改后再发送"
                        aria-label="填入输入框" @click.stop="fillInput(c.t)">
                  <el-icon><Edit /></el-icon>
                </button>
              </div>
              <div class="t">{{ c.t }}</div>
            </div>
          </div>
        </div>

        <!-- 消息流 -->
        <div v-else class="thread">
          <div v-for="(msg, i) in messages" :key="i" :id="'msg-' + i" class="turn"
               :class="[msg.role, { 'msg-flash': flashIdx === i }]">
            <div v-if="msg.role === 'user'" class="content">
              {{ msg.text }}
              <div v-if="msg.attachments?.length" class="msg-atts">
                <span v-for="a in msg.attachments" :key="a.file_id" class="msg-att"
                      :class="{ clickable: a.file_id }" title="在右侧边栏预览" @click="openPanelPreview(a)">
                  {{ fileIcon(a) }} {{ a.name }}
                </span>
              </div>
            </div>
            <div v-else class="content-wrap" style="display:flex;gap:12px">
              <div class="avatar">DP</div>
              <div class="content">
                <!-- 处理过程（Codex 式：单行摘要，展开为步骤时间轴） -->
                <div v-if="msg.steps?.length" class="process">
                  <button class="process-toggle" :class="{ open: expanded[i] }"
                          :data-tip="msg.running ? '正在执行，点击查看步骤' : `已处理 ${msg.steps.length} 步，点击查看`"
                          @click="!msg.running && (expanded[i] = !expanded[i])">
                    <span v-if="msg.running" class="spinner" />
                    <el-icon v-else class="done-icon"><CircleCheck /></el-icon>
                    <!-- 步骤数用点阵反馈，替代「已处理 N 步」文字 -->
                    <span class="pt-dots" aria-hidden="true">
                      <i v-for="n in Math.min(msg.steps.length, 9)" :key="n"
                         :class="{ on: !msg.running || n < msg.steps.length }"></i>
                    </span>
                    <span v-if="msg.running && liveElapsed(msg)" class="pt-time live">{{ liveElapsed(msg) }}</span>
                    <span v-if="!msg.running && msg.elapsedMs" class="pt-time">{{ fmtMs(msg.elapsedMs) }}</span>
                    <span v-if="msg.estScanRows" class="scan-chip mono"
                          data-tip="执行前由数仓预估的扫描行数，实际结果以执行为准">≈{{ Number(msg.estScanRows).toLocaleString('zh-CN') }} 行</span>
                    <span v-if="msg.running" class="pt-current">{{ msg.steps[msg.steps.length - 1] }}</span>
                    <el-icon v-if="!msg.running" class="pt-caret"><ArrowDown /></el-icon>
                  </button>
                  <div v-if="expanded[i] || msg.running" class="process-list">
                    <div v-for="(s, j) in msg.steps" :key="j" class="p-item"
                         :class="{ active: msg.running && j === msg.steps.length - 1 }">
                      <span class="p-idx">{{ String(j + 1).padStart(2, '0') }}</span>
                      <span class="p-text">{{ s }}</span>
                    </div>
                  </div>
                </div>

                <div v-if="msg.isClarify" class="clarify-tag">
                  <el-icon><InfoFilled /></el-icon> 需要补充信息
                </div>

                <!-- U-F03：口径差异用可比卡片呈现；点击仅发送用户选择的口径，不把摘要塞回 prompt -->
                <div v-if="msg.clarify?.options?.length" class="clarify-panel">
                  <div class="clarify-head" data-tip="不同口径会取不同指标定义，选择后继续原问题">
                    <el-icon><Reading /></el-icon>口径差异 · 选一个继续
                  </div>
                  <button v-for="(opt, j) in msg.clarify.options" :key="j" class="clarify-opt"
                          @click="send(opt.metric_id || opt.metric_name)">
                    <span class="co-key">{{ String.fromCharCode(65 + j) }}</span>
                    <span class="co-main">
                      <b>{{ opt.metric_name || opt.metric_id }}</b>
                      <span v-if="opt.definition" class="co-def">{{ opt.definition }}</span>
                    </span>
                    <span v-if="opt.metric_id" class="co-id">{{ opt.metric_id }}</span>
                  </button>
                </div>

                <div v-if="msg.text" class="answer-text">{{ msg.text }}</div>

                <!-- 答案优先（F29）：单行聚合结果直接给 KPI 大数字 -->
                <div v-if="kpisOf(msg).length" class="kpi-row" :class="{ single: kpisOf(msg).length === 1 }">
                  <div v-for="k in kpisOf(msg)" :key="k.label" class="kpi">
                    <div class="kpi-value" :title="k.raw">{{ k.value }}</div>
                    <div class="kpi-label">{{ k.label }}</div>
                  </div>
                </div>

                <!-- 口径可信卡（F71）：数据截止日/口径/来源/脱敏常驻可见 -->
                <TrustCard :result="msg.result" @click="evOpen[i] = true" />

                <!-- 结果呈现（F13：表格 / 图表切换） -->
                <template v-if="msg.result?.rows?.length">
                  <div v-if="chartOf(msg)" class="result-toolbar">
                    <button class="view-btn" :class="{ active: (msg.view || 'table') === 'table' }"
                            data-tip="表格视图" @click="msg.view = 'table'">
                      <el-icon><Document /></el-icon>
                    </button>
                    <button class="view-btn"
                            :class="{ active: !msg.view || msg.view === 'chart' || msg.view === 'bar' }"
                            data-tip="条形图" @click="msg.view = 'bar'">
                      <el-icon><Histogram /></el-icon>
                    </button>
                    <button class="view-btn" :class="{ active: msg.view === 'line' }"
                            data-tip="折线趋势 · 多指标" @click="msg.view = 'line'">
                      <el-icon><TrendCharts /></el-icon>
                    </button>
                    <button v-if="canDonut(msg)" class="view-btn" :class="{ active: msg.view === 'donut' }"
                            data-tip="结构占比" @click="msg.view = 'donut'">
                      <el-icon><PieChart /></el-icon>
                    </button>
                  </div>
                  <ResultTable v-if="(msg.view || 'table') === 'table'"
                               :columns="msg.result.columns"
                               :rows="msg.result.rows"
                               :numeric-columns="msg.result.numeric_columns"
                               :truncated="!!msg.result.truncated"
                               :row-count="msg.result.row_count"
                               v-model:sort="msg.sort"
                               v-model:page="msg.page"
                               @copy="copyTable(msg)" />
                  <div v-if="(msg.view || 'table') !== 'table'" class="chart-wrap">
                    <ChartView :columns="msg.result.columns" :rows="msg.result.rows"
                               :mode="msg.view === 'line' || msg.view === 'donut' ? msg.view : 'bar'" />
                  </div>
                </template>
                <!-- 空结果引导（F73）：0 行时给可操作的排查建议 -->
                <EmptyResultGuide v-else-if="msg.result" :result="msg.result"
                                  @ask-boundary="send('数据到几号')" />
                <!-- 依据折叠（F30）：SQL/口径/行数/耗时默认收起 -->
                <div v-if="msg.result" class="result-foot">
                  <button v-if="hasEvidence(msg)" class="evidence-toggle" :class="{ open: evOpen[i] }"
                          @click="evOpen[i] = !evOpen[i]">
                    <!-- 依据用「图标 + 数字」徽标反馈，说明文字放进 tooltip -->
                    <span class="ev-icon" data-tip="点击查看取数依据"><el-icon><Document /></el-icon></span>
                    <span v-if="msg.result.sql" class="ev-chip mono" data-tip="含 SQL">SQL</span>
                    <span v-if="msg.result.explanation" class="ev-chip" data-tip="生成说明">
                      <el-icon><MagicStick /></el-icon></span>
                    <span v-if="msg.result.assumptions?.length" class="ev-chip" data-tip="口径条数">
                      <el-icon><Reading /></el-icon>{{ msg.result.assumptions.length }}</span>
                    <span v-if="msg.result.sources?.length" class="ev-chip" data-tip="来源条数">
                      <el-icon><DataLine /></el-icon>{{ msg.result.sources.length }}</span>
                    <span v-if="msg.result.data_as_of" class="ev-chip" data-tip="数据截止日（数仓真实最新分区）">
                      <el-icon><Timer /></el-icon>{{ msg.result.data_as_of }}</span>
                    <span v-if="msg.result.row_count > 0" class="ev-chip" data-tip="返回行数">
                      <el-icon><Grid /></el-icon>{{ msg.result.row_count }}<i v-if="msg.result.truncated" class="ev-more">+</i></span>
                    <span v-if="msg.elapsedMs" class="ev-chip mono" data-tip="耗时">{{ fmtMs(msg.elapsedMs) }}</span>
                    <el-icon class="ev-caret"><ArrowDown /></el-icon>
                  </button>
                  <button v-if="msg.result.row_count > 0" class="foot-link" data-tip="导出 CSV" @click="exportCSV(msg)">
                    <el-icon><Download /></el-icon>
                  </button>
                  <button v-if="msg.result.row_count > 0" class="foot-link" data-tip="生成分享报告（可打印/另存 PDF）" @click="shareReport(msg)">
                    <el-icon><Printer /></el-icon>
                  </button>
                  <button v-if="msg.result.sql" class="foot-link" data-tip="申请导出" @click="startExport(msg)">
                    <el-icon><Document /></el-icon>
                  </button>
                </div>
                <div v-if="exportReq && exportReq.msg === msg" class="export-apply">
                  <input v-model="exportReq.reason" maxlength="80" placeholder="导出用途（选填）" />
                  <button class="btn-dark" :disabled="exportBusy" @click="submitExport">提交申请</button>
                  <button class="model-act" @click="exportReq = null">取消</button>
                </div>
                <div v-if="evOpen[i] && hasEvidence(msg)" class="evidence-body">
                  <div v-if="msg.result.explanation" class="ev-window ev-explain">说明 {{ msg.result.explanation }}</div>
                  <div v-if="msg.result.spec_display" class="ev-window">理解 {{ msg.result.spec_display }}</div>
                  <div v-if="msg.result.time_window" class="ev-window">时间窗 {{ msg.result.time_window }}</div>
                  <div v-if="msg.result.data_as_of" class="ev-window">
                    数据截至 {{ msg.result.data_as_of }}
                    <span v-if="msg.result.data_as_of_basis" class="ev-basis">（{{ msg.result.data_as_of_basis }}）</span>
                  </div>
                  <div v-if="msg.result.image_notes" class="ev-window ev-image">
                    <div class="ev-image-head">图片识别（视觉模型，可能有误，关键数字请核对）</div>
                    <div class="ev-image-body">{{ msg.result.image_notes }}</div>
                  </div>
                  <div v-if="msg.result.sources?.length" class="source-row">
                    <span v-for="(s, j) in msg.result.sources" :key="j" class="source-chip"
                          :title="s.detail || s.name">
                      <span class="source-kind">{{ SOURCE_LABEL[s.type] || s.type }}</span>{{ s.name }}
                    </span>
                  </div>
                  <ul v-if="msg.result.assumptions?.length" class="assumptions">
                    <li v-for="(a, j) in msg.result.assumptions" :key="j">{{ a }}</li>
                  </ul>
                  <div v-if="msg.result.memory_notes?.length" class="ev-window">
                    参考了你的 {{ msg.result.memory_notes.length }} 条记忆：{{ msg.result.memory_notes.join('；') }}
                  </div>
                  <div v-if="msg.result.sql" class="code-card">
                    <div class="code-head">
                      <span class="code-lang">SQL</span>
                      <span class="code-note">只读</span>
                      <button class="code-copy" title="复制 SQL" @click="copyText(msg.result.sql)">
                        <el-icon><CopyDocument /></el-icon>复制
                      </button>
                    </div>
                    <pre class="sql-block">{{ msg.result.sql }}</pre>
                  </div>
                </div>

                <!-- 追问建议（U-F07）：点击仍走完整意图识别，不直出 SQL -->
                <div v-if="!msg.running && suggestionsFor(msg).length" class="followups">
                  <span class="fu-label">继续问</span>
                  <button v-for="s in suggestionsFor(msg)" :key="s" class="fu-chip" @click="send(s)">{{ s }}</button>
                </div>

                <!-- 拒答 -->
                <div v-if="msg.reject" class="reject-box">
                  <el-icon style="margin-top:3px"><InfoFilled /></el-icon>
                  <div>
                    <div><span class="code">[{{ msg.reject.reason_code }}]</span> {{ msg.reject.message }}</div>
                    <div v-if="!msg.running" class="reject-actions">
                      <button v-if="msg.reject?.can_request && msg.reject.tables?.length"
                              class="r-act accent" @click="requestAccess(msg.reject.tables[0])">
                        申请该表权限
                      </button>
                      <button class="r-act" @click="rejectAction('range')">数据到几号</button>
                      <button class="r-act" @click="rejectAction('example')">换个示例试试</button>
                      <button class="r-act" @click="rejectAction('help')">哪些问题能问</button>
                    </div>
                  </div>
                </div>

                <!-- 网络异常（F28） -->
                <div v-if="msg.error" class="error-box">
                  <el-icon><WarningFilled /></el-icon>
                  <span>{{ msg.error }}</span>
                  <button class="link" @click="regenerate(msg, i)">重试</button>
                </div>

                <div v-if="msg.stopped" class="stopped-note">已停止生成</div>

                <!-- X-C01 消息级时间戳：常驻可见，完整日期与审计说明放 tooltip -->
                <div v-if="msg.ts" class="msg-stamp mono" :data-tip="fmtDateTime(msg.ts)">{{ fmtClock(msg.ts) }}</div>

                <!-- 消息操作（F25） -->
                <div v-if="!msg.running && (msg.text || msg.result)" class="msg-actions">
                  <button v-if="traceCode(msg)" class="act trace-tag" title="点击复制溯源标记码，可凭它定位这条问答"
                          @click="copyText(traceCode(msg))">
                    {{ traceCode(msg) }}
                  </button>
                  <button class="act" @click="copyAnswer(msg)">
                    <el-icon><CopyDocument /></el-icon> 复制
                  </button>
                  <button v-if="messages[i - 1]?.role === 'user'" class="act"
                          title="把上一轮的提问加入常用问法" @click="addFavorite(messages[i - 1]?.text)">
                    <el-icon><Star /></el-icon> 收藏
                  </button>
                  <button v-if="i === messages.length - 1" class="act" @click="regenerate(msg, i)">
                    <el-icon><Refresh /></el-icon> 重新生成
                  </button>
                </div>

                <!-- 反馈 -->
                <div v-if="(msg.result || msg.reject) && !msg.running" class="feedback-bar">
                  <button class="fb-btn" :class="{ 'on-up': msg.feedback === 'up' }"
                          title="结果准确" @click="clickFeedback(msg, 'up', $event)"><el-icon><CircleCheck /></el-icon></button>
                  <button class="fb-btn" :class="{ 'on-down': msg.feedback === 'down' }"
                          title="结果不对" @click="clickFeedback(msg, 'down', $event)"><el-icon><CircleClose /></el-icon></button>
                </div>
                <!-- A11：本轮自动学习提示（后端异步写入，延迟探测到才显示） -->
                <div v-if="msg.learnedTip" class="learn-tip">
                  <el-icon><MagicStick /></el-icon>
                  <span>
                    已自动学习<template v-if="msg.learnedTip.skills"> {{ msg.learnedTip.skills }} 条技能规范</template><template v-if="msg.learnedTip.skills && msg.learnedTip.memories">、</template><template v-if="msg.learnedTip.memories"> {{ msg.learnedTip.memories }} 条记忆</template>
                  </span>
                  <button class="link" @click="viewLearned(msg.learnedTip)">查看</button>
                </div>
                <div v-if="reasonFor === msg" class="reason-panel">
                  <span style="width:100%;color:var(--muted)">问题类型</span>
                  <button v-for="r in REASONS" :key="r" class="opt" @click="submitReason(r)">{{ r }}</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </el-scrollbar>

      <!-- 回到底部（F31：上翻浏览时出现） -->
      <button v-if="activeView === 'chat' && !atBottom && !showHero" class="jump-bottom"
              title="回到底部" @click="scrollToBottom(true)">
        <el-icon><ArrowDown /></el-icon>
      </button>

      <!-- 输入区（仅对话视图，支持拖拽上传 F22） -->
      <div v-if="activeView === 'chat'" class="composer-zone"
           @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="onDrop($event)">
        <QueuedMessage v-model:text="queuedText" @remove="removeQueued" @insert="takeQueued" />
        <div class="composer" :class="{ dragging: dragOver }">
          <div v-if="pendingFiles.length" class="pending-files">
            <span v-for="(f, idx) in pendingFiles" :key="f.file_id" class="pending-chip">
              {{ fileIcon(f) }} {{ f.name }}
              <button class="chip-x" @click="dropPending(idx)"><el-icon><Close /></el-icon></button>
            </span>
          </div>
          <textarea ref="taRef" v-model="input" rows="1"
                    :placeholder="busy ? '可继续输入，Enter 加入待发送' : '问点什么，或拖入文件'"
                    @keydown="onKeydown" @input="autoResize" />
          <div class="composer-bottom">
            <div class="fav-wrap">
              <button class="attach-btn" title="常用问法" @click.stop="toggleFav">
                <el-icon><Star /></el-icon>
              </button>
              <div v-if="favOpen" class="fav-pop" @click.stop>
                <div class="fav-head">常用问法<span class="fav-hint">本地保存</span></div>
                <div v-if="!favorites.length" class="fav-empty">在回答下方点「收藏」即可加入</div>
                <div v-for="f in favorites" :key="f" class="fav-item">
                  <button class="fav-text" :title="busy ? '回答中，点此加入待发送' : '点击直接重跑'" @click="favRun(f)">
                    {{ f }}
                  </button>
                  <button class="fav-fill" title="仅填入输入框" @click="input = f; favOpen = false; taRef?.focus()">
                    <el-icon><Edit /></el-icon>
                  </button>
                  <button class="fav-del" title="移除" @click="removeFavorite(f)">
                    <el-icon><Close /></el-icon>
                  </button>
                </div>
              </div>
            </div>
            <button v-if="can('files.upload')" class="attach-btn" :disabled="uploading" title="上传文件" @click="pickFile">
              <el-icon><Paperclip /></el-icon>
            </button>
            <input ref="fileInput" type="file" multiple hidden @change="onFileChange" />
            <span class="composer-count mono" data-tip="仅作长度提示，当前无字数上限">{{ input.length }} 字</span>
            <button v-if="busy && !input.trim()" class="send-btn stop" title="停止生成" @click="stop">
              <el-icon><ArrowUpBold style="transform:rotate(180deg)" /></el-icon>
            </button>
            <button v-else-if="busy" class="send-btn insert" title="加入待发送，本轮结束后自动发出" @click="insertQueued">
              <el-icon><Plus /></el-icon><span>待发送</span>
            </button>
            <button v-else class="send-btn" :class="{ labeled: !!input.trim() }"
                    :disabled="!input.trim()" data-tip="Enter 发送 · Shift+Enter 换行" @click="send()">
              <el-icon><ArrowUp /></el-icon><span v-if="input.trim()">发送</span>
            </button>
          </div>
        </div>
        <div v-if="dragOver" class="drop-mask">松开即可上传文件</div>
      </div>

      <!-- 文件预览视图（F34：无弹窗，主区呈现） -->
      <el-scrollbar v-else-if="activeView === 'file' && previewFile" class="thread-scroll">
        <div class="page-view preview-view">
          <div class="preview-head">
            <button v-if="!editing" class="icon-btn" title="返回对话" @click="closePreview">
              <el-icon><ArrowLeft /></el-icon>
            </button>
            <span class="preview-ico">{{ fileIcon(previewFile) }}</span>
            <div class="preview-meta">
              <div class="preview-name">{{ previewFile.name }}</div>
              <div class="preview-sub">
                <span>{{ fmtSize(previewFile.size || 0) }}</span>
                <span v-if="previewData?.sheets?.length">{{ previewData.sheets.length }} 个工作表</span>
                <span v-if="previewFile.version" class="ver-chip">V{{ previewFile.version }}</span>
                <span v-if="previewLoading" class="skel-inline" aria-label="加载中"><i></i><i></i><i></i></span>
              </div>
            </div>
            <template v-if="editing">
              <button class="preview-save" :disabled="saving" @click="saveEdit">
                {{ saving ? '保存中…' : '保存为新版本' }}
              </button>
              <button class="preview-cancel" :disabled="saving" @click="cancelEdit">取消</button>
            </template>
            <button v-else-if="canEdit" class="preview-edit" @click="startEdit">编辑</button>
            <button v-if="!editing" class="model-act" @click="toggleVersions">
              {{ versionsOpen ? '收起版本' : '历史版本' }}
            </button>
            <a class="preview-dl" :href="downloadUrl(previewFile.file_id)" :download="previewFile.name">
              <el-icon><Download /></el-icon> 下载
            </a>
          </div>

          <div v-if="versionsOpen" class="version-panel">
            <div class="version-head">
              版本记录
              <span v-if="versionsLoading" class="sched-q">加载中…</span>
            </div>
            <div v-for="v in versions" :key="v.file_id" class="version-row"
                 :class="{ current: v.file_id === previewFile.file_id }">
              <span class="ver-chip">V{{ v.version }}</span>
              <span class="version-name">{{ v.name }}</span>
              <span class="sched-q">{{ fmtSize(v.size || 0) }} · {{ v.created_at?.slice(0, 16) }}</span>
              <button class="model-act" @click="openPreview(v)">查看</button>
              <button v-if="v.file_id !== previewFile.file_id" class="model-act"
                      @click="compareVersion(v)">对比</button>
              <button v-if="can('files.upload') && v.file_id !== previewFile.file_id" class="model-act"
                      @click="restoreVersion(v)">恢复</button>
            </div>
            <div v-if="!versions.length && !versionsLoading" class="empty-hint">暂无版本记录</div>
          </div>

          <!-- 版本对比：当前版本 vs 选中历史版本 -->
          <div v-if="diffLoading || diffData" class="diff-panel">
            <div class="diff-head">
              <span class="ver-chip">对比</span>
              <span v-if="diffTarget">V{{ diffTarget.version }} → V{{ previewFile.version }}</span>
              <span v-if="diffLoading" class="sched-q">对比中…</span>
              <template v-else-if="diffData">
                <span class="insp-badge fail">{{ diffData.summary.changed_cells }} 处修改</span>
                <span class="insp-badge ok">+{{ diffData.summary.added_rows }} 行</span>
                <span class="insp-badge skip">-{{ diffData.summary.removed_rows }} 行</span>
                <span v-if="diffData.summary.header_changes" class="insp-badge skip">
                  表头 {{ diffData.summary.header_changes }} 处
                </span>
                <span v-if="diffData.summary.truncated" class="sched-q">（变更过多，仅展示前 300 处）</span>
              </template>
              <button class="model-act" @click="diffData = null; diffTarget = null">关闭</button>
            </div>
            <template v-if="diffData">
              <div v-for="s in diffData.sheets" :key="s.name" class="diff-sheet">
                <div class="diff-sheet-head">
                  <b>{{ s.name || '工作表' }}</b>
                  <span class="insp-badge" :class="s.status === 'same' ? 'ok' : 'fail'">
                    {{ DIFF_STATUS[s.status] || s.status }}
                  </span>
                  <span class="sched-q" v-if="s.status === 'modified'">
                    行 {{ s.before_rows }} → {{ s.after_rows }}
                  </span>
                </div>
                <div v-for="(h, i) in s.header_changes" :key="'h' + i" class="diff-row">
                  <span class="diff-cell">{{ h.col }}1</span>
                  <span class="diff-before">{{ h.before || '（空）' }}</span>
                  <span class="diff-arrow">→</span>
                  <span class="diff-after">{{ h.after || '（空）' }}</span>
                </div>
                <div v-for="(c, i) in s.changes" :key="'c' + i" class="diff-row">
                  <span class="diff-cell">{{ c.col }}{{ c.row + 1 }}</span>
                  <span class="diff-before">{{ c.before || '（空）' }}</span>
                  <span class="diff-arrow">→</span>
                  <span class="diff-after">{{ c.after || '（空）' }}</span>
                </div>
                <div v-if="s.status === 'same'" class="empty-hint">该工作表无变化</div>
              </div>
            </template>
          </div>

          <div v-if="editing" class="editor-wrap">
            <div v-if="!editorReady" class="editor-loading skel-block" aria-label="编辑器加载中">
              <i></i><i></i><i></i><i></i>
            </div>
            <UniverSheet ref="editorRef" v-bind="editorProps" @ready="editorReady = true" />
          </div>

          <div v-else-if="previewLoading" class="preview-body skel-block" aria-label="预览加载中">
            <i></i><i></i><i></i><i></i><i></i>
          </div>

          <div v-else-if="previewKind === 'table' && activePreviewSheet" class="preview-body">
            <div v-if="previewData.sheets.length > 1" class="sheet-tabs">
              <button v-for="(s, idx) in previewData.sheets" :key="s.table" class="sheet-tab"
                      :class="{ active: previewSheet === idx }" @click="previewSheet = idx">
                {{ s.sheet || `工作表${idx + 1}` }}
              </button>
            </div>
            <div class="result-table preview-table">
              <table>
                <thead>
                  <tr><th v-for="c in activePreviewSheet.labels" :key="c">{{ c }}</th></tr>
                </thead>
                <tbody>
                  <tr v-for="(row, r) in activePreviewSheet.rows" :key="r">
                    <td v-for="c in activePreviewSheet.columns" :key="c" :title="String(row[c])">
                      {{ row[c] }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="preview-note">
              预览前 {{ activePreviewSheet.rows.length }} 行，共 {{ activePreviewSheet.row_count }} 行
            </div>
          </div>

          <div v-else-if="previewKind === 'image'" class="preview-media">
            <img :src="inlineUrl(previewFile.file_id)" :alt="previewFile.name" />
          </div>
          <iframe v-else-if="previewKind === 'pdf'" class="preview-pdf"
                  :src="inlineUrl(previewFile.file_id)" title="PDF 预览" />

          <div v-else class="preview-empty">
            <div class="panel-empty-graphic"><el-icon><Document /></el-icon></div>
            <div>该格式暂不支持在线预览，请下载查看</div>
          </div>
        </div>
      </el-scrollbar>

      <!-- 设置视图（F21/F37：无弹窗，二级设置页） -->
      <el-scrollbar v-else-if="activeView === 'settings'" class="thread-scroll">
        <div class="page-view settings-view">
          <div class="settings-head">
            <div class="settings-head-ico"><el-icon><Setting /></el-icon></div>
            <div class="settings-head-main">
              <h2 class="settings-title">设置 · {{ settingsTitle }}</h2>
              <!-- 视图性质用图形徽章表达：管理员=锁，普通用户=人 -->
              <span class="view-badge" :class="role">
                <el-icon><Lock v-if="role === 'admin'" /><User v-else /></el-icon>
                {{ role === 'admin' ? '管理视图' : '个人视图' }}
              </span>
            </div>
            <div v-if="llmStatus && role === 'admin'" class="settings-state">
              <span class="state-dot" :class="{ on: llmStatus.available }"></span>
              {{ llmStatus.available ? llmStatus.default_model : '示例模式' }}
            </div>
          </div>

          <!-- 页签按个人 / 管理分组，图标即语义 -->
          <SettingsTabs :tabs="settingsTabs" :active="settingsTab" @select="switchSettings" />

          <!-- 模型与AI -->
          <div v-if="settingsTab === 'model'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><Cpu /></el-icon></span>
                <div>
                  <b>AI 大模型</b>
                </div>
              </div>
              <button v-if="can('llm.manage')" class="btn-dark" @click="newApi">
                <el-icon><Plus /></el-icon> 添加模型
              </button>
            </div>

            <div v-if="showApiForm && can('llm.manage')" class="inline-form">
              <div class="inline-form-title">{{ apiForm.id ? `编辑模型：${apiForm.name}` : '添加模型' }}</div>
              <el-form label-width="90px">
                <el-form-item label="名称"><el-input v-model="apiForm.name" placeholder="如：DeepSeek 官方" /></el-form-item>
                <el-form-item label="Base URL"><el-input v-model="apiForm.base_url" placeholder="https://api.deepseek.com" /></el-form-item>
                <el-form-item label="模型"><el-input v-model="apiForm.model" placeholder="如：deepseek-flash" /></el-form-item>
                <el-form-item label="Key 引用">
                  <el-input v-model="apiForm.api_key_ref"
                            :placeholder="apiForm.id ? '留空表示清除引用' : 'env:LLM_API_KEY'"
                            clearable />
                  <div class="form-hint">存引用不存明文：env:变量名 / file:路径#变量 / kms://路径#字段</div>
                </el-form-item>
                <el-form-item label="启用"><el-switch v-model="apiForm.enabled" /></el-form-item>
              </el-form>
              <div class="inline-form-actions">
                <el-button size="small" @click="showApiForm = false">取消</el-button>
                <el-button size="small" type="primary" @click="submitApi">保存</el-button>
              </div>
            </div>

            <table class="model-table">
              <thead>
                <tr>
                  <th>名称</th><th>Base URL</th><th>模型</th><th>Key 引用</th><th class="ta-r">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in apiList" :key="row.id">
                  <td>
                    <div class="model-name">
                      <b>{{ row.name }}</b>
                      <span v-if="row.is_default" class="pill-current">当前</span>
                      <span v-if="!row.enabled" class="pill-off">已停用</span>
                    </div>
                    <div class="model-provider">{{ hostOf(row.base_url) }}</div>
                  </td>
                  <td class="model-mono model-ellipsis" :title="row.base_url">{{ row.base_url }}</td>
                  <td class="model-mono">{{ row.model }}</td>
                  <td class="model-mono model-key">
                    <div>{{ row.key_masked }}</div>
                    <div class="model-provider" :title="row.key_detail || row.api_key_ref">
                      {{ row.api_key_ref || '未配置' }}
                      <span v-if="row.key_state === 'unresolved'" class="pill-off">不可解析</span>
                    </div>
                  </td>
                  <td>
                    <div v-if="can('llm.manage')" class="model-actions">
                      <button v-if="!row.is_default" class="model-act" @click="makeDefault(row)">
                        <el-icon><CircleCheck /></el-icon>设为当前
                      </button>
                      <button class="model-act" :disabled="testingId === row.id" @click="runTest(row)">
                        <el-icon><Refresh /></el-icon>{{ testingId === row.id ? '测试中…' : '测试连接' }}
                      </button>
                      <button class="model-act" @click="editApi(row)"><el-icon><Edit /></el-icon>编辑</button>
                      <button class="model-act danger" @click="removeApi(row)">
                        <el-icon><Delete /></el-icon>{{ deletingId === row.id ? '确认删除' : '删除' }}
                      </button>
                    </div>
                    <el-icon v-else class="lock-hint" title="仅管理员可管理"><Lock /></el-icon>
                    <div v-if="row.lastTest" class="model-test">{{ row.lastTest }}</div>
                  </td>
                </tr>
                <tr v-if="!apiList.length">
                  <td colspan="5" class="model-empty">
                    <button v-if="can('llm.manage')" class="model-act" @click="newApi">
                      <el-icon><Plus /></el-icon>添加模型
                    </button>
                    <span v-else>—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 数据源 -->
          <div v-else-if="settingsTab === 'datasource'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><DataLine /></el-icon></span>
                <div><b>数据源</b></div>
              </div>
            </div>
            <div class="ds-list">
              <div class="ds-row">
                <b>dwd_shop</b>
                <div class="ds-tags">
                  <span class="pill-neutral">MySQL 8.0</span>
                  <span class="pill-neutral">数仓</span>
                  <span class="pill-lock"><el-icon><Lock /></el-icon>只读</span>
                </div>
              </div>
              <div class="ds-row">
                <b>datapro</b>
                <div class="ds-tags">
                  <span class="pill-neutral">MySQL</span>
                  <span class="pill-neutral">业务库</span>
                </div>
              </div>
              <div class="ds-row">
                <b>dp_uploads</b>
                <div class="ds-tags">
                  <span class="pill-neutral">MySQL</span>
                  <span class="pill-neutral">临时</span>
                  <span class="pill-neutral">24h</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 外观风格 -->
          <SettingsAppearance v-else-if="settingsTab === 'appearance'" :theme="theme"
                              @update:theme="setTheme" />

          <!-- 账户与统计 -->
          <SettingsAccount v-else-if="settingsTab === 'account'"
                           :username="authUser?.username || ''"
                           :display-name="displayName"
                           :role="role" :role-name="roleName"
                           :abilities="abilities" :users="userList"
                           @navigate="openView" @logout="doLogout" />

          <!-- S-F05 最小用量页：真实 query_record 计数，不含套餐/计费 -->
          <div v-else-if="settingsTab === 'usage'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><DataAnalysis /></el-icon></span>
                <div>
                  <b>用量</b>
                  <span class="faint-cell" data-tip="内部工具用量统计，不作为计费账单">近 30 天 · 按账号隔离</span>
                </div>
              </div>
              <button v-if="role === 'admin'" class="model-act"
                      @click="usageAll = !usageAll; loadUsage()">
                {{ usageAll ? '只看本人' : '看全部账号' }}
              </button>
              <button class="model-act" @click="loadUsage">
                <el-icon><Refresh /></el-icon>刷新
              </button>
            </div>
            <div class="usage-grid">
              <div class="usage-cell">
                <b>{{ usage?.total ?? '—' }}</b><span>近 30 天问答</span>
              </div>
              <div class="usage-cell">
                <b>{{ usage?.today ?? '—' }}</b><span>今日问答</span>
              </div>
              <div class="usage-cell">
                <b>{{ usage ? Math.round((usage.by_outcome?.result || 0) / (usage.total || 1) * 100) + '%' : '—' }}</b>
                <span>成功结果占比</span>
              </div>
              <div class="usage-cell">
                <b>{{ usage?.scope === 'all' ? '全部账号' : '当前账号' }}</b><span>统计范围</span>
              </div>
            </div>
            <div v-if="usage?.daily?.length" class="usage-trend">
              <ChartView :columns="['date', 'count']" :rows="usage.daily" mode="bar" />
            </div>
            <div v-if="usage" class="usage-outcomes">
              <span v-for="(n, k) in usage.by_outcome" :key="k" class="usage-chip">{{ k }} · {{ n }}</span>
            </div>
          </div>

          <!-- U-B05 数据表权限申请：越权 E003 后自助开通，管理员在此审批 -->
          <div v-else-if="settingsTab === 'access'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><Key /></el-icon></span>
                <div>
                  <b>数据表权限申请</b>
                  <span class="faint-cell">被 E003 拦截后在这里申请；审批通过按有效期自动授权，到期自动失效</span>
                </div>
              </div>
              <button class="model-act" @click="loadAccess">
                <el-icon><Refresh /></el-icon>刷新
              </button>
            </div>

            <div class="skill-form">
              <div class="skill-form-row">
                <label>数据表
                  <select v-model="accessForm.table_name">
                    <option value="">选择要申请的数据表</option>
                    <!-- E003 预填的表名可能在目录加载前就写入，先给一个占位选项保住选择 -->
                    <option v-if="accessForm.table_name && !accessCatalog.some(t => t.table_name === accessForm.table_name)"
                            :value="accessForm.table_name">{{ accessForm.table_name }}</option>
                    <option v-for="t in accessCatalog" :key="t.table_name" :value="t.table_name">
                      {{ t.table_name }}{{ t.cn_name ? '（' + t.cn_name + '）' : '' }}
                    </option>
                  </select>
                </label>
                <label>有效期（天）
                  <input v-model.number="accessForm.duration_days" type="number" min="1" max="365" />
                </label>
              </div>
              <div class="skill-form-row">
                <label>申请理由
                  <input v-model="accessForm.reason" maxlength="120" placeholder="说明用途，便于管理员判断" />
                </label>
                <button class="btn-dark" :disabled="accessBusy === 'submit'" @click="submitAccess">
                  提交申请
                </button>
              </div>
            </div>

            <div class="sched-row sched-head">
              <span>申请</span><span>数据表 / 有效期</span><span>状态</span><span>审批</span>
            </div>
            <div v-for="r in accessMine" :key="r.id" class="sched-row">
              <div class="sched-main">
                <b>{{ r.id }}</b>
                <span class="sched-q">{{ r.reason || '无用途说明' }}</span>
              </div>
              <div class="sched-plan">
                {{ r.table_name }}<br />
                <span class="sched-q">{{ r.expires_at || '未授权' }}</span>
              </div>
              <div class="sched-plan">
                <span class="insp-badge" :class="accessStatus(r.status).cls">
                  {{ accessStatus(r.status).text }}
                </span>
              </div>
              <div class="sched-acts">
                <span class="sched-q">{{ r.review_note || r.reviewed_by || '—' }}</span>
              </div>
            </div>
            <div v-if="!accessMine.length" class="empty-hint">暂无申请</div>

            <template v-if="role === 'admin'">
              <div class="sched-row sched-head">
                <span>待审批</span><span>申请人</span><span>数据表</span><span>操作</span>
              </div>
              <div v-for="r in accessPending" :key="r.id" class="sched-row">
                <div class="sched-main">
                  <b>{{ r.id }} · {{ r.requester }}</b>
                  <span class="sched-q">{{ r.reason || '无用途说明' }}</span>
                </div>
                <div class="sched-plan">
                  {{ r.table_name }}<br />
                  <span class="sched-q">申请 {{ r.duration_days }} 天</span>
                </div>
                <div class="sched-plan">
                  <span class="sched-q">{{ r.created_at?.slice(5, 16) }}</span>
                </div>
                <div class="sched-acts">
                  <input v-model="accessNotes[r.id]" class="export-note" maxlength="40"
                         placeholder="审批备注（选填）" />
                  <button class="model-act" :disabled="accessBusy === r.id"
                          @click="decideAccess(r.id, 'approve')">通过</button>
                  <button class="model-act danger" :disabled="accessBusy === r.id"
                          @click="decideAccess(r.id, 'reject')">驳回</button>
                </div>
              </div>
              <div v-if="!accessPending.length" class="empty-hint">暂无待审批申请</div>
            </template>
          </div>

          <!-- 权限与角色 -->
          <div v-else-if="settingsTab === 'role'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><Lock /></el-icon></span>
                <div><b>权限与角色</b></div>
              </div>
              <button v-if="can('users.manage')" class="model-act" @click="startUserCreate()">
                <el-icon><Plus /></el-icon>新增用户
              </button>
            </div>
            <div class="settings-row">
              <span>登录账号</span>
              <b>{{ authUser?.username }}</b>
            </div>
            <div class="settings-row">
              <span>角色</span>
              <span class="pill-current">{{ roleName }}</span>
            </div>
            <div class="settings-row">
              <span>登录状态</span>
              <button class="model-act" @click="doLogout">
                <el-icon><SwitchButton /></el-icon>退出登录
              </button>
            </div>

            <template v-if="can('users.manage')">
              <div v-if="userForm" class="skill-form user-form">
                <div class="skill-form-row">
                  <label>账号
                    <input v-model="userForm.username" :disabled="!!userForm.id" maxlength="32"
                           placeholder="字母/数字/下划线，2-32 位" />
                  </label>
                  <label>显示名
                    <input v-model="userForm.display_name" maxlength="32" placeholder="留空同账号" />
                  </label>
                </div>
                <div class="skill-form-row">
                  <label>密码
                    <input v-model="userForm.password" type="password" maxlength="64"
                           :placeholder="userForm.id ? '留空表示不修改' : '至少 6 位'" />
                  </label>
                  <label>角色
                    <select v-model="userForm.role">
                      <option value="user">普通用户</option>
                      <option value="admin">管理员</option>
                    </select>
                  </label>
                  <label v-if="userForm.id">状态
                    <select v-model="userForm.status">
                      <option value="active">启用</option>
                      <option value="disabled">禁用</option>
                    </select>
                  </label>
                </div>
                <div v-if="userForm.id" class="skill-form-row">
                  <label>权限
                    <select v-model="userForm.permMode">
                      <option value="inherit">继承角色默认</option>
                      <option value="custom">自定义覆盖</option>
                    </select>
                  </label>
                </div>
                <div v-if="userForm.id && userForm.permMode === 'custom'" class="perm-checks">
                  <label v-for="p in permDefs" :key="p.code" class="perm-check">
                    <input type="checkbox" :value="p.code" v-model="userForm.permissions" />
                    {{ p.label }}
                  </label>
                </div>
                <!-- 用户级数据表权限（A02/S1）：默认继承全局白名单，可收紧到勾选的表；管理员不受限 -->
                <div v-if="userForm.id && userForm.role !== 'admin'" class="skill-form-row">
                  <label>数据表权限
                    <select v-model="tableAccess.inherit">
                      <option :value="true">继承全部表</option>
                      <option :value="false">仅限勾选表</option>
                    </select>
                  </label>
                  <span v-if="tableAccess.loading" class="faint-cell">加载中…</span>
                  <span v-else-if="tableAccess.failed" class="faint-cell">
                    表权限读取失败，保存将不改动原配置
                  </span>
                  <span v-else-if="!tableAccess.inherit" class="faint-cell">
                    已选 {{ tableAccess.tables.length }} / {{ metaTables.length }} 张
                  </span>
                </div>
                <div v-if="userForm.id && userForm.role !== 'admin' && !tableAccess.inherit"
                     class="perm-checks table-access-list">
                  <label v-for="t in metaTables" :key="t.table_name" class="perm-check">
                    <input type="checkbox" :checked="tableAccess.tables.includes(t.table_name.toLowerCase())"
                           @change="toggleTableAccess(t.table_name.toLowerCase())" />
                    {{ t.cn_name || t.table_name }}
                    <span class="faint-cell">{{ t.table_name }}</span>
                  </label>
                  <div v-if="!metaTables.length" class="faint-cell">暂无元数据表</div>
                  <template v-if="extraAccessTables.length">
                    <div class="faint-cell" style="width:100%;margin-top:6px">其他已授权表（不在当前元数据中）</div>
                    <label v-for="t in extraAccessTables" :key="t" class="perm-check">
                      <input type="checkbox" checked @change="toggleTableAccess(t)" />
                      {{ t }}
                    </label>
                  </template>
                </div>
                <div class="user-form-actions">
                  <button class="btn-dark"
                          :disabled="userSaving || (userForm.id && tableAccess.loading)"
                          @click="submitUser">保存</button>
                  <button class="preview-cancel" @click="userForm = null">取消</button>
                </div>
              </div>

              <table class="model-table">
                <thead>
                  <tr>
                    <th>账号</th>
                    <th>显示名</th>
                    <th>角色</th>
                    <th>状态</th>
                    <th>最近登录</th>
                    <th class="ta-r">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="u in userList" :key="u.id">
                    <td><b>{{ u.username }}</b></td>
                    <td>{{ u.display_name }}</td>
                    <td>{{ u.role === 'admin' ? '管理员' : '普通用户' }}</td>
                    <td>
                      <span :class="u.status === 'active' ? 'pill-neutral' : 'pill-lock'">
                        {{ u.status === 'active' ? '启用' : '禁用' }}
                      </span>
                    </td>
                    <td class="faint-cell">{{ (u.last_login_at || '').replace('T', ' ') || '—' }}</td>
                    <td>
                      <div class="model-actions">
                        <button class="model-act" @click="startUserEdit(u)">
                          <el-icon><Edit /></el-icon>编辑
                        </button>
                        <button class="model-act danger" @click="removeUser(u)">
                          <el-icon><Delete /></el-icon>{{ userDeletingId === u.id ? '确认删除' : '删除' }}
                        </button>
                      </div>
                    </td>
                  </tr>
                  <tr v-if="!userList.length">
                    <td colspan="6" class="faint-cell">暂无用户</td>
                  </tr>
                </tbody>
              </table>
            </template>

            <!-- 权限矩阵：管理员固定全权，普通用户默认权限可调整 -->
            <div v-if="role === 'admin'" class="perm-matrix">
              <div class="settings-subhead">
                <b>角色权限矩阵</b>
                <span class="faint-cell">管理员固定全权</span>
              </div>
              <table class="model-table">
                <thead>
                  <tr>
                    <th>权限点</th>
                    <th>分组</th>
                    <th>管理员</th>
                    <th>普通用户</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="p in permDefs" :key="p.code">
                    <td><b>{{ p.label }}</b><br /><span class="faint-cell">{{ p.code }}</span></td>
                    <td>{{ p.group }}</td>
                    <td><el-icon><CircleCheck /></el-icon></td>
                    <td>
                      <input type="checkbox" :checked="rolePerms.user.includes(p.code)"
                             @change="toggleRolePerm(p.code)" />
                    </td>
                  </tr>
                </tbody>
              </table>
              <div class="user-form-actions perm-actions">
                <button class="btn-dark" :disabled="permSaving" @click="saveRolePerms">保存矩阵</button>
                <span class="faint-cell">保存后该角色用户需重新登录生效</span>
              </div>
            </div>
          </div>

          <!-- 对话记录（管理后台） -->
          <div v-else-if="settingsTab === 'records'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><Document /></el-icon></span>
                <div><b>对话记录</b><span class="faint-cell">取数记录 + 全部消息（含闲聊/澄清）</span></div>
              </div>
              <button class="model-act" @click="loadRecords">
                <el-icon><Refresh /></el-icon>刷新
              </button>
            </div>
            <div class="skill-form-row">
              <label>用户
                <input v-model="recordFilters.username" placeholder="账号" @keyup.enter="searchRecords" />
              </label>
              <label>天数
                <input v-model.number="recordFilters.days" type="number" min="0" max="365" />
              </label>
              <label>反馈
                <select v-model="recordFilters.rating">
                  <option value="">全部</option>
                  <option value="up">👍 准确</option>
                  <option value="down">👎 不对</option>
                </select>
              </label>
              <label>结果
                <select v-model="recordFilters.outcome">
                  <option v-for="[k, v] in OUTCOME_OPTIONS" :key="k" :value="k">{{ v }}</option>
                </select>
              </label>
              <label>错误码
                <select v-model="recordFilters.error_code">
                  <option v-for="[k, v] in ERROR_OPTIONS" :key="k" :value="k">{{ v }}</option>
                </select>
              </label>
              <label>关键词
                <input v-model="recordFilters.keyword" placeholder="问题包含…" @keyup.enter="searchRecords" />
              </label>
              <button class="model-act" @click="searchRecords">查询</button>
            </div>
            <table class="model-table">
              <thead>
                <tr><th>时间</th><th>来源</th><th>用户</th><th>内容</th><th>行数</th><th>结果</th><th>反馈</th></tr>
              </thead>
              <tbody>
                <tr v-for="r in recordList" :key="r.source + '-' + r.id" @click="toggleRecordDetail(r)"
                    style="cursor: pointer;">
                  <td class="faint-cell">{{ r.created_at }}</td>
                  <td>{{ r.source === 'query' ? '取数' : (r.kind || '消息') }}</td>
                  <td>{{ r.username || '—' }}</td>
                  <td>{{ r.source === 'query' ? r.question : (r.question || r.answer) }}</td>
                  <td>{{ r.row_count ?? '—' }}</td>
                  <td>
                    <span v-if="r.outcome" class="outcome-tag" :class="'oc-' + r.outcome">
                      {{ OUTCOME_LABEL[r.outcome] || r.outcome }}
                    </span>
                    <span v-if="r.error_code" class="code faint-cell">{{ r.error_code }}</span>
                    <span v-if="!r.outcome && !r.error_code">—</span>
                  </td>
                  <td>{{ r.feedback === 'up' ? '👍' : r.feedback === 'down' ? '👎' : '—' }}</td>
                </tr>
                <tr v-if="!recordLoading && !recordList.length">
                  <td colspan="7" class="faint-cell">暂无记录</td>
                </tr>
              </tbody>
            </table>
            <div v-if="recordDetail" class="skill-form" style="margin-top: 12px;">
              <div class="faint-cell">
                <template v-if="recordDetail.source === 'query'">
                  DP-{{ String(recordDetail.id).padStart(4, '0') }}
                </template>
                <template v-else>消息 #{{ recordDetail.id }} · {{ recordDetail.kind }}</template>
                · {{ recordDetail.session_id }}
              </div>
              <div v-if="recordDetail.question_context?.length" class="faint-cell">
                上下文：{{ recordDetail.question_context.join(' / ') }}
              </div>
              <pre v-if="recordDetail.sql" style="white-space: pre-wrap; font-size: 12px; margin: 8px 0;">{{ recordDetail.sql }}</pre>
              <div v-if="recordDetail.answer">{{ recordDetail.answer }}</div>
            </div>
            <div class="user-form-actions" style="justify-content: space-between;">
              <span class="faint-cell">共 {{ recordTotal }} 条 · 第 {{ recordPage }} 页</span>
              <span>
                <button class="model-act" :disabled="recordPage <= 1" @click="changeRecordPage(-1)">上一页</button>
                <button class="model-act" :disabled="recordPage * recordPageSize >= recordTotal"
                        @click="changeRecordPage(1)">下一页</button>
              </span>
            </div>
          </div>

          <!-- 账户与统计 -->
          <div v-else-if="settingsTab === 'ops'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><Monitor /></el-icon></span>
                <div><b>运维处置</b><span class="faint-cell">口径裁决（D03）· 会话回收（S07）· 缓存（U-B03）</span></div>
              </div>
              <button class="model-act" @click="loadRulings">
                <el-icon><Refresh /></el-icon>刷新
              </button>
            </div>
            <div class="ops-grid">
              <div class="ops-block">
                <div class="ops-title">口径裁决</div>
                <div class="ops-hint">同一术语对应多个口径时，裁决后按指定口径取数，不再反复澄清。</div>
                <div class="skill-form-row">
                  <label>术语<input v-model="rulingForm.term" placeholder="如：销售额" /></label>
                  <label>指标 ID<input v-model="rulingForm.metric_id" placeholder="如：M004" /></label>
                  <label>说明<input v-model="rulingForm.note" placeholder="选填" /></label>
                  <button class="model-act" @click="submitRuling">保存裁决</button>
                </div>
                <table class="model-table">
                  <thead><tr><th>术语</th><th>指标</th><th>说明</th><th>裁决人</th><th></th></tr></thead>
                  <tbody>
                    <tr v-for="r in rulings" :key="r.term">
                      <td>{{ r.term }}</td>
                      <td class="model-mono">{{ r.metric_id }}</td>
                      <td class="faint-cell">{{ r.note || '—' }}</td>
                      <td>{{ r.by || '—' }}</td>
                      <td><button class="model-act danger" @click="removeRuling(r.term)">删除</button></td>
                    </tr>
                    <tr v-if="!rulings.length"><td colspan="5" class="model-empty">暂无裁决</td></tr>
                  </tbody>
                </table>
              </div>
              <div class="ops-block">
                <div class="ops-title">会话回收</div>
                <div class="ops-hint">
                  清理超过 TTL 无活动的会话临时表与数据库账号；先标记过期再删除，避免误删在用会话。
                </div>
                <button class="btn-dark" @click="runSweep">立即回收</button>
                <pre v-if="sweepResult" class="ops-result">{{ JSON.stringify(sweepResult, null, 2) }}</pre>
              </div>
              <div class="ops-block">
                <div class="ops-title">限定范围缓存</div>
                <div class="ops-hint">
                  只缓存意图分类与口径问答，<b>取数结果永不缓存</b>；键里含资料指纹与身份，资料或权限一变自动不命中。
                </div>
                <div v-if="cacheStats" class="cache-grid">
                  <div class="cache-cell"><span>后端</span><b>{{ cacheStats.backend }}</b></div>
                  <div class="cache-cell"><span>命中率</span><b>{{ Math.round((cacheStats.hit_rate || 0) * 100) }}%</b></div>
                  <div class="cache-cell"><span>命中 / 未命中</span><b>{{ cacheStats.hits }} / {{ cacheStats.misses }}</b></div>
                  <div class="cache-cell"><span>写入 / 异常</span><b>{{ cacheStats.sets }} / {{ cacheStats.errors }}</b></div>
                  <div class="cache-cell"><span>TTL（意图 / 口径）</span>
                    <b>{{ cacheStats.ttl?.intent }}s / {{ cacheStats.ttl?.meta }}s</b></div>
                  <div class="cache-cell"><span>状态</span>
                    <b>{{ cacheStats.enabled ? '已启用' : '未启用（mock 模式或开关关闭）' }}</b></div>
                </div>
                <div class="ops-actions">
                  <button class="model-act" @click="loadCache"><el-icon><Refresh /></el-icon>刷新</button>
                  <button class="model-act" @click="purgeCache('intent')">清意图缓存</button>
                  <button class="model-act" @click="purgeCache('meta')">清口径缓存</button>
                  <button class="model-act danger" @click="purgeCache('')">全部清空</button>
                </div>
              </div>
            </div>
          </div>

          <div v-else class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><User /></el-icon></span>
                <div><b>账户与统计</b></div>
              </div>
            </div>
            <div class="settings-row"><span>登录账号</span><b>{{ authUser?.username }}</b></div>
            <div class="settings-row"><span>当前角色</span><b>{{ roleName }}</b></div>
            <div class="settings-row"><span>本地会话</span><b>{{ sessions.length }} 个</b></div>
            <div class="settings-row"><span>模型状态</span><b>{{ llmStatus?.available ? llmStatus.default_model : '示例模式' }}</b></div>
            <div class="settings-row"><span>版本</span><b>DataPro v0.1</b></div>
          </div>
        </div>
      </el-scrollbar>

      <!-- 技能（F38）：可复用问法 -->
      <el-scrollbar v-else-if="activeView === 'help'" class="thread-scroll">
        <div class="page-view settings-view">
          <div class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><QuestionFilled /></el-icon></span>
                <div><b>使用帮助</b><span class="faint-cell">提问公式 · 数据新鲜度 · 常见问题</span></div>
              </div>
            </div>
            <div class="help-grid">
              <div v-for="(f, idx) in HELP_FAQ" :key="idx" class="help-card">
                <div class="help-q">{{ f.q }}</div>
                <div class="help-a">{{ f.a }}</div>
              </div>
            </div>
            <div class="help-foot">
              需要更详细说明？联系数据团队或在「对话记录」里用溯源标记码定位具体问答。
            </div>
          </div>
        </div>
      </el-scrollbar>

      <el-scrollbar v-else-if="activeView === 'skills'" class="thread-scroll">
        <div class="page-view settings-view">
          <div class="settings-head">
            <div class="settings-head-ico"><el-icon><MagicStick /></el-icon></div>
            <div class="settings-head-main"><h2 class="settings-title">技能</h2></div>
            <button v-if="can('skills.manage') && skillTab === 'skills'" class="btn-dark" @click="startSkillEdit()">
              <el-icon><Plus /></el-icon> 新增技能
            </button>
          </div>

          <div class="settings-tabs">
            <button class="settings-tab" :class="{ active: skillTab === 'skills' }"
                    @click="switchSkillTab('skills')">技能</button>
            <button v-if="can('skills.manage')" class="settings-tab"
                    :class="{ active: skillTab === 'learned' }"
                    @click="switchSkillTab('learned')">
              技能规范<span v-if="learnedSkills.length" class="tab-n">{{ learnedSkills.length }}</span>
            </button>
            <button v-if="can('skills.manage')" class="settings-tab"
                    :class="{ active: skillTab === 'memory' }"
                    @click="switchSkillTab('memory')">
              记忆<span v-if="memories.length" class="tab-n">{{ memories.length }}</span>
            </button>
          </div>

          <div v-if="skillTab === 'skills' && skillForm" class="model-card tab-panel">
            <div class="skill-form">
              <label>名称
                <input v-model="skillForm.name" maxlength="24" placeholder="如：支付订单数" />
              </label>
              <label>发送内容
                <input v-model="skillForm.question" maxlength="80" placeholder="如：数据率有多少数据；点击技能卡即发送这句" />
              </label>
              <label>回复内容
                <textarea v-model="skillForm.answer" rows="3" maxlength="500"
                          placeholder="预设回复，填写后命中该内容直接回复，不再执行取数" />
              </label>
              <label>说明
                <input v-model="skillForm.description" maxlength="60" placeholder="可选" />
              </label>
              <div class="skill-form-foot">
                <button class="btn-dark" @click="submitSkill">保存</button>
                <button class="model-act" @click="skillForm = null">取消</button>
              </div>
            </div>
          </div>

          <div v-if="skillTab === 'skills'" class="skill-grid">
            <div v-for="s in skills" :key="s.id" class="skill-card" :class="{ off: !s.enabled }">
              <div class="skill-card-top">
                <span class="skill-name">{{ s.name }}</span>
                <span class="skill-badge">{{ s.answer ? '预设回复' : (s.enabled ? '启用' : '停用') }}</span>
              </div>
              <div class="skill-q" title="用这个问法发起对话"
                   @click="openView('chat'); send(s.question)">{{ s.question }}</div>
              <div v-if="s.answer" class="skill-desc skill-answer">{{ s.answer }}</div>
              <div v-else class="skill-desc">{{ s.description }}</div>
              <div v-if="can('skills.manage')" class="skill-acts">
                <button class="model-act" @click="toggleSkill(s)">{{ s.enabled ? '停用' : '启用' }}</button>
                <button class="model-act" @click="startSkillEdit(s)">编辑</button>
                <button class="model-act danger" @click="removeSkill(s)">
                  {{ pendingDelKey === 'skill:' + s.id ? '确认删除' : '删除' }}
                </button>
              </div>
            </div>
            <div v-if="!skills.length" class="empty-hint">暂无技能</div>
          </div>

          <!-- 自动沉淀的技能规范（A11）：成功取数后由 AI 判断是否值得复用 -->
          <div v-if="skillTab === 'learned'" class="model-card tab-panel">
            <div class="learning-head">
              <b>自动学习的技能规范</b>
              <span class="faint-cell">成功取数后由 AI 判断沉淀；仅本人可见与复用，可停用或删除</span>
            </div>
            <div class="learning-tools">
              <input v-model="learnedQ" class="learning-search" maxlength="40"
                     placeholder="搜索规范 / 问法 / 口径" />
              <span class="learning-stat">共 {{ learnedStat.total }} 条 · 启用 {{ learnedStat.on }}</span>
              <div v-if="role === 'admin'" class="learning-scope">
                <button :class="{ on: learningScope === 'mine' }" @click="switchLearningScope('mine')">我的</button>
                <button :class="{ on: learningScope === 'all' }" @click="switchLearningScope('all')">全部用户</button>
              </div>
            </div>
            <div v-for="s in shownLearned" :key="s.id" class="learning-row"
                 :class="{ off: !s.enabled }">
              <div class="learning-main">
                <b>{{ s.name }}</b>
                <span class="sched-q">{{ s.question }}</span>
                <span v-if="s.sql_template" class="learning-sql" :class="{ open: learnOpen[s.id] }"
                      :title="learnOpen[s.id] ? '点击收起' : '点击查看完整 SQL'"
                      @click="toggleLearnOpen(s.id)">{{ s.sql_template }}</span>
                <span v-if="s.notes" class="sched-q">注意：{{ s.notes }}</span>
              </div>
              <div class="learning-meta">
                <span v-if="learningAll" class="insp-badge">{{ s.username }}</span>
                <span v-if="s.caliber" class="insp-badge">{{ s.caliber }}</span>
                <span class="sched-q">{{ s.updated_at }}</span>
                <span class="sched-q">命中 {{ s.hit }} 次</span>
                <span class="insp-badge" :class="s.enabled ? 'ok' : 'skip'">{{ s.enabled ? '启用' : '停用' }}</span>
              </div>
              <div class="sched-acts">
                <button v-if="can('skills.manage')" class="model-act" @click="toggleLearned(s)">
                  {{ s.enabled ? '停用' : '启用' }}
                </button>
                <button v-if="can('skills.manage')" class="model-act danger" @click="removeLearned(s)">
                  {{ pendingDelKey === 'learned:' + s.id ? '确认删除' : '删除' }}
                </button>
              </div>
            </div>
            <div v-if="!shownLearned.length && !learningLoading" class="empty-hint">
              {{ learnedSkills.length ? '没有匹配的规范' : '暂无自动沉淀的规范：多问几次常规取数问题后会自动学习' }}
            </div>
          </div>

          <!-- 自动记忆（A11）：AI 判断值得长期记住的偏好/术语/规则/口径 -->
          <div v-if="skillTab === 'memory'" class="model-card tab-panel">
            <div class="learning-head">
              <b>长期记忆</b>
              <span class="faint-cell">AI 自动判断后记录；仅本人生效，可停用或删除</span>
            </div>
            <div class="learning-tools">
              <input v-model="memoryQ" class="learning-search" maxlength="40"
                     placeholder="搜索记忆内容 / 来源" />
              <span class="learning-stat">共 {{ memoryStat.total }} 条 · 启用 {{ memoryStat.on }}</span>
              <div v-if="role === 'admin'" class="learning-scope">
                <button :class="{ on: learningScope === 'mine' }" @click="switchLearningScope('mine')">我的</button>
                <button :class="{ on: learningScope === 'all' }" @click="switchLearningScope('all')">全部用户</button>
              </div>
            </div>
            <div class="learning-types">
              <button v-for="t in memoryTypes" :key="t.k" class="learn-chip"
                      :class="{ on: memoryType === t.k }" @click="memoryType = t.k">
                {{ t.label }} {{ t.n }}
              </button>
            </div>
            <div v-for="m in shownMemories" :key="m.id" class="learning-row"
                 :class="{ off: !m.enabled }">
              <div class="learning-main">
                <span class="learning-content" :class="{ clamp: !learnOpen[m.id] }"
                      :title="learnOpen[m.id] ? '点击收起' : '点击展开全文'"
                      @click="toggleLearnOpen(m.id)">{{ m.content }}</span>
                <span class="sched-q">
                  <template v-if="learningAll">{{ m.username }} · </template>
                  来源：{{ m.source || '—' }} · 置信度 {{ fmtConfidence(m.confidence) }}
                  <template v-if="m.hit"> · 重复提到 {{ m.hit }} 次</template>
                  · {{ m.updated_at }}
                </span>
              </div>
              <div class="learning-meta">
                <span class="insp-badge">{{ MEMORY_TYPES[m.type] || m.type }}</span>
                <span class="insp-badge" :class="m.enabled ? 'ok' : 'skip'">{{ m.enabled ? '启用' : '停用' }}</span>
              </div>
              <div class="sched-acts">
                <button v-if="can('skills.manage')" class="model-act" @click="toggleMemory(m)">
                  {{ m.enabled ? '停用' : '启用' }}
                </button>
                <button v-if="can('skills.manage')" class="model-act danger" @click="removeMemory(m)">
                  {{ pendingDelKey === 'memory:' + m.id ? '确认删除' : '删除' }}
                </button>
              </div>
            </div>
            <div v-if="!shownMemories.length && !learningLoading" class="empty-hint">
              {{ memories.length ? '没有匹配的记忆' : '暂无记忆：说“以后默认…”“我们把这个叫…”这类约定后会自动记住' }}
            </div>
          </div>
        </div>
      </el-scrollbar>

      <!-- 定时任务（F39）：到点自动执行并记录结果 -->
      <el-scrollbar v-else-if="activeView === 'schedules'" class="thread-scroll">
        <div class="page-view settings-view">
          <div class="settings-head">
            <div class="settings-head-ico"><el-icon><Timer /></el-icon></div>
            <div class="settings-head-main"><h2 class="settings-title">定时任务</h2></div>
            <button v-if="can('schedules.manage')" class="btn-dark" @click="startScheduleEdit()">
              <el-icon><Plus /></el-icon> 新建任务
            </button>
          </div>

          <div v-if="scheduleForm" class="model-card tab-panel">
            <div class="skill-form">
              <label>任务名称
                <input v-model="scheduleForm.name" maxlength="24" placeholder="如：每日支付订单数" />
              </label>
              <label>取数问题
                <input v-model="scheduleForm.question" maxlength="80" placeholder="如：查昨天的支付订单数" />
              </label>
              <div class="skill-form-row">
                <label>频率
                  <select v-model="scheduleForm.freq">
                    <option value="daily">每天</option>
                    <option value="weekly">每周</option>
                  </select>
                </label>
                <label v-if="scheduleForm.freq === 'weekly'">星期
                  <select v-model.number="scheduleForm.weekday">
                    <option v-for="(w, i) in WEEKDAYS" :key="w" :value="i + 1">周{{ w }}</option>
                  </select>
                </label>
                <label>时间
                  <input v-model="scheduleForm.time" type="time" />
                </label>
              </div>
              <div class="skill-form-foot">
                <button class="btn-dark" @click="submitSchedule">保存</button>
                <button class="model-act" @click="scheduleForm = null">取消</button>
              </div>
            </div>
          </div>

          <div class="model-card tab-panel">
            <div class="sched-row sched-head">
              <span>任务</span><span>计划</span><span>上次运行</span><span>操作</span>
            </div>
            <div v-for="t in schedules" :key="t.id" class="sched-row">
              <div class="sched-main">
                <b>{{ t.name }}</b>
                <span class="sched-q">
                  {{ t.question }}<template v-if="role === 'admin'"> · {{ t.owner || '—' }}</template>
                </span>
              </div>
              <div class="sched-plan">
                {{ t.freq === 'weekly' ? '每周' + WEEKDAYS[t.weekday - 1] : '每天' }} {{ t.time }}
                <span v-if="!t.enabled" class="skill-badge">停用</span>
              </div>
              <div class="sched-last">
                {{ t.last_run_at ? t.last_run_at.slice(5, 16) + ' · ' + statusText(t.last_status) : '—' }}
              </div>
              <div class="sched-acts">
                <button v-if="can('schedules.manage')" class="model-act" :disabled="scheduleRunning === t.id"
                        @click="runNow(t)">{{ scheduleRunning === t.id ? '运行中' : '运行' }}</button>
                <button v-if="can('schedules.manage')" class="model-act" @click="toggleSchedule(t)">
                  {{ t.enabled ? '停用' : '启用' }}
                </button>
                <button v-if="can('schedules.manage')" class="model-act" @click="startScheduleEdit(t)">编辑</button>
                <button class="model-act" @click="runOpen = runOpen === t.id ? '' : t.id">记录</button>
                <button v-if="can('schedules.manage')" class="model-act danger" @click="removeSchedule(t)">
                  {{ pendingDelKey === 'sched:' + t.id ? '确认删除' : '删除' }}
                </button>
              </div>
              <div v-if="runOpen === t.id" class="run-list">
                <div v-for="(r, i) in (t.runs || [])" :key="i" class="run-item">
                  <span>{{ r.ts.slice(5, 16) }}</span>
                  <span>{{ statusText(r.status) }}</span>
                  <span v-if="r.status === 'ok'">返回 {{ r.row_count ?? 0 }} 行</span>
                  <span v-else class="run-err">{{ r.error }}</span>
                </div>
                <div v-if="!(t.runs || []).length" class="empty-hint">暂无运行记录</div>
              </div>
            </div>
            <div v-if="!schedules.length" class="empty-hint">暂无定时任务</div>
          </div>
        </div>
      </el-scrollbar>

      <!-- 新媒体行业知识库：术语/指标定义与口径 -->
      <el-scrollbar v-else-if="activeView === 'knowledge'" class="thread-scroll">
        <div class="page-view settings-view">
          <div class="settings-head">
            <div class="settings-head-ico"><el-icon><Reading /></el-icon></div>
            <div class="settings-head-main"><h2 class="settings-title">知识库</h2></div>
            <div class="kb-search">
              <input v-model="kbQuery" placeholder="搜索术语 / 别名 / 分类"
                     @keydown.enter="loadKnowledge(kbQuery)" />
              <button class="model-act" title="搜索" @click="loadKnowledge(kbQuery)">
                <el-icon><Search /></el-icon>
              </button>
            </div>
            <button v-if="can('knowledge.manage')" class="btn-dark" @click="startKbEdit()">
              <el-icon><Plus /></el-icon> 新增词条
            </button>
          </div>

          <div v-if="knowledgeForm" class="model-card tab-panel">
            <div class="skill-form">
              <div class="skill-form-row">
                <label>术语
                  <input v-model="knowledgeForm.term" maxlength="20" placeholder="如：完播率" />
                </label>
                <label>分类
                  <input v-model="knowledgeForm.category" maxlength="12" placeholder="如：内容质量" />
                </label>
              </div>
              <label>别名
                <input v-model="knowledgeForm.aliasesText" maxlength="60" placeholder="多个用、分隔，如：完整播放率" />
              </label>
              <label>定义
                <textarea v-model="knowledgeForm.definition" rows="2" maxlength="200" placeholder="一句话解释该指标" />
              </label>
              <label>计算口径
                <textarea v-model="knowledgeForm.formula" rows="2" maxlength="160" placeholder="如：完播率 = 完播次数 / 播放次数 × 100%" />
              </label>
              <div class="skill-form-foot">
                <button class="btn-dark" @click="submitKb">保存</button>
                <button class="model-act" @click="knowledgeForm = null">取消</button>
              </div>
            </div>
          </div>

          <div class="kb-cats">
            <button class="kb-chip" :class="{ active: !kbCategory }" @click="kbCategory = ''">
              全部 <b>{{ knowledge.length }}</b>
            </button>
            <button v-for="c in kbCategories" :key="c.name" class="kb-chip"
                    :class="{ active: kbCategory === c.name }"
                    @click="kbCategory = kbCategory === c.name ? '' : c.name">
              {{ c.name }} <b>{{ c.count }}</b>
            </button>
          </div>

          <div class="kb-list">
            <div v-for="k in kbVisible" :key="k.id" class="kb-row" :class="{ open: kbOpen.has(k.id) }">
              <button class="kb-row-head" @click="toggleKb(k.id)">
                <span class="kb-term">{{ k.term }}</span>
                <span class="skill-badge">{{ k.category }}</span>
                <span v-if="k.aliases?.length" class="kb-alias">{{ k.aliases.join('、') }}</span>
                <span class="kb-brief">{{ k.definition }}</span>
                <el-icon class="kb-caret"><ArrowDown /></el-icon>
              </button>
              <div v-if="kbOpen.has(k.id)" class="kb-body">
                <div class="kb-def">{{ k.definition }}</div>
                <div v-if="k.formula" class="kb-formula">{{ k.formula }}</div>
                <div v-if="can('knowledge.manage')" class="skill-acts">
                  <button class="model-act" @click="startKbEdit(k)">编辑</button>
                  <button class="model-act danger" @click="removeKb(k)">
                    {{ pendingDelKey === 'kb:' + k.id ? '确认删除' : '删除' }}
                  </button>
                </div>
              </div>
            </div>
            <div v-if="!kbVisible.length" class="empty-hint">暂无词条</div>
          </div>

          <!-- 文档知识库（RAG）：长文本切段检索，回答里给出章节级引用 -->
          <div class="kb-section">
            <div class="kb-section-head">
              <h3 class="kb-section-title">文档知识库</h3>
              <span class="kb-section-sub">
                {{ docList.length }} 篇
                <template v-if="docStats">· {{ docStats.chunks }} 段 · 已向量化 {{ docStats.embedded }} 段</template>
                <b v-if="docStats && !docStats.embedding_available" class="kb-warn">embedding 未配置，仅按字面检索</b>
              </span>
              <div class="kb-doc-search">
                <input v-model="docQuery" placeholder="用真实问题预览命中"
                       @keydown.enter="runDocSearch" />
                <button class="model-act" title="检索预览" @click="runDocSearch">
                  <el-icon><Search /></el-icon>
                </button>
              </div>
              <label v-if="can('knowledge.manage')" class="btn-dark" :class="{ disabled: docBusy }">
                <el-icon><Upload /></el-icon> {{ docBusy ? '处理中…' : '上传文档' }}
                <input type="file" accept=".md,.txt,.html,.htm" hidden :disabled="docBusy"
                       @change="handleDocUpload" />
              </label>
            </div>

            <div v-if="docHits" class="kb-doc-hits">
              <div class="kb-doc-hits-head">
                命中 {{ docHits.hits.length }} 段
                <span class="kb-hits-th">阈值 {{ docHits.threshold }}</span>
                <button class="model-act" @click="docHits = null">关闭</button>
              </div>
              <div v-for="(h, i) in docHits.hits" :key="i" class="kb-doc-hit">
                <span class="kb-score" :class="{ low: h.score < docHits.threshold }">{{ h.score.toFixed(2) }}</span>
                <div class="kb-hit-main">
                  <div class="kb-hit-src">{{ h.title }}<i v-if="h.section"> § {{ h.section }}</i></div>
                  <p class="kb-hit-text">{{ h.text }}</p>
                </div>
              </div>
              <div v-if="!docHits.hits.length" class="empty-hint">无命中：该问题在知识库中没有依据</div>
            </div>

            <div class="kb-list">
              <div v-for="d in docList" :key="d.id" class="kb-row" :class="{ open: docOpen.has(d.id) }">
                <button class="kb-row-head" @click="toggleDoc(d.id)">
                  <span class="kb-term">{{ d.title }}</span>
                  <span class="skill-badge">{{ d.category || '通用' }}</span>
                  <span class="kb-alias">{{ d.chunk_count }} 段 · {{ (d.char_count / 1000).toFixed(1) }}k 字</span>
                  <span class="kb-brief">{{ d.source || '手动录入' }}</span>
                  <span v-if="!d.embed_model" class="kb-tag-warn">未向量化</span>
                  <el-icon class="kb-caret"><ArrowDown /></el-icon>
                </button>
                <div v-if="docOpen.has(d.id)" class="kb-body">
                  <div class="kb-def">
                    来源 {{ d.source || '—' }}<i v-if="d.embed_model"> · 向量模型 {{ d.embed_model }}</i>
                    · 更新于 {{ d.updated_at || '—' }}
                  </div>
                  <div v-if="can('knowledge.manage')" class="skill-acts">
                    <button class="model-act" :disabled="docBusy" @click="handleReindex(d)">重建索引</button>
                    <button class="model-act danger" @click="removeDoc(d)">
                      {{ pendingDelKey === 'doc:' + d.id ? '确认删除' : '删除' }}
                    </button>
                  </div>
                </div>
              </div>
              <div v-if="!docList.length" class="empty-hint">
                暂无文档。上传 .md / .txt / .html，系统会切分、向量化，回答时按段落引用
              </div>
            </div>
          </div>
        </div>
      </el-scrollbar>

      <!-- 巡检（A08/F16）：规则状态 + 最近结果 -->
      <el-scrollbar v-else-if="activeView === 'inspections'" class="thread-scroll">
        <div class="page-view settings-view">
          <div class="settings-head">
            <div class="settings-head-ico"><el-icon><Monitor /></el-icon></div>
            <div class="settings-head-main"><h2 class="settings-title">巡检</h2></div>
            <button v-if="can('inspections.manage')" class="btn-dark" :disabled="inspectionRunning"
                    @click="runInspectionNow">
              <el-icon><Refresh /></el-icon>{{ inspectionRunning ? '巡检中' : '立即巡检' }}
            </button>
          </div>

          <div class="model-card tab-panel">
            <div class="sched-row sched-head"><span>规则</span><span>检查</span><span>状态</span><span>说明</span></div>
            <div v-for="r in inspectionRules" :key="r.id" class="sched-row">
              <div class="sched-main"><b>{{ r.id }} · {{ r.table }}</b></div>
              <div class="sched-plan">{{ CHECK_TEXT[r.check] || r.check }}</div>
              <div class="sched-plan">
                <span class="insp-badge" :class="r.enabled ? 'ok' : 'skip'">{{ r.enabled ? '启用' : '停用' }}</span>
              </div>
              <div class="sched-acts">
                <span class="sched-q">{{ r.desc || '' }}</span>
                <button v-if="can('inspections.manage')" class="model-act"
                        @click="toggleInspectionRule(r)">{{ r.enabled ? '停用' : '启用' }}</button>
              </div>
            </div>
            <div v-if="!inspectionRules.length" class="empty-hint">暂无巡检规则</div>
          </div>

          <div class="model-card tab-panel">
            <div class="sched-row sched-head"><span>时间</span><span>结果</span><span>触发</span><span>操作</span></div>
            <div v-for="(run, idx) in inspectionHistory" :key="run.ts + idx" class="sched-row">
              <div class="sched-main"><b>{{ run.ts }}</b></div>
              <div class="sched-plan">
                <span class="insp-badge" :class="run.failed ? 'fail' : 'ok'">
                  {{ run.failed ? run.failed + ' 条异常' : '全部通过' }}
                </span>
              </div>
              <div class="sched-last">{{ run.triggered_by || '—' }} · {{ run.duration_ms }}ms</div>
              <div class="sched-acts">
                <button class="model-act" @click="inspectionOpen = inspectionOpen === run.ts ? '' : run.ts">明细</button>
              </div>
              <div v-if="inspectionOpen === run.ts" class="run-list">
                <div v-for="it in run.items" :key="it.rule_id" class="run-item">
                  <span>{{ it.rule_id }}</span>
                  <span class="insp-badge" :class="inspectionStatus(it.status).cls">{{ inspectionStatus(it.status).text }}</span>
                  <span class="insp-msg">{{ it.message }}</span>
                </div>
              </div>
            </div>
            <div v-if="!inspectionHistory.length" class="empty-hint">暂无巡检记录</div>
          </div>
        </div>
      </el-scrollbar>

      <!-- 导出申请与审批（B13/F11） -->
      <el-scrollbar v-else-if="activeView === 'exports'" class="thread-scroll">
        <div class="page-view settings-view">
          <div class="settings-head">
            <div class="settings-head-ico"><el-icon><Download /></el-icon></div>
            <div class="settings-head-main"><h2 class="settings-title">{{ role === 'admin' ? '导出审批' : '导出记录' }}</h2></div>
            <button class="btn-dark" @click="loadExports">
              <el-icon><Refresh /></el-icon>刷新
            </button>
          </div>

          <div class="model-card tab-panel">
            <div class="sched-row sched-head">
              <span>申请</span><span>申请人 / 时间</span><span>状态</span><span>操作</span>
            </div>
            <div v-for="rec in exportsList" :key="rec.id" class="sched-row">
              <div class="sched-main">
                <b>{{ rec.id }} · {{ rec.question || '未命名查询' }}</b>
                <span class="sched-q">{{ rec.reason || '无用途说明' }}</span>
              </div>
              <div class="sched-plan">
                {{ rec.requester }}<br /><span class="sched-q">{{ rec.created_at?.slice(5, 16) }}</span>
              </div>
              <div class="sched-plan">
                <span class="insp-badge" :class="exportStatus(rec.status).cls">
                  {{ exportStatus(rec.status).text }}
                </span>
                <span v-if="rec.row_count != null" class="sched-q"> {{ rec.row_count }} 行</span>
              </div>
              <div class="sched-acts">
                <template v-if="role === 'admin' && rec.status === 'pending'">
                  <input v-model="exportNotes[rec.id]" class="export-note" maxlength="40"
                         placeholder="审批备注（选填）" />
                  <button class="model-act" :disabled="exportDeciding === rec.id"
                          @click="decideExport(rec, 'approve')">通过</button>
                  <button class="model-act danger" :disabled="exportDeciding === rec.id"
                          @click="decideExport(rec, 'reject')">驳回</button>
                </template>
                <template v-else-if="rec.status === 'approved' && rec.file">
                  <button class="model-act" @click="saveExportedFile(rec)">下载</button>
                </template>
                <span v-else class="sched-q">{{ rec.error || rec.review_note || '—' }}</span>
              </div>
            </div>
            <div v-if="!exportsList.length" class="empty-hint">暂无导出申请</div>
          </div>
        </div>
      </el-scrollbar>
    </main>

    <!-- 右侧小侧边栏（F22-F24，Codex 式辅助面板） -->
    <aside v-if="activeView === 'chat' && panelOpen" class="right-panel">
      <div class="panel-head">
        <button class="panel-tab" :class="{ active: panelTab === 'files' }"
                @click="panelTab = 'files'; loadFiles()">
          <el-icon><Document /></el-icon> 文件 <span class="badge">{{ files.length }}</span>
        </button>
        <button class="panel-tab" :class="{ active: panelTab === 'search' }" @click="panelTab = 'search'">
          <el-icon><Search /></el-icon> 搜索
        </button>
        <button v-if="panelPreview" class="panel-tab" :class="{ active: panelTab === 'preview' }"
                @click="panelTab = 'preview'">
          <el-icon><Reading /></el-icon> 预览
        </button>
        <button class="panel-close" title="关闭（Esc）" @click="togglePanel()"><el-icon><Close /></el-icon></button>
      </div>

      <!-- 文件 Tab：本会话上传 + 生成 -->
      <div v-if="panelTab === 'files'" class="panel-body">
        <button v-if="can('files.upload')" class="panel-upload" :disabled="uploading" @click="pickFile">
          {{ uploading ? '上传中' : '上传文件' }}
        </button>
        <div class="panel-filters">
          <button v-for="sc in fileScopes" :key="sc.k" class="scope-chip"
                  :class="{ active: fileScope === sc.k }" @click="fileScope = sc.k">
            {{ sc.label }}<b>{{ sc.n }}</b>
          </button>
        </div>
        <input v-if="files.length" v-model="fileQ" class="panel-search" placeholder="搜索文件名" />
        <div v-if="!shownFiles.length" class="panel-empty">
          <div class="panel-empty-graphic"><el-icon><Document /></el-icon></div>
          <div>{{ files.length ? '没有匹配的文件' : '拖入文件或点击上传' }}</div>
        </div>
        <div v-for="f in shownFiles" :key="f.file_id" class="file-item"
             :title="`${f.name} · ${f.created_at.slice(0, 16).replace('T', ' ')}`">
          <span class="file-ico">{{ fileIcon(f) }}</span>
          <div class="file-info">
            <div class="file-name clickable" title="点击预览" @click="openPreview(f)">{{ f.name }}</div>
            <div class="file-sub">
              <span class="file-tag" :class="f.origin">{{ f.origin === 'generated' ? '生成' : f.origin === 'edited' ? '编辑' : '上传' }}</span>
              <span class="file-stat">{{ fmtSize(f.size) }}</span>
              <span v-if="f.parsed" class="file-parsed">{{ f.parsed.row_count }}×{{ f.parsed.col_count }}</span>
              <span v-if="f.parsed?.tables?.length > 1" class="file-parsed">{{ f.parsed.tables.length }}表</span>
              <span v-if="f.parse_error" class="file-err" :title="f.parse_error">!</span>
              <span v-if="f.secret_hits?.length" class="file-secret"
                    :title="`内容疑似包含密钥/凭据（${f.secret_hits.map(h => h.rule).join('、')}），建议删除并轮换`">密</span>
            </div>
          </div>
          <a class="file-act" :href="downloadUrl(f.file_id)" :download="f.name" title="下载">
            <el-icon><Download /></el-icon>
          </a>
          <button v-if="can('files.delete')" class="file-act"
                  :class="{ danger: confirmFileId === f.file_id }"
                  :title="confirmFileId === f.file_id ? '再点一次确认删除' : '删除'"
                  @click="removeFile(f)">
            <el-icon><Close /></el-icon>
            <span v-if="confirmFileId === f.file_id" class="file-del-hint">确认</span>
          </button>
        </div>
      </div>

      <!-- 搜索 Tab：检索本会话并定位 -->
      <div v-else-if="panelTab === 'search'" class="panel-body">
        <input v-model="searchQ" class="panel-search" placeholder="搜索内容，或粘贴标记码 DP-0001 回车定位"
               @keydown.enter="lookupTrace" />
        <div v-if="traceHit" class="trace-hit">
          <div class="trace-hit-head">
            <span class="trace-code-pill">{{ traceHit.code }}</span>
            <span class="trace-hit-time">{{ traceHit.created_at }}</span>
          </div>
          <div class="search-role">问题</div>
          <div class="search-text">{{ traceHit.question }}</div>
          <template v-if="traceHit.answer">
            <div class="search-role">回答</div>
            <div class="search-text">{{ traceHit.answer.slice(0, 160) }}</div>
          </template>
          <button class="model-act" @click="copyText(traceHit.question)">复制问题</button>
        </div>
        <div v-if="searchQ && !searchResults.length && !traceHit" class="panel-empty">没有匹配内容</div>
        <div v-for="r in searchResults" :key="r.i" class="search-item" @click="jumpTo(r.i)">
          <div class="search-role">{{ r.role === 'user' ? '我' : 'DataPro' }}</div>
          <div class="search-text" v-html="highlight(r.text.slice(0, 140))"></div>
        </div>
      </div>

      <!-- 预览 Tab：对话中点附件的紧凑预览（不打断对话；主区仍停留在消息流） -->
      <div v-else-if="panelTab === 'preview'" class="panel-body panel-preview">
        <template v-if="panelPreview">
          <div class="pp-head">
            <span class="pp-ico">{{ fileIcon(panelPreview) }}</span>
            <div class="pp-meta">
              <div class="pp-name" :title="panelPreview.name">{{ panelPreview.name }}</div>
              <div class="pp-sub">
                {{ fmtSize(panelPreview.size || 0) }}
                <span v-if="panelPreviewData?.sheets?.length">· {{ panelPreviewData.sheets.length }} 个工作表</span>
              </div>
            </div>
            <button class="model-act" title="在主区打开完整预览（可编辑、看版本）"
                    @click="openPreview(panelPreview)">主区打开</button>
          </div>

          <div v-if="panelPreviewLoading" class="preview-body skel-block" aria-label="预览加载中">
            <i></i><i></i><i></i><i></i>
          </div>

          <template v-else>
            <div v-if="panelPreviewKind === 'table' && activePanelSheet" class="pp-table">
              <div v-if="panelPreviewData.sheets.length > 1" class="sheet-tabs">
                <button v-for="(s, idx) in panelPreviewData.sheets" :key="s.table" class="sheet-tab"
                        :class="{ active: panelPreviewSheet === idx }" @click="panelPreviewSheet = idx">
                  {{ s.sheet || `工作表${idx + 1}` }}
                </button>
              </div>
              <div class="result-table preview-table">
                <table>
                  <thead>
                    <tr><th v-for="c in activePanelSheet.labels" :key="c">{{ c }}</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(row, r) in activePanelSheet.rows.slice(0, 50)" :key="r">
                      <td v-for="c in activePanelSheet.columns" :key="c" :title="String(row[c])">
                        {{ row[c] }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="pp-note">
                前 {{ Math.min(activePanelSheet.rows.length, 50) }} 行 · 共 {{ activePanelSheet.row_count }} 行
              </div>
            </div>
            <img v-else-if="panelPreviewKind === 'image'" class="pp-img"
                 :src="inlineUrl(panelPreview.file_id)" :alt="panelPreview.name" />
            <iframe v-else-if="panelPreviewKind === 'pdf'" class="pp-pdf"
                    :src="inlineUrl(panelPreview.file_id)" title="PDF 预览" />
            <div v-else class="panel-empty">该格式暂不支持在线预览，请下载查看</div>
          </template>
        </template>
        <div v-else class="panel-empty">在对话中点击文件，即可在此预览</div>
      </div>
    </aside>

    <!-- 右侧栏常驻开合把手：面板关闭时贴屏幕右缘，打开时贴面板左缘 -->
    <button v-if="activeView === 'chat'" class="panel-rail" :class="{ open: panelOpen }"
            :title="panelOpen ? '收起侧栏' : '展开文件与搜索'"
            :aria-label="panelOpen ? '收起侧栏' : '展开文件与搜索'"
            :aria-expanded="panelOpen" @click="togglePanel()">
      <el-icon><ArrowRight v-if="panelOpen" /><ArrowLeft v-else /></el-icon>
    </button>
  </div>
</template>
