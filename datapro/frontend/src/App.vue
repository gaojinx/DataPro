<script setup>
import { computed, defineAsyncComponent, nextTick, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowLeft, ArrowUp, ArrowUpBold, ChatDotSquare, CircleCheck, CircleClose, Close, CopyDocument, Cpu, DataAnalysis, DataLine, Delete, Document, Download, Edit, Grid, InfoFilled, Key, Lock, Menu, Moon, Paperclip, Plus, Refresh, Search, Setting, Sunny, SwitchButton, User, WarningFilled } from '@element-plus/icons-vue'
import { sendFeedback, streamChat } from './api/chat'
import { deleteApi, fetchApis, fetchLlmStatus, saveApi, setDefaultApi, testApi } from './api/admin'
import { deleteFile, downloadUrl, fetchPreview, inlineUrl, listFiles, lockFile, unlockFile, uploadFile } from './api/files'
import { getUser, logout as apiLogout } from './api/auth'
import LoginView from './components/LoginView.vue'

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

// ---- 会话管理（localStorage 持久化） ----
const STORAGE_KEY = 'dp_sessions_v1'
const sessions = ref([])            // [{id, title, ts, snapshot}]
const activeId = ref('')
const sessionId = ref('')
const sidebarOpen = ref(false)
const input = ref('')
const busy = ref(false)

function loadSessions() {
  try { sessions.value = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') } catch { sessions.value = [] }
}
function saveSessions() {
  // 最多保留 30 个会话
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions.value.slice(0, 30)))
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
  INTENT: '理解意图', CLARIFY: '澄清确认', RECALL: '检索数据资产', GENERATE: '生成查询',
  VALIDATE: '校验语句', EXECUTE: '执行查询', PRESENT: '整理结果', DONE: '完成',
}

// ---- 登录态（B12：Token + 角色由后端签发） ----
const authUser = ref(getUser())
const role = computed(() => authUser.value?.role === 'admin' ? 'admin' : 'user')
const roleName = computed(() => role.value === 'admin' ? '管理员' : '普通用户')
const displayName = computed(() => authUser.value?.display_name || authUser.value?.username || '未登录')

function onLogin(user) {
  authUser.value = user
  loadLlmStatus()
  if (user.role === 'admin') loadApis()
}

async function doLogout() {
  controller.value?.abort()
  accountMenuOpen.value = false
  await apiLogout()
  authUser.value = null
  sessions.value = []
  messages.value = []
  pendingFiles.value = []
  files.value = []
  sessionId.value = ''
  activeId.value = ''
  localStorage.removeItem(STORAGE_KEY)
  sidebarOpen.value = false
}

const activeTitle = computed(() => {
  if (activeView.value === 'settings') return '设置'
  return sessions.value.find(s => s.id === activeId.value)?.title || '新对话'
})
const showHero = computed(() => !busy.value && messages.value.length === 0)

const HERO_CARDS = [
  { t: '查昨天的支付订单数', b: '口径 M003', icon: DataAnalysis },
  { t: '本周每个品类的销量', b: '分组汇总', icon: Grid },
  { t: '支付订单数的口径是什么', b: '元数据', icon: InfoFilled },
  { t: '把订单价格都改一下', b: '越界演示', icon: CircleClose, danger: true },
]

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
const scrollToBottom = async (force = false) => {
  await nextTick()
  if (!force && !nearBottom()) return
  if (scrollRef.value) scrollRef.value.setScrollTop(999999)
  atBottom.value = true
}

onMounted(() => {
  applyTheme()
  loadSessions()
  if (authUser.value) loadLlmStatus()   // 顶栏/composer 模型 chip
  window.addEventListener('keydown', onGlobalKey)
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.account-wrap')) accountMenuOpen.value = false
  })
})

function newChat() {
  snapshotSession()
  // 前端先生成会话 id，保证上传文件与会话一一对应（后端复用该 id）
  const id = 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
  sessionId.value = id
  activeId.value = id
  activeView.value = 'chat'
  messages.value = []
  pendingFiles.value = []
  files.value = []
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
  if (busy.value) return
  snapshotSession()
  activeId.value = id
  sessionId.value = id
  activeView.value = 'chat'
  const s = sessions.value.find(x => x.id === id)
  messages.value = s ? JSON.parse(JSON.stringify(s.snapshot)) : []
  pendingFiles.value = []
  sidebarOpen.value = false
  if (panelOpen.value) loadFiles()
  scrollToBottom()
}

function deleteSession(id, e) {
  e?.stopPropagation()
  // 两步确认，避免误删（3 秒后复位）
  if (confirmDelId.value !== id) {
    confirmDelId.value = id
    setTimeout(() => { if (confirmDelId.value === id) confirmDelId.value = '' }, 3000)
    return
  }
  confirmDelId.value = ''
  const wasActive = id === activeId.value
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

// ---- 主流程（支持中断） ----
const controller = ref(null)

async function send(preset) {
  const content = (preset ?? input.value).trim()
  if (!content || busy.value) return
  const atts = pendingFiles.value.splice(0)
  input.value = ''
  busy.value = true
  messages.value.push({ role: 'user', text: content, attachments: atts })

  const msg = { role: 'assistant', steps: [], running: true }
  messages.value.push(msg)
  // 关键：取回响应式代理引用，直接用 msg 修改属性会绕过 Proxy 导致 DOM 不更新
  const tracked = messages.value[messages.value.length - 1]
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
      signal: controller.value.signal,
      onEvent: (ev) => handleEvent(ev, tracked, t0),
    })
  } catch (err) {
    if (err.name === 'AbortError') tracked.stopped = true
    else tracked.error = err.message || '网络异常，请重试'
  } finally {
    tracked.running = false
    busy.value = false
    controller.value = null
    autoResize()
    snapshotSession()
    if (panelOpen.value && panelTab.value === 'files') loadFiles()
    await scrollToBottom()
  }
}

function stop() {
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
      break
    case 'result':
      msg.result = ev
      msg.text = ev.summary
      msg.queryId = ev.query_id        // B08：反馈回写 query_record
      if (t0) msg.elapsedMs = Math.round(performance.now() - t0)   // F31 结果耗时
      break
    case 'reject':
      msg.reject = ev
      break
    case 'escalate':
      msg.reject = { reason_code: 'E010', message: '本次请求已转人工处理，数据团队会跟进。' }
      break
    case 'error':
      msg.reject = { reason_code: ev.reason_code || 'E010', message: ev.message || '服务异常' }
      if (ev.session_id) sessionId.value = ev.session_id
      break
  }
  scrollToBottom()
}

// ---- 反馈（👎 需选原因，对应章程反馈闭环） ----
const REASONS = ['数据不对', '口径不对', '没听懂我的问题', '结果不完整']
const reasonFor = ref(null)   // 需要选原因的 msg 引用

function clickFeedback(msg, rating) {
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
const flashIdx = ref(-1)
const fileInput = ref()
const dragOver = ref(false)

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

async function openPreview(f) {
  if (!f?.file_id) return
  previewFile.value = f
  previewData.value = null
  previewSheet.value = 0
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
  () => EDITABLE_EXTS.includes((previewFile.value?.ext || '').toLowerCase())
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
  const ext = previewFile.value.ext
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
    const meta = await uploadFile({ sessionId: lockOwner(), file, origin: 'edited' })
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
  try {
    await deleteFile(f.file_id)
    await loadFiles()
  } catch (e) { ElMessage.error(e.message) }
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// ---- 图表可视化（F13）：自动识别标签列 + 数值列，纯 CSS 条形图 ----
const NUM_RE = /^-?\d+(\.\d+)?$/
function toNum(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && NUM_RE.test(v.trim())) return Number(v)
  return null
}

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

// ---- F29 答案优先：单行聚合结果提取 KPI ----
function fmtNum(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return String(v ?? '—')
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
function fmtMs(ms) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
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

// 是否有可折叠依据（SQL / 口径假设）
const hasEvidence = (msg) => !!(msg.result?.sql || msg.result?.assumptions?.length)

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
async function copyAnswer(msg) {
  const text = [msg.text, msg.result?.sql].filter(Boolean).join('\n\n')
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch { ElMessage.error('复制失败，请手动选择文本') }
}

async function regenerate(msg, i) {
  if (busy.value) return
  const prevUser = [...messages.value.slice(0, i)].reverse().find(m => m.role === 'user')
  if (!prevUser) return
  messages.value.splice(i, 1)
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
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
}

// ---- 功能视图化（F21，Codex 式：无弹窗，功能在主区以页面视图呈现） ----
const activeView = ref('chat')   // chat | settings
const llmStatus = ref(null)
const apiList = ref([])
const showApiForm = ref(false)
const apiForm = ref({ id: null, name: '', base_url: '', model: '', api_key: '', enabled: true })
const testingId = ref(null)
const deletingId = ref(null)     // 内联两步删除：第一次点变"确认删除"，再点确认

function openView(view) {
  activeView.value = view
  if (view === 'settings') {
    loadLlmStatus()
    if (role.value === 'admin') loadApis()
  }
}

// ---- 二级设置页（参考 Codex/WorkBuddy 设置中心：横向分组 + 卡片式内容） ----
const settingsTab = ref('model')   // datasource | model | appearance | role | account
const SETTINGS_TABS = [
  { k: 'datasource', label: '数据源' },
  { k: 'model', label: '模型与AI' },
  { k: 'appearance', label: '外观风格' },
  { k: 'role', label: '权限与角色' },
  { k: 'account', label: '账户与统计' },
]
const settingsTitle = computed(
  () => SETTINGS_TABS.find(t => t.k === settingsTab.value)?.label || '设置')

// ---- 账户二级菜单：管理员专属入口在头像菜单内，点击后进入主区视图 ----
const accountMenuOpen = ref(false)
function openAccountView(tab) {
  accountMenuOpen.value = false
  settingsTab.value = tab
  openView('settings')
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
  apiForm.value = { id: row.id, name: row.name, base_url: row.base_url, model: row.model, api_key: '', enabled: row.enabled }
  showApiForm.value = true
}

function newApi() {
  apiForm.value = { id: null, name: '', base_url: '', model: '', api_key: '', enabled: true }
  showApiForm.value = true
}

async function submitApi() {
  const f = apiForm.value
  if (!f.name || !f.base_url || !f.model || (!f.id && !f.api_key)) {
    ElMessage.warning('请填写完整信息' + (f.id ? '（Key 留空表示不修改）' : ''))
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
    <!-- 左侧会话栏（Codex 式：品牌/新对话/日期分组/账户区） -->
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">DP</div>
        <div class="name">DataPro</div>
        <div class="ver">v0.1</div>
      </div>
      <button class="new-chat-btn" @click="newChat">
        <el-icon><Plus /></el-icon> 新对话
      </button>
      <div class="nav-list">
        <div class="nav-item" :class="{ active: activeView === 'chat' }" @click="openView('chat')">
          <el-icon><ChatDotSquare /></el-icon> 对话
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
          <div v-if="accountMenuOpen" class="account-menu" @click.stop>
            <div class="am-group">通用</div>
            <button class="am-item" @click="openAccountView('appearance')">
              <el-icon><Sunny /></el-icon>外观风格
            </button>
            <button class="am-item" @click="openAccountView('account')">
              <el-icon><User /></el-icon>账户与统计
            </button>
            <template v-if="role === 'admin'">
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
            </template>
          </div>
        </transition>
        <div class="account-bar" :class="{ open: accountMenuOpen }">
          <button class="account-main" @click.stop="accountMenuOpen = !accountMenuOpen">
            <div class="account-info">
              <div class="account-avatar"><el-icon><User /></el-icon></div>
              <div class="account-meta">
                <div class="account-name">
                  {{ displayName }}<span v-if="role === 'admin'" class="admin-dot" title="管理员" />
                </div>
                <div class="account-role">{{ roleName }}</div>
              </div>
            </div>
            <el-icon class="account-chevron"><ArrowUp /></el-icon>
          </button>
          <button class="account-logout" title="退出登录" @click.stop="doLogout">
            <el-icon><SwitchButton /></el-icon>
          </button>
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
          <el-dropdown trigger="click" @visible-change="onModelMenu">
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
          <div class="title">{{ activeTitle }}</div>
        </div>
        <div class="status">
          <span class="chip green">只读模式</span>
          <span class="chip">{{ busy ? '处理中…' : '就绪' }}</span>
          <button class="icon-btn" :class="{ active: panelOpen }" title="文件与搜索（Ctrl+K）"
                  @click="togglePanel()">
            <el-icon><Document /></el-icon>
          </button>
          <button class="icon-btn" title="导出 Markdown" @click="exportMarkdown">
            <el-icon><Download /></el-icon>
          </button>
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
          <div class="big-logo">DP</div>
          <h2>有什么数据可以帮你？</h2>
          <div class="hero-badges">
            <span class="hero-badge"><i class="bdot"></i>只读查询</span>
            <span class="hero-badge"><i class="bdot"></i>口径优先</span>
            <span class="hero-badge"><i class="bdot"></i>全程留痕</span>
          </div>
          <div class="hero-cards">
            <div v-for="c in HERO_CARDS" :key="c.t" class="hero-card" @click="send(c.t)">
              <div class="hero-card-head">
                <span class="hero-card-icon" :class="{ danger: c.danger }"><el-icon><component :is="c.icon" /></el-icon></span>
                <span class="hero-card-badge" :class="{ danger: c.danger }">{{ c.b }}</span>
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
                      :class="{ clickable: a.file_id }" @click="openPreview(a)">
                  {{ fileIcon(a) }} {{ a.name }}
                </span>
              </div>
            </div>
            <div v-else class="content-wrap" style="display:flex;gap:12px">
              <div class="avatar">DP</div>
              <div class="content">
                <!-- 处理过程 -->
                <div v-if="msg.steps?.length" class="process">
                  <button v-if="!msg.running" class="process-toggle" @click="expanded[i] = !expanded[i]">
                    <el-icon><CircleCheck class="done-icon" /></el-icon>
                    {{ expanded[i] ? '收起处理过程' : `处理过程（${msg.steps.length} 步）` }}
                  </button>
                  <div v-if="msg.running" class="running-line">
                    <span class="spinner" />
                    正在执行：{{ msg.steps[msg.steps.length - 1] }}
                  </div>
                  <div v-else-if="expanded[i]" class="process-list">
                    <div v-for="(s, j) in msg.steps" :key="j" class="p-item">✓ {{ s }}</div>
                  </div>
                </div>

                <div v-if="msg.isClarify" class="clarify-tag">
                  <el-icon><InfoFilled /></el-icon> 需要补充信息
                </div>

                <div v-if="msg.text" class="answer-text">{{ msg.text }}</div>

                <!-- 答案优先（F29）：单行聚合结果直接给 KPI 大数字 -->
                <div v-if="kpisOf(msg).length" class="kpi-row" :class="{ single: kpisOf(msg).length === 1 }">
                  <div v-for="k in kpisOf(msg)" :key="k.label" class="kpi">
                    <div class="kpi-value" :title="k.raw">{{ k.value }}</div>
                    <div class="kpi-label">{{ k.label }}</div>
                  </div>
                </div>

                <!-- 结果呈现（F13：表格 / 图表切换） -->
                <template v-if="msg.result?.rows?.length">
                  <div v-if="chartOf(msg)" class="result-toolbar">
                    <button class="view-btn" :class="{ active: (msg.view || 'table') === 'table' }"
                            data-tip="表格视图" @click="msg.view = 'table'">
                      <el-icon><Document /></el-icon>
                    </button>
                    <button class="view-btn" :class="{ active: msg.view === 'chart' }"
                            data-tip="条形图" @click="msg.view = 'chart'">
                      <el-icon><DataLine /></el-icon>
                    </button>
                  </div>
                  <div v-if="(msg.view || 'table') === 'table'" class="result-table">
                    <table>
                      <thead>
                        <tr><th v-for="col in msg.result.columns" :key="col">{{ col }}</th></tr>
                      </thead>
                      <tbody>
                        <tr v-for="(row, r) in msg.result.rows" :key="r">
                          <td v-for="col in msg.result.columns" :key="col" :title="String(row[col])">{{ row[col] }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <div v-else class="chart">
                    <div v-for="(it, k) in chartOf(msg).items" :key="k" class="chart-row">
                      <span class="chart-label" :title="it.label">{{ it.label }}</span>
                      <div class="chart-bar-wrap">
                        <div class="chart-bar" :style="{ width: it.pct + '%' }"></div>
                      </div>
                      <span class="chart-value">{{ it.value }}</span>
                    </div>
                  </div>
                </template>
                <!-- 依据折叠（F30）：SQL/口径/行数/耗时默认收起 -->
                <div v-if="msg.result" class="result-foot">
                  <button v-if="hasEvidence(msg)" class="evidence-toggle" @click="evOpen[i] = !evOpen[i]">
                    <span class="ev-title">依据</span>
                    <span v-if="msg.result.sql" class="ev-chip">SQL</span>
                    <span v-if="msg.result.assumptions?.length" class="ev-chip">{{ msg.result.assumptions.length }} 条口径</span>
                    <span v-if="msg.result.row_count > 0" class="ev-chip">{{ msg.result.row_count }} 行{{ msg.result.truncated ? '+' : '' }}</span>
                    <span v-if="msg.elapsedMs" class="ev-chip">{{ fmtMs(msg.elapsedMs) }}</span>
                    <span class="ev-caret">{{ evOpen[i] ? '收起' : '展开' }}</span>
                  </button>
                  <button v-if="msg.result.row_count > 0" class="foot-link" @click="exportCSV(msg)">
                    <el-icon><Download /></el-icon> 导出 CSV
                  </button>
                </div>
                <div v-if="evOpen[i] && hasEvidence(msg)" class="evidence-body">
                  <ul v-if="msg.result.assumptions?.length" class="assumptions">
                    <li v-for="(a, j) in msg.result.assumptions" :key="j">{{ a }}</li>
                  </ul>
                  <template v-if="msg.result.sql">
                    <span class="sql-label">执行 SQL（只读）</span>
                    <div class="sql-block">{{ msg.result.sql }}</div>
                  </template>
                </div>

                <!-- 拒答 -->
                <div v-if="msg.reject" class="reject-box">
                  <el-icon style="margin-top:3px"><InfoFilled /></el-icon>
                  <div><span class="code">[{{ msg.reject.reason_code }}]</span> {{ msg.reject.message }}</div>
                </div>

                <!-- 网络异常（F28） -->
                <div v-if="msg.error" class="error-box">
                  <el-icon><WarningFilled /></el-icon>
                  <span>{{ msg.error }}</span>
                  <button class="link" @click="regenerate(msg, i)">重试</button>
                </div>

                <div v-if="msg.stopped" class="stopped-note">已停止生成</div>

                <!-- 消息操作（F25） -->
                <div v-if="!msg.running && (msg.text || msg.result)" class="msg-actions">
                  <button class="act" @click="copyAnswer(msg)">
                    <el-icon><CopyDocument /></el-icon> 复制
                  </button>
                  <button v-if="i === messages.length - 1" class="act" @click="regenerate(msg, i)">
                    <el-icon><Refresh /></el-icon> 重新生成
                  </button>
                </div>

                <!-- 反馈 -->
                <div v-if="(msg.result || msg.reject) && !msg.running" class="feedback-bar">
                  <button class="fb-btn" :class="{ 'on-up': msg.feedback === 'up' }"
                          title="结果准确" @click="clickFeedback(msg, 'up')"><el-icon><CircleCheck /></el-icon></button>
                  <button class="fb-btn" :class="{ 'on-down': msg.feedback === 'down' }"
                          title="结果不对" @click="clickFeedback(msg, 'down')"><el-icon><CircleClose /></el-icon></button>
                  <span class="note" data-tip="结果需人工复核">
                    <el-icon><Lock /></el-icon> AI 生成
                  </span>
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
        <div class="composer" :class="{ dragging: dragOver }">
          <div v-if="pendingFiles.length" class="pending-files">
            <span v-for="(f, idx) in pendingFiles" :key="f.file_id" class="pending-chip">
              {{ fileIcon(f) }} {{ f.name }}
              <button class="chip-x" @click="dropPending(idx)"><el-icon><Close /></el-icon></button>
            </span>
          </div>
          <textarea ref="taRef" v-model="input" rows="1" :disabled="busy"
                    placeholder="问点什么，或拖入文件"
                    @keydown="onKeydown" @input="autoResize" />
          <div class="composer-bottom">
            <button class="attach-btn" :disabled="uploading" title="上传文件" @click="pickFile">
              <el-icon><Paperclip /></el-icon>
            </button>
            <input ref="fileInput" type="file" multiple hidden @change="onFileChange" />
            <span class="composer-model" :class="{ user: role !== 'admin' }"
                  :title="role === 'admin' ? '在顶栏可切换默认模型' : '由管理员统一配置'">
              <el-icon><Cpu /></el-icon> {{ llmStatus?.default_model || '未配置' }}
            </span>
            <span class="secure-mark" data-tip="结果需人工复核">
              <el-icon><Lock /></el-icon>
            </span>
            <button v-if="busy" class="send-btn stop" title="停止生成" @click="stop">
              <el-icon><ArrowUpBold style="transform:rotate(180deg)" /></el-icon>
            </button>
            <button v-else class="send-btn" :disabled="!input.trim()" @click="send">
              <el-icon><ArrowUp /></el-icon>
            </button>
          </div>
        </div>
        <div v-if="dragOver" class="drop-mask">松开即可上传文件</div>
      </div>

      <!-- 文件预览视图（F34：无弹窗，主区呈现） -->
      <el-scrollbar v-else-if="activeView === 'file' && previewFile" class="thread-scroll">
        <div class="page-view preview-view">
          <div class="preview-head">
            <button class="icon-btn" title="返回对话" @click="closePreview">
              <el-icon><ArrowLeft /></el-icon>
            </button>
            <span class="preview-ico">{{ fileIcon(previewFile) }}</span>
            <div class="preview-meta">
              <div class="preview-name">{{ previewFile.name }}</div>
              <div class="preview-sub">
                <span>{{ fmtSize(previewFile.size || 0) }}</span>
                <span v-if="previewData?.sheets?.length">{{ previewData.sheets.length }} 个工作表</span>
                <span v-if="previewLoading">加载中…</span>
              </div>
            </div>
            <template v-if="editing">
              <button class="preview-save" :disabled="saving" @click="saveEdit">
                {{ saving ? '保存中…' : '保存为新版本' }}
              </button>
              <button class="preview-cancel" :disabled="saving" @click="cancelEdit">取消</button>
            </template>
            <button v-else-if="canEdit" class="preview-edit" @click="startEdit">编辑</button>
            <a class="preview-dl" :href="downloadUrl(previewFile.file_id)" :download="previewFile.name">
              <el-icon><Download /></el-icon> 下载
            </a>
          </div>

          <div v-if="editing" class="editor-wrap">
            <div v-if="!editorReady" class="editor-loading">编辑器加载中…</div>
            <UniverSheet ref="editorRef" v-bind="editorProps" @ready="editorReady = true" />
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
            <div class="panel-empty-graphic">▤</div>
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
            </div>
            <div v-if="llmStatus" class="settings-state">
              <span class="state-dot" :class="{ on: llmStatus.available }"></span>
              {{ llmStatus.available ? llmStatus.default_model : '示例模式' }}
            </div>
          </div>

          <div class="settings-tabs">
            <button v-for="t in SETTINGS_TABS" :key="t.k" class="settings-tab"
                    :class="{ active: settingsTab === t.k }" @click="settingsTab = t.k">
              {{ t.label }}
            </button>
          </div>

          <!-- 模型与AI -->
          <div v-if="settingsTab === 'model'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><Cpu /></el-icon></span>
                <div>
                  <b>AI 大模型</b>
                </div>
              </div>
              <button v-if="role === 'admin'" class="btn-dark" @click="newApi">
                <el-icon><Plus /></el-icon> 添加模型
              </button>
            </div>

            <div v-if="showApiForm && role === 'admin'" class="inline-form">
              <div class="inline-form-title">{{ apiForm.id ? `编辑模型：${apiForm.name}` : '添加模型' }}</div>
              <el-form label-width="90px">
                <el-form-item label="名称"><el-input v-model="apiForm.name" placeholder="如：DeepSeek 官方" /></el-form-item>
                <el-form-item label="Base URL"><el-input v-model="apiForm.base_url" placeholder="https://api.deepseek.com/v1" /></el-form-item>
                <el-form-item label="模型"><el-input v-model="apiForm.model" placeholder="如：deepseek-chat" /></el-form-item>
                <el-form-item label="API Key">
                  <el-input v-model="apiForm.api_key" type="password" show-password
                            :placeholder="apiForm.id ? '留空表示不修改' : 'sk-...'" />
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
                  <th>名称</th><th>Base URL</th><th>模型</th><th>API Key</th><th class="ta-r">操作</th>
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
                  <td class="model-mono model-key">{{ row.key_masked }}</td>
                  <td>
                    <div v-if="role === 'admin'" class="model-actions">
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
                    <button v-if="role === 'admin'" class="model-act" @click="newApi">
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
          <div v-else-if="settingsTab === 'appearance'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><Sunny /></el-icon></span>
                <div><b>外观风格</b></div>
              </div>
            </div>
            <div class="settings-row">
              <span>界面主题</span>
              <el-radio-group :model-value="theme" @update:model-value="toggleTheme">
                <el-radio-button value="light">亮色</el-radio-button>
                <el-radio-button value="dark">暗色</el-radio-button>
              </el-radio-group>
            </div>
          </div>

          <!-- 权限与角色 -->
          <div v-else-if="settingsTab === 'role'" class="model-card tab-panel">
            <div class="model-card-head">
              <div class="model-card-title">
                <span class="model-ico"><el-icon><Lock /></el-icon></span>
                <div><b>权限与角色</b></div>
              </div>
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
          </div>

          <!-- 账户与统计 -->
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
            <div class="settings-row muted">只读查询 · AI 生成结果请人工复核</div>
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
        <button class="panel-close" title="关闭（Esc）" @click="togglePanel()"><el-icon><Close /></el-icon></button>
      </div>

      <!-- 文件 Tab：本会话上传 + 生成 -->
      <div v-if="panelTab === 'files'" class="panel-body">
        <button class="panel-upload" :disabled="uploading" @click="pickFile">
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
          <div class="panel-empty-graphic">▤</div>
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
            </div>
          </div>
          <a class="file-act" :href="downloadUrl(f.file_id)" :download="f.name" title="下载">
            <el-icon><Download /></el-icon>
          </a>
          <button class="file-act" title="删除" @click="removeFile(f)"><el-icon><Close /></el-icon></button>
        </div>
      </div>

      <!-- 搜索 Tab：检索本会话并定位 -->
      <div v-else class="panel-body">
        <input v-model="searchQ" class="panel-search" placeholder="搜索会话内容" />
        <div v-if="searchQ && !searchResults.length" class="panel-empty">没有匹配内容</div>
        <div v-for="r in searchResults" :key="r.i" class="search-item" @click="jumpTo(r.i)">
          <div class="search-role">{{ r.role === 'user' ? '我' : 'DataPro' }}</div>
          <div class="search-text" v-html="highlight(r.text.slice(0, 140))"></div>
        </div>
      </div>
    </aside>
  </div>
</template>
