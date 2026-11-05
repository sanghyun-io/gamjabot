import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { memorySharingApi, type CommonFact, type MemoryPolicy } from '../api/memorySharing'
import type { BotMemoryEntry } from '../api/botMemories'
import { agentsApi } from '../api/agents'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'

type Phase = 'sending'|'unknown'|'conflict'
const pending = new Map<string, Phase>()
useAuthStore.subscribe((current, previous) => { if (current.epoch !== previous.epoch || current.user?.id !== previous.user?.id || !current.token) pending.clear() })
function usePending(key: string) {
  const [phase, setPhase] = useState<Phase | null>(() => pending.get(key) || null)
  const set = (value: Phase | null) => { if (value) pending.set(key, value); else pending.delete(key); setPhase(value) }
  useEffect(() => { setPhase(pending.get(key) || null); return () => { if (pending.get(key) === 'sending') pending.set(key, 'unknown') } }, [key])
  return [phase, set] as const
}
const status = (failure: unknown) => (failure as { response?: { status?: number } }).response?.status
const rejected = (failure: unknown) => !!status(failure) && [400,401,403,404,409,413,422,429].includes(status(failure)!)

export function MemoryVisibility({ agentId, row, scopeRevision, disabled, refresh }: { agentId: number; row: BotMemoryEntry; scopeRevision: number; disabled: boolean; refresh: () => Promise<void> }) {
  const scope = useBotScope(), { t } = useI18n(), lock = useRef(false), [phase, setPhase] = usePending(`${scope.identity}:visibility:${agentId}:${row.memory_id}`)
  const read = async () => { if (!scope.canUse() || lock.current) return; lock.current = true; try { await refresh(); if (scope.canUse()) setPhase(null) } catch {} finally { lock.current = false } }
  const change = async () => {
    if (!scope.canUse() || disabled || lock.current || phase) return
    lock.current = true; setPhase('sending')
    try { await memorySharingApi.visibility(agentId, row.memory_id, { expected_scope_revision: scopeRevision, expected_revision: row.revision, group_allowed: !row.group_allowed }, crypto.randomUUID()); if (scope.canUse()) { await refresh(); if (scope.canUse()) setPhase(null) } }
    catch (e) { if (scope.canUse()) setPhase(rejected(e) ? 'conflict' : 'unknown') }
    finally { lock.current = false }
  }
  return <div className="memory-sharing__visibility"><button type="button" className="text-link" aria-pressed={row.group_allowed} disabled={disabled || !!phase} onClick={() => { void change() }}>{t(row.group_allowed ? 'memorySharing.private' : 'memorySharing.privateOff')} · {row.group_allowed ? '✓' : '—'}</button><p className="metadata">{t('memorySharing.privateHint')}</p>
    {phase && <p role={phase === 'sending' ? 'status' : 'alert'}>{t(phase === 'sending' ? 'memorySharing.saving' : phase === 'unknown' ? 'memorySharing.unknown' : 'memorySharing.changed')}{phase !== 'sending' && <> <button type="button" className="text-link" onClick={() => { void read() }}>{t('memorySharing.read')}</button></>}</p>}</div>
}

export default function MemorySharing({ agentId, onClose }: { agentId: number; onClose: () => void }) {
  const { t } = useI18n(), scope = useBotScope(), cache = useQueryClient(), lock = useRef(false)
  const [policyPhase, setPolicyPhase] = usePending(`${scope.identity}:policy:${agentId}`)
  const [commonPhase, setCommonPhase] = usePending(`${scope.identity}:common`)
  const [editing, setEditing] = useState<{ fact: CommonFact; scopeRevision: number; text: string } | null>(null)
  const [adding, setAdding] = useState<{ kind: CommonFact['kind']; text: string; scopeRevision: number } | null>(null)
  const [removing, setRemoving] = useState<{ fact: CommonFact; scopeRevision: number } | null>(null)
  const [notice, setNotice] = useState<'saved'|'forgotten'|null>(null)
  const policyKey = ['bot-memory-policy', scope.identity, agentId] as const, commonKey = ['owner-common-memory', scope.identity] as const
  const policy = useQuery({ queryKey: policyKey, retry: false, queryFn: async () => { const value = await memorySharingApi.policy(agentId); if (!scope.canUse()) throw new Error('Owner changed'); return value } })
  const agents = useQuery({ queryKey: ['agents', scope.identity], retry: false, queryFn: async () => { const value = await agentsApi.list(); if (!scope.canUse()) throw new Error('Owner changed'); return value } })
  const common = useQuery({ queryKey: commonKey, retry: false, queryFn: async () => { const value = await memorySharingApi.common(); if (!scope.canUse()) throw new Error('Owner changed'); return value } })
  useEffect(() => { setEditing(null); setAdding(null); setRemoving(null); setNotice(null) }, [scope.identity, agentId])
  const readPolicy = async () => { if (!scope.canUse() || lock.current) return; lock.current = true; try { const result = await policy.refetch({ cancelRefetch: false }); if (scope.canUse() && result.isSuccess) setPolicyPhase(null) } finally { lock.current = false } }
  const changePolicy = async (mode: MemoryPolicy['mode']) => {
    if (!scope.canUse() || lock.current || policyPhase || !policy.isSuccess || policy.isFetching || policy.data.mode === mode) return
    const expected = policy.data.revision; lock.current = true; setPolicyPhase('sending')
    try { await cache.cancelQueries({ queryKey: policyKey }); const value = await memorySharingApi.setPolicy(agentId, expected, mode); if (scope.canUse()) { cache.setQueryData(policyKey, value); setPolicyPhase(null) } }
    catch (e) { if (scope.canUse()) setPolicyPhase(rejected(e) ? 'conflict' : 'unknown') }
    finally { lock.current = false }
  }
  const readCommon = async () => { if (!scope.canUse() || lock.current) return; lock.current = true; try { const result = await common.refetch({ cancelRefetch: false }); if (scope.canUse() && result.isSuccess) { setCommonPhase(null); setEditing(null); setAdding(null); setRemoving(null); setNotice(null) } } finally { lock.current = false } }
  const mutateCommon = async (target: { fact: CommonFact; scopeRevision: number }, content: string | null) => {
    if (!scope.canUse() || lock.current || commonPhase || !common.isSuccess || common.isFetching || target.scopeRevision !== common.data.revision
      || !common.data.items.some(row => row.fact_id === target.fact.fact_id && row.revision === target.fact.revision) || content !== null && (!content.trim() || content.length > 2000)) return
    lock.current = true; setCommonPhase('sending'); setNotice(null)
    try { await cache.cancelQueries({ queryKey: commonKey }); await memorySharingApi.changeCommon(target.fact, target.scopeRevision, content === null ? null : content.trim(), crypto.randomUUID());
      if (scope.canUse()) { const result = await common.refetch({ cancelRefetch: false }); if (!result.isSuccess) { setCommonPhase('unknown'); return }; setCommonPhase(null); setEditing(null); setRemoving(null); setNotice(content === null ? 'forgotten' : 'saved') } }
    catch (e) { if (scope.canUse()) setCommonPhase(rejected(e) ? 'conflict' : 'unknown') }
    finally { lock.current = false }
  }
  const addCommon = async () => {
    if (!adding || !scope.canUse() || lock.current || commonPhase || !common.isSuccess || common.isFetching || adding.scopeRevision !== common.data.revision || !adding.text.trim() || adding.text.length > 2000) return
    lock.current = true; setCommonPhase('sending'); setNotice(null)
    try { await cache.cancelQueries({ queryKey: commonKey }); await memorySharingApi.addCommon(agentId, adding.scopeRevision, adding.kind, adding.text.trim(), crypto.randomUUID())
      if (scope.canUse()) { const result = await common.refetch({ cancelRefetch: false }); if (!result.isSuccess) { setCommonPhase('unknown'); return }; setCommonPhase(null); setAdding(null); setNotice('saved') } }
    catch (e) { if (scope.canUse()) setCommonPhase(rejected(e) ? 'conflict' : 'unknown') }
    finally { lock.current = false }
  }
  const commonChanged = (target: { fact: CommonFact; scopeRevision: number }) => !common.data || target.scopeRevision !== common.data.revision || !common.data.items.some(row => row.fact_id === target.fact.fact_id && row.revision === target.fact.revision)
  return <>
    <details className="compact-details memory-sharing"><summary>{t('memorySharing.scope')}{policy.isSuccess && <> · {t(policy.data.mode === 'canonical_only' ? 'memorySharing.only' : 'memorySharing.other')}</>}</summary><p className="field-hint">{t('memorySharing.scopeHelp')}</p>
      {policy.isError && <p role="alert">{t('memorySharing.failed')} <button type="button" className="text-link" onClick={() => { void readPolicy() }}>{t('memorySharing.read')}</button></p>}
      {policy.isSuccess && <div className="memory-sharing__options">{(['canonical_only','owner_rooms'] as const).map(mode => <label key={mode}><input type="radio" name={`memory-policy-${agentId}`} checked={policy.data.mode === mode} disabled={!!policyPhase || policy.isFetching} onChange={() => { void changePolicy(mode) }} />{t(mode === 'canonical_only' ? 'memorySharing.only' : 'memorySharing.other')}</label>)}</div>}
      {policyPhase && <p role={policyPhase === 'sending' ? 'status' : 'alert'}>{t(policyPhase === 'sending' ? 'memorySharing.saving' : policyPhase === 'unknown' ? 'memorySharing.unknown' : 'memorySharing.scopeChanged')}{policyPhase !== 'sending' && <> <button type="button" className="text-link" onClick={() => { void readPolicy() }}>{t('memorySharing.read')}</button></>}</p>}
    </details>
    <section className="memory-sharing stack stack--tight"><h3 className="section-title">{t('memorySharing.common')}</h3><p className="field-hint">{t('memorySharing.commonHelp')}</p>
      {common.isError && <p role="alert">{t('memorySharing.failed')} <button type="button" className="text-link" onClick={() => { void readCommon() }}>{t('memorySharing.read')}</button></p>}
      {commonPhase && <p role={commonPhase === 'sending' ? 'status' : 'alert'}>{t(commonPhase === 'sending' ? 'memorySharing.saving' : commonPhase === 'unknown' ? 'memorySharing.unknown' : 'memorySharing.changed')}{commonPhase !== 'sending' && <> <button type="button" className="text-link" onClick={() => { void readCommon() }}>{t('memorySharing.read')}</button></>}</p>}
      {notice && <p role="status">{t(notice === 'saved' ? 'memorySharing.saved' : 'memorySharing.forgotten')}</p>}
      {common.isSuccess && !adding && <div><button type="button" className="button button--secondary button--compact" disabled={!!commonPhase || !!editing || !!adding || !!removing || common.isFetching} onClick={() => setAdding({ kind: 'name', text: '', scopeRevision: common.data!.revision })}>{t('memorySharing.add')}</button></div>}
      {adding && <form className="stack stack--tight" onSubmit={event => { event.preventDefault(); void addCommon() }}><label>{t('memorySharing.kind')}<select value={adding.kind} disabled={!!commonPhase} onChange={event => setAdding({ ...adding, kind: event.target.value as CommonFact['kind'] })}><option value="name">{t('memorySharing.name')}</option><option value="timezone">{t('memorySharing.timezone')}</option></select></label><label>{t('memorySharing.value')}<input value={adding.text} maxLength={2000} disabled={!!commonPhase} onChange={event => setAdding({ ...adding, text: event.target.value })} /></label>{common.data && adding.scopeRevision !== common.data.revision && <p role="status">{t('memorySharing.changed')}</p>}<div className="page-actions"><button className="button button--primary button--compact" disabled={!!commonPhase || common.isFetching || !adding.text.trim() || adding.scopeRevision !== common.data?.revision}>{t('common.save')}</button><button type="button" className="button button--ghost button--compact" disabled={!!commonPhase} onClick={() => setAdding(null)}>{t('common.cancel')}</button></div></form>}
      {common.isSuccess && !common.data.items.length && <p className="metadata">{t('memorySharing.empty')}</p>}
      {common.data?.items.map(fact => <article key={fact.fact_id} className="memory-sharing__fact"><strong>{t(fact.kind === 'name' ? 'memorySharing.name' : 'memorySharing.timezone')}</strong><p>{fact.content}</p><p className="metadata">{t('memorySharing.recordedBy', { name: agents.data?.find(agent => agent.id === fact.origin.agent_id)?.name || t('memorySharing.botFallback') })}</p>
        <div className="page-actions">{fact.origin.source_kind === 'event' && fact.origin.room_id && fact.origin.seq && fact.origin.event_id ? <Link className="text-link" to={`/chat/${fact.origin.room_id}?${new URLSearchParams({seq:String(fact.origin.seq),event:fact.origin.event_id})}`} onClick={onClose}>{t('memorySharing.origin')}</Link> : <span className="metadata">{t('memorySharing.manual')}</span>}
          <button type="button" className="text-link" disabled={!!commonPhase || !!editing || !!adding || !!removing || common.isFetching} onClick={() => setEditing({ fact, scopeRevision: common.data!.revision, text: fact.content })}>{t('memorySharing.edit')}</button>
          <button type="button" className="text-link" disabled={!!commonPhase || !!editing || !!adding || !!removing || common.isFetching} onClick={() => setRemoving({ fact, scopeRevision: common.data!.revision })}>{t('memorySharing.forget')}</button></div>
        {editing?.fact.fact_id === fact.fact_id && <form className="stack stack--tight" onSubmit={event => { event.preventDefault(); void mutateCommon(editing, editing.text) }}><label>{t(fact.kind === 'name' ? 'memorySharing.name' : 'memorySharing.timezone')}<input value={editing.text} maxLength={2000} disabled={!!commonPhase} onChange={event => setEditing({ ...editing, text: event.target.value })} /></label>
          {commonChanged(editing) && <p role="status">{t('memorySharing.changed')}</p>}<div className="page-actions"><button className="button button--primary button--compact" disabled={!!commonPhase || common.isFetching || commonChanged(editing) || !editing.text.trim()}>{t('common.save')}</button><button type="button" className="button button--ghost button--compact" disabled={!!commonPhase} onClick={() => setEditing(null)}>{t('common.cancel')}</button></div></form>}
        {removing?.fact.fact_id === fact.fact_id && <div className="stack stack--tight"><p>{t('memorySharing.forgetConfirm')}</p><div className="page-actions"><button type="button" className="button button--danger button--compact" disabled={!!commonPhase || common.isFetching || commonChanged(removing)} onClick={() => { void mutateCommon(removing, null) }}>{t('memorySharing.forget')}</button><button type="button" className="button button--ghost button--compact" disabled={!!commonPhase} onClick={() => setRemoving(null)}>{t('common.cancel')}</button></div></div>}
      </article>)}
    </section>
  </>
}
