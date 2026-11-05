import { useImeFormGuard } from '../lib/useImeFormGuard'
import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { CheckCircle2, Users } from 'lucide-react'
import { starterApi, type StarterIntent, type StarterPreview, type StarterResult } from '../api/starter'
import { connectionsApi } from '../api/connections'
import { readinessApi } from '../api/readiness'
import { draftsApi } from '../api/drafts'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { editorSaveFailure } from './EditorSaveNotice'
import { freezeStarterIntent, readStarterWorkspace, starterHasWork, subscribeStarterWorkspace, updateStarterWorkspace,
  type StarterForm, type StarterOperation, type StarterRequest, type StarterWorkspace } from '../lib/starterWorkspace'

type ReadyBinding = { connectionId: number; label: string; modelId: string }
const positiveId = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0
const validResultIds = (value: StarterResult | null | undefined) => !!value && positiveId(value.project_id) && positiveId(value.room_id)
  && positiveId(value.agents?.proposer) && positiveId(value.agents?.reviewer) && value.agents.proposer !== value.agents.reviewer
const intentKey = (value: StarterIntent) => JSON.stringify(value)
function matchesPreview(value: StarterPreview, intent: StarterIntent) {
  return !!value && /^[0-9a-f]{64}$/.test(value.plan_hash) && typeof value.project_title === 'string' && !!value.project_title
    && value.room_title === intent.room_title && Array.isArray(value.agents) && value.agents.length === 2
    && intent.agents.every(agent => value.agents.filter(row => row.role === agent.role && row.requested_name === agent.name
      && row.connection_id === agent.connection_id && row.model_id === agent.model_id && typeof row.name === 'string' && !!row.name
      && positiveId(row.test_history_id) && positiveId(row.config_revision) && typeof row.config_fingerprint === 'string' && !!row.config_fingerprint).length === 1)
    && value.policy?.max_rounds === 2 && value.policy.max_tool_calls === 0 && value.policy.max_executor_seconds === 60
}

export default function FirstSuccessGuide({ activeRoomCount = 0 }: { activeRoomCount?: number }) {
  const owner = useAuthStore(state => state.user?.id ?? 0), epoch = useAuthStore(state => state.epoch)
  return <StarterView key={`${owner}:${epoch}`} identity={`${owner}:${epoch}`} activeRoomCount={activeRoomCount} />
}
function StarterView({ identity, activeRoomCount }: { identity: string; activeRoomCount: number }) {
  const { t } = useI18n(), cache = useQueryClient(), navigate = useNavigate()
  const [generation] = useState(directoryViewGeneration)
  const mounted = useRef(false)
  const bindingDetails = useRef<HTMLDetailsElement>(null)
  const bindingControls = useRef<Partial<Record<'proposer' | 'reviewer', HTMLSelectElement | null>>>({})
  const bindingHelpId = useId()
  const navigation = useRef<{ token: string; button: HTMLButtonElement; cancelled: boolean } | null>(null)
  useEffect(() => {
    mounted.current = true
    const elsewhere = (event: Event) => { const intent = navigation.current; if (intent && event.target instanceof Node && !intent.button.contains(event.target)) intent.cancelled = true }
    const key = (event: KeyboardEvent) => { if (navigation.current && !['Enter', ' '].includes(event.key)) navigation.current.cancelled = true }
    const blur = () => { if (navigation.current) navigation.current.cancelled = true }
    document.addEventListener('pointerdown', elsewhere, true); document.addEventListener('focusin', elsewhere, true)
    document.addEventListener('keydown', key, true); window.addEventListener('blur', blur)
    return () => { mounted.current = false; navigation.current = null; document.removeEventListener('pointerdown', elsewhere, true); document.removeEventListener('focusin', elsewhere, true); document.removeEventListener('keydown', key, true); window.removeEventListener('blur', blur) }
  }, [])
  const sameOwner = () => { const auth = useAuthStore.getState(); return !!auth.token && `${auth.user?.id ?? 0}:${auth.epoch}` === identity && directoryViewGeneration() === generation }
  const read = async <T,>(task: () => Promise<T>): Promise<T> => {
    if (!sameOwner()) throw new Error('Starter Owner changed')
    const value = await task()
    if (!sameOwner()) throw new Error('Starter Owner changed')
    return value
  }
  const workspace = useSyncExternalStore(subscribeStarterWorkspace, () => readStarterWorkspace(identity))
  const edit = (update: (previous: StarterWorkspace) => StarterWorkspace) => sameOwner() && updateStarterWorkspace(identity, generation, update)
  const { form, plan, operation } = workspace
  const { goal, projectTitle, roomTitle, proposerName, reviewerName, proposerBinding, reviewerBinding } = form
  const busy = !!operation, previewing = operation?.kind === 'preview'
  const ime = useImeFormGuard(identity)
  const lastResult = useQuery({ queryKey: ['starter-last-result', identity], retry: false, queryFn: () => read(async () => {
    const value = await starterApi.lastResult()
    if (value !== null && !validResultIds(value)) throw new Error('Latest starter result unavailable')
    return value
  }) })
  const acceptedCurrent = workspace.requests.some(request => request.status === 'accepted' && request.revision === form.revision)
  const formNeeded = !acceptedCurrent && (!lastResult.data || form.dirty || workspace.requests.some(request => request.status !== 'accepted'))
  const connections = useQuery({ queryKey: ['connections', identity, 'first-success'], enabled: lastResult.isSuccess && formNeeded, retry: false,
    queryFn: () => read(connectionsApi.list) })
  const readiness = useQuery({ queryKey: ['readiness', identity, 'first-success'], enabled: lastResult.isSuccess && formNeeded, retry: false,
    queryFn: () => read(readinessApi.get) })
  const bindings = useMemo<ReadyBinding[]>(() => {
    if (!connections.data || !readiness.data) return []
    return readiness.data.connections.flatMap(status => {
      const saved = connections.data.find(item => item.id === status.connection_id), test = status.latest_test
      if (!saved || !saved.enabled || !test?.ok || !test.config_current || !test.model_id) return []
      return [{ connectionId: saved.id, label: saved.label, modelId: test.model_id }]
    })
  }, [connections.data, readiness.data])
  useEffect(() => {
    if (!bindings.length || !connections.isSuccess || !readiness.isSuccess) return
    const binding = `${bindings[0].connectionId}:${bindings[0].modelId}`
    edit(previous => previous.operation || previous.form.proposerBinding && previous.form.reviewerBinding ? previous : ({ ...previous, plan: null,
      form: Object.freeze({ ...previous.form, proposerBinding: previous.form.proposerBinding || binding,
        reviewerBinding: previous.form.reviewerBinding || binding, revision: previous.form.revision + 1 }) }))
  }, [bindings, connections.isSuccess, readiness.isSuccess, operation?.token])
  const change = (patch: Partial<Pick<StarterForm, 'goal' | 'projectTitle' | 'roomTitle' | 'proposerName' | 'reviewerName' | 'proposerBinding' | 'reviewerBinding'>>) => {
    if (!mounted.current) return
    edit(previous => previous.operation && previous.operation.kind !== 'preview' ? previous : ({ ...previous, plan: null, error: '',
      form: Object.freeze({ ...previous.form, ...patch, revision: previous.form.revision + 1, dirty: true }) }))
  }
  const intentFor = (value: StarterForm): StarterIntent => {
    const binding = (raw: string) => { const split = raw.indexOf(':'); return { connection_id: Number(raw.slice(0, split)), model_id: raw.slice(split + 1) } }
    return freezeStarterIntent({ project_title: value.projectTitle.trim() || t('firstSuccess.projectDefault'),
      room_title: value.roomTitle.trim() || value.goal.trim().replace(/\s+/g, ' ').slice(0, 200), agents: [
        { role: 'proposer', name: value.proposerName.trim() || t('firstSuccess.proposerDefault'), ...binding(value.proposerBinding) },
        { role: 'reviewer', name: value.reviewerName.trim() || t('firstSuccess.reviewerDefault'), ...binding(value.reviewerBinding) },
      ] })
  }
  const selectedBindingsReady = [proposerBinding, reviewerBinding].every(raw => bindings.some(item => `${item.connectionId}:${item.modelId}` === raw))
  const unavailableRoles = (['proposer', 'reviewer'] as const).filter(role => {
    const raw = role === 'proposer' ? proposerBinding : reviewerBinding
    return !!raw && !bindings.some(item => `${item.connectionId}:${item.modelId}` === raw)
  })
  const readingBindings = connections.isFetching || readiness.isFetching
  const refreshBindings = () => {
    if (!mounted.current || !sameOwner() || busy || readingBindings || !lastResult.isSuccess || !formNeeded) return
    void connections.refetch({ cancelRefetch: false }); void readiness.refetch({ cancelRefetch: false })
  }
  const focusBinding = (role: 'proposer' | 'reviewer') => {
    const details = bindingDetails.current, control = bindingControls.current[role]
    if (!mounted.current || !sameOwner() || busy || !details?.isConnected || !control?.isConnected || control.matches(':disabled')
      || document.visibilityState !== 'visible' || !document.hasFocus()
      || Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).some(node => !node.hidden && node.getClientRects().length > 0)) return
    details.open = true
    control.focus({ preventScroll: true }); control.scrollIntoView({ block: 'nearest' })
  }
  const canPreview = lastResult.isSuccess && connections.isSuccess && readiness.isSuccess && !readingBindings && selectedBindingsReady && !!goal.trim()
  const planFormCurrent = !!plan && plan.revision === form.revision && intentKey(plan.intent) === intentKey(intentFor(form)) && plan.goal === form.goal.trim()
  // A preview may be newer than the cached metadata that preceded it. Only
  // completed reads received after that preview can expose a known difference.
  // This is UI evidence ordering, not a server clock or an atomic validity gate.
  const afterPreview = (receivedAt: number) => !!plan?.receivedAt && Number.isFinite(plan.receivedAt) && receivedAt > plan.receivedAt
  const planEvidenceChanged = !!plan && planFormCurrent && !readingBindings && connections.isSuccess && readiness.isSuccess
    && plan.value.agents.some(agent => {
      if (afterPreview(connections.dataUpdatedAt)) {
        const saved = connections.data.find(item => item.id === agent.connection_id)
        if (!saved || !saved.enabled || positiveId(saved.config_revision) && saved.config_revision !== agent.config_revision) return true
      }
      if (afterPreview(readiness.dataUpdatedAt)) {
        const status = readiness.data.connections.find(item => item.connection_id === agent.connection_id)
        if (!status || !status.enabled) return true
        const test = status.latest_test
        // Do not treat a different model's latest record as this model's proof.
        if (test?.model_id === agent.model_id && test.ok && test.code === 'connected' && test.config_current) {
          return test.id !== agent.test_history_id || test.config_fingerprint !== agent.config_fingerprint
        }
      }
      return false
    })
  const planCurrent = planFormCurrent && !planEvidenceChanged
  const claim = (snapshot: StarterWorkspace, next: StarterOperation, update: (previous: StarterWorkspace) => StarterWorkspace = value => value) =>
    mounted.current && edit(previous => previous !== snapshot || previous.operation ? previous : { ...update(previous), operation: next, error: '' })
  const during = (sent: StarterOperation, update: (previous: StarterWorkspace) => StarterWorkspace) => edit(previous => previous.operation?.token === sent.token ? update(previous) : previous)
  const finish = (sent: StarterOperation) => during(sent, previous => ({ ...previous, operation: null }))
  const updateRequest = (sent: StarterOperation, key: string, update: (previous: StarterRequest) => StarterRequest) =>
    during(sent, previous => ({ ...previous, requests: previous.requests.map(request => request.key === key ? Object.freeze(update(request)) : request) }))

  const preview = async (event: React.FormEvent) => {
    event.preventDefault()
    if (ime.isSubmitBlocked(event) || !canPreview || !sameOwner()) return
    const snapshot = readStarterWorkspace(identity), intent = intentFor(snapshot.form), sent = Object.freeze({ token: crypto.randomUUID(), kind: 'preview' as const })
    if (!claim(snapshot, sent)) return
    try {
      const value = await starterApi.preview(intent)
      if (!matchesPreview(value, intent)) throw new Error('Starter preview mismatch')
      during(sent, previous => previous.form.revision !== snapshot.form.revision ? { ...previous, error: 'changed' } : ({ ...previous,
        plan: Object.freeze({ intent, value: Object.freeze(value), goal: snapshot.form.goal.trim(), revision: snapshot.form.revision, receivedAt: Date.now() }) }))
    } catch { during(sent, previous => ({ ...previous, error: 'preview' })) }
    finally { finish(sent) }
  }
  const launchApply = async (request: StarterRequest, snapshot: StarterWorkspace, acknowledgeEarlier = false) => {
    if (!sameOwner() || request.mismatch || request.status === 'accepted') return
    const sent = Object.freeze({ token: crypto.randomUUID(), kind: 'apply' as const, requestKey: request.key })
    if (!claim(snapshot, sent, previous => ({ ...previous, requests: previous.requests.some(row => row.key === request.key)
      ? previous.requests.map(row => row.key === request.key ? Object.freeze({ ...row, status: 'sending' as const }) : row)
      : [...previous.requests.map(row => acknowledgeEarlier && (row.status === 'unknown' || row.status === 'conflict' || row.mismatch) ? Object.freeze({ ...row, acknowledged: true }) : row), Object.freeze({ ...request, status: 'sending' as const })] }))) return
    try {
      const result = await starterApi.apply(request.body, request.key)
      if (!validResultIds(result) || typeof result.replayed !== 'boolean') {
        updateRequest(sent, request.key, row => ({ ...row, status: 'unknown', mismatch: true })); return
      }
      during(sent, previous => ({ ...previous,
        form: previous.form.revision === request.revision ? Object.freeze({ ...previous.form, dirty: false }) : previous.form,
        requests: previous.requests.map(row => row.key === request.key ? Object.freeze({ ...row, status: 'accepted' as const, result: Object.freeze(result) }) : row) }))
      if (sameOwner()) {
        void cache.invalidateQueries({ queryKey: ['starter-last-result', identity], exact: true })
        void cache.invalidateQueries({ queryKey: ['dashboard'] }); void cache.invalidateQueries({ queryKey: ['projects'] }); void cache.invalidateQueries({ queryKey: ['agents'] })
      }
    } catch (failure) {
      const code = (failure as { response?: { data?: { detail?: { code?: string } } } }).response?.data?.detail?.code
      updateRequest(sent, request.key, row => row.status === 'accepted' ? row : ({ ...row,
        status: code === 'starter_plan_stale' ? 'stale' : code === 'starter_idempotency_conflict' ? 'conflict' : editorSaveFailure(failure) === 'unknown' ? 'unknown' : 'rejected' }))
      if (code === 'starter_plan_stale' || code === 'starter_idempotency_conflict') during(sent, previous => previous.plan?.value.plan_hash === request.body.plan_hash ? { ...previous, plan: null } : previous)
    } finally { finish(sent) }
  }
  const apply = () => {
    if (!planCurrent || !plan || !canPreview || !sameOwner()) return
    const snapshot = readStarterWorkspace(identity)
    if (snapshot.plan !== plan) return
    const body = Object.freeze({ ...plan.intent, plan_hash: plan.value.plan_hash }), fingerprint = JSON.stringify([body, plan.goal])
    const prior = [...snapshot.requests].reverse().find(row => row.fingerprint === fingerprint && !row.mismatch && row.status !== 'stale' && row.status !== 'conflict')
    if (prior?.status === 'accepted' || prior?.status === 'stale' || prior?.status === 'conflict') return
    const acknowledge = !prior && snapshot.requests.some(row => (row.status === 'unknown' || row.status === 'conflict' || row.mismatch) && !row.acknowledged)
    if (acknowledge && !window.confirm(t('firstSuccess.newRequestConfirm'))) return
    const request: StarterRequest = prior || Object.freeze({ key: crypto.randomUUID(), fingerprint, body, preview: plan.value,
      goal: plan.goal, title: plan.value.room_title, revision: plan.revision, status: 'unknown' as const })
    void launchApply(request, snapshot, acknowledge)
  }
  const seedAndOpen = async (key: string, button: HTMLButtonElement) => {
    const snapshot = readStarterWorkspace(identity), request = snapshot.requests.find(row => row.key === key)
    if (!request?.result || request.status !== 'accepted' || !sameOwner() || snapshot.operation) return
    if (request.seed?.status === 'saved' || request.seed?.status === 'kept') { navigate(`/chat/${request.result.room_id}`); return }
    if (request.seed?.mismatch || request.seed?.status === 'conflict' || request.seed?.status === 'rejected') return
    const sent = Object.freeze({ token: crypto.randomUUID(), kind: 'seed' as const, requestKey: key })
    if (!claim(snapshot, sent)) return
    navigation.current = { token: sent.token, button, cancelled: false }
    button.focus({ preventScroll: true })
    const mayNavigate = () => {
      const target = navigation.current
      return sameOwner() && mounted.current && target?.token === sent.token && !target.cancelled
        && target.button.isConnected && document.activeElement === target.button && document.hasFocus()
    }
    let version = request.seed?.status === 'unknown' ? request.seed.version : undefined, writing = false, ready = false
    try {
      if (version === undefined) {
        updateRequest(sent, key, row => ({ ...row, seed: { status: 'reading' } }))
        const current = await draftsApi.get(request.result.room_id)
        if (!sameOwner() || !mounted.current || navigation.current?.cancelled) return
        if (typeof current.content !== 'string' || !Number.isSafeInteger(current.version) || current.version < 0) throw new Error('Draft read unavailable')
        if (current.content) { updateRequest(sent, key, row => ({ ...row, seed: { status: 'kept' } })); ready = true }
        else version = current.version
      }
      if (!ready) {
        if (!sameOwner() || !mounted.current || navigation.current?.cancelled || version === undefined) return
        updateRequest(sent, key, row => ({ ...row, seed: { status: 'saving', version } })); writing = true
        const value = await draftsApi.put(request.result.room_id, request.goal, version)
        if (value.content !== request.goal || value.version !== version + 1) {
          updateRequest(sent, key, row => ({ ...row, seed: { status: 'unknown', version, mismatch: true } })); return
        }
        updateRequest(sent, key, row => ({ ...row, seed: { status: 'saved', version } })); ready = true
      }
      if (ready && mayNavigate()) navigate(`/chat/${request.result.room_id}`)
    } catch (failure) {
      const status = (failure as { response?: { status?: number } }).response?.status
      updateRequest(sent, key, row => ({ ...row, seed: { version, status: !writing ? 'read-error' : status === 409 ? 'conflict' : editorSaveFailure(failure) === 'unknown' ? 'unknown' : 'rejected' } }))
    } finally {
      if (!writing && !ready) updateRequest(sent, key, row => row.seed?.status === 'reading' ? { ...row, seed: undefined } : row)
      finish(sent); if (navigation.current?.token === sent.token) navigation.current = null
    }
  }
  const reset = () => {
    const before = readStarterWorkspace(identity)
    if (before.operation || !window.confirm(t('firstSuccess.clearInputConfirm'))) return
    edit(previous => previous !== before ? previous : ({ ...previous, plan: null, error: '', form: Object.freeze({ goal: '', projectTitle: '', roomTitle: '',
      proposerName: '', reviewerName: '', proposerBinding: '', reviewerBinding: '', dirty: false, revision: previous.form.revision + 1 }) }))
  }
  const requestStatus = (request: StarterRequest): Parameters<typeof t>[0] => request.mismatch ? 'firstSuccess.resultMismatch' : request.status === 'sending' ? 'firstSuccess.creating'
    : request.status === 'accepted' ? 'firstSuccess.committed' : request.status === 'unknown' ? 'firstSuccess.requestUnknown'
    : request.status === 'stale' ? 'firstSuccess.previewStale' : request.status === 'conflict' ? 'firstSuccess.requestConflict' : 'editor.rejected'
  if (lastResult.isSuccess && !lastResult.data && activeRoomCount > 0 && !starterHasWork(workspace)) return null
  return <section className="surface first-success stack" role="region" aria-label={t('firstSuccess.title')}>
    <div className="split first-success__heading"><div><h2 className="section-title">{t('firstSuccess.title')}</h2></div><Users aria-hidden="true" size={28} /></div>
    <details className="compact-details"><summary>{t('common.inputNotes')}</summary><p>{t('firstSuccess.sessionOnly')}</p><p>{t('firstSuccess.metadataHelp')}</p></details>
    {lastResult.isPending && <p role="status">{t('firstSuccess.loading')}</p>}
    {lastResult.isError && <div className="notice notice--danger"><p role="alert">{t('firstSuccess.loadFailure')}</p><button type="button" className="button button--secondary button--compact" disabled={lastResult.isFetching} onClick={() => void lastResult.refetch({ cancelRefetch: false })}>{t('common.retry')}</button></div>}
    {lastResult.data && !workspace.requests.some(row => row.result?.room_id === lastResult.data?.room_id) && <div className="notice notice--info stack stack--tight"><p>{t('firstSuccess.lastResultOnly')}</p><Link className="button button--secondary" to={`/chat/${lastResult.data.room_id}`}>{t('firstSuccess.openRoom', { room: t('firstSuccess.createdRoom') })}</Link></div>}
    {[...workspace.requests].reverse().map(request => <section key={request.key} className="notice notice--info stack stack--tight">
      <p role={request.status === 'unknown' || request.mismatch ? 'alert' : 'status'}>{request.status === 'accepted' && <CheckCircle2 aria-hidden="true" size={16} />}{t(requestStatus(request))}</p>
      <strong>{request.preview.project_title} · {request.title}</strong>
      <details><summary>{t('firstSuccess.originalRequest')}</summary><p className="first-success__task section-copy">{request.goal}</p><p>{request.preview.agents.map(agent => `${agent.name} · ${agent.model_id}`).join(' / ')}</p><p>{t('firstSuccess.readNotReceipt')}</p></details>
      {request.status === 'accepted' && request.result && <div className="page-actions">
        <Link className="button button--secondary" to={`/chat/${request.result.room_id}`}>{t('firstSuccess.openOnly')}</Link>
        {!request.seed?.mismatch && request.seed?.status !== 'conflict' && request.seed?.status !== 'rejected' && <button type="button" className="button button--primary" aria-disabled={busy} style={busy ? { opacity: 0.6 } : undefined} onClick={event => void seedAndOpen(request.key, event.currentTarget)}>{request.seed?.status === 'saved' || request.seed?.status === 'kept' ? t('firstSuccess.openRoom', { room: request.title }) : request.seed?.status === 'unknown' ? t('firstSuccess.retryOriginalDraft') : t('firstSuccess.openWithGoal')}</button>}
      </div>}
      {request.status === 'accepted' && <p className="field-hint">{t('firstSuccess.draftHandoffHelp')}</p>}
      {request.seed && <p role={request.seed.status === 'unknown' ? 'alert' : 'status'}>{t(request.seed.mismatch ? 'firstSuccess.resultMismatch' : request.seed.status === 'saved' ? 'firstSuccess.draftReady' : request.seed.status === 'kept' ? 'firstSuccess.draftKept' : request.seed.status === 'reading' || request.seed.status === 'saving' ? 'firstSuccess.draftPreparing' : request.seed.status === 'unknown' ? 'firstSuccess.draftUnknown' : request.seed.status === 'conflict' ? 'firstSuccess.draftConflict' : 'firstSuccess.draftFailure')}</p>}
      {(request.status === 'unknown' || request.status === 'rejected') && !request.mismatch && <button type="button" className="button button--secondary" disabled={busy} onClick={() => void launchApply(request, readStarterWorkspace(identity))}>{t('firstSuccess.retryOriginal')}</button>}
    </section>)}
    {formNeeded && <>
      {readingBindings && lastResult.isSuccess && <p role="status">{t('firstSuccess.checkingConnection')}</p>}
      {(connections.isError || readiness.isError) && <div className="notice notice--danger"><p role="alert">{t('firstSuccess.connectionFailure')}</p><button type="button" className="button button--secondary button--compact" disabled={busy || readingBindings || !lastResult.isSuccess} onClick={refreshBindings}>{t('common.retry')}</button></div>}
      <form {...ime.handlers} className="form-grid" onSubmit={preview}>
        <fieldset className="first-success__setup form-grid" disabled={busy}>
          <label className="field">{t('firstSuccess.taskLabel')}<textarea required maxLength={8000} rows={3} value={goal} onChange={event => change({ goal: event.target.value })} placeholder={t('firstSuccess.taskPlaceholder')} /></label>
          <div className="stack stack--tight"><button type="button" className="button button--secondary button--compact" disabled={busy || readingBindings || !lastResult.isSuccess} onClick={refreshBindings}>{t('firstSuccess.readConnections')}</button>

          </div>
          {connections.isSuccess && readiness.isSuccess && bindings.length === 0 && <div className="notice notice--info stack stack--tight"><p>{t(connections.data.length === 0 ? 'firstSuccess.addConnection' : 'firstSuccess.connectionNeeded')}</p><p className="field-hint">{t('firstSuccess.sessionOnly')}</p><Link className="button button--secondary" to="/connections?first=1">{t(connections.data.length === 0 ? 'dashboard.setup.connection' : 'firstSuccess.prepareConnection')}</Link></div>}
          {bindings.length > 0 && <>
            <div className="first-success__team stack stack--tight"><h3 className="section-title">{t('firstSuccess.defaultTeam')}</h3>{(['proposer', 'reviewer'] as const).map(role => {
              const raw = role === 'proposer' ? proposerBinding : reviewerBinding
              const selected = bindings.find(item => `${item.connectionId}:${item.modelId}` === raw)
              return <p className="section-copy" key={role}><strong>{t(`firstSuccess.${role}`)}</strong> · {selected ? `${selected.label} · ${selected.modelId}` : t(raw ? 'firstSuccess.bindingUnavailable' : 'common.loading')}</p>
            })}</div>
            {connections.isSuccess && readiness.isSuccess && unavailableRoles.length > 0 && <div id={bindingHelpId} className="notice notice--warning stack stack--tight">
              <p role="alert">{t('firstSuccess.unavailableTeamHelp')}</p>
              <div className="page-actions">{unavailableRoles.map(role => <button type="button" key={role} className="button button--secondary button--compact" disabled={busy} onClick={() => focusBinding(role)}>{t('firstSuccess.changeBinding', { role: t(`firstSuccess.${role}`) })}</button>)}</div>
            </div>}
            <details ref={bindingDetails} className="compact-details"><summary>{t('firstSuccess.customize')}</summary><div className="form-grid">
              <div className="form-grid form-grid--two"><label className="field">{t('firstSuccess.projectName')}<input maxLength={100} value={projectTitle} placeholder={t('firstSuccess.projectDefault')} onChange={event => change({ projectTitle: event.target.value })} /></label><label className="field">{t('firstSuccess.roomName')}<input maxLength={200} value={roomTitle} placeholder={goal.trim().replace(/\s+/g, ' ').slice(0, 200)} onChange={event => change({ roomTitle: event.target.value })} /></label></div>
              {(['proposer', 'reviewer'] as const).map(role => {
                const name = role === 'proposer' ? proposerName : reviewerName, binding = role === 'proposer' ? proposerBinding : reviewerBinding
                return <fieldset className="first-success__role stack stack--tight" key={role}><legend className="section-title">{t(`firstSuccess.${role}`)}</legend>
                  <label className="field">{t('firstSuccess.agentName')}<input maxLength={100} value={name} placeholder={t(`firstSuccess.${role}Default`)} onChange={event => change(role === 'proposer' ? { proposerName: event.target.value } : { reviewerName: event.target.value })} /></label>
                  <label className="field">{t('firstSuccess.modelBinding')}<select ref={control => { bindingControls.current[role] = control }} aria-invalid={connections.isSuccess && readiness.isSuccess && unavailableRoles.includes(role) || undefined} aria-describedby={unavailableRoles.includes(role) && connections.isSuccess && readiness.isSuccess ? bindingHelpId : undefined} value={binding} onChange={event => change(role === 'proposer' ? { proposerBinding: event.target.value } : { reviewerBinding: event.target.value })}>
                    {binding && !bindings.some(item => `${item.connectionId}:${item.modelId}` === binding) && <option value={binding}>{t('firstSuccess.bindingUnavailable')} · {binding}</option>}
                    {bindings.map(item => <option key={`${item.connectionId}:${item.modelId}`} value={`${item.connectionId}:${item.modelId}`}>{item.label} · {item.modelId}</option>)}
                  </select></label></fieldset>
              })}<p className="field-hint">{t('firstSuccess.sameConnection')}</p>
            </div></details>
          </>}
        </fieldset>
        {(!plan || !planCurrent) && <button type="submit" className="button button--primary" disabled={busy || !canPreview}>{previewing ? t('firstSuccess.previewing') : t('firstSuccess.review')}</button>}
        {plan && <div className="notice notice--info stack stack--tight"><p>{t(planEvidenceChanged ? 'firstSuccess.previewEvidenceChanged' : planCurrent ? 'firstSuccess.previewReady' : 'firstSuccess.previewChanged')}</p><p className="first-success__task section-copy">{plan.goal}</p><p>{plan.value.project_title} · {plan.value.room_title} · {plan.value.agents.map(agent => agent.name).join(' / ')}</p><p>{t('firstSuccess.noAutomaticRun')}</p>
          <button type="button" className="button button--primary" disabled={busy || !planCurrent || !canPreview} onClick={apply}>{t('firstSuccess.create')}</button></div>}
        {workspace.error && <p role="alert" className="notice notice--danger">{t(workspace.error === 'changed' ? 'firstSuccess.previewChanged' : 'firstSuccess.previewFailure')}</p>}
        <button type="button" className="button button--ghost button--compact" disabled={busy} onClick={reset}>{t('firstSuccess.clearInput')}</button>
        <p className="metadata"><Link className="text-link" to="/agents">{t('firstSuccess.solo')}</Link> · <Link className="text-link" to="/projects">{t('firstSuccess.custom')}</Link></p>
      </form>
    </>}
  </section>
}
