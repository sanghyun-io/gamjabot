import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { skillsApi, type Skill, type SkillFields } from '../api/skills'
import { useBotScope } from '../lib/useBotScope'
import { canonicalEventId } from '../lib/peerReply'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import ServerTime from '../components/ServerTime'
import SkillPackages from '../components/SkillPackages'

type Source = { room_id: number; root_event_id: string; result_event_id: string; agent_id: number }
type Create = { key: string; body: SkillFields; source: Source | null; phase: 'sending'|'unknown' }
const pending = new Map<string, Create>()
useAuthStore.subscribe((current, previous) => { if (!current.token || current.epoch !== previous.epoch || current.user?.id !== previous.user?.id) pending.clear() })
const status = (failure: unknown) => (failure as { response?: { status?: number } }).response?.status
const definite = (failure: unknown) => !!status(failure) && [400,401,403,404,409,413,422,429].includes(status(failure)!)
const clean = (fields: SkillFields): SkillFields => ({ name: fields.name.trim(), description: fields.description.trim(), content: fields.content.trim() })
const blank: SkillFields = { name: '', description: '', content: '' }
function sourceFrom(params: URLSearchParams): Source | null {
  const room_id = Number(params.get('room')), agent_id = Number(params.get('agent')), root_event_id = params.get('request'), result_event_id = params.get('result')
  return Number.isSafeInteger(room_id) && room_id > 0 && Number.isSafeInteger(agent_id) && agent_id > 0 && canonicalEventId(root_event_id) && canonicalEventId(result_event_id) ? { room_id, agent_id, root_event_id, result_event_id } : null
}
function resultMatches(item: Skill, request: Create) {
  return item.name === request.body.name && item.description === request.body.description && item.content === request.body.content
    && (request.source === null ? item.source === null && item.active : !!item.source && !('kind' in item.source) && !item.active && item.source.room_id === request.source.room_id && item.source.agent_id === request.source.agent_id && item.source.root_event_id === request.source.root_event_id && item.source.result_event_id === request.source.result_event_id)
}
export default function Skills() {
  const { t } = useI18n(), scope = useBotScope(), cache = useQueryClient(), [params, setParams] = useSearchParams(), source = sourceFrom(params)
  const target = params.get('skill'), targetId = canonicalEventId(target) ? target : null
  const lock = useRef(false), [search, setSearch] = useState(''), [fields, setFields] = useState<SkillFields>(blank)
  const [creating, setCreating] = useState(!!source), [createState, setCreateState] = useState<Create | null>(() => pending.get(scope.identity) || null)
  const [message, setMessage] = useState<'saved'|'sourceRejected'|'unknown'|'rejected'|'limit' | null>(null)
  const list = useQuery({ queryKey: ['skills', scope.identity, search], enabled: !targetId, retry: false, queryFn: async () => { const rows = await skillsApi.list(search); if (!scope.canUse()) throw new Error('Owner changed'); return rows } })
  const selected = useQuery({ queryKey: ['skill', scope.identity, targetId], enabled: !!targetId, retry: false, queryFn: async () => { if (!targetId) throw new Error('Skill unavailable'); const item = await skillsApi.read(targetId); if (!scope.canUse()) throw new Error('Owner changed'); return item } })
  useEffect(() => { setFields(blank); setMessage(null); setCreateState(pending.get(scope.identity) || null) }, [scope.identity])
  useEffect(() => () => { const op = pending.get(scope.identity); if (op?.phase === 'sending') pending.set(scope.identity, { ...op, phase: 'unknown' }) }, [scope.identity])
  const publishResult = (item: Skill, request: Create) => { if (!scope.canUse() || !resultMatches(item, request)) throw new Error('Skill result mismatch'); pending.delete(scope.identity); setCreateState(null); setCreating(false); setFields(blank); setMessage('saved'); setParams({}); void cache.invalidateQueries({ queryKey: ['skills', scope.identity] }) }
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (!scope.canUse() || lock.current || createState) return
    const body = clean(fields); if (!body.name || body.name.length > 120 || body.description.length > 500 || !body.content || body.content.length > 8000) return
    const request: Create = { key: crypto.randomUUID(), body, source, phase: 'sending' }; pending.set(scope.identity, request); setCreateState(request); lock.current = true; setMessage(null)
    try { const item = source ? await skillsApi.fromRequest({ ...body, ...source }, request.key) : await skillsApi.create(body, request.key); if (scope.canUse()) publishResult(item, request) }
    catch (failure) { if (scope.canUse()) { if (definite(failure)) { pending.delete(scope.identity); setCreateState(null); const code=(failure as {response?:{data?:{detail?:{code?:string}}}}).response?.data?.detail?.code; setMessage(code === 'skill_source_not_completed' ? 'sourceRejected' : code === 'skill_library_limit' ? 'limit' : 'rejected') } else { const unknown = { ...request, phase: 'unknown' as const }; pending.set(scope.identity, unknown); setCreateState(unknown) } } }
    finally { lock.current = false }
  }
  const check = async () => { const op = pending.get(scope.identity); if (!scope.canUse() || lock.current || !op || op.phase !== 'unknown') return; lock.current = true
    try { const item = await skillsApi.command(op.key); if (scope.canUse()) publishResult(item, op) } catch { if (scope.canUse()) setMessage('unknown') } finally { lock.current = false } }
  return <main className="page-frame page-frame--wide page-content stack skills-page">
    <header className="page-heading"><div><h1 className="page-title">{t('skills.title')}</h1><p className="section-copy">{t('skills.description')}</p></div><button className="button button--primary" disabled={!!createState} onClick={() => { if (scope.canUse()) { setParams({}); setCreating(true); setMessage(null) } }}>{t('skills.new')}</button></header>
    {message && <p role={message === 'saved' ? 'status' : 'alert'}>{t(message === 'saved' ? 'skills.saved' : message === 'sourceRejected' ? 'skills.sourceRejected' : message === 'limit' ? 'skills.limit' : message === 'rejected' ? 'skills.rejected' : 'skills.pending')}</p>}
    {createState && <section className="notice notice--warning stack stack--tight"><p role="status">{t(createState.phase === 'sending' ? 'skills.saving' : 'skills.unknown')}</p>{createState.phase === 'unknown' && <><p className="metadata">{t('skills.checkUnresolved')}</p><button className="button button--secondary button--compact" onClick={() => { void check() }}>{t('skills.check')}</button></>}</section>}
    {creating && <form className="stack skill-editor" onSubmit={event => { void save(event) }}><h2 className="section-title">{t(source ? 'skills.draftTitle' : 'skills.new')}</h2>{source && <p className="field-hint">{t('skills.draftHelp')}</p>}
      <label className="field">{t('skills.name')}<input value={fields.name} maxLength={120} disabled={!!createState} onChange={event => setFields({...fields,name:event.target.value})} /></label>
      <label className="field">{t('skills.summary')}<input value={fields.description} maxLength={500} disabled={!!createState} onChange={event => setFields({...fields,description:event.target.value})} /></label>
      <label className="field">{t('skills.content')}<textarea value={fields.content} rows={8} maxLength={8000} disabled={!!createState} onChange={event => setFields({...fields,content:event.target.value})} /></label>
      <div className="page-actions"><button className="button button--primary button--compact" disabled={!!createState || !fields.name.trim() || !fields.content.trim()}>{t('skills.create')}</button><button type="button" className="button button--ghost button--compact" disabled={!!createState} onClick={() => { setCreating(false); setParams({}) }}>{t('common.cancel')}</button></div>
    </form>}
    {!targetId && <SkillPackages />}
    {targetId && <button className="text-link" type="button" onClick={() => { if (scope.canUse()) setParams({}) }}>{t('skills.goLibrary')}</button>}
    {!targetId && <label className="field skills-search">{t('skills.search')}<input type="search" maxLength={120} value={search} onChange={event => setSearch(event.target.value)} /></label>}
    {targetId && selected.isPending && <p role="status">{t('common.loading')}</p>}{targetId && selected.isError && <p role="alert">{t('skills.readFailed')} <button className="text-link" onClick={() => { if (scope.canUse()) void selected.refetch({ cancelRefetch: false }) }}>{t('skills.reload')}</button></p>}
    {targetId && selected.data && <SkillRow key={selected.data.skill_id} item={selected.data} identity={scope.identity} refresh={() => { void selected.refetch({ cancelRefetch: false }) }} />}
    {!targetId && list.isPending && <p role="status">{t('common.loading')}</p>}{!targetId && list.isError && <p role="alert">{t('skills.readFailed')} <button className="text-link" onClick={() => { if (scope.canUse()) void list.refetch({ cancelRefetch: false }) }}>{t('skills.reload')}</button></p>}
    {!targetId && list.isSuccess && !list.data.length && <p>{t(search ? 'skills.noMatches' : 'skills.empty')}</p>}
    {!targetId && list.data?.map(item => <SkillRow key={item.skill_id} item={item} identity={scope.identity} refresh={() => { void cache.invalidateQueries({ queryKey: ['skills', scope.identity] }) }} />)}
  </main>
}
function SkillRow({ item, identity, refresh }: { item: Skill; identity: string; refresh: () => void }) {
  const { t } = useI18n(), scope = useBotScope(), cache = useQueryClient(), lock = useRef(false)
  const install = (value: Skill) => { cache.setQueryData(['skill', identity, value.skill_id], value); cache.setQueriesData<Skill[]>({ queryKey: ['skills', identity] }, rows => rows?.map(row => row.skill_id === value.skill_id ? value : row)) }
  const [edit, setEdit] = useState<(SkillFields & { base: number; active: boolean }) | null>(null), [phase, setPhase] = useState<'idle'|'sending'|'unknown'|'conflict'|'rejected'>('idle')
  const [historyOpen, setHistoryOpen] = useState(false), changed = !!edit && edit.base !== item.revision
  const history = useInfiniteQuery({ queryKey: ['skill-versions', identity, item.skill_id], enabled: historyOpen, retry: false, initialPageParam: null as number | null, queryFn: async ({ pageParam }) => { const data = await skillsApi.versions(item.skill_id, pageParam); if (!scope.canUse()) throw new Error('Owner changed'); return data }, getNextPageParam: last => last.next_before_revision ?? undefined })
  const update = async (body: SkillFields & { active: boolean }, base: number) => {
    if (!scope.canUse() || lock.current || phase !== 'idle' || item.revision !== base) return
    lock.current = true; setPhase('sending')
    try { const result = await skillsApi.update(item.skill_id, base, body); if (scope.canUse()) { install(result); setEdit(null); setPhase('idle'); void cache.invalidateQueries({ queryKey: ['skill-versions', identity, item.skill_id] }); refresh() } }
    catch (failure) { if (scope.canUse()) { const response=(failure as {response?:{status?:number;data?:{detail?:{code?:string}}}}).response; setPhase(response?.status === 409 ? response.data?.detail?.code === 'skill_revision_conflict' ? 'conflict' : 'rejected' : 'unknown') } }
    finally { lock.current = false }
  }
  const read = async () => { if (!scope.canUse() || lock.current) return; lock.current = true; try { const result = await skillsApi.read(item.skill_id); if (scope.canUse()) { install(result); setPhase('idle'); setEdit(null); refresh() } } catch {} finally { lock.current = false } }
  return <article className="skill-row stack stack--tight"><header className="split"><div><h2 className="section-title">{item.name}</h2><p className="metadata">{item.active ? t('skills.active') : t('skills.inactive')}</p></div></header>
    {item.description && <p>{item.description}</p>}<details className="compact-details"><summary>{t('skills.content')}</summary><p className="skill-content">{item.content}</p></details>
    {item.package_provenance && <div className="metadata"><p>{t(item.package_modified ? 'packages.modified' : 'packages.original')} · {item.package_provenance.package_id} · {item.package_provenance.package_version} · {item.package_provenance.author} · {item.package_provenance.license}</p><p>{t('packages.unverified')}</p><details className="compact-details"><summary>{t('packages.sourceDetails')}</summary><p>{t(item.package_provenance.origin==='builtin'?'packages.builtin':'packages.manual')} · {item.package_provenance.digest}</p></details></div>}
    {item.source && 'kind' in item.source && <p className="metadata">{t('demonstration.source')} · {t('demonstration.review')}</p>}
    {item.source && !('kind' in item.source) && <p className="metadata">{t('skills.source')} · <Link to={`/chat/${item.source.room_id}?${new URLSearchParams({seq:String(item.source.root_seq),event:item.source.root_event_id})}`}>{t('skills.request')}</Link> · <Link to={`/chat/${item.source.room_id}?${new URLSearchParams({seq:String(item.source.result_seq),event:item.source.result_event_id})}`}>{t('skills.result')}</Link></p>}
    {(phase === 'unknown' || phase === 'conflict' || phase === 'rejected') && <p role="alert">{t(phase === 'unknown' ? 'skills.unknown' : phase === 'rejected' ? 'skills.bindingRejected' : 'skills.conflict')} <button className="text-link" onClick={() => { void read() }}>{t('skills.reload')}</button></p>}
    {edit ? <form className="stack stack--tight" onSubmit={event => { event.preventDefault(); const body = clean(edit); if (body.name && body.content) void update({ ...body, active: edit.active }, edit.base) }}>
      <label className="field">{t('skills.name')}<input value={edit.name} maxLength={120} disabled={phase !== 'idle'} onChange={event => setEdit({...edit,name:event.target.value})} /></label>
      <label className="field">{t('skills.summary')}<input value={edit.description} maxLength={500} disabled={phase !== 'idle'} onChange={event => setEdit({...edit,description:event.target.value})} /></label>
      <label className="field">{t('skills.content')}<textarea rows={7} value={edit.content} maxLength={8000} disabled={phase !== 'idle'} onChange={event => setEdit({...edit,content:event.target.value})} /></label>
      {changed && <p role="status">{t('skills.conflict')}</p>}<div className="page-actions"><button className="button button--primary button--compact" disabled={phase !== 'idle' || changed || !edit.name.trim() || !edit.content.trim()}>{t('skills.save')}</button><button type="button" className="button button--ghost button--compact" disabled={phase === 'sending'} onClick={() => setEdit(null)}>{t('common.cancel')}</button></div></form>
      : <div className="page-actions"><button className="button button--ghost button--compact" disabled={phase !== 'idle'} onClick={() => setEdit({name:item.name,description:item.description,content:item.content,active:item.active,base:item.revision})}>{t('skills.edit')}</button><button className="button button--ghost button--compact" disabled={phase !== 'idle'} onClick={() => { void update({name:item.name,description:item.description,content:item.content,active:!item.active},item.revision) }}>{t(item.active ? 'skills.deactivate' : 'skills.activate')}</button></div>}
    <p className="metadata">{t('skills.statusHelp')}</p><details className="compact-details" onToggle={event => setHistoryOpen(event.currentTarget.open)}><summary>{t('skills.history')}</summary>{history.isError && <p role="alert">{t('skills.readFailed')}</p>}{history.data?.pages.flatMap(page => page.versions).map(version => <details key={version.revision}><summary><ServerTime value={version.updated_at}/> · {version.active ? t('skills.active') : t('skills.inactive')}</summary><strong>{version.name}</strong><p>{version.description}</p><p className="skill-content">{version.content}</p></details>)}{history.hasNextPage && <button type="button" className="text-link" disabled={history.isFetchingNextPage} onClick={() => { if (scope.canUse()) void history.fetchNextPage() }}>{t('skills.moreVersions')}</button>}</details>
  </article>
}
