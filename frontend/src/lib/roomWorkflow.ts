import type { Agent } from '../api/agents'
import type { Connection } from '../api/connections'
import type { RoomEvent } from '../api/events'
import type { RoomMember } from '../api/conversations'
import { assistantVisibleMarkdown } from './assistantContent'

export function finalResultSource(reply: RoomEvent, events: RoomEvent[]) {
  if (reply.actor_type !== 'agent' || reply.kind !== 'message' || typeof reply.payload.trigger_event_id !== 'string') return null
  const request = events.find((event) => event.event_id === reply.payload.trigger_event_id && event.room_id === reply.room_id)
  const value = request?.payload.finalize
  if (request?.actor_type !== 'user' || request.kind !== 'message' || !value || typeof value !== 'object') return null
  const binding = value as Record<string, unknown>
  if (binding.version !== 1 || binding.agent_id !== reply.actor_id || typeof binding.source_request_event_id !== 'string'
    || !binding.source_request_event_id || !Array.isArray(binding.source_outcome_event_ids)) return null
  const result = { sourceRequestId: binding.source_request_event_id, state: 'unverified' as 'ready' | 'partial' | 'unverified' }
  const ids = binding.source_outcome_event_ids
  if (!ids.length || ids.some((id) => typeof id !== 'string' || !id) || new Set(ids).size !== ids.length) return result
  const outcomes = ids.map((id) => events.find((event) => event.event_id === id && event.room_id === reply.room_id))
  if (outcomes.some((event) => event?.kind === 'failure'
    || event?.kind === 'activity' && event.payload.status === 'passed')) return { ...result, state: 'partial' as const }
  const status = binding.source_status
  if (status && typeof status === 'object') {
    const facts = status as Record<string, unknown>
    if (facts.outcome_status === 'partial' || facts.outcome_status === 'failed'
      || typeof facts.unanswered_turn_count === 'number' && facts.unanswered_turn_count > 0
      || typeof facts.omitted_message_count === 'number' && facts.omitted_message_count > 0
      || typeof facts.truncated_event_count === 'number' && facts.truncated_event_count > 0) {
      return { ...result, state: 'partial' as const }
    }
  }
  // A missing historical event never proves that every source response succeeded.
  if (outcomes.every((event) => event?.actor_type === 'agent' && event.kind === 'message')) {
    return { ...result, state: 'ready' as const }
  }
  return result
}

// Python's `re` uses Unicode letters/numbers/underscore for `\w`; combining marks are outside it.
const WORD = '\\p{L}\\p{N}_'

function escaped(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function backendCaseFold(value: string) {
  return value.replace(/[İı]/g, 'i').replace(/ſ/g, 's').replace(/K/g, 'k').toLowerCase()
}

function mentionPattern(name: string) {
  const folded = backendCaseFold(name)
  if (!folded || !new RegExp(`[${WORD}]`, 'u').test(folded.at(-1) || '')) return null
  return new RegExp(`(^|[^${WORD}])@${escaped(folded)}(?=$|[^${WORD}])`, 'iu')
}

export type RecipientGroup = {
  name: string
  agentIds: number[]
  matches: (content: string) => boolean
}

export type MentionToken = { start: number; end: number; query: string }

export function routableMentionContent(content: string) {
  return content.split('\n').filter((line) => !/^[ \t]*>/.test(line)).join('\n')
}

export function buildRecipientGroups(agents: Agent[], members: RoomMember[]): RecipientGroup[] {
  const active = new Set(members.filter((item) => item.is_active && !item.paused).map((item) => item.agent_id))
  const grouped: Array<{ name: string; ids: number[] }> = []
  for (const item of agents) {
    if (!item.is_active || !active.has(item.id) || !mentionPattern(item.name)) continue
    const existing = grouped.find((group) => mentionPattern(group.name)?.test(`@${backendCaseFold(item.name)}`)
      && mentionPattern(item.name)?.test(`@${backendCaseFold(group.name)}`))
    if (existing) existing.ids.push(item.id)
    else grouped.push({ name: item.name, ids: [item.id] })
  }
  return grouped.sort((left, right) => Math.min(...left.ids) - Math.min(...right.ids)).map((item) => ({
    name: item.name,
    agentIds: item.ids.sort((left, right) => left - right),
    matches: (content: string) => mentionPattern(item.name)?.test(backendCaseFold(routableMentionContent(content))) || false,
  }))
}

export function mentionAtCaret(content: string, caret: number): MentionToken | null {
  if (!Number.isInteger(caret) || caret < 0 || caret > content.length) return null
  const lineStart = content.lastIndexOf('\n', Math.max(0, caret - 1)) + 1
  if (/^[ \t]*>/.test(content.slice(lineStart, caret))) return null
  const prefix = content.slice(0, caret)
  const match = prefix.match(/(^|[^\p{L}\p{N}_])@([^\s@]*)$/u)
  if (!match) return null
  const suffixLength = content.slice(caret).match(new RegExp(`^[${WORD}]*`, 'u'))?.[0].length || 0
  return { start: caret - match[2].length - 1, end: caret + suffixLength, query: match[2] }
}

export function quoteAtCaret(content: string, quoted: string, caret: number) {
  const position = Math.max(0, Math.min(Number.isInteger(caret) ? caret : content.length, content.length))
  const lineBoundary = position === 0 || content[position - 1] === '\n'
  const insertion = `${lineBoundary ? '' : '\n'}${quoted.split('\n').map((line) => `> ${line}`).join('\n')}\n\n`
  return { content: `${content.slice(0, position)}${insertion}${content.slice(position)}`, caret: position + insertion.length }
}

// This creates an editable quote and explicit mention, not a threaded reply reference.
export function quoteReplyToBot(content:string, quoted:string, caret:number, targetId:number, groups:RecipientGroup[]) {
  const target=groups.find(group=>group.agentIds.includes(targetId))
  if(!target || target.agentIds.length!==1) return {status:'unavailable' as const}
  const candidate=`@${target.name} `
  const routed=groups.filter(group=>group.matches(candidate))
  if(routed.length!==1 || routed[0]!==target) return {status:'unavailable' as const}
  const existing=groups.filter(group=>group.matches(content))
  if(existing.some(group=>group!==target) || !existing.length && /(^|[^\p{L}\p{N}_])@\S+/u.test(routableMentionContent(content))) return {status:'conflict' as const}
  const quotedDraft=quoteAtCaret(content,quoted,caret)
  const next={content:quotedDraft.content.slice(0,quotedDraft.caret)+candidate+quotedDraft.content.slice(quotedDraft.caret),caret:quotedDraft.caret+candidate.length}
  const final=groups.filter(group=>group.matches(next.content))
  if(final.length!==1 || final[0]!==target) return {status:'conflict' as const}
  return {status:'ready' as const,...next}
}

export function filterRecipientGroups(groups: RecipientGroup[], query: string) {
  if (!query) return groups
  const pattern = new RegExp(`^${escaped(backendCaseFold(query))}`, 'iu')
  return groups.filter((group) => pattern.test(backendCaseFold(group.name)))
}

export function replaceMentionAtCaret(content: string, token: MentionToken, name: string) {
  const suffix = content.slice(token.end)
  const separator = suffix.length === 0 || new RegExp(`^[${WORD}]`, 'u').test(suffix) ? ' ' : ''
  const mention = `@${name}${separator}`
  return { content: `${content.slice(0, token.start)}${mention}${suffix}`, caret: token.start + mention.length }
}

export type ReadinessState = 'ready' | 'unverified' | 'missing_connection' | 'disabled' | 'missing_secret' | 'missing_model'

export function memberReadiness(agent: Agent | undefined, connections: Connection[]) {
  if (!agent?.connection_id) return { state: 'missing_connection' as const, connection: undefined, verified: undefined }
  const connection = connections.find((item) => item.id === agent.connection_id)
  if (!connection) return { state: 'missing_connection' as const, connection: undefined, verified: undefined }
  if (!connection.enabled) return { state: 'disabled' as const, connection, verified: undefined }
  if (connection.auth_required && !connection.has_secret) return { state: 'missing_secret' as const, connection, verified: undefined }
  if (!agent.model_id) return { state: 'missing_model' as const, connection, verified: undefined }
  const verified = connection.capabilities?.verified?.models?.[agent.model_id]
  return { state: verified ? 'ready' as const : 'unverified' as const, connection, verified }
}

export type TranscriptActorFilter = 'all' | 'people' | 'agents'

function authoredMessage(event: RoomEvent) {
  return event.kind === 'message' && (event.actor_type === 'user' || event.actor_type === 'agent')
    && typeof event.payload.content === 'string'
}

export function filterTranscriptEvents(events: RoomEvent[], query: string, actor: TranscriptActorFilter, visibleTexts?: ReadonlyMap<RoomEvent, string>) {
  const needle = query.trim().toLocaleLowerCase()
  const content = (event: RoomEvent) => visibleTexts?.get(event)
    ?? (event.actor_type === 'agent' ? assistantVisibleMarkdown(String(event.payload.content)) : String(event.payload.content))
  return events.filter((event) => authoredMessage(event)
    && (actor === 'all' || actor === 'people' && event.actor_type === 'user' || actor === 'agents' && event.actor_type === 'agent')
    && (!needle || `${event.actor_name_snapshot}\n${content(event)}`.toLocaleLowerCase().includes(needle)))
}

type TranscriptLabels = { title: string; user: string; agent: string; sequence: string; scope?:string }

function codeFence(content: string) {
  const longest = Math.max(0, ...[...content.matchAll(/`+/g)].map((match) => match[0].length))
  const fence = '`'.repeat(Math.max(3, longest + 1))
  return `${fence}\n${content}\n${fence}`
}

export function transcriptMarkdown(roomTitle: string, events: RoomEvent[], labels: TranscriptLabels,
                                   formatDateTime: (value: string) => string, body?: (event:RoomEvent)=>string) {
  const messages = events.filter(authoredMessage)
  const sections = messages.map((event) => {
    const actor = event.actor_type === 'user' ? labels.user : labels.agent
    return `## ${event.actor_name_snapshot} · ${actor} · ${labels.sequence} ${event.seq}\n\n${formatDateTime(event.created_at)}\n\n${codeFence(body?body(event):String(event.payload.content))}`
  })
  return [`# ${labels.title}: ${roomTitle}`, ...(labels.scope?[codeFence(labels.scope)]:[]), ...sections].join('\n\n') + '\n'
}

export function transcriptFilename(roomTitle: string) {
  const normalized = [...roomTitle.normalize('NFKC')].map((value) => value.charCodeAt(0) < 32 ? '-' : value).join('')
  const safe = normalized.replace(/[\\/:*?"<>|]/g, '-').replace(/\s+/g, ' ').trim().slice(0, 80)
  return `${safe || 'room'}-transcript.md`
}
