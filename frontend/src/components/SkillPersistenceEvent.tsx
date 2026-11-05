import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { eventsApi, type RoomEvent } from '../api/events'
import { validBotMessageRef } from '../api/botMessages'
import { skillsApi, type Skill, type SkillSource, type SkillBindingList } from '../api/skills'
import { canonicalEventId } from '../lib/peerReply'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'

type Meta = { version: 1; receipt_id: string; operation: 'create_ready'|'create_draft'|'edit'; state: 'saved'; agent_id: number; root_request_event_id: string; skill_id: string; revision: number; name: string; description: string; content: string; active: boolean; source: SkillSource | null; creator_binding: { agent_id: number; skill_id: string; revision: 1; enabled: true } | null }
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const positive = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0
function validSource(v: unknown): v is SkillSource | null {
  return v === null || object(v) && positive(v.room_id) && positive(v.agent_id) && canonicalEventId(v.root_event_id) && positive(v.root_seq) && canonicalEventId(v.result_event_id) && positive(v.result_seq) && v.result_seq > v.root_seq
}
function sameSource(a: unknown, b: unknown): boolean {
  if (a === null || b === null) return a === null && b === null
  return object(a) && object(b) && ['room_id','root_event_id','root_seq','result_event_id','result_seq','agent_id'].every(k => a[k] === b[k])
}
export function skillPersistenceMetadata(event: RoomEvent): Meta | null {
  const m = event.payload.skill_persistence
  if (!object(m) || event.kind !== 'activity' || event.actor_type !== 'system' || event.actor_id !== null || event.payload.category !== 'skill_persistence' || event.payload.status !== 'saved'
    || m.version !== 1 || m.state !== 'saved' || typeof m.receipt_id !== 'string' || !/^[0-9a-f]{64}$/.test(m.receipt_id)
    || !['create_ready','create_draft','edit'].includes(String(m.operation)) || !positive(m.agent_id) || !canonicalEventId(m.root_request_event_id) || event.payload.trigger_event_id !== m.root_request_event_id
    || !canonicalEventId(m.skill_id) || !positive(m.revision) || typeof m.name !== 'string' || !m.name.trim() || typeof m.description !== 'string' || typeof m.content !== 'string' || !m.content.trim() || typeof m.active !== 'boolean' || !validSource(m.source)
    || (m.operation === 'create_ready' || m.operation === 'create_draft') && (m.revision !== 1 || m.source === null || !object(m.source) || m.source.agent_id !== m.agent_id)
    || m.operation === 'create_ready' && (!m.active || !object(m.creator_binding) || m.creator_binding.agent_id !== m.agent_id || m.creator_binding.skill_id !== m.skill_id || m.creator_binding.revision !== 1 || m.creator_binding.enabled !== true)
    || m.operation !== 'create_ready' && (m.creator_binding !== null || m.operation === 'create_draft' && m.active)) return null
  return m as Meta
}
export function matchingSkillReceipt(event: RoomEvent, owner: number, data: unknown): boolean {
  const meta = skillPersistenceMetadata(event)
  if (!meta || !object(data)) return false
  for (const key of ['version','receipt_id','operation','state','agent_id','root_request_event_id','skill_id','revision','name','description','content','active'] as const) if (data[key] !== meta[key]) return false
  const bindingMatches = meta.creator_binding === null ? data.creator_binding === null : object(data.creator_binding) && data.creator_binding.agent_id === meta.creator_binding.agent_id && data.creator_binding.skill_id === meta.creator_binding.skill_id && data.creator_binding.revision === meta.creator_binding.revision && data.creator_binding.enabled === true
  return bindingMatches && sameSource(data.source,meta.source) && validBotMessageRef(data.root_ref) && data.root_ref.room_id === event.room_id && data.root_ref.event_id === meta.root_request_event_id && data.root_ref.actor_type === 'user' && data.root_ref.actor_id === owner && data.root_ref.kind === 'message' && data.root_ref.seq < event.seq
    && validBotMessageRef(data.result_ref) && data.result_ref.room_id === event.room_id && data.result_ref.event_id === event.event_id && data.result_ref.seq === event.seq && data.result_ref.actor_type === 'system' && data.result_ref.actor_id === null && data.result_ref.kind === 'activity' && typeof data.replayed === 'boolean'
}
export function matchingSkillVersion(meta: Meta, item: Skill): boolean {
  return item.skill_id === meta.skill_id && item.revision === meta.revision && item.name === meta.name && item.description === meta.description && item.content === meta.content && item.active === meta.active && sameSource(item.source, meta.source)
}
export function readyBindingState(meta: Meta, bindings: SkillBindingList): 'same'|'changed'|'unknown' {
  if (meta.operation !== 'create_ready' || bindings.agent_id !== meta.agent_id) return 'unknown'
  const item = bindings.items.find(row => row.skill_id === meta.skill_id)
  if (!item || item.revision < 1) return 'unknown'
  if (item.revision === 1) return item.enabled ? 'same' : 'unknown'
  return 'changed'
}
export default function SkillPersistenceEvent({ event, allowed, canUse, onOpen }: { event: RoomEvent; allowed: boolean; canUse: () => boolean; onOpen: (seq: number, eventId: string) => void }) {
  const { t } = useI18n(), scope = useBotScope(), meta = skillPersistenceMetadata(event)
  const query = useQuery({ queryKey: ['skill-persistence', scope.identity, event.room_id, event.event_id, event.seq, meta], enabled: !!meta && allowed && scope.ownerId > 0, retry: false, queryFn: async () => {
    if (!meta || !scope.canUse() || !canUse()) throw new Error('Skill result unavailable')
    const receipt = await skillsApi.persistence(meta.agent_id, meta.receipt_id)
    if (!scope.canUse() || !canUse() || !matchingSkillReceipt(event, scope.ownerId, receipt) || !object(receipt) || !validBotMessageRef(receipt.root_ref)) throw new Error('Skill result unavailable')
    const refs = [receipt.root_ref, ...(meta.source ? [{ room_id: meta.source.room_id, event_id: meta.source.root_event_id, seq: meta.source.root_seq }, { room_id: meta.source.room_id, event_id: meta.source.result_event_id, seq: meta.source.result_seq }] : [])]
    const actual = await Promise.all(refs.map(async ref => (await eventsApi.list(ref.room_id, ref.seq - 1, 1))[0]))
    if (!scope.canUse() || !canUse() || actual.some((row, index) => !row || row.room_id !== refs[index].room_id || row.event_id !== refs[index].event_id || row.seq !== refs[index].seq)
      || actual[0].actor_type !== 'user' || actual[0].actor_id !== scope.ownerId || actual[0].kind !== 'message'
      || meta.source && (actual[1].actor_type !== 'user' || actual[1].actor_id !== scope.ownerId || actual[1].kind !== 'message'
        || actual[2].actor_type !== 'agent' || actual[2].actor_id !== meta.source.agent_id || actual[2].kind !== 'message' || actual[2].payload.trigger_event_id !== meta.source.root_event_id)) throw new Error('Skill source unavailable')
    const current = await skillsApi.read(meta.skill_id)
    if (!scope.canUse() || !canUse() || current.revision < meta.revision) throw new Error('Skill result unavailable')
    let snapshot: Skill | undefined = current.revision === meta.revision ? current : undefined
    let before: number | null = null, pages = 0
    while (!snapshot && current.revision > meta.revision && pages++ < 20) {
      const page = await skillsApi.versions(meta.skill_id, before)
      if (!scope.canUse() || !canUse()) throw new Error('Skill result unavailable')
      snapshot = page.versions.find(item => item.revision === meta.revision)
      const next = page.next_before_revision
      if (snapshot || next === null || next <= meta.revision) break
      if (before !== null && next >= before) throw new Error('Skill history changed')
      before = next
    }
    if (!snapshot || !matchingSkillVersion(meta, snapshot)) throw new Error('Skill result mismatch')
    let bindingChanged = false
    if (meta.operation === 'create_ready') {
      const bindings = await skillsApi.bindings(meta.agent_id)
      const state = readyBindingState(meta, bindings)
      if (!scope.canUse() || !canUse() || state === 'unknown') throw new Error('Skill binding unavailable')
      bindingChanged = state === 'changed'
    }
    return { root: receipt.root_ref, changed: current.revision > meta.revision, readyCurrent: current.active, bindingChanged }
  } })
  if (!meta) return null
  return <section className="bot-creation stack stack--tight" aria-label={t('skillEvent.record')}>
    {query.isSuccess ? <><div style={{ overflowWrap: 'anywhere' }}><strong>{t(meta.operation === 'create_draft' ? 'skillEvent.created' : meta.operation === 'create_ready' ? 'skillEvent.readySaved' : 'skillEvent.edited')}</strong> · {meta.name}</div>
      {meta.operation === 'create_draft' && <p className="metadata">{t('skillEvent.draft')}</p>}
      {meta.operation === 'create_ready' && <p className="metadata">{t(query.data.bindingChanged ? 'skillEvent.bindingChanged' : query.data.readyCurrent ? 'skillEvent.readyHint' : 'skillEvent.readyPrevious')}</p>}
      {query.data.changed && <p className="metadata">{t('skillEvent.changed')}</p>}
      <div className="page-actions"><button className="text-link" type="button" onClick={() => { if (scope.canUse() && canUse()) onOpen(query.data.root.seq, query.data.root.event_id) }}>{t('skillEvent.request')}</button>
        {meta.source && <><Link className="text-link" to={`/chat/${meta.source.room_id}?${new URLSearchParams({ seq: String(meta.source.root_seq), event: meta.source.root_event_id })}`} onClick={e => { if (!scope.canUse() || !canUse()) e.preventDefault() }}>{t('skillEvent.sourceRequest')}</Link><Link className="text-link" to={`/chat/${meta.source.room_id}?${new URLSearchParams({ seq: String(meta.source.result_seq), event: meta.source.result_event_id })}`} onClick={e => { if (!scope.canUse() || !canUse()) e.preventDefault() }}>{t('skillEvent.sourceResult')}</Link></>}
        <Link className="text-link" to={`/skills?${new URLSearchParams({ skill: meta.skill_id })}`} onClick={e => { if (!scope.canUse() || !canUse()) e.preventDefault() }}>{t('skillEvent.open')}</Link></div></>
      : <><span role="status">{t(query.isFetching ? 'skillEvent.checking' : 'skillEvent.unknown')}</span>{!query.isFetching && allowed && <button type="button" className="text-link" onClick={() => { if (scope.canUse() && canUse()) void query.refetch({ cancelRefetch: false }) }}>{t('skillEvent.readAgain')}</button>}</>}
  </section>
}
