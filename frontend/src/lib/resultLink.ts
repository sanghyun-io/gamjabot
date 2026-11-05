import { canonicalEventId } from './peerReply'
export type ResultRoute = Readonly<{ state: 'none' | 'invalid' }> | Readonly<{ state: 'valid'; id: string; version: number; base: number | null }>
const versionNumber = (value: string | null) => value && /^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null
export function readResultRoute(search: string): ResultRoute {
  const query = new URLSearchParams(search)
  if (!['result', 'version', 'base'].some(key => query.has(key))) return { state: 'none' }
  const id = query.get('result'), version = versionNumber(query.get('version')), base = versionNumber(query.get('base'))
  if (query.getAll('result').length !== 1 || query.getAll('version').length !== 1 || query.getAll('base').length > 1
      || !canonicalEventId(id) || version === null || query.has('base') && (base === null || base === version)) return { state: 'invalid' }
  return { state: 'valid', id, version, base }
}
export function resultVersionHref(room: number, id: string, version: number, base: number | null = null): string | null {
  if (!Number.isSafeInteger(room) || room < 1 || !canonicalEventId(id) || !Number.isSafeInteger(version) || version < 1
      || base !== null && (!Number.isSafeInteger(base) || base < 1 || base === version)) return null
  const query = new URLSearchParams({ result: id, version: String(version) })
  if (base !== null) query.set('base', String(base))
  return `/chat/${room}?${query}`
}
export function resultReferenceMarkdown(title: string, version: number, href: string): string {
  const label = `${title.replace(/[\r\n\u2028\u2029]+/g, ' ')} · v${version}`.replace(/[\\\[\]]/g, '\\$&')
  return `[${label}](${href})`
}

export type PastedResultLink = Readonly<{ state: 'none' | 'invalid' }> | Readonly<{ state: 'valid'; room: number; id: string; version: number; base: number | null; href: string }>
// Explicitly pasted local reference only; never accept an origin, credentials,
// external protocol, source-event override, extra query key, or arbitrary route.
export function readPastedResultLink(input: string): PastedResultLink {
  const text = input.trim(), markdown = /^\[(?:\\.|[^\]\\])*\]\((\/chat\/[^\s()]*)\)$/.exec(text)
  const looksInternal = text.startsWith('/chat/') || text.startsWith('[') && text.includes('](/chat/')
  if (!looksInternal) return { state: 'none' }
  if (text.length > 2048 || /[\r\n]/.test(text)) return { state: 'invalid' }
  const href = markdown ? markdown[1] : text, match = /^\/chat\/([1-9]\d*)\?([^#\s]*)$/.exec(href)
  if (!match || !Number.isSafeInteger(Number(match[1]))) return { state: 'invalid' }
  const params = new URLSearchParams(match[2])
  if ([...params.keys()].some(key => !['result', 'version', 'base'].includes(key))) return { state: 'invalid' }
  const target = readResultRoute('?' + match[2])
  if (target.state !== 'valid') return { state: 'invalid' }
  const canonical = resultVersionHref(Number(match[1]), target.id, target.version, target.base)
  return canonical ? { ...target, room: Number(match[1]), href: canonical } : { state: 'invalid' }
}

export function messageEventHref(room: number, seq: number, event: string): string | null {
  if (!Number.isSafeInteger(room) || room < 1 || !Number.isSafeInteger(seq) || seq < 1 || !canonicalEventId(event)) return null
  return `/chat/${room}?seq=${seq}&event=${event}`
}

export type PastedChatReference = Readonly<{ state: 'none' | 'invalid' }>
  | (Extract<PastedResultLink, { state: 'valid' }> & Readonly<{ kind: 'result' }>)
  | Readonly<{ state: 'valid'; kind: 'event'; room: number; seq: number; event: string; href: string }>

// Parse a pasted reference without implying that the target exists or is accessible.
export function readPastedChatReference(input: string): PastedChatReference {
  const text = input.trim()
  const markdown = /^\[(?:\\.|[^\]\\])*\]\((\/chat\/[^\s()]*)\)$/.exec(text)
  const href = markdown ? markdown[1] : text
  const match = /^\/chat\/([1-9]\d*)\?([^#\s]*)$/.exec(href)
  if (match && match[2].split('&').some(part => part.startsWith('seq=') || part.startsWith('event='))) {
    if (text.length > 2048 || /[\r\n]/.test(text) || !Number.isSafeInteger(Number(match[1]))) return { state: 'invalid' }
    const parts = match[2].split('&')
    if (parts.length !== 2 || !parts.some(part => /^seq=[1-9]\d*$/.test(part)) || !parts.some(part => /^event=[0-9a-f-]+$/.test(part))) return { state: 'invalid' }
    const seq = versionNumber(parts.find(part => part.startsWith('seq='))!.slice(4))
    const event = parts.find(part => part.startsWith('event='))!.slice(6)
    if (seq === null || !canonicalEventId(event)) return { state: 'invalid' }
    const room = Number(match[1])
    return { state: 'valid', kind: 'event', room, seq, event, href: messageEventHref(room, seq, event)! }
  }
  if (/^(?:[a-z][a-z0-9+.-]*:\/\/|\[[^\]]*\]\([a-z][a-z0-9+.-]*:\/\/)/i.test(text)) return { state: 'invalid' }
  const result = readPastedResultLink(input)
  return result.state === 'valid' ? { ...result, kind: 'result' } : result
}
