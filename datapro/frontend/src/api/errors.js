/**
 * 统一错误映射：把后端结构化的 code/message 或 HTTP 状态码映射为可直接展示的人话。
 * 目标：任何报错都不再只显示裸 "500" / "Not Found"，而是「[码] 人话」+ 追踪号。
 */
import { sessionExpired } from './auth'

// 与后端 app/core/errors.py CODE_CATALOG / HTTP_STATUS_MESSAGE 对齐
const CODE_MESSAGES = {
  E001: '问题信息不足，请补充时间范围、指标口径或输出粒度',
  E002: '未找到相关数据或指标口径，请联系数据团队',
  E003: '超出服务范围或没有权限，请申请权限或联系管理员',
  E004: '内容未通过校验，请检查后重试',
  E005: '服务超时，请稍后重试',
  E006: '服务暂时不可用，请稍后重试',
  E007: '操作过于频繁，请稍后重试',
  E008: '登录已失效，请重新登录',
  E009: '查询范围过大，请缩小时间范围或增加筛选条件',
  E010: '系统内部异常，已记录并转人工处理',
}

const STATUS_CODE = {
  400: 'E004', 401: 'E008', 403: 'E003', 404: 'E002',
  405: 'E004', 408: 'E005', 409: 'E004', 422: 'E004',
  429: 'E007', 500: 'E010', 502: 'E006', 503: 'E006', 504: 'E005',
}

const STATUS_MESSAGES = {
  400: '请求参数有误，请检查后重试',
  401: '登录已失效，请重新登录',
  403: '没有权限执行该操作，或超出服务范围',
  404: '请求的资源不存在或已被移除',
  405: '不支持该操作方式',
  408: '请求超时，请稍后重试',
  409: '操作冲突，请刷新后重试',
  422: '提交内容未通过校验，请检查后重试',
  429: '操作过于频繁，请稍后重试',
  500: '服务开小差了，请稍后重试；若持续出现请联系管理员',
  502: '上游服务暂时不可用，请稍后重试',
  503: '服务暂时不可用，请稍后重试',
  504: '服务响应超时，请稍后重试',
}

/** 英文/裸状态码等无展示价值的 detail，不作为人话使用 */
const GENERIC = /^(Not Found|Internal Server Error|Method Not Allowed|HTTP \d+|请求失败（\d+）|\d{3})$/

/**
 * 从失败响应构造可读 Error。
 * 优先级：后端 message（非通用串）> 错误码映射 > 状态码映射 > detail 原文。
 * Error 上挂 code/status/traceId，便于上层做差异化处理。
 */
export function toApiError(resp, data = {}) {
  const status = resp?.status || 500
  const isAuth = status === 401
  if (isAuth) sessionExpired()

  const payload = data && typeof data === 'object' ? data : {}
  const backendMsg = typeof payload.message === 'string' ? payload.message : ''
  const rawDetail = typeof payload.detail === 'string' ? payload.detail : ''
  let code = String(payload.code || '').toUpperCase()
  const hasBizCode = !!CODE_MESSAGES[code]
  if (!hasBizCode) code = STATUS_CODE[status] || 'E010'

  // 有业务码用业务人话；仅由 HTTP 状态推导时优先状态语义（404=资源不存在，而非 E002 的「未找到口径」）
  const codeMsg = (hasBizCode ? CODE_MESSAGES[code] : STATUS_MESSAGES[status])
    || CODE_MESSAGES[code] || STATUS_MESSAGES[status] || '请求未成功，请稍后重试'
  // 后端给了具体人话就优先展示，但要避免英文/裸状态码
  const meaningful = backendMsg && !GENERIC.test(backendMsg) ? backendMsg : ''
  const human = meaningful || codeMsg

  const traceId = (resp?.headers?.get?.('x-trace-id')) || payload.trace_id || ''
  const suffix = code ? ` [${code}]` : ''
  const trace = traceId ? `（追踪号 ${traceId}）` : ''
  // 401 由 App.handleAuthExpired 统一弹一次提示，API 层静默，避免并发请求弹出多条
  const err = new Error(isAuth ? '' : `${human}${suffix}${trace}`)
  err.code = code
  err.status = status
  err.traceId = traceId
  err.detail = rawDetail || backendMsg
  err.auth = isAuth
  return err
}

/**
 * 统一的响应解析：401 触发登录过期；非 2xx 抛可读 Error；否则返回 JSON。
 * 各 api 封装的 parse()/handle() 都委托到这里，保证报错文案一致。
 */
export async function parseResponse(resp) {
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw toApiError(resp, data)
  return data
}
