import { useLayoutEffect, useRef, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import CommandHistoryArchive, { type ArchiveCommand } from '../CommandHistoryArchive'
import { FlaskConical } from 'lucide-react'
import { connectionsApi, type Connection, type TestStatus } from '../../api/connections'
import { useAuthStore } from '../../stores/authStore'
import { useI18n } from '../../i18n'
import type { MessageKey } from '../../i18n/locales/en'
import { directoryViewGeneration } from '../../lib/directoryViewMemory'
import { beginConnectionTest, connectionTestsBusy, connectionTestsVersion, effectiveTestModel, hasUnknownConnectionTest, readConnectionTests,
  stopConnectionTestWait, subscribeConnectionTests, updateConnectionTest, type ConnectionTestRequest } from '../../lib/connectionTestWorkspace'

const statusKeys: Record<string, MessageKey> = {
  connected: 'connections.testSucceeded', verified: 'connections.testSucceeded', missing_secret: 'connections.testMissingSecret',
  secret_store_unavailable: 'connections.testCredentialLocked', credential_locked: 'connections.testCredentialLocked', connection_disabled: 'connections.testDisabled',
  unreachable: 'connections.testUnreachable', redirect_refused: 'connections.testUnreachable', invalid_credential: 'connections.testInvalidCredential',
  rate_limited: 'connections.testRateLimited', quota_exhausted: 'connections.limitReached', configuration_changed: 'connections.testConfigurationChanged',
  missing_model: 'connections.testFailed', invalid_url: 'connections.testFailed', invalid_configuration: 'connections.testFailed', storage_unavailable: 'connections.testFailed',
  provider_error: 'connections.testFailed', invalid_response: 'connections.testFailed', test_failed: 'connections.testFailed',
}
function originalTestNotice(request: ConnectionTestRequest): MessageKey {
  return request.notice || (request.phase === 'received' && request.response
    ? request.response.ok ? 'connections.testSucceeded' : statusKeys[request.response.code] || 'connections.testFailed'
    : request.phase === 'preparing' ? 'connections.testSession.preparing'
    : request.phase === 'sending' ? 'connections.testSession.sending'
    : request.phase === 'not_sent' ? 'connections.testSession.notSent' : 'connections.testSession.unknown')
}
function sameOwner(identity: string, generation: number) {
  const state = useAuthStore.getState()
  return !!state.token && `${state.user?.id || 0}:${state.epoch}` === identity && generation === directoryViewGeneration()
}
function matchingResponse(response: TestStatus, original: ConnectionTestRequest) {
  const target = original.target, record = response.record
  return typeof response.ok === 'boolean' && typeof response.code === 'string'
    && (response.model_id ?? record?.model_id) === target.modelId && (response.profile ?? record?.profile) === target.profile
    && (!record || record.connection_id === target.connectionId && record.model_id === target.modelId && (!record.profile || record.profile === target.profile))
    && (response.max_request_count == null || Number.isSafeInteger(response.max_request_count) && response.max_request_count === target.maxRequests)
    && (response.actual_request_count == null || Number.isSafeInteger(response.actual_request_count) && response.actual_request_count >= 0 && response.actual_request_count <= (response.max_request_count ?? target.maxRequests))
}

export default function ConnectionTestControls({ connection, identity, visible }: { connection: Connection; identity: string; visible: boolean }) {
  const { t } = useI18n(), cache = useQueryClient(), generation = useRef(directoryViewGeneration()).current
  useSyncExternalStore(subscribeConnectionTests, connectionTestsVersion)
  const workspace = readConnectionTests(identity, connection.id), model = effectiveTestModel(connection, workspace)
  const scope = JSON.stringify([identity, connection.id, model, connection.config_revision ?? 0, connection.auth_grant_revision ?? 0])
  const view = useRef({ scope, visible }), mounted = useRef(false), intent = useRef<{ cancelled: boolean; scope: string } | null>(null)
  view.current = { scope, visible }
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false; if (intent.current) intent.current.cancelled = true } }, [])
  useLayoutEffect(() => () => { if (intent.current) intent.current.cancelled = true }, [scope, visible])
  const valid = () => sameOwner(identity, generation)
  const current = (origin: { cancelled: boolean; scope: string }) => valid() && mounted.current && view.current.visible && view.current.scope === origin.scope && !origin.cancelled
  const preview = useQuery({
    queryKey: ['connection-test-preview', identity, connection.id, model, connection.config_revision ?? 0, connection.auth_grant_revision ?? 0],
    enabled: visible && !!model, retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: async () => {
      if (!valid()) throw new Error('Owner changed')
      const [text, full] = await Promise.all([connectionsApi.testPreview(connection.id, model, 'text'), connectionsApi.testPreview(connection.id, model, 'full')])
      if (!valid()) throw new Error('Owner changed')
      if (text.model_id !== model || full.model_id !== model || text.profile !== 'text' || full.profile !== 'full'
        || !Number.isSafeInteger(text.max_request_count) || text.max_request_count < 0 || !Number.isSafeInteger(full.max_request_count) || full.max_request_count < 0) throw new Error('Test preview target changed')
      return { text, full }
    },
  })
  const start = async (profile: 'text' | 'full') => {
    if (!valid() || !visible || preview.isFetching || preview.isError || !preview.data || connectionTestsBusy(identity)) return
    const origin = { cancelled: false, scope }; intent.current = origin
    const budget = preview.data[profile]
    if (hasUnknownConnectionTest(identity) && !window.confirm(t('connections.testSession.newConfirm', { model, count: budget.max_request_count }))) return
    if (!current(origin)) return
    const original = beginConnectionTest(identity, generation, { connectionId: connection.id, label: connection.label, modelId: model, profile,
      configRevision: connection.config_revision ?? 0, authGrantRevision: connection.auth_grant_revision ?? 0, maxRequests: budget.max_request_count, startedAt: new Date().toISOString() })
    if (!original) return
    const stillPreparing = () => readConnectionTests(identity, connection.id).requests.find(value => value.id === original.id)?.phase === 'preparing'
    let sent = false
    try {
      const revisions = await connectionsApi.revisions(original.target.connectionId)
      if (!valid()) return
      if (!stillPreparing()) return
      if (!current(origin)) { updateConnectionTest(identity, generation, original, 'not_sent', undefined, 'connections.testSession.notSent'); return }
      if (revisions.config_revision !== original.target.configRevision || revisions.auth_grant_revision !== original.target.authGrantRevision) {
        updateConnectionTest(identity, generation, original, 'not_sent', undefined, 'connections.testSession.changedBefore'); return
      }
      updateConnectionTest(identity, generation, original, 'sending'); sent = true
      const response = await connectionsApi.test(original.target.connectionId, original.target.modelId, original.target.profile)
      if (!valid()) return
      const matches = matchingResponse(response, original)
      updateConnectionTest(identity, generation, original, matches ? 'received' : 'unknown', response, matches ? undefined : 'connections.testSession.scopeMismatch')
      void cache.invalidateQueries({ queryKey: ['connections', identity] })
      void cache.invalidateQueries({ queryKey: ['readiness', identity] })
      void cache.invalidateQueries({ queryKey: ['readiness-history', identity, original.target.connectionId] })
      void cache.invalidateQueries({ queryKey: ['model-features', identity, original.target.connectionId, original.target.modelId] })
    } catch {
      if (!valid()) return
      if (!sent && !stillPreparing()) return
      updateConnectionTest(identity, generation, original, sent ? 'unknown' : 'not_sent', undefined, sent ? 'connections.testSession.unknown' : 'connections.testSession.notSent')
    }
  }
  const busy = connectionTestsBusy(identity), canRun = !!preview.data && !preview.isError && !preview.isFetching && !busy && visible
  return <div className="notice notice--info stack stack--tight">
    <p>{t('connections.testSession.currentTarget', { model })}</p>
    <p className="field-hint">{t('connections.testSession.retained')}</p>
    {preview.isFetching && <p role="status">{t('connections.testPreviewLoading')}</p>}
    {preview.isError && <p role="alert" className="notice notice--warning">{t('connections.testPreviewFailure')}</p>}
    {(preview.isError || !preview.data) && <button type="button" className="button button--secondary button--compact" disabled={preview.isFetching} onClick={() => { if (valid()) void preview.refetch({ cancelRefetch: false }) }}>{t('connections.testSession.reloadPreview')}</button>}
    {preview.data && <>
      {(preview.isError || preview.isFetching) && <p className="field-hint">{t('connections.testSession.oldPreview')}</p>}
      <p>{t('connections.textCheckBudget', { count: preview.data.text.max_request_count })}</p>
      <button type="button" className="button button--primary button--compact" disabled={!canRun} onClick={() => void start('text')}><FlaskConical aria-hidden="true" size={15}/>{t('connections.testText')}</button>
      <details className="compact-details"><summary>{t('connections.fullCheckDetails')}</summary><div className="stack stack--tight">
        <p>{t('connections.fullCheckBudget', { count: preview.data.full.max_request_count })}</p>
        <button type="button" className="button button--secondary button--compact" disabled={!canRun} onClick={() => void start('full')}><FlaskConical aria-hidden="true" size={15}/>{t('connections.testFull')}</button>
      </div></details>
    </>}
    {busy && <p role="status">{t('connections.testSession.pending')}</p>}
  </div>
}

function OriginalTest({ request, connection, currentModel, identity, generation }: { request: ConnectionTestRequest; connection: Connection; currentModel: string; identity: string; generation: number }) {
  const { t, formatDateTime } = useI18n(), target = request.target, response = request.response
  const changed = target.modelId !== currentModel || target.configRevision !== (connection.config_revision ?? 0) || target.authGrantRevision !== (connection.auth_grant_revision ?? 0)
  const stop = () => {
    if (!sameOwner(identity, generation)) return
    const actual = readConnectionTests(identity, connection.id).requests.find(value => value.id === request.id)
    if (!actual || !['preparing', 'sending'].includes(actual.phase)) return
    if (actual.phase === 'sending' && !window.confirm(t('connections.testSession.stopConfirm'))) return
    if (sameOwner(identity, generation)) stopConnectionTestWait(identity, generation, actual)
  }
  return <div className="stack stack--tight">
    <p className="metadata">#{target.connectionId} · {target.label} · {target.modelId} · {t(target.profile === 'text' ? 'connections.testText' : 'connections.testFull')}</p>
    <p className="metadata">{t('connections.testSession.started', { time: formatDateTime(target.startedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>
    {changed && <p className="notice notice--warning">{t('connections.testSession.differentSelection')}</p>}
    <p role="status" className={request.phase === 'received' && response?.ok ? 'status-text status-text--success' : request.phase === 'received' ? 'status-text status-text--error' : 'status-text status-text--warning'}>
      {t(originalTestNotice(request))}
    </p>
    {request.phase === 'received' && <p className="field-hint">{t('connections.testSession.originalResponse')}</p>}
    {request.phase === 'unknown' && <p className="field-hint">{t('connections.testSession.unknownHelp')}</p>}
    {request.receivedAt && <p className="metadata">{t('readiness.browse.lastReceived', { time: formatDateTime(request.receivedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
    {response && <>
      <p className="metadata">{t('connections.testSession.actualCounts', { actual: response.actual_request_count ?? response.record?.actual_request_count ?? '?', limit: response.max_request_count ?? '?' })}</p>
      {response.record && <p className="field-hint">{t(response.record.config_current ? 'connections.testSession.recordCurrent' : 'connections.testSession.recordOld')}</p>}
      {!response.record && <p className="field-hint">{t('connections.testSession.noRecord')}</p>}
    </>}
    {['preparing', 'sending'].includes(request.phase) && <button type="button" className="button button--secondary button--compact" onClick={stop}>{t('connections.testSession.stopWaiting')}</button>}
    <details className="compact-details"><summary>{t('common.technicalDetails')}</summary>
      <p>{t('connections.testSession.capturedVersions', { config: target.configRevision, grant: target.authGrantRevision, count: target.maxRequests })}</p>
      {response && <><p className="technical">{response.model_id ?? response.record?.model_id ?? '?'} · {response.profile ?? response.record?.profile ?? '?'}</p><p className="technical">{response.code}{response.message ? ` · ${response.message}` : ''}</p>{response.record && <p className="technical">{response.record.config_fingerprint}</p>}</>}
      <p className="field-hint">{t('connections.testSession.scopeLimit')}</p>
    </details>
  </div>
}
export function ConnectionTestStatus({ connection, identity }: { connection: Connection; identity: string }) {
  const { t, formatDateTime } = useI18n(), generation = useRef(directoryViewGeneration()).current
  useSyncExternalStore(subscribeConnectionTests, connectionTestsVersion)
  const workspace = readConnectionTests(identity, connection.id), latest = workspace.requests[workspace.requests.length - 1]
  if (!latest) return null
  const model = effectiveTestModel(connection, workspace)
  const pinned = new Set(workspace.requests.filter(request => ['preparing', 'sending', 'unknown'].includes(request.phase)).map(request => request.id))
  pinned.add(latest.id)
  const content = (request: ConnectionTestRequest) => <OriginalTest request={request} connection={connection} currentModel={model} identity={identity} generation={generation}/>
  const older: ArchiveCommand[] = workspace.requests.filter(request => !pinned.has(request.id)).slice().reverse().map<ArchiveCommand>(request => ({
    id: request.id,
    phase: request.phase === 'not_sent' ? 'not_sent' : request.phase === 'received' && request.response?.ok === true ? 'succeeded' : 'failed',
    search: [request.target.label, String(request.target.connectionId), request.target.modelId, request.id,
      t(request.target.profile === 'text' ? 'connections.testText' : 'connections.testFull'), request.target.startedAt,
      formatDateTime(request.target.startedAt, { dateStyle: 'medium', timeStyle: 'medium' }),
      request.receivedAt || '', request.receivedAt ? formatDateTime(request.receivedAt, { dateStyle: 'medium', timeStyle: 'medium' }) : '',
      request.response?.code || '', t(originalTestNotice(request))],
    content: content(request),
  }))
  return <section className="stack stack--tight" aria-label={t('connections.testSession.results')}>
    <h4>{t('connections.testSession.results')}</h4>
    <p className="field-hint">{t('connections.testSession.historyScope')}</p>
    {workspace.requests.filter(request => pinned.has(request.id)).slice().reverse().map(request => <div key={request.id}>{content(request)}</div>)}
    {older.length > 0 && <CommandHistoryArchive key={`${identity}:${connection.id}:${generation}`} title={t('connections.testSession.previous')}
      phases={['succeeded', 'failed', 'not_sent']} items={older}/>}
  </section>
}
