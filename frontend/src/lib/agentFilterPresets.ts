export type AgentPresetFilters = {
  query: string; provider: string; auth: string; model: string; connectionId: number | null
}
export type AgentFilterPreset = { version: 1; id: string; name: string; filters: AgentPresetFilters; savedAt: string }
export type StoredAgentPreset = AgentFilterPreset & { raw: string }
export const AGENT_PRESET_LIMIT = 20
const authGroups = new Set(['', 'oauth', 'api_key', 'local_none', 'native_chain', 'unknown'])
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
export function agentPresetPrefix(owner: number) {
  if (!Number.isSafeInteger(owner) || owner <= 0) throw new Error('invalid')
  return `gamja.agent-filter-preset.v1.owner.${owner}.`
}
function encodedFilter(value: unknown, max: number, nullable: boolean) {
  if (typeof value !== 'string' || value.length > max * 4 + 8) return false
  if (value === '') return true
  try { const decoded: unknown = JSON.parse(value); return nullable && decoded === null || typeof decoded === 'string' && decoded.length <= max }
  catch { return false }
}
export function validAgentPresetFilters(value: unknown): value is AgentPresetFilters {
  if (!value || typeof value !== 'object') return false
  const f = value as AgentPresetFilters
  return typeof f.query === 'string' && f.query.length <= 1000
    && encodedFilter(f.provider, 256, true) && encodedFilter(f.model, 512, false)
    && typeof f.auth === 'string' && authGroups.has(f.auth)
    && (f.connectionId === null || Number.isSafeInteger(f.connectionId) && f.connectionId > 0)
}
export function readAgentPresets(owner: number) {
  const prefix = agentPresetPrefix(owner), storage = window.localStorage
  const items: StoredAgentPreset[] = []; let skipped = 0, count = 0
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index)
    if (!key?.startsWith(prefix)) continue
    count++
    const raw = storage.getItem(key)
    try {
      if (!raw || raw.length > 8192) throw new Error('invalid')
      const value: AgentFilterPreset = JSON.parse(raw)
      if (value.version !== 1 || typeof value.id !== 'string' || !uuid.test(value.id) || key !== prefix + value.id
        || typeof value.name !== 'string' || !value.name.trim() || value.name.length > 80
        || typeof value.savedAt !== 'string' || !Number.isFinite(Date.parse(value.savedAt)) || !validAgentPresetFilters(value.filters)) throw new Error('invalid')
      items.push({ version: 1, id: value.id, name: value.name, filters: value.filters, savedAt: value.savedAt, raw })
    } catch { skipped++ } // Preserve unreadable records; never repair or delete them implicitly.
  }
  items.sort((a, b) => b.savedAt.localeCompare(a.savedAt) || a.id.localeCompare(b.id))
  return { items, skipped, count }
}
export function saveAgentPreset(owner: number, name: string, filters: AgentPresetFilters) {
  name = name.trim()
  if (!name || name.length > 80 || !validAgentPresetFilters(filters)) throw new Error('invalid')
  const current = readAgentPresets(owner)
  if (current.count >= AGENT_PRESET_LIMIT) throw new Error('limit')
  if (current.items.some(row => row.name.normalize('NFKC').toLowerCase() === name.normalize('NFKC').toLowerCase())) throw new Error('duplicate')
  const { query, provider, auth, model, connectionId } = filters
  const value: AgentFilterPreset = { version: 1, id: crypto.randomUUID(), name,
    filters: { query, provider, auth, model, connectionId }, savedAt: new Date().toISOString() }
  const raw = JSON.stringify(value)
  if (raw.length > 8192) throw new Error('invalid')
  // Independent keys avoid rewriting other presets from a stale whole-list snapshot.
  // The local count/name checks are not a cross-window transaction or a sync service.
  window.localStorage.setItem(agentPresetPrefix(owner) + value.id, raw)
  return { ...value, raw }
}
export function deleteAgentPreset(owner: number, row: StoredAgentPreset) {
  const key = agentPresetPrefix(owner) + row.id
  if (!uuid.test(row.id) || window.localStorage.getItem(key) !== row.raw) throw new Error('changed')
  window.localStorage.removeItem(key)
}
export function presetFilterLabel(value: string, empty: string, missing: string) {
  if (!value) return empty
  try { const decoded: unknown = JSON.parse(value); return typeof decoded === 'string' ? decoded || missing : missing }
  catch { return missing }
}
