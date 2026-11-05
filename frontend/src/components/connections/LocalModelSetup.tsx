import { useImeFormGuard } from '../../lib/useImeFormGuard'
import { useRef, useEffect, useSyncExternalStore } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { connectionsApi, type Connection, type ModelFeatureReport, type VersionedConnectionInput } from '../../api/connections'
import { useAuthStore } from '../../stores/authStore'
import { useI18n } from '../../i18n'
import type { MessageKey } from '../../i18n/locales/en'
import { directoryViewGeneration } from '../../lib/directoryViewMemory'
import { localPresets, readLocalSetup, subscribeLocalSetup, updateLocalSetup, snapshotLocalSetup, normalizeLocalUrl,
  type LocalProvider, type LocalSetupAttempt, type LocalSetupForm, type LocalSetupWorkspace } from '../../lib/localSetupWorkspace'
import { featureChecksVersion, subscribeFeatureChecks, featureCheckBusy, readFeatureChecks, startFeatureCheck, updateFeatureCheck } from '../../lib/localFeatureChecks'
import { editorSaveFailure } from '../EditorSaveNotice'
import FeatureTable, { FeatureReportDetails } from './FeatureTable'
import FeatureCheckHistory from './FeatureCheckHistory'
import LocalSetupHistory from './LocalSetupHistory'
import { ModelPicker } from './ModelPicker'
import { agentCreationState } from '../../lib/agentCreationRoute'
import { validEditorModelId } from '../../lib/editorValidation'

export default function LocalModelSetup({ identity, visible = true, onSaved, onClose }: { identity: string; visible?: boolean; onSaved: (c: Connection) => void; onClose: () => void }) {
  const { t } = useI18n(), cache = useQueryClient()
  const workspace = useSyncExternalStore(subscribeLocalSetup, () => readLocalSetup(identity))
  useSyncExternalStore(subscribeFeatureChecks, featureChecksVersion)
  const featureBusy = featureCheckBusy(identity)
  const generation = useRef(directoryViewGeneration()).current
  const continuation = useRef<{ cancelled: boolean } | null>(null)
  const mounted = useRef(false), visibility = useRef(visible)
  visibility.current = visible
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; if (continuation.current) continuation.current.cancelled = true } }, [])
  useEffect(() => { if (!visible && continuation.current) continuation.current.cancelled = true }, [visible])
  const ownerCurrent = () => { const state = useAuthStore.getState(); return !!state.token && `${state.user?.id || 0}:${state.epoch}` === identity && generation === directoryViewGeneration() }
  const change = (update: (value: LocalSetupWorkspace) => LocalSetupWorkspace) => ownerCurrent() && updateLocalSetup(identity, generation, update)
  const attemptChange = (id: string, update: (attempt: LocalSetupAttempt) => LocalSetupAttempt) => change(value => ({ ...value, attempts: value.attempts.map(attempt => attempt.id === id ? update(attempt) : attempt) }))
  const start = (kind: NonNullable<LocalSetupWorkspace['operation']>['kind']) => {
    if (!ownerCurrent() || readLocalSetup(identity).operation) return null
    const id = crypto.randomUUID(); change(value => ({ ...value, operation: { id, kind }, notice: null })); return id
  }
  const finish = (id: string) => change(value => value.operation?.id === id ? { ...value, operation: null } : value)
  const notice = (key: MessageKey) => change(value => ({ ...value, notice: key }))
  const intentLive = (intent: { cancelled: boolean }) => ownerCurrent() && mounted.current && visibility.current && !intent.cancelled
  const close = () => { if (continuation.current) continuation.current.cancelled = true; onClose() }
  const form = workspace.form, current = workspace.attempts.find(attempt => attempt.id === workspace.currentId)
  const busy = !!workspace.operation, frozen = busy || !!current && current.status !== 'rejected'
  const imeGuard = useImeFormGuard(`${identity}:${workspace.currentId || 'new'}`)
  const edit = (patch: Partial<LocalSetupForm>, server = false) => {
    if (frozen) return
    change(value => ({ ...value, currentId: null, notice: null, catalog: server ? null : value.catalog,
      form: { ...value.form, ...patch, serverRevision: value.form.serverRevision + (server ? 1 : 0) } }))
  }
  const newSetup = () => {
    if (!ownerCurrent() || busy) return
    if (current?.status === 'unknown') {
      if (!current.inspected) { notice('localModels.session.inspectFirst'); return }
      if (!window.confirm(t('localModels.resetUnknown'))) return
    } else if (!current?.saved && (form.model || form.name || form.url !== localPresets[form.provider]) && !window.confirm(t('localModels.discardSetup'))) return
    change(value => ({ ...value, currentId: null, notice: null, form: { ...value.form, model: '', name: '' } }))
  }
  const refresh = async () => {
    if (frozen) return
    const url = normalizeLocalUrl(form.url)
    if (!url) { notice('localModels.session.invalidInput'); return }
    const token = start('list'); if (!token) return
    const revision = form.serverRevision
    try { const value = await connectionsApi.localModels(form.provider, url)
      change(previous => previous.form.serverRevision === revision ? { ...previous, catalog: { serverRevision: revision, value: snapshotLocalSetup(value) } } : previous)
    } catch { notice('localModels.listFailed') } finally { finish(token) }
  }
  const inspect = async (attempt: LocalSetupAttempt) => {
    const token = start('inspect'); if (!token) return
    try {
      const connections = await connectionsApi.list()
      const candidates = connections.filter(item => item.is_local && item.auth_method_id === 'local_none' &&
        (item.provider_id || item.provider_kind) === attempt.body.provider_id && normalizeLocalUrl(item.base_url || '') === attempt.url &&
        item.default_model_id === attempt.model && item.label === attempt.body.label).map(item => ({ id: item.id, label: item.label }))
      attemptChange(attempt.id, value => ({ ...value, inspected: true, candidates }))
    } catch { notice('editor.inspectFailure') } finally { finish(token) }
  }
  const matchingReport = (report: ModelFeatureReport, attempt: LocalSetupAttempt) => report.local === true && report.model_id === attempt.model && normalizeLocalUrl(report.base_url || '') === attempt.url
  const readReport = async (attempt: LocalSetupAttempt) => {
    if (!attempt.saved) return
    const token = start('report'); if (!token) return
    try { const report = await connectionsApi.features(attempt.saved.id, attempt.model)
      if (!matchingReport(report, attempt)) { notice('localModels.session.configurationChanged'); return }
      attemptChange(attempt.id, value => ({ ...value, storedReport: snapshotLocalSetup(report) }))
    } catch { notice('localModels.reportLoadFailed') } finally { finish(token) }
  }
  const probe = async (attempt: LocalSetupAttempt, intent: { cancelled: boolean }) => {
    const saved = attempt.saved; if (!saved) return
    const record = startFeatureCheck(identity, generation, { connectionId: saved.id, modelId: attempt.model, revision: `${saved.config_revision ?? 0}:${saved.auth_grant_revision ?? 0}`, baseUrl: attempt.url })
    if (!record) { notice('localModels.featureSession.pending'); return }
    const id = record.id
    attemptChange(attempt.id, value => ({ ...value, checks: [...value.checks, { id, status: 'preparing' }] }))
    const checked = (status: LocalSetupAttempt['checks'][number]['status'], report?: ModelFeatureReport, key?: MessageKey) => {
      if (ownerCurrent()) updateFeatureCheck(identity, generation, record, status, report, key)
      return attemptChange(attempt.id, value => ({ ...value,
      checks: value.checks.map(check => check.id === id ? { ...check, status, report: report && snapshotLocalSetup(report), notice: key } : check) }))
    }
    let sent = false
    try {
      const revisions = await connectionsApi.revisions(saved.id)
      if (!intentLive(intent)) { checked('not_sent', undefined, 'localModels.session.deferred'); return }
      if (typeof saved.config_revision !== 'number' || typeof saved.auth_grant_revision !== 'number' || revisions.config_revision !== saved.config_revision || revisions.auth_grant_revision !== saved.auth_grant_revision) {
        checked('not_sent', undefined, 'localModels.session.configurationChanged'); return
      }
      checked('checking'); sent = true
      const report = await connectionsApi.checkFeatures(saved.id, attempt.model)
      if (!matchingReport(report, attempt)) { checked('unknown', undefined, 'localModels.session.checkUnknown'); return }
      checked('done', report)
      if (ownerCurrent()) void cache.invalidateQueries({ queryKey: ['model-features', identity, saved.id] })
    } catch { checked(sent ? 'unknown' : 'not_sent', undefined, sent ? 'localModels.session.checkUnknown' : 'localModels.session.preflightFailed') }
  }
  const save = async (event: React.FormEvent) => {
    event.preventDefault()
    if (imeGuard.isSubmitBlocked(event) || busy || featureBusy || current?.status === 'unknown' || current?.status === 'creating') return
    const url = normalizeLocalUrl(form.url), model = form.model.trim()
    if (!url || !model || model.length > 200 || form.name.trim().length > 100) { notice('localModels.session.invalidInput'); return }
    const sharedChecks = current?.saved ? readFeatureChecks(identity, current.saved.id, current.model, `${current.saved.config_revision ?? 0}:${current.saved.auth_grant_revision ?? 0}`) : []
    if (current?.saved && (current.checks.some(check => check.status === 'unknown') || sharedChecks[sharedChecks.length - 1]?.status === 'unknown') && !window.confirm(t('localModels.session.newCheckConfirm'))) return
    const token = start(current?.saved ? 'check' : 'create'); if (!token) return
    const intent = { cancelled: false }; continuation.current = intent
    let attempt = current?.saved ? current : undefined
    try {
      if (!attempt) {
        const fallback = `${form.provider} · ${model}`
        const body: VersionedConnectionInput = { version: 1, provider_id: form.provider, protocol_id: 'openai_chat_completions', auth_method_id: 'local_none',
          fields: { base_url: url }, label: form.name.trim() || (fallback.length > 100 ? `${fallback.slice(0, 99)}…` : fallback), default_model_id: model, enabled: true, capabilities: {} }
        attempt = { id: crypto.randomUUID(), body: snapshotLocalSetup(body), model, url, status: 'creating', checks: [] }
        const original = attempt
        change(value => ({ ...value, currentId: original.id, attempts: [...value.attempts, original] }))
        let saved: Connection
        try { saved = await connectionsApi.create(snapshotLocalSetup(body)) }
        catch (error) { const status = editorSaveFailure(error) === 'rejected' ? 'rejected' : 'unknown'
          attemptChange(original.id, value => ({ ...value, status })); notice(status === 'rejected' ? 'editor.rejected' : 'localModels.saveUnknown'); return }
        if (!ownerCurrent()) return
        // A successful creation response is stored even when its originating view has gone away.
        if (!Number.isSafeInteger(saved.id) || saved.id <= 0 || !saved.is_local || saved.default_model_id !== model || normalizeLocalUrl(saved.base_url || '') !== url) {
          attemptChange(original.id, value => ({ ...value, status: 'unknown' })); notice('localModels.saveUnknown'); return
        }
        attempt = { ...original, status: 'saved', saved: snapshotLocalSetup(saved) }
        const accepted = attempt
        attemptChange(original.id, () => accepted)
        void cache.invalidateQueries({ queryKey: ['connections', identity] })
        if (intentLive(intent)) onSaved(saved)
      }
      if (!intentLive(intent)) { notice('localModels.session.deferred'); return }
      await probe(attempt, intent)
    } finally { finish(token) }
  }
  const catalog = workspace.catalog?.serverRevision === form.serverRevision ? workspace.catalog.value : undefined
  const checks = current?.checks || [], latest = checks[checks.length - 1]
  return <section className="surface stack local-model-setup">
    <header className="split"><h2 className="section-title">{t('localModels.title')}</h2><div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={newSetup}>{t('localModels.newSetup')}</button>
      <button type="button" className="button button--secondary button--compact" onClick={close}>{t('common.close')}</button>
    </div></header><p>{t('localModels.help')}</p><p className="field-hint">{t('localModels.session.retained')}</p>
    <form {...imeGuard.handlers} className="form-grid" onSubmit={event => void save(event)}>
      <fieldset className="first-success__setup form-grid" disabled={frozen}>
        <label className="field">{t('localModels.server')}<select value={form.provider} onChange={event => { const provider = event.target.value as LocalProvider; edit({ provider, url: localPresets[provider] }, true) }}>
          <option value="ollama">Ollama</option><option value="lm-studio">LM Studio</option><option value="local-openai">{t('localModels.otherServer')}</option></select></label>
        <label className="field">{t('localModels.address')}<input type="url" required value={form.url} onChange={event => edit({ url: event.target.value }, true)} /></label><p className="field-hint">{t('localModels.localOnly')}</p>
        <ModelPicker value={form.model} onChange={model => edit({ model })} catalog={catalog} busy={workspace.operation?.kind === 'list'} canRefresh={!frozen} onRefresh={() => void refresh()} searchScope={`${identity}:${form.serverRevision}`}
          copy={{ knownModel: t('localModels.choose'), chooseKnownModel: t('localModels.choose'), manualModel: t('localModels.model'), refresh: t('localModels.load'), refreshing: t('localModels.loading'), stale: t('connections.modelCacheStale'), truncated: t('connections.modelCacheTruncated'), empty: t(catalog ? 'localModels.empty' : 'connections.modelCacheEmpty'), loadFailure: t('localModels.listFailed'), refreshFailure: t('localModels.listFailed'), configurationChanged: t('localModels.session.configurationChanged'), retry: t('common.retry'), retryRefresh: t('common.retry') }} />
        <label className="field">{t('connections.name')}<input value={form.name} onChange={event => edit({ name: event.target.value })} placeholder={`${form.provider} · ${form.model || 'local'}`} /></label>
      </fieldset>
      <p className="field-hint">{t('localModels.probeBudget')}</p><button type="submit" className="button button--primary" disabled={busy || featureBusy || current?.status === 'unknown' || !form.model.trim()}>{t(busy ? 'common.loading' : current?.saved ? 'localModels.check' : 'localModels.saveAndCheck')}</button>
    </form>
    {featureBusy && <p role="status">{t('localModels.featureSession.pending')}</p>}
    {workspace.notice && <p role="alert" className="notice notice--warning">{t(workspace.notice)}</p>}
    {current?.status === 'unknown' && <div className="notice notice--warning stack stack--tight"><p>{t('localModels.session.inspectFirst')}</p>
      <button type="button" className="button button--secondary" disabled={busy} onClick={() => void inspect(current)}>{t('editor.inspect')}</button>
      {current.inspected && <><p>{t('localModels.session.listNotReceipt')}</p>{current.candidates?.length ? <ul>{current.candidates.map(item => <li key={item.id}>#{item.id} · {item.label}</li>)}</ul> : <p>{t('editor.noMatch')}</p>}</>}
    </div>}
    {current?.saved && <div className="notice notice--info stack stack--tight"><p>#{current.saved.id} · {current.body.label} · {current.model}</p><p>{t('localModels.session.saved')}</p>
      <button type="button" className="button button--secondary" disabled={busy} onClick={() => void readReport(current)}>{t('localModels.session.readReport')}</button>
      {validEditorModelId(current.model.trim()) ? <Link className="text-link" to="/agents" state={agentCreationState(identity, generation, current.saved.id, current.model)} onClick={event => { if (!ownerCurrent()) event.preventDefault() }}>{t('agents.fromConnection.prepare')}</Link> : <Link className="text-link" to="/agents">{t('localModels.agents')}</Link>}
    </div>}
    {latest && <p role="status">{t(latest.notice || (latest.status === 'done' ? 'localModels.session.reportOriginal' : latest.status === 'preparing' || latest.status === 'checking' ? 'localModels.checking' : 'localModels.session.deferred'))}</p>}
    {checks.length > 0 && current && <FeatureCheckHistory key={`history:${identity}:${current.id}`} checks={checks} context={{ connectionId: current.saved?.id, modelId: current.model, baseUrl: current.url }}/>}
    {latest?.report && <FeatureTable report={latest.report} />}
    {current?.storedReport && <FeatureReportDetails key={`stored:${identity}:${current.id}`} report={current.storedReport} summary={t('localModels.session.storedReport')} help={t('localModels.session.readNotReceipt')}/>}
    <LocalSetupHistory key={`previous:${identity}:${generation}`} identity={identity} attempts={workspace.attempts} currentId={workspace.currentId}/>
    <details className="compact-details"><summary>{t('localModels.startHelp')}</summary><p>{t('localModels.startSteps')}</p><p className="field-hint">{t('localModels.limits')}</p></details>
  </section>
}
