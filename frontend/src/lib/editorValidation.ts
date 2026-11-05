// Keep these input rules aligned with backend/app/schemas/{agent,project,provider_models}.py.
// Python's str length counts Unicode code points, not UTF-16 code units.
export const editorTextLength = (value: string) => Array.from(value).length
export const validEditorModelId = (value: string) => value.length > 0 && value.length <= 200
  && /^[A-Za-z0-9_][A-Za-z0-9._:-]*(?:\/[A-Za-z0-9_][A-Za-z0-9._:-]*)*$/.exec(value)?.[0] === value
export const validEditorTemperature = (value: string) => /^(auto|(?:[0-1](?:\.\d+)?|2(?:\.0+)?))$/.exec(value)?.[0] === value

export function focusEditorField(root: HTMLElement | null, key: string) {
  const container = root?.querySelector<HTMLElement>(`[data-editor-field="${key}"]`)
  const field = container?.matches('input, select, textarea') ? container : container?.querySelector<HTMLElement>('input, select, textarea')
  if (!field) return
  for (let ancestor = field.parentElement; ancestor && root?.contains(ancestor); ancestor = ancestor.parentElement) {
    if (ancestor instanceof HTMLDetailsElement) ancestor.open = true
  }
  requestAnimationFrame(() => { if (field.isConnected) { field.focus(); field.scrollIntoView({ block: 'nearest' }) } })
}
