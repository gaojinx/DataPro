/**
 * 输入草稿（X-I01）：按账号隔离、只存文本。
 * 所有操作都吞掉 storage 异常（配额溢出 / 隐私模式），不打断用户输入。
 */

export function draftKeyFor(username) {
  return `dp_draft_${username || 'anon'}`
}

export function readDraft(storage, key) {
  try { return storage.getItem(key) || '' } catch { return '' }
}

export function writeDraft(storage, key, value) {
  try {
    if (value && value.trim()) storage.setItem(key, value)
    else storage.removeItem(key)
    return true
  } catch { return false }
}

export function removeDraft(storage, key) {
  try { storage.removeItem(key); return true } catch { return false }
}
