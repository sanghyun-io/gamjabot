import { invoke, isTauri } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'

const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/

/** Parse an untrusted OS link. The returned preview URL still requires the
 * backend's DNS-pinned HTTPS validation after the Owner explicitly opens it. */
export function parseTemplateLink(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw.length > 4096 || !raw.startsWith('gamjabot://template?url=') || /[\u0000-\u0020\u007f\\]/.test(raw)) return null
  try {
    const link = new URL(raw)
    if (link.protocol !== 'gamjabot:' || link.hostname !== 'template' || link.username || link.password || link.port || link.pathname || link.hash) return null
    const entries = Array.from(link.searchParams.entries())
    if (entries.length !== 1 || entries[0][0] !== 'url') return null
    const target = entries[0][1]
    if (!target.startsWith('https://') || /[\u0000-\u0020\u007f\\]/.test(target)) return null
    const url = new URL(target)
    if (url.protocol !== 'https:' || url.username || url.password || url.port || url.search || url.hash || target !== url.href) return null
    const host = url.hostname
    const labels = host.split('.')
    if (host.length > 253 || labels.length < 2 || labels.some(label => !LABEL.test(label)) || /^(?:\d+\.){3}\d+$/.test(host) || ['localhost','local','internal','home'].includes(labels[labels.length - 1])) return null
    const match = /^\/t\/([^/]+)$/.exec(url.pathname)
    if (!match || !UUID4.test(match[1])) return null
    return url.href
  } catch {
    return null
  }
}

/** Subscribe once for the app lifetime; the caller owns Owner/session memory
 * and should dispose the subscription at unmount. This never fetches a URL. */
export async function subscribeTemplateLinks(onUrl: (url: string) => void): Promise<() => void> {
  if (!isTauri()) return () => {}
  let active = true
  let starting = true
  const duringStartup = new Set<string>()
  const deliver = (payload: unknown, initial = false) => {
    if (!active || !Array.isArray(payload) || payload.length > 10) return
    for (const value of payload) {
      const url = parseTemplateLink(value)
      if (!url || (initial && duringStartup.has(url))) continue
      if (!initial && starting) duringStartup.add(url)
      onUrl(url)
    }
  }
  const unlisten = await listen<unknown>('deep-link://new-url', event => deliver(event.payload))
  try {
    deliver(await invoke<unknown>('plugin:deep-link|get_current'), true)
    starting = false
    duringStartup.clear()
    return () => { active = false; unlisten() }
  } catch (error) {
    active = false
    unlisten()
    throw error
  }
}
