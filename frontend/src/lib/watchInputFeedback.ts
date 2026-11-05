import type { WatchForm } from './roomPlanEditorState'
import { editorTextLength } from './editorValidation'

export type WatchInputField = 'name' | 'url' | 'interval_minutes' | 'timezone' | 'keyword'
// Input feedback only. The existing server still validates public routing,
// DNS/redirect safety, IANA timezone availability and admission at execution.
export function watchInputIssues(value: WatchForm): WatchInputField[] {
  const issues: WatchInputField[] = []
  if (!value.name.trim() || editorTextLength(value.name) > 100 || /[\x00-\x1f\x7f]/.test(value.name)) issues.push('name')
  let urlValid = editorTextLength(value.url) >= 8 && editorTextLength(value.url) <= 2048 && !/[\x00-\x20\x7f]/.test(value.url)
  try {
    const url = new URL(value.url)
    urlValid = urlValid && /^https?:\/\//i.test(value.url) && ['http:', 'https:'].includes(url.protocol)
      && !!url.hostname && !url.username && !url.password && !url.search
      && (!url.port || url.port === (url.protocol === 'https:' ? '443' : '80'))
      && url.hostname !== 'localhost' && !['.localhost', '.local', '.internal'].some(suffix => url.hostname.endsWith(suffix))
  } catch { urlValid = false }
  if (!urlValid) issues.push('url')
  const minutes = Number(value.interval_minutes)
  if (!/^[1-9]\d*$/.test(value.interval_minutes) || !Number.isSafeInteger(minutes) || minutes < 5 || minutes > 10080) issues.push('interval_minutes')
  // Do not use the browser's smaller timezone catalog as a server allowlist.
  if (!value.timezone.trim() || editorTextLength(value.timezone) > 100 || /[\x00-\x20\x7f]/.test(value.timezone)) issues.push('timezone')
  if (editorTextLength(value.keyword) > 200 || value.mode !== 'text_change' && !value.keyword.trim()) issues.push('keyword')
  return issues
}
