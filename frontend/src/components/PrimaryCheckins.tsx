import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { primaryCheckinsApi, checkinDraftCurrent, type CheckinDraftBase, type CheckinState, type CheckinWrite, type PrimaryCheckin } from '../api/primaryCheckins'
import { botsApi, type PrimaryBot } from '../api/bots'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'
import ServerTime from './ServerTime'

type Draft = { enabled: boolean; interval: string; prompt: string; base: CheckinDraftBase }
const toDraft = (value: PrimaryCheckin, fallback: string, agentId: number | null): Draft => ({ enabled: value.enabled && value.availability === 'available', interval: String(value.interval_minutes), prompt: value.prompt || fallback, base: { revision: value.revision, primary_revision: value.primary_revision, agent_id: agentId } })
export default function PrimaryCheckins({ primary }: { primary: PrimaryBot | undefined }) {
  const { t } = useI18n(), scope = useBotScope(), cache = useQueryClient(), lock = useRef(false)
  const live = useRef(primary); live.current = primary
  const [open, setOpen] = useState(false), [draft, setDraft] = useState<Draft | null>(null)
  const [phase, setPhase] = useState<'idle'|'saving'|'unknown'|'conflict'|'unavailable'>('idle'), [saved, setSaved] = useState(false)
  const [validation, setValidation] = useState(false), [reading, setReading] = useState(false)
  const key = ['primary-checkins', scope.identity, primary?.revision, primary?.agent_id] as const
  const query = useQuery({ queryKey: key, enabled: open && !!primary && scope.ownerId > 0 && !reading && phase !== 'saving', refetchInterval: open && !reading && phase !== 'saving' ? 5000 : false, refetchIntervalInBackground: false, retry: false, queryFn: async () => {
    const data = await primaryCheckinsApi.get(); if (!scope.canUse()) throw new Error('Owner changed'); return data
  } })
  const runs = useQuery({ queryKey: ['primary-checkin-runs', scope.identity], enabled: open && scope.ownerId > 0, refetchInterval: open ? 5000 : false, refetchIntervalInBackground: false, retry: false, queryFn: async () => {
    const data = await primaryCheckinsApi.runs(); if (!scope.canUse()) throw new Error('Owner changed'); return data
  } })
  useEffect(() => { setDraft(null); setSaved(false); setValidation(false); setPhase('idle') }, [scope.identity])
  useEffect(() => { if (query.data && !draft && phase === 'idle') setDraft(toDraft(query.data, t('checkins.defaultPrompt'), primary?.agent_id ?? null)) }, [query.data, draft, phase, t])
  useEffect(() => { if (draft && query.data && primary && phase === 'idle' && !checkinDraftCurrent(draft.base, query.data, primary)) { setPhase('conflict'); setSaved(false) } }, [draft, query.data, primary, phase])
  const inspect = async () => {
    if (!scope.canUse() || lock.current) return
    lock.current = true; setReading(true)
    try {
      await cache.cancelQueries({ queryKey: ['primary-checkins', scope.identity] })
      const selected = await botsApi.primary(), result = await primaryCheckinsApi.get()
      if (!scope.canUse() || result.primary_revision !== selected.revision) return
      cache.setQueryData(['primary-checkins', scope.identity, selected.revision, selected.agent_id], result)
      cache.setQueryData(['primary-bot', scope.ownerId, scope.epoch], selected)
      setDraft(toDraft(result, t('checkins.defaultPrompt'), selected.agent_id)); setPhase('idle'); setSaved(false); setValidation(false)
    } catch { /* Keep the unconfirmed state and allow another read. */ }
    finally { lock.current = false; if (scope.canUse()) setReading(false) }
  }
  const save = async () => {
    if (!scope.canUse() || lock.current || phase !== 'idle' || !primary || !query.isSuccess || query.isFetching || reading || !draft) return
    const minutes = Number(draft.interval)
    if (!/^\d+$/.test(draft.interval) || !Number.isSafeInteger(minutes) || minutes < 1 || minutes > 10080 || !draft.prompt.trim() || draft.prompt.length > 2000 || draft.enabled && (!primary.agent_id || !primary.agent?.is_active)) { setValidation(true); return }
    if (!checkinDraftCurrent(draft.base, query.data, primary)) { setPhase('conflict'); return }
    const selected = primary, body: CheckinWrite = { expected_revision: draft.base.revision, expected_primary_revision: draft.base.primary_revision, enabled: draft.enabled, interval_minutes: minutes, prompt: draft.prompt.trim() }
    lock.current = true; setPhase('saving'); setSaved(false); setValidation(false)
    try {
      await cache.cancelQueries({ queryKey: ['primary-checkins', scope.identity] })
      if (!scope.canUse() || live.current?.revision !== selected.revision || live.current?.agent_id !== selected.agent_id) { setPhase('conflict'); return }
      const result = await primaryCheckinsApi.put(body)
      if (!scope.canUse() || live.current?.revision !== selected.revision || live.current?.agent_id !== selected.agent_id) return
      if (result.enabled && (result.primary_agent_id !== selected.agent_id || result.availability !== 'available')) throw new Error('Check-in target unavailable')
      cache.setQueryData(key, result); setDraft(toDraft(result, t('checkins.defaultPrompt'), selected.agent_id)); setPhase('idle'); setSaved(true)
      void runs.refetch({ cancelRefetch: false })
    } catch (failure) {
      if (scope.canUse()) {
        const status = (failure as { response?: { status?: number } }).response?.status
        const code = (failure as { response?: { data?: { detail?: { code?: string } } } }).response?.data?.detail?.code
        setPhase(status === 409 ? code === 'primary_checkin_room_unavailable' ? 'unavailable' : 'conflict' : 'unknown')
      }
    } finally { lock.current = false; if (scope.canUse() && live.current?.revision !== selected.revision) setPhase('conflict') }
  }
  const duration = draft?.interval ?? '60', preset = ['30','60','1440'].includes(duration) ? duration : 'custom'
  const availability = query.data?.availability, blocked = !primary?.agent_id || !primary.agent?.is_active
  const stateKey = (state: CheckinState) => state === 'unknown' ? 'checkins.runUnknown' : `checkins.${state}` as const
  return <details className="primary-checkins" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="button button--ghost button--compact">{t('checkins.title')}</summary>
    {open && <div className="primary-checkins__panel stack stack--tight">
      <p className="metadata">{t('checkins.description')}</p>
      {blocked && <p role="status">{t('checkins.noPrimary')}</p>}
      {availability === 'primary_changed' && <p role="status">{t('checkins.changed')}</p>}
      {availability && ['room_unavailable','agent_inactive','owner_inactive'].includes(availability) && <p role="status">{t('checkins.unavailable')}</p>}
      {availability === 'profile_changed' && <p role="status">{t('checkins.profileChanged')}</p>}
      {query.isPending && <p role="status">{t('common.loading')}</p>}
      {query.isError && <p role="alert">{t('checkins.readFailed')}{' '}<button className="text-link" type="button" onClick={() => { void inspect() }}>{t('checkins.readAgain')}</button></p>}
      {(phase === 'unknown' || phase === 'conflict' || phase === 'unavailable') && <p role="alert">{t(phase === 'unknown' ? 'checkins.unknown' : phase === 'unavailable' ? 'checkins.unavailable' : 'checkins.conflict')}{' '}<button type="button" className="text-link" disabled={query.isFetching || reading} onClick={() => { void inspect() }}>{t('checkins.readAgain')}</button></p>}
      {draft && query.isSuccess && <form className="stack stack--tight" onSubmit={event => { event.preventDefault(); void save() }}>
        <label className="primary-checkins__toggle"><input type="checkbox" checked={draft.enabled} disabled={blocked || phase !== 'idle'} onChange={event => { setDraft({ ...draft, enabled: event.target.checked }); setSaved(false) }} />{t('checkins.enabled')}</label>
        <label>{t('checkins.interval')}<select value={preset} disabled={phase !== 'idle'} onChange={event => { const v = event.target.value; setDraft({ ...draft, interval: v === 'custom' ? '' : v }); setSaved(false) }}>
          <option value="30">{t('checkins.halfHour')}</option><option value="60">{t('checkins.hour')}</option><option value="1440">{t('checkins.day')}</option><option value="custom">{t('checkins.custom')}</option></select></label>
        {preset === 'custom' && <label>{t('checkins.minutes')}<input type="number" min="1" max="10080" step="1" value={duration === '60' && draft.interval === '' ? '' : draft.interval} disabled={phase !== 'idle'} onChange={event => { setDraft({ ...draft, interval: event.target.value }); setSaved(false) }} /></label>}
        <label>{t('checkins.prompt')}<textarea rows={3} maxLength={2000} value={draft.prompt} disabled={phase !== 'idle'} onChange={event => { setDraft({ ...draft, prompt: event.target.value }); setSaved(false) }} /></label>
        {validation && <p role="alert">{t('checkins.invalid')}</p>}
        <div><button type="submit" className="button button--primary button--compact" disabled={phase !== 'idle' || query.isFetching || reading || blocked && draft.enabled}>{t(phase === 'saving' ? 'checkins.saving' : 'checkins.save')}</button></div>
        {saved && <p role="status" className="metadata">{t('checkins.saved')}</p>}
      </form>}
      {query.data?.enabled && availability === 'available' && query.data.next_run_at && <p className="metadata">{t('checkins.next')} · <ServerTime value={query.data.next_run_at} /></p>}
      <div><strong>{t('checkins.history')}</strong>{runs.isError && <p role="alert">{t('checkins.historyFailed')}{' '}<button className="text-link" type="button" onClick={() => { if (scope.canUse()) void runs.refetch({ cancelRefetch: false }) }}>{t('checkins.readAgain')}</button></p>}
        {runs.isSuccess && !runs.data.runs.length && <p className="metadata">{t('checkins.empty')}</p>}
        {runs.isSuccess && runs.data.runs.map(run => <div className="primary-checkins__run" key={run.run_id}><span>{run.primary_agent_id === primary?.agent_id ? primary.agent?.name : t('checkins.previousBot')} · {t(stateKey(run.state === 'completed' && !run.result_ref ? 'unknown' : run.state))} · <ServerTime value={run.completed_at || run.started_at || run.scheduled_at} /></span>{(run.result_ref || run.root_ref) && <Link className="text-link" to={`/chat/${run.room_id}?${new URLSearchParams({ seq: String((run.result_ref || run.root_ref)!.seq), event: (run.result_ref || run.root_ref)!.event_id })}`}>{t(run.result_ref ? 'checkins.result' : 'checkins.source')}</Link>}</div>)}
      </div>
    </div>}
  </details>
}
