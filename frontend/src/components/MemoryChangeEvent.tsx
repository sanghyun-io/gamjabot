import { useQuery } from '@tanstack/react-query'
import type { RoomEvent } from '../api/events'
import { validBotMessageRef } from '../api/botMessages'
import { memorySharingApi } from '../api/memorySharing'
import { canonicalEventId } from '../lib/peerReply'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'

type Meta = { version: 1; receipt_id: string; scope: 'owner_common'|'bot_private'; operation: 'add'|'edit'|'delete'|'policy'|'visibility'; state: 'saved'; agent_id: number; root_request_event_id: string; content: string; memory_id: string | null; revision: number; entry_revision: number | null; effective: 'next_request'; history_retained: boolean; kind?: 'name'|'timezone'; group_allowed?: boolean; origin?: unknown }
const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const pos = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0
export function memoryChangeMetadata(event: RoomEvent): Meta | null {
  const m = event.payload.bot_persistence
  if (!obj(m) || event.kind !== 'activity' || event.actor_type !== 'system' || event.actor_id !== null || event.payload.status !== 'saved'
    || m.version !== 1 || m.state !== 'saved' || typeof m.receipt_id !== 'string' || !/^[0-9a-f]{64}$/.test(m.receipt_id)
    || !canonicalEventId(m.root_request_event_id) || event.payload.trigger_event_id !== m.root_request_event_id || !pos(m.agent_id)
    || typeof m.content !== 'string' || !m.content.trim() || !pos(m.revision) || m.effective !== 'next_request' || typeof m.history_retained !== 'boolean') return null
  if (m.scope === 'owner_common') {
    if (event.payload.category !== 'owner_common_memory' || !['add','edit','delete'].includes(String(m.operation)) || !canonicalEventId(m.memory_id) || !pos(m.entry_revision)
      || !['name','timezone'].includes(String(m.kind)) || !obj(m.origin) || !pos(m.origin.agent_id) || !['event','manual'].includes(String(m.origin.source_kind))) return null
  } else if (m.scope === 'bot_private') {
    if (event.payload.category !== 'bot_persistence' || !['policy','visibility'].includes(String(m.operation))) return null
    if (m.operation === 'policy' ? m.memory_id !== null || m.entry_revision !== null || !['canonical_only','owner_rooms'].includes(String(m.content)) : !canonicalEventId(m.memory_id) || !pos(m.entry_revision) || typeof m.group_allowed !== 'boolean') return null
  } else return null
  if (m.history_retained !== ['edit','delete','visibility'].includes(String(m.operation))) return null
  return m as Meta
}
function sameOrigin(a: unknown, b: unknown) {
  if (a === undefined && b === undefined) return true
  return obj(a) && obj(b) && ['agent_id','source_kind','room_id','event_id','seq'].every(key => a[key] === b[key])
}
export default function MemoryChangeEvent({ event, allowed, canUse, onOpen }: { event: RoomEvent; allowed: boolean; canUse: () => boolean; onOpen: (seq: number, id: string) => void }) {
  const scope = useBotScope(), { t } = useI18n(), meta = memoryChangeMetadata(event)
  const read = useQuery({ queryKey: ['memory-change', scope.identity, event.room_id, meta?.receipt_id, meta?.agent_id], enabled: !!meta && allowed && scope.ownerId > 0, retry: false, queryFn: async () => {
    if (!meta || !scope.canUse() || !canUse()) throw new Error('Memory change unavailable')
    const data = await memorySharingApi.readChange(meta.agent_id, meta.receipt_id)
    if (!scope.canUse() || !canUse() || !obj(data)) throw new Error('Memory change unavailable')
    for (const key of ['version','receipt_id','scope','operation','state','agent_id','root_request_event_id','content','memory_id','revision','entry_revision','effective','history_retained','kind','group_allowed'] as const) if (data[key] !== meta[key]) throw new Error('Memory change mismatch')
    if (!sameOrigin(data.origin, meta.origin) || !validBotMessageRef(data.root_ref) || data.root_ref.room_id !== event.room_id || data.root_ref.event_id !== meta.root_request_event_id || data.root_ref.actor_type !== 'user' || data.root_ref.actor_id !== scope.ownerId || data.root_ref.kind !== 'message' || data.root_ref.seq >= event.seq
      || !validBotMessageRef(data.result_ref) || data.result_ref.room_id !== event.room_id || data.result_ref.event_id !== event.event_id || data.result_ref.seq !== event.seq || data.result_ref.actor_type !== 'system' || data.result_ref.actor_id !== null || data.result_ref.kind !== 'activity' || typeof data.replayed !== 'boolean') throw new Error('Memory change mismatch')
    return data.root_ref
  } })
  if (!meta) return null
  const label = meta.scope === 'owner_common' ? meta.operation === 'add' ? 'memorySharing.commonSaved' : meta.operation === 'edit' ? 'memorySharing.commonUpdated' : 'memorySharing.commonForgotten' : meta.operation === 'policy' ? 'memorySharing.policySaved' : 'memorySharing.visibilitySaved'
  return <section className="bot-creation stack stack--tight">{read.isSuccess ? <>
    <div><strong>{t(label)}</strong>{meta.operation === 'policy' ? <> · {t(meta.content === 'owner_rooms' ? 'memorySharing.other' : 'memorySharing.only')}</> : <> · {Array.from(meta.content).length > 120 ? `${Array.from(meta.content).slice(0,120).join('')}…` : meta.content}{meta.operation === 'visibility' && <> · {t(meta.group_allowed ? 'memorySharing.private' : 'memorySharing.privateOff')}</>}</>}</div>
    <details className="compact-details"><summary>{t('memorySharing.details')}</summary>{meta.operation !== 'policy' && Array.from(meta.content).length > 120 && <p className="bot-message__content">{meta.content}</p>}{meta.history_retained && <p>{t('memorySharing.retained')}</p>}<button type="button" className="text-link" onClick={() => { if (scope.canUse() && canUse()) onOpen(read.data.seq, read.data.event_id) }}>{t('memorySharing.request')}</button></details>
  </> : <><span role="status">{t(read.isFetching ? 'memorySharing.saving' : 'memorySharing.eventUnknown')}</span>{!read.isFetching && allowed && <button type="button" className="text-link" onClick={() => { if (scope.canUse() && canUse()) void read.refetch({ cancelRefetch: false }) }}>{t('memorySharing.eventRead')}</button>}</>}</section>
}
