import ConnectionDraftReader from '../components/connections/ConnectionDraftReader'
import {connectionDraftVersion,forgetConnectionDraft,keepConnectionDraft,readConnectionDraft,subscribeConnectionDrafts,type ConnectionDraftInput,type ConnectionDraftSource} from '../lib/connectionEditorDraft'
import ConnectionSaveHistory from '../components/connections/ConnectionSaveHistory'
import {connectionSaveBusy,connectionSavesVersion,nextConnectionSaveAttempt,readConnectionSaves,subscribeConnectionSaves,updateConnectionSave} from '../lib/connectionSaveWorkspace'
import ConnectionRemovalHistory from '../components/connections/ConnectionRemovalHistory'
import {beginConnectionRemoval,connectionRemovalBusy,connectionRemovalVersion,previousConnectionRemovalFailure,settleConnectionRemoval,subscribeConnectionRemovals} from '../lib/connectionRemovalWorkspace'
import InactiveReferenceHistory from '../components/connections/InactiveReferenceHistory'
import InactiveAgentReferences from '../components/connections/InactiveAgentReferences'
import {agentConnectionHref} from '../lib/agentConnectionFilter'
import ConnectionRemoveDialog,{connectionRemovalTarget,sameConnectionRemovalTarget,type ConnectionRemovalTarget,type ConnectionRemovalFailure} from '../components/connections/ConnectionRemoveDialog'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { authSessionsVersion, subscribeAuthSessions, readAuthSession, updateAuthSession, authSessionHint, authSessionLocked, currentAuthAttempt, authTerminal, snapshotAuth, type AuthSessionWorkspace, type AuthStartAttempt } from '../lib/authSessionWorkspace'
import { connectionTestsVersion, subscribeConnectionTests, readConnectionTests, connectionTestsBusy, connectionTestActive, effectiveTestModel, selectConnectionTestModel } from '../lib/connectionTestWorkspace'
import ConnectionTestControls, { ConnectionTestStatus } from '../components/connections/ConnectionTestControls'
import { useDirectoryValue, useDirectoryPosition } from '../lib/useDirectoryView'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { agentCreationState } from '../lib/agentCreationRoute'
import { editorTextLength, validEditorModelId } from '../lib/editorValidation'
import { Cable, CheckCircle2, KeyRound, Pencil, RotateCcw, Save, Server, Trash2, Unplug, Plus, MoreHorizontal, X } from 'lucide-react'
import { connectionsApi } from '../api/connections'
import type { AuthMethodId, Capability, Connection, DisconnectOperationResponse, DisconnectPending, DisconnectResult,
  DisconnectRestoredCredentialState, DisconnectRevokeState, ProtocolId, ProviderDescriptor, PublicFieldValue,
  VersionedConnectionInput, AuthOperationResponse } from '../api/connections'
import { isUsableAuthMethod } from '../api/connections'
import { AppShell, PageHeader } from '../components/ui'
import { ProviderCatalog, providerDisplayName } from '../components/connections/ProviderCatalog'
import { MethodFields, MethodFieldsReview, type MethodCopy } from '../components/connections/MethodFields'
import { ModelPicker } from '../components/connections/ModelPicker'
import { SavedModelPicker } from '../components/connections/SavedModelPicker'
import ConnectionSaveNotice, { connectionSentKeyLabel, type ConnectionSaveFeedback } from '../components/ConnectionSaveNotice'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { readLocalSetup, subscribeLocalSetup, localSetupHasWork } from '../lib/localSetupWorkspace'
import LocalModelSetup from '../components/connections/LocalModelSetup'
import ModelFeatures from '../components/connections/ModelFeatures'
import { AuthMethodPicker } from '../components/connections/AuthMethodPicker'
import { AuthOperationStatus } from '../components/connections/AuthOperationStatus'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import DirectorySearch, { directoryMatcher, type DirectorySearchHandle } from '../components/DirectorySearch'
import { focusSavedEditorCard } from '../components/EditorSavedNotice'
import ReadinessCenter from '../components/ReadinessCenter'
import { useAuthStore } from '../stores/authStore'

const options: Capability[] = ['max_tokens', 'temperature', 'tools', 'vision']
const capabilityLabels: Record<Capability, MessageKey> = {
  max_tokens: 'connections.cap.maxTokens', temperature: 'connections.cap.temperature',
  tools: 'connections.cap.tools', vision: 'connections.cap.vision',
}
const disconnectCredentialLabels: Record<DisconnectRestoredCredentialState, MessageKey> = {
  authorizing: 'connections.disconnectCredential.authorizing', present: 'connections.disconnectCredential.present',
  refresh_required: 'connections.disconnectCredential.refreshRequired', rejected: 'connections.disconnectCredential.rejected',
  locked: 'connections.disconnectCredential.locked', unavailable: 'connections.disconnectCredential.unavailable',
}
const disconnectRevokeLabels: Record<DisconnectRevokeState, MessageKey> = {
  revoked: 'connections.disconnectRevoke.revoked', skipped: 'connections.disconnectRevoke.skipped',
  not_supported: 'connections.disconnectRevoke.notSupported', already_invalid: 'connections.disconnectRevoke.alreadyInvalid',
  remote_revoke_failed: 'connections.disconnectRevoke.failed',
}
type DisconnectView =
  | { kind: 'immediate'; result: DisconnectResult }
  | { kind: 'operation'; response: DisconnectOperationResponse; cancelling?: boolean; statusError?: boolean }
type DisconnectFocusRequest = { id: number; identity: string; target: 'trigger' | 'cancel' | 'status' }

function newIdempotencyKey() {
  const bytes = new Uint8Array(16)
  window.crypto.getRandomValues(bytes)
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
}

function isDisconnectPending(value: DisconnectOperationResponse): value is DisconnectPending {
  return value.state === 'pending' || value.state === 'finalizing'
}
const blank: VersionedConnectionInput = {
  version: 1, provider_id: 'openai', protocol_id: 'openai_chat_completions', auth_method_id: 'api_key', fields: {},
  label: '', default_model_id: '', enabled: true, capabilities: {},
}

function legacyFields(connection: Connection): Record<string, PublicFieldValue> {
  if (connection.fields) return { ...connection.fields }
  if (connection.provider_kind !== 'compatible') return {}
  return { ...(connection.base_url ? { base_url: connection.base_url } : {}), allow_local_http: Boolean(connection.base_url?.startsWith('http://')) }
}

function legacyMethod(connection: Connection): AuthMethodId {
  return connection.auth_method_id || (connection.provider_kind === 'compatible' && !connection.auth_required ? 'local_none' : 'api_key')
}

function initialProviderFields(provider: ProviderDescriptor, protocolId: ProtocolId): Record<string, PublicFieldValue> {
  return provider.provider_id === 'azure-openai' && protocolId === 'azure_openai' ? { api_version: 'v1' } : {}
}

function connectionInputFingerprint(input: VersionedConnectionInput) {
  const sorted = (values: Record<string, unknown> | undefined) => Object.fromEntries(Object.entries(values || {}).sort(([left], [right]) => left.localeCompare(right)))
  return JSON.stringify({ version: input.version, provider_id: input.provider_id, protocol_id: input.protocol_id,
    auth_method_id: input.auth_method_id, fields: sorted(input.fields),
    default_model_id: input.default_model_id, enabled: input.enabled, capabilities: sorted(input.capabilities) })
}

function connectionEditorSnapshot(input: VersionedConnectionInput) {
  // Public configuration only; the separate API-key input is never serialized here.
  return JSON.stringify([input.label, connectionInputFingerprint(input)])
}

function apiErrorCode(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (!detail || typeof detail !== 'object') return undefined
  const code = (detail as { code?: unknown }).code
  return typeof code === 'string' ? code : undefined
}


function authMethodLabel(method: AuthMethodId): MessageKey {
  return ({ api_key: 'connections.methodApiKey', local_none: 'connections.methodLocalNone',
    oauth_browser: 'connections.methodOauthBrowser', oauth_device: 'connections.methodOauthDevice',
    native_chain: 'connections.methodNativeChain' })[method] as MessageKey
}

function SavedAuthPanel(props: { connection: Connection; provider?: ProviderDescriptor; identity: string; visible: boolean }) {
  return <SavedAuthSession key={`${props.identity}:${props.connection.id}`} {...props} />
}
function SavedAuthSession({ connection, provider, identity, visible }: { connection: Connection; provider?: ProviderDescriptor; identity: string; visible: boolean }) {
  const { t, formatDateTime } = useI18n(), cache = useQueryClient()
  useSyncExternalStore(subscribeAuthSessions, authSessionsVersion)
  const generation = useRef(directoryViewGeneration()).current
  const workspace = readAuthSession(identity, connection.id), attempt = currentAuthAttempt(workspace)
  const methods = (provider?.methods || []).filter(method => !['api_key', 'local_none'].includes(method.method_id))
  const selected = workspace.selected || connection.auth_method_id || methods[0]?.method_id || 'oauth_browser'
  const [watching, setWatching] = useState(false)
  const mounted = useRef(false), visibility = useRef(visible), intent = useRef<{ cancelled: boolean } | null>(null)
  visibility.current = visible
  const startRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; if (intent.current) intent.current.cancelled = true } }, [])
  useEffect(() => { if (!visible) { setWatching(false); if (intent.current) intent.current.cancelled = true } }, [visible])
  const valid = () => { const s = useAuthStore.getState(); return !!s.token && `${s.user?.id || 0}:${s.epoch}` === identity && generation === directoryViewGeneration() }
  const live = (origin?: { cancelled: boolean }) => valid() && mounted.current && visibility.current && !origin?.cancelled
  const edit = (update: (value: AuthSessionWorkspace) => AuthSessionWorkspace) => valid() && updateAuthSession(identity, connection.id, generation, update)
  const during = (token: string, update: (value: AuthSessionWorkspace) => AuthSessionWorkspace) => edit(value => value.operation?.token === token ? update(value) : value)
  const changeAttempt = (value: AuthSessionWorkspace, id: string, update: (item: AuthStartAttempt) => AuthStartAttempt): AuthSessionWorkspace => ({ ...value, attempts: value.attempts.map(item => item.id === id ? update(item) : item) })
  const finish = (token: string) => during(token, value => ({ ...value, operation: null }))
  const claim = (kind: NonNullable<AuthSessionWorkspace['operation']>['kind'], attemptId: string) => {
    if (!valid()) return null
    const previous = readAuthSession(identity, connection.id)
    // Cancellation can preempt a slow status read. A late read cannot replace its newer command result.
    if (previous.operation && !(kind === 'cancel' && previous.operation.kind === 'status')) return null
    const token = crypto.randomUUID(); edit(value => ({ ...value, operation: { token, kind, attemptId }, notice: null })); return token
  }
  const refreshAuthTruth = () => {
    if (!valid()) return
    void cache.invalidateQueries({ queryKey: ['connections', identity] })
    void cache.invalidateQueries({ queryKey: ['readiness', identity] })
    void cache.invalidateQueries({ queryKey: ['readiness-history', identity, connection.id] })
  }
  const acceptResponse = (token: string, original: AuthStartAttempt, response: AuthOperationResponse, cancelling = false) => {
    const expectedId = original.response?.operation_id
    if (expectedId && response.operation_id !== expectedId) throw new Error('Auth operation target changed')
    const copy = snapshotAuth(response)
    const accepted = during(token, value => changeAttempt({ ...value, notice: null }, original.id, previous => {
      // Terminal server truth must never be replaced by a delayed pending snapshot.
      if (authTerminal(previous.response) && !authTerminal(copy)) return previous
      if (authTerminal(previous.response) && previous.response?.state !== copy.state) throw new Error('Auth terminal state changed')
      return { ...previous, status: 'accepted', response: copy, receivedAt: new Date().toISOString(), retryRejected: false, statusFailed: false,
        cancellation: cancelling ? { operationId: copy.operation_id, status: 'response' } : previous.cancellation }
    }))
    if (accepted && response.state === 'succeeded') refreshAuthTruth()
    return accepted
  }
  const methodsQuery = useQuery({
    queryKey: ['connection-auth-methods', identity, connection.id, connection.config_revision ?? 0, connection.auth_grant_revision ?? 0],
    enabled: methods.length > 0 && visible, retry: false,
    queryFn: async () => { if (!valid()) throw new Error('Owner changed'); const result = await connectionsApi.authMethods(connection.id); if (!valid()) throw new Error('Owner changed'); return result },
  })
  const busy = !!workspace.operation, locked = authSessionLocked(workspace)
  const availability = new Map(methodsQuery.data?.methods.map(method => [method.method_id, method.available]))
  const choices = methods.map(method => ({ method: method.method_id, label: t(authMethodLabel(method.method_id)),
    enabled: method.method_id === connection.auth_method_id && isUsableAuthMethod(method) && availability.get(method.method_id) === true && !methodsQuery.isError && !methodsQuery.isFetching,
    reason: method.method_id !== connection.auth_method_id ? t('connections.authSession.configuredMethod') : methodsQuery.isPending ? t('connections.authAvailabilityLoading') : t('connections.methodUnavailable') }))
  const canStart = choices.some(choice => choice.method === selected && choice.enabled)
  const sendStart = async (original: AuthStartAttempt, token: string, origin: { cancelled: boolean }, retry: boolean) => {
    during(token, value => changeAttempt({ ...value, operation: { token, kind: 'start', attemptId: original.id } }, original.id, item => ({ ...item, status: 'sending' })))
    try {
      const response = await connectionsApi.startAuth(original.connectionId, original.method, snapshotAuth(original.body), original.key)
      acceptResponse(token, original, response)
      if (live(origin)) setWatching(!authTerminal(response))
    } catch (error) {
      const rejected = editorSaveFailure(error) === 'rejected'
      during(token, value => changeAttempt({ ...value, notice: retry && rejected ? 'connections.authSession.retryRejected' : rejected ? 'editor.rejected' : 'connections.authSession.unknownStart' }, original.id,
        item => ({ ...item, status: retry || !rejected ? 'unknown' : 'rejected', retryRejected: retry && rejected })))
      if (live()) setWatching(false)
    }
  }
  const start = async () => {
    const previous = readAuthSession(identity, connection.id)
    if (!live() || authSessionLocked(previous) || !canStart) return
    const id = crypto.randomUUID(), token = claim('preflight', id); if (!token) return
    const origin = { cancelled: false }; intent.current = origin; setWatching(false)
    const original: AuthStartAttempt = { id, key: newIdempotencyKey(), connectionId: connection.id, label: connection.label, method: selected,
      body: Object.freeze({ version: 1, config_revision: connection.config_revision ?? 0, auth_grant_revision: connection.auth_grant_revision ?? 0 }), status: 'preparing' }
    during(token, value => ({ ...value, currentId: id, selected, attempts: [...value.attempts, original] }))
    try {
      const revisions = await connectionsApi.revisions(original.connectionId)
      if (!live(origin)) { during(token, value => changeAttempt({ ...value, notice: 'connections.authSession.notSent' }, id, item => ({ ...item, status: 'not_sent' }))); return }
      if (revisions.config_revision !== original.body.config_revision || revisions.auth_grant_revision !== original.body.auth_grant_revision) {
        during(token, value => changeAttempt({ ...value, notice: 'connections.authSession.changed' }, id, item => ({ ...item, status: 'not_sent' }))); refreshAuthTruth(); return
      }
      await sendStart(original, token, origin, false)
    } catch { during(token, value => changeAttempt({ ...value, notice: 'connections.authSession.notSent' }, id, item => ({ ...item, status: 'not_sent' }))) }
    finally { finish(token) }
  }
  const retryStart = async () => {
    const original = currentAuthAttempt(readAuthSession(identity, connection.id))
    if (!live() || original?.status !== 'unknown') return
    const token = claim('start', original.id); if (!token) return
    const origin = { cancelled: false }; intent.current = origin; setWatching(false)
    try { await sendStart(original, token, origin, true) } finally { finish(token) }
  }
  const readStatus = async (watch: boolean) => {
    const original = currentAuthAttempt(readAuthSession(identity, connection.id))
    if (!live() || !original?.response) return
    const token = claim('status', original.id); if (!token) return
    const origin = { cancelled: false }; intent.current = origin
    setWatching(watch)
    try {
      const response = await connectionsApi.authStatus(original.connectionId, original.response.operation_id)
      acceptResponse(token, original, response)
      if (live(origin) && readAuthSession(identity, connection.id).operation?.token === token) setWatching(watch && !authTerminal(response))
    } catch {
      during(token, value => changeAttempt({ ...value, notice: 'connections.authStatusFailure' }, original.id, item => ({ ...item, statusFailed: true })))
      if (live(origin) && readAuthSession(identity, connection.id).operation?.token === token) setWatching(false)
    } finally { finish(token) }
  }
  const cancel = async () => {
    const original = currentAuthAttempt(readAuthSession(identity, connection.id))
    if (!live() || !original?.response || authTerminal(original.response) || original.response.state === 'finalizing') return
    const token = claim('cancel', original.id); if (!token) return
    const origin = { cancelled: false }; intent.current = origin
    setWatching(false)
    const operationId = original.response.operation_id
    during(token, value => changeAttempt(value, original.id, item => ({ ...item, cancellation: { operationId, status: 'sending' } })))
    try {
      const response = await connectionsApi.cancelAuth(original.connectionId, operationId)
      acceptResponse(token, original, response, true)
      if (live(origin) && readAuthSession(identity, connection.id).operation?.token === token) setWatching(!authTerminal(response))
    } catch (error) {
      const rejected = editorSaveFailure(error) === 'rejected', remainsUnknown = original.cancellation?.status === 'unknown' || !rejected
      during(token, value => changeAttempt({ ...value, notice: remainsUnknown ? 'connections.authSession.unknownCancel' : 'editor.rejected' }, original.id,
        item => ({ ...item, cancellation: { operationId, status: remainsUnknown ? 'unknown' : 'rejected' } })))
    } finally { finish(token) }
  }
  const response = attempt?.response, terminal = authTerminal(response)
  const delay = response && 'poll_interval' in response ? response.poll_interval || 2 : 2
  useEffect(() => {
    if (!visible || !watching || busy || !response || terminal || attempt?.statusFailed) return
    const timer = window.setTimeout(() => { void readStatus(true) }, delay * 1000)
    return () => window.clearTimeout(timer)
  }, [visible, watching, busy, response, terminal, delay, attempt?.statusFailed])
  if (!methods.length && !workspace.attempts.length) return null
  return <section className="saved-auth stack stack--tight" aria-label={t('connections.authSection')}>
    <p className="field-hint">{t('connections.authSession.retained')}</p>
    {connection.chatgpt_account && <p className="metadata">ChatGPT · {connection.chatgpt_account.label}</p>}
    <AuthMethodPicker legend={t('connections.authMethod')} name={`saved-auth-${connection.id}`} value={selected} choices={choices}
      disabled={locked} onChange={method => { if (!live() || authSessionLocked(readAuthSession(identity, connection.id)) || !choices.some(choice => choice.method === method && choice.enabled)) return; edit(value => ({ ...value, selected: method })) }} />
    <p className="field-hint">{t('connections.authSession.configuredMethod')}</p>
    {methodsQuery.isError && <p role="alert" className="notice notice--warning">{t('connections.authMethodsFailure')}</p>}
    {methods.length > 0 && <button type="button" className="button button--secondary button--compact" disabled={methodsQuery.isFetching} onClick={() => { if (live()) void methodsQuery.refetch({ cancelRefetch: false }) }}>{t('connections.authSession.reloadMethods')}</button>}
    <button ref={startRef} type="button" className="button button--secondary button--compact" disabled={!visible || !canStart || locked} onClick={() => void start()}>{t(workspace.operation?.kind === 'preflight' || workspace.operation?.kind === 'start' ? 'connections.authStarting' : 'connections.authStart')}</button>
    {workspace.notice && <p role="alert" className="notice notice--warning">{t(workspace.notice)}</p>}
    {attempt && <div className="stack stack--tight"><p className="metadata">#{attempt.connectionId} · {attempt.label} · {t(authMethodLabel(attempt.method))}</p>
      {attempt.status === 'unknown' && <><p>{t('connections.authSession.unknownStart')}</p><p className="field-hint">{t('connections.authSession.retryHelp')}</p><button type="button" className="button button--secondary button--compact" disabled={busy} onClick={() => void retryStart()}>{t('connections.authSession.retryOriginal')}</button></>}
      {attempt.receivedAt && <p className="metadata">{t('connections.authSession.lastReceived')} {formatDateTime(attempt.receivedAt, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</p>}
      {attempt.cancellation?.status === 'unknown' && <p className="notice notice--warning">{t('connections.authSession.unknownCancel')}</p>}
      {response && !terminal && <><p className="field-hint">{t('connections.authSession.resumeHelp')}</p><button type="button" className="button button--secondary button--compact" disabled={busy} onClick={() => void readStatus(true)}>{t(watching ? 'connections.authSession.refreshStatus' : 'connections.authSession.resume')}</button>
        {watching && <button type="button" className="button button--ghost button--compact" onClick={() => { if (intent.current) intent.current.cancelled = true; setWatching(false) }}>{t('connections.authSession.pauseUpdates')}</button>}
      </>}
      {response && <AuthOperationStatus key={`${identity}:${response.operation_id}`} identity={identity} operation={response} cancelling={busy && workspace.operation?.kind !== 'status'} onCancel={() => void cancel()} onTerminalFocus={() => startRef.current?.focus()} copy={{
        browserInstruction: t('connections.authBrowserInstruction'), deviceInstruction: t('connections.authDeviceInstruction'), userCode: t('connections.authUserCode'), copy: t('connections.authCopyCode'), copied: t('connections.authCodeCopied'),
        cancel: t(attempt.cancellation?.status === 'unknown' ? 'connections.authSession.retryCancel' : 'connections.authCancel'), cancelling: t('connections.authCancelling'), pending: t('connections.authPending'), finalizing: t('connections.authFinalizing'), succeeded: t('connections.authSucceeded'), cancelled: t('connections.authCancelled'), expired: t('connections.authExpired'), failed: t('connections.authFailed'),
      }} />}
    </div>}
    {workspace.attempts.some(item => item.id !== workspace.currentId) && <details className="compact-details"><summary>{t('connections.authSession.previous')}</summary>{workspace.attempts.filter(item => item.id !== workspace.currentId).map(item => <p key={item.id}>{item.label} · {t(authMethodLabel(item.method))} · {t(item.status === 'not_sent' ? 'connections.authSession.notSent' : item.response?.state === 'succeeded' ? 'connections.authSucceeded' : item.response ? 'connections.authFailed' : 'editor.rejected')}</p>)}</details>}
  </section>
}

export default function Connections() {
  const cache = useQueryClient()
  const [searchParams] = useSearchParams()
  const { t } = useI18n()
  const identity = useAuthStore((state) => `${state.user?.id || 0}:${state.epoch}`)
  useSyncExternalStore(subscribeAuthSessions, authSessionsVersion)
  useSyncExternalStore(subscribeConnectionTests, connectionTestsVersion)
  const testInFlight = connectionTestsBusy(identity)
  const testViewGeneration = directoryViewGeneration()
  const directoryOwnerCurrent = () => `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}` === identity
  const [directoryQuery, setDirectoryQuery] = useDirectoryValue<string>(identity, 'connection.search', '')
  const directoryRoot = useRef<HTMLElement>(null)
  const directoryFinder = useRef<DirectorySearchHandle>(null)
  const [pendingSavedFocus, setPendingSavedFocus] = useState<{ identity: string; id: number; snapshotVersion: number } | null>(null)
  const changeDirectoryQuery = (value: string) => {
    if (!directoryOwnerCurrent()) return
    setPendingSavedFocus(null); setDirectoryQuery(value)
  }
  const clearDirectorySearch = () => {
    if (!(directoryOwnerCurrent() && directoryViewGeneration() === testViewGeneration) || !directoryRoot.current?.isConnected) return
    directoryFinder.current?.focus()
    changeDirectoryQuery('')
  }

  const firstUse = searchParams.get('first') === '1'
  const catalogQuery = useQuery({ queryKey: ['provider-catalog', identity], queryFn: connectionsApi.catalog })
  const connectionsQuery = useQuery({ queryKey: ['connections', identity], queryFn: async () => { if (!directoryOwnerCurrent()) throw new Error('Owner changed'); const value = await connectionsApi.list()
    const current = useAuthStore.getState(); if (`${current.user?.id || 0}:${current.epoch}` !== identity) throw new Error('Owner changed'); return value } })
  useDirectoryPosition(identity, 'connection', directoryQuery, directoryRoot, connectionsQuery.data !== undefined, !firstUse && !searchParams.has('edit'))
  const descriptorFor = useCallback((providerId: string) => catalogQuery.data?.providers.find((provider) => provider.provider_id === providerId), [catalogQuery.data])
  const providerLabel = (connection: Connection) => {
    const descriptor = descriptorFor(connection.provider_id || connection.provider_kind)
    return descriptor ? providerDisplayName(descriptor, t) : t('connections.providers.unknown')
  }
  const { data: connections = [], isLoading, isError, isFetching } = connectionsQuery
  const localWorkspace = useSyncExternalStore(subscribeLocalSetup, () => readLocalSetup(identity))
  const [localSetup,setLocalSetup]=useState(false)
  const [localSetupSession, setLocalSetupSession] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(firstUse&&!readConnectionDraft(identity))
  const [step, setStep] = useState(1)
  useSyncExternalStore(subscribeConnectionDrafts,connectionDraftVersion)
  const parkedDraft=readConnectionDraft(identity),editorSession=useRef<string>(crypto.randomUUID()),editorDraftSource=useRef<ConnectionDraftSource|null>(null)
  const [draftReading,setDraftReading]=useState(false),[draftFailure,setDraftFailure]=useState<'changed'|'unavailable'|'read'|null>(null)
  const draftReadLock=useRef<symbol|null>(null)
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})
  const [loadedDetails, setLoadedDetails] = useState<Record<number, boolean>>({})
  const formRef = useRef<HTMLFormElement>(null)
  const editorRef = useRef<HTMLElement>(null)
  const [editing, setEditing] = useState<number | null>(null)
  const formIme = useImeFormGuard(`${identity}:${showForm}:${editing ?? 'new'}`)
  const [form, setForm] = useState<VersionedConnectionInput>({ ...blank, fields: {}, capabilities: {} })
  const [key, setKey] = useState('')
  const [showInputIssues, setShowInputIssues] = useState(false)
  const [invalidFocus, setInvalidFocus] = useState<{ field: HTMLInputElement | HTMLSelectElement; root: HTMLFormElement;
    owner: string; editing: number | null; form: VersionedConnectionInput; stage: number; origin: Element | null; native: boolean } | null>(null)
  const handledInvalidFocus = useRef<typeof invalidFocus>(null)
  const currentEditorForm = useRef(form)
  currentEditorForm.current = form

  const editorBaseline = useRef({ identity, snapshot: connectionEditorSnapshot(form) })
  const editorDirty = showForm && editorBaseline.current.identity === identity
    && (editorBaseline.current.snapshot !== connectionEditorSnapshot(form) || key.length > 0)
  const methodFocusVersion = 0
  const [requiresSecretReentry, setRequiresSecretReentry] = useState(false)
  const [error, setError] = useState<MessageKey | ''>('')
  useSyncExternalStore(subscribeConnectionSaves,connectionSavesVersion)
  const saveFeedback=readConnectionSaves(identity).active
  const setSaveFeedback=(value:ConnectionSaveFeedback|null)=>updateConnectionSave(identity,testViewGeneration,value)
  const saveLock = useRef<string | null>(null)
  useSyncExternalStore(subscribeConnectionRemovals,connectionRemovalVersion)
  const [savedFind,setSavedFind]=useState<{identity:string;id:number;name:string;phase:'reading'|'moving'|'found'|'missing'|'failed'|'not_moved';token:symbol;origin:Element|null}|null>(null)
  const savedFindLock=useRef<symbol|null>(null)
  const findingSaved=savedFind?.identity===identity&&(savedFind.phase==='reading'||savedFind.phase==='moving')
  const [inactiveTarget,setInactiveTarget]=useState<{id:number;label:string;identity:string;generation:number}|null>(null)
  const [removing,setRemoving]=useState<{target:ConnectionRemovalTarget;identity:string;generation:number}|null>(null)
  const removalRef=useRef(removing);removalRef.current=removing
  const deleting=removing?.identity===identity&&removing.generation===testViewGeneration?removing.target.id:null
  const [removalFailure,setRemovalFailure]=useState<ConnectionRemovalFailure|null>(null),removalFailureRef=useRef(removalFailure)
  const changeRemovalFailure=(value:ConnectionRemovalFailure|null)=>{removalFailureRef.current=value;setRemovalFailure(value)}
  const [removalRead,setRemovalRead]=useState<{target:ConnectionRemovalTarget;phase:'reading'|'ready'|'error'}|null>(null)
  const removalReadLock=useRef<ConnectionRemovalTarget|null>(null),removeLock=useRef<symbol|null>(null),removalMounted=useRef(true)
  useEffect(()=>{removalMounted.current=true;return()=>{removalMounted.current=false}},[])
  const removalOwner=()=>removalMounted.current&&directoryOwnerCurrent()&&directoryViewGeneration()===testViewGeneration

  const [unlockStates, setUnlockStates] = useState<Record<number, string>>({})
  const [disconnecting, setDisconnecting] = useState<number | null>(null)
  const [revokeRemote, setRevokeRemote] = useState(false)
  const [disconnectStartError, setDisconnectStartError] = useState(false)
  const [disconnectBusy, setDisconnectBusy] = useState(false)
  const [disconnectViews, setDisconnectViews] = useState<Record<number, DisconnectView>>({})
  const [disconnectFocus, setDisconnectFocus] = useState<DisconnectFocusRequest | null>(null)
  const keyInput = useRef<HTMLInputElement>(null)
  const disconnectDialogFocus = useRef<HTMLInputElement>(null)
  const disconnectButtonRefs = useRef<Record<number, HTMLButtonElement | null>>({})
  const disconnectCancelRefs = useRef<Record<number, HTMLButtonElement | null>>({})
  const disconnectStatusRefs = useRef<Record<number, HTMLParagraphElement | null>>({})
  const disconnectTimers = useRef<Record<number, number>>({})
  const disconnectTokens = useRef<Record<number, string>>({})
  const disconnectSequence = useRef(0)
  const appliedRequestedEdit = useRef<number | null>(null)
  const editingBaseline = useRef<string | null>(null)
  const selectedProvider = descriptorFor(form.provider_id)
  const selectedMethod = selectedProvider?.methods.find((method) => method.method_id === form.auth_method_id)
  const formUsable = Boolean(selectedMethod && isUsableAuthMethod(selectedMethod))
  const save = useMutation({
    retry: false,
    mutationFn: ({ data, editing, identity: sent, generation }: { data: VersionedConnectionInput; editing: number | null; identity: string; baselineFingerprint: string | null; feedback: ConnectionSaveFeedback; generation:number; draftSession:string }) => {
      if (sent !== `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}` || directoryViewGeneration()!==generation) throw new Error('Owner changed')
      return editing === null ? connectionsApi.create(data) : connectionsApi.update(editing, data)
    },
    onSuccess: (_result, sent) => { if (sent.identity !== `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`||directoryViewGeneration()!==sent.generation) return
      forgetConnectionDraft(sent.identity,sent.generation,sent.draftSession)
      setSaveFeedback({ ...sent.feedback, target: { ...sent.feedback.target, id: _result.id, label: _result.label }, phase: 'success' })
      setDirectoryQuery(''); setPendingSavedFocus({ identity: sent.identity, id: _result.id, snapshotVersion: connectionsQuery.dataUpdatedAt })
      if (sent.editing !== null && sent.baselineFingerprint !== null
        && (sent.baselineFingerprint !== connectionInputFingerprint(sent.data) || Boolean(sent.data.api_key))) {
      }
      setShowForm(false); setStep(1); setExpanded((current) => ({ ...current, [_result.id]: true })); setLoadedDetails((current) => ({ ...current, [_result.id]: true }))
      editingBaseline.current = null
      editorBaseline.current = { identity: sent.identity, snapshot: connectionEditorSnapshot({ ...blank, fields: {}, capabilities: {} }) }
      setKey('')
      cache.invalidateQueries({ queryKey: ['connections', sent.identity] }); cache.invalidateQueries({ queryKey: ['readiness', sent.identity] }); cache.invalidateQueries({ queryKey: ['model-features', sent.identity, _result.id] }); setEditing(null); setRequiresSecretReentry(false); setForm({ ...blank, fields: {}, capabilities: {} }); setError('') },
    onError: (cause, sent) => { if (sent.identity === `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`&&directoryViewGeneration()===sent.generation) {
      const phase = editorSaveFailure(cause) === 'rejected' ? 'rejected' : 'unknown'
      setSaveFeedback({ ...sent.feedback, phase })
      setError(phase === 'rejected' && apiErrorCode(cause) === 'invalid_model_protocol' ? 'connections.modelProtocolMismatch' : '')
    } },
    onSettled: (_result, _failure, sent) => { if (saveLock.current === sent.identity) saveLock.current = null },
  })
  const inspectConnectionSave = async () => {
    if (!saveFeedback || !directoryOwnerCurrent()) throw new Error('Owner changed')
    const target = saveFeedback.target
    const result = await connectionsQuery.refetch()
    if (result.isError || !directoryOwnerCurrent()) throw new Error('List read failed')
    return (result.data || []).filter(connection => target.id !== null ? connection.id === target.id
      : connection.label === target.label && (connection.provider_id || connection.provider_kind) === target.providerId && connection.default_model_id === target.modelId)
      .map(connection => ({ id: connection.id, name: connection.label, observation: {
        provider_id: connection.provider_id, provider_kind: connection.provider_kind, default_model_id: connection.default_model_id,
        enabled: connection.enabled, has_secret: connection.has_secret,
      } }))
  }
  const findSavedConnection=async(feedback:ConnectionSaveFeedback)=>{
    if(!removalOwner()||feedback.identity!==identity||feedback.phase!=='success'||!Number.isSafeInteger(feedback.target.id)||!feedback.target.id||feedback.target.id<1||savedFindLock.current)return
    const token=Symbol('saved connection navigation'),original={identity,id:feedback.target.id,name:feedback.target.label,token,origin:document.activeElement}
    savedFindLock.current=token;setPendingSavedFocus(null);setSavedFind({...original,phase:'reading'})
    const current=()=>removalOwner()&&savedFindLock.current===token
    let moving=false
    try{
      let state=cache.getQueryState<Connection[]>(['connections',identity])
      if(state?.status!=='success'||state.fetchStatus!=='idle'||state.isInvalidated||!state.data?.some(row=>row.id===original.id)){
        const read=await connectionsQuery.refetch()
        if(!current())return
        if(read.isError){setSavedFind({...original,phase:'failed'});return}
        state=cache.getQueryState<Connection[]>(['connections',identity])
      }
      if(!current())return
      if(state?.status!=='success'||state.fetchStatus!=='idle'||state.isInvalidated){setSavedFind({...original,phase:'failed'});return}
      const target=state.data?.find(row=>row.id===original.id)
      if(!target){setSavedFind({...original,phase:'missing'});return}
      setDirectoryQuery('');moving=true;setSavedFind({...original,name:target.label,phase:'moving'})
    }catch{if(current())setSavedFind({...original,phase:'failed'})}
    finally{if(!moving&&savedFindLock.current===token)savedFindLock.current=null}
  }
  const remove = useMutation({
    retry:false,
    mutationFn:async(sent:{target:ConnectionRemovalTarget;identity:string;generation:number;token:symbol;started:boolean;requestId:string})=>{
      if(!removalOwner()||sent.identity!==identity||sent.generation!==testViewGeneration||removeLock.current!==sent.token||removalRef.current?.target!==sent.target||!removalReady(sent.target)||removalBlocked(sent.target.id))throw Error('Connection removal not started')
      sent.started=true
      await connectionsApi.delete(sent.target.id)
    },
    onSuccess:(_result,sent)=>{
      if(!directoryOwnerCurrent()||directoryViewGeneration()!==sent.generation)return
      settleConnectionRemoval(sent.identity,sent.generation,sent.requestId,'succeeded')
      void cache.invalidateQueries({queryKey:['connections',sent.identity]});void cache.invalidateQueries({queryKey:['readiness',sent.identity]})
      if(removalOwner()&&removalRef.current?.target===sent.target){removalRef.current=null;setRemoving(null);changeRemovalFailure(null);setRemovalRead(null)}
    },
    onError:(cause,sent)=>{
      if(!directoryOwnerCurrent()||directoryViewGeneration()!==sent.generation)return
      const failure:ConnectionRemovalFailure=!sent.started?'not_sent':apiErrorCode(cause)==='connection_in_use'?'in_use':editorSaveFailure(cause)==='rejected'?'rejected':'unknown'
      settleConnectionRemoval(sent.identity,sent.generation,sent.requestId,failure)
      if(!removalOwner()||removalRef.current?.target!==sent.target)return
      changeRemovalFailure(failure)
      setRemovalRead(null)
    },
    onSettled:(_result,_cause,sent)=>{if(removeLock.current===sent.token)removeLock.current=null},
  })
  const removalBlocked=(id:number)=>connectionSaveBusy(identity)||!!saveLock.current||unlock.isPending&&unlock.variables?.id===id||disconnectBusy||
    authSessionLocked(readAuthSession(identity,id))||connectionTestActive(identity,id)||
    (disconnectViews[id]?.kind==='operation'&&isDisconnectPending((disconnectViews[id] as Extract<DisconnectView,{kind:'operation'}>).response))
  const removalReady=(target:ConnectionRemovalTarget)=>{
    const state=cache.getQueryState<Connection[]>(['connections',identity])
    return state?.status==='success'&&state.fetchStatus==='idle'&&!state.isInvalidated&&sameConnectionRemovalTarget(target,state.data?.find(row=>row.id===target.id))
  }
  const openRemoval=(connection:Connection)=>{
    if(!removalOwner()||removeLock.current||remove.isPending||connectionRemovalBusy(identity))return
    const value={target:connectionRemovalTarget(connection),identity,generation:testViewGeneration}
    removalRef.current=value;setRemoving(value);changeRemovalFailure(previousConnectionRemovalFailure(identity,connection.id));setRemovalRead(null)
  }
  const openInactive=(connection:Connection)=>{
    if(!removalOwner()||removeLock.current||connectionRemovalBusy(identity))return
    closeRemoval();setInactiveTarget({id:connection.id,label:connection.label,identity,generation:testViewGeneration})
  }
  const closeRemoval=()=>{
    if(!removalOwner()||removeLock.current||connectionRemovalBusy(identity))return
    removalRef.current=null;setRemoving(null);changeRemovalFailure(null);setRemovalRead(null)
  }
  const readRemoval=async()=>{
    const target=removalRef.current?.target
    if(!target||!removalOwner()||removeLock.current||connectionRemovalBusy(identity)||removalReadLock.current)return
    removalReadLock.current=target;setRemovalRead({target,phase:'reading'})
    try{
      const result=await connectionsQuery.refetch()
      if(removalOwner()&&removalRef.current?.target===target)setRemovalRead({target,phase:result.isError?'error':'ready'})
    }catch{if(removalOwner()&&removalRef.current?.target===target)setRemovalRead({target,phase:'error'})}
    finally{if(removalReadLock.current===target)removalReadLock.current=null}
  }
  const adoptRemoval=()=>{
    const target=removalRef.current?.target,state=cache.getQueryState<Connection[]>(['connections',identity]),row=state?.data?.find(value=>value.id===target?.id)
    if(!target||!row||!removalOwner()||removeLock.current||connectionRemovalBusy(identity)||removalReadLock.current||removalBlocked(target.id)||state?.status!=='success'||state.fetchStatus!=='idle'||state.isInvalidated)return
    if(removalFailureRef.current&&!(removalRead?.target===target&&removalRead.phase==='ready'))return
    if(sameConnectionRemovalTarget(target,row)&&!(removalFailureRef.current&&removalRead?.target===target&&removalRead.phase==='ready'))return
    const value={target:connectionRemovalTarget(row),identity,generation:testViewGeneration}
    removalRef.current=value;setRemoving(value);changeRemovalFailure(null);setRemovalRead(null)
  }
  const confirmRemoval=()=>{
    const target=removalRef.current?.target
    if(!target||!removalOwner()||removeLock.current||connectionRemovalBusy(identity)||removalReadLock.current||removalFailureRef.current||!removalReady(target)||removalBlocked(target.id))return
    const request=beginConnectionRemoval(identity,testViewGeneration,target)
    if(!request)return
    const token=Symbol('connection removal');removeLock.current=token
    remove.mutate({target,identity,generation:testViewGeneration,token,started:false,requestId:request.id})
  }
  const unlock = useMutation({
    mutationFn: async ({ id }: { id: number; identity: string }) => {
      let status = await connectionsApi.unlock(id)
      for (let attempts = 0; status.state === 'pending' && attempts < 120; attempts += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 500))
        status = await connectionsApi.unlockStatus(id)
      }
      return { id, state: status.state }
    },
    onSuccess: ({ id, state }, sent) => { if (sent.identity === `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`) setUnlockStates((current) => ({ ...current, [id]: state })) },
    onError: (_cause, sent) => { if (sent.identity === `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`) setUnlockStates((current) => ({ ...current, [sent.id]: 'error' })) },
  })

  const clearDisconnectTimer = useCallback((id: number) => {
    const timer = disconnectTimers.current[id]
    if (timer !== undefined) window.clearTimeout(timer)
    delete disconnectTimers.current[id]
  }, [])
  const refreshAfterDisconnect = useCallback((sentIdentity: string, id: number) => {
    cache.invalidateQueries({ queryKey: ['connections', sentIdentity] })
    cache.invalidateQueries({ queryKey: ['readiness', sentIdentity] })
    cache.invalidateQueries({ queryKey: ['readiness-history', sentIdentity, id] })
  }, [cache])
  const nextDisconnectToken = () => String(++disconnectSequence.current)
  const requestDisconnectFocus = (id: number, sentIdentity: string, response?: DisconnectOperationResponse) => {
    const target = !response || !isDisconnectPending(response) ? 'trigger'
      : response.state === 'pending' ? 'cancel' : 'status'
    setDisconnectFocus({ id, identity: sentIdentity, target })
  }
  const closeDisconnectDialog = (id: number, restoreFocus = true) => {
    setDisconnecting(null); setRevokeRemote(false); setDisconnectStartError(false)
    if (restoreFocus) disconnectButtonRefs.current[id]?.focus()
  }
  const startDisconnect = async (id: number) => {
    const sentIdentity = identity
    const requestKey = newIdempotencyKey()
    const token = nextDisconnectToken()
    disconnectTokens.current[id] = token
    clearDisconnectTimer(id)
    setDisconnectBusy(true); setDisconnectStartError(false)
    try {
      const response = await connectionsApi.disconnect(id, revokeRemote, requestKey)
      const currentIdentity = `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
      if (currentIdentity !== sentIdentity || disconnectTokens.current[id] !== token) return
      if ('operation_id' in response) {
        const operationToken = nextDisconnectToken()
        disconnectTokens.current[id] = operationToken
        setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response } }))
        requestDisconnectFocus(id, sentIdentity, response)
      } else {
        delete disconnectTokens.current[id]
        setDisconnectViews((current) => ({ ...current, [id]: { kind: 'immediate', result: response } }))
        requestDisconnectFocus(id, sentIdentity)
        refreshAfterDisconnect(sentIdentity, id)
      }
      closeDisconnectDialog(id, false)
    } catch {
      const currentIdentity = `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
      if (currentIdentity === sentIdentity && disconnectTokens.current[id] === token) setDisconnectStartError(true)
    } finally {
      if (`${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}` === sentIdentity) setDisconnectBusy(false)
    }
  }
  const cancelDisconnect = async (id: number, response: DisconnectPending) => {
    const sentIdentity = identity
    clearDisconnectTimer(id)
    const token = nextDisconnectToken()
    disconnectTokens.current[id] = token
    setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response, cancelling: true } }))
    try {
      const next = await connectionsApi.cancelDisconnect(id, response.operation_id)
      const currentIdentity = `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
      if (currentIdentity !== sentIdentity || disconnectTokens.current[id] !== token) return
      if (next.operation_id !== response.operation_id) {
        setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response, statusError: true } }))
        requestDisconnectFocus(id, sentIdentity, response)
        return
      }
      setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response: next } }))
      requestDisconnectFocus(id, sentIdentity, next)
      if (!isDisconnectPending(next)) {
        delete disconnectTokens.current[id]
        refreshAfterDisconnect(sentIdentity, id)
      }
    } catch {
      const currentIdentity = `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
      if (currentIdentity === sentIdentity && disconnectTokens.current[id] === token) {
        setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response, statusError: true } }))
        requestDisconnectFocus(id, sentIdentity, response)
      }
    }
  }
  const retryDisconnectStatus = (id: number, response: DisconnectPending) => {
    clearDisconnectTimer(id)
    disconnectTokens.current[id] = nextDisconnectToken()
    setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response } }))
    requestDisconnectFocus(id, identity, response)
  }

  useEffect(() => {
    if (!disconnectFocus || disconnectFocus.identity !== identity) return
    setDirectoryQuery(''); setPendingSavedFocus(null)
    const element = disconnectFocus.target === 'trigger' ? disconnectButtonRefs.current[disconnectFocus.id]
      : disconnectFocus.target === 'cancel' ? disconnectCancelRefs.current[disconnectFocus.id]
        : disconnectStatusRefs.current[disconnectFocus.id]
    if (!element || ('disabled' in element && element.disabled)) return
    element.focus()
    setDisconnectFocus((current) => current === disconnectFocus ? null : current)
  }, [disconnectFocus, disconnectBusy, disconnectViews, identity])

  useEffect(() => {
    for (const [rawId, view] of Object.entries(disconnectViews)) {
      const id = Number(rawId)
      if (view.kind !== 'operation' || view.statusError || view.cancelling || !isDisconnectPending(view.response) || disconnectTimers.current[id] !== undefined) continue
      const expectedIdentity = identity
      const expectedOperation = view.response.operation_id
      const token = disconnectTokens.current[id]
      disconnectTimers.current[id] = window.setTimeout(async () => {
        delete disconnectTimers.current[id]
        try {
          const next = await connectionsApi.disconnectStatus(id, expectedOperation)
          const currentIdentity = `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
          if (currentIdentity !== expectedIdentity || disconnectTokens.current[id] !== token) return
          if (next.operation_id !== expectedOperation) {
            setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response: view.response, statusError: true } }))
            requestDisconnectFocus(id, expectedIdentity, view.response)
            return
          }
          setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response: next } }))
          requestDisconnectFocus(id, expectedIdentity, next)
          if (!isDisconnectPending(next)) {
            delete disconnectTokens.current[id]
            refreshAfterDisconnect(expectedIdentity, id)
          }
        } catch {
          const currentIdentity = `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
          if (currentIdentity === expectedIdentity && disconnectTokens.current[id] === token) {
            setDisconnectViews((current) => ({ ...current, [id]: { kind: 'operation', response: view.response, statusError: true } }))
            requestDisconnectFocus(id, expectedIdentity, view.response)
          }
        }
      }, view.response.poll_interval * 1000)
    }
  }, [disconnectViews, identity, refreshAfterDisconnect])
  useEffect(() => () => {
    for (const id of Object.keys(disconnectTimers.current).map(Number)) clearDisconnectTimer(id)
    disconnectTokens.current = {}
  }, [clearDisconnectTimer])

  const confirmDiscardEditor = useCallback(() => {
    if (!directoryOwnerCurrent() || connectionSaveBusy(identity)) return false
    if((editorDirty||readConnectionDraft(identity))&&!window.confirm(t('connections.draft.replace')))return false
    if(!directoryOwnerCurrent())return false
    forgetConnectionDraft(identity,testViewGeneration)
    forgetConnectionDraft(identity,testViewGeneration,editorSession.current)
    editorSession.current=crypto.randomUUID()
    return true
  }, [identity, editorDirty, connectionSaveBusy(identity), t])
  useEffect(() => {
    if (!editorDirty && !connectionSaveBusy(identity)) return
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!directoryOwnerCurrent()) return
      event.preventDefault(); event.returnValue = ''
    }
    const leaveByLink = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !directoryOwnerCurrent()) return
      const link = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>('a[href]') : null
      if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return
      let destination: URL
      try { destination = new URL(link.href, window.location.href) } catch { return }
      if (destination.origin !== window.location.origin || destination.pathname === window.location.pathname) return
      const allowed = window.confirm(t(connectionSaveBusy(identity)?'connections.leaveWhileSaving':'connections.draft.leave'))
      if (!allowed) { event.preventDefault(); event.stopPropagation() }
    }
    window.addEventListener('beforeunload', beforeUnload)
    document.addEventListener('click', leaveByLink, true)
    return () => {
      window.removeEventListener('beforeunload', beforeUnload)
      document.removeEventListener('click', leaveByLink, true)
    }
  }, [identity, editorDirty, connectionSaveBusy(identity), confirmDiscardEditor, t])

  const edit = useCallback((connection: Connection, requireSecretReentry = false) => {
    if (!directoryOwnerCurrent() || connectionSaveBusy(identity)) return
    if (showForm && editing === connection.id) {
      if (requireSecretReentry) { setRequiresSecretReentry(true); setStep(2) }
      editorRef.current?.focus(); return
    }
    if (!confirmDiscardEditor()) return
    const providerId = connection.provider_id || connection.provider_kind
    const descriptor = descriptorFor(providerId)
    const nextForm: VersionedConnectionInput = { version: 1, provider_id: providerId,
      protocol_id: connection.protocol_id || descriptor?.protocols[0] || 'openai_chat_completions',
      auth_method_id: legacyMethod(connection), fields: legacyFields(connection), label: connection.label,
      default_model_id: connection.default_model_id, enabled: connection.enabled,
      capabilities: { ...connection.capabilities.declared } }
    editorSession.current=crypto.randomUUID();editorDraftSource.current={config:connection.config_revision??null,auth:connection.auth_grant_revision??null}
    editingBaseline.current = connectionInputFingerprint(nextForm)
    editorBaseline.current = { identity, snapshot: connectionEditorSnapshot(nextForm) }
    setLocalSetup(false)
    setShowForm(true); setStep(requireSecretReentry ? 2 : 1)
    setEditing(connection.id)
    setRequiresSecretReentry(requireSecretReentry)
    setForm(nextForm)
    setKey('')
    setError('')
  }, [identity, descriptorFor, connectionSaveBusy(identity), showForm, editing, confirmDiscardEditor])
  const requestedEdit = Number(searchParams.get('edit'))
  useEffect(() => {
    if (connectionSaveBusy(identity) || !Number.isSafeInteger(requestedEdit) || requestedEdit < 1 || appliedRequestedEdit.current === requestedEdit) return
    const connection = connections.find((item) => item.id === requestedEdit)
    if (!connection) return
    appliedRequestedEdit.current = requestedEdit
    edit(connection, true)
  }, [connections, edit, requestedEdit, connectionSaveBusy(identity)])
  useEffect(() => {
    if (editing !== null && requiresSecretReentry) keyInput.current?.focus()
  }, [editing, requiresSecretReentry])
  useEffect(() => {
    if (disconnecting !== null) disconnectDialogFocus.current?.focus()
  }, [disconnecting])
  useEffect(() => {
    for (const id of Object.keys(disconnectTimers.current).map(Number)) clearDisconnectTimer(id)
    disconnectTokens.current = {}
    setDisconnectFocus(null)
    setDisconnectViews({}); setDisconnecting(null); setRevokeRemote(false); setDisconnectStartError(false); setDisconnectBusy(false)
    setLocalSetup(false); setLocalSetupSession(null); setShowForm(firstUse&&!readConnectionDraft(identity)); setStep(1); setExpanded({}); setLoadedDetails({})
    // Same-login notice lives in the shared workspace. Owner reset already
    // purges it through authStore; mounting this page must not dismiss it.
    saveLock.current = null
    editorSession.current=crypto.randomUUID();editorDraftSource.current=null;draftReadLock.current=null;setDraftReading(false);setDraftFailure(null);savedFindLock.current=null;setSavedFind(null)
    setUnlockStates({}); setEditing(null); setRequiresSecretReentry(false); setError('')
    editingBaseline.current = null; editorBaseline.current = { identity, snapshot: connectionEditorSnapshot({ ...blank, fields: {}, capabilities: {} }) }; setForm({ ...blank, fields: {}, capabilities: {} }); setKey(''); setRemoving(null); changeRemovalFailure(null); setRemovalRead(null); setInactiveTarget(null); appliedRequestedEdit.current = null; setPendingSavedFocus(null)
  }, [identity, clearDisconnectTimer])
  const changeForm = (next: VersionedConnectionInput) => setForm(next)
  useEffect(() => { if (firstUse&&!readConnectionDraft(identity)) setShowForm(true) }, [firstUse,identity])
  const providerForm = (providerId: string): VersionedConnectionInput | null => {
    const provider = descriptorFor(providerId)
    if (!provider) return null
    const method = provider.methods.find((candidate) => isUsableAuthMethod(candidate)) || provider.methods[0]
    const protocolId = provider.protocols[0]
    return { ...blank, provider_id: provider.provider_id, protocol_id: protocolId,
      auth_method_id: method.method_id, fields: initialProviderFields(provider, protocolId), capabilities: provider.provider_id === 'chatgpt' ? { tools: true, vision: false } : {}, enabled: form.enabled,
      label: providerDisplayName(provider, t), default_model_id: provider.model_source.static_model_ids.find((id) => !provider.model_source.static_model_protocols?.[id] || provider.model_source.static_model_protocols[id] === protocolId) || '' }
  }
  const selectProvider = (providerId: string) => {
    if (providerId === form.provider_id || !confirmDiscardEditor()) return
    const next = providerForm(providerId)
    if (!next) return
    setForm(next)
    setKey(''); setError(''); setRequiresSecretReentry(false)
  }
  const resetEditor = (preserveDraft=false) => {
    if(!preserveDraft){forgetConnectionDraft(identity,testViewGeneration);forgetConnectionDraft(identity,testViewGeneration,editorSession.current)}
    editorSession.current=crypto.randomUUID();editorDraftSource.current=null
    editingBaseline.current = null; setShowForm(false); setEditing(null); setRequiresSecretReentry(false)
    editorBaseline.current = { identity, snapshot: connectionEditorSnapshot({ ...blank, fields: {}, capabilities: {} }) }
    setForm({ ...blank, fields: {}, capabilities: {} }); setKey(''); setError(''); setStep(1)
  }
  const closeEditor = () => { if (confirmDiscardEditor()) resetEditor() }
  const startLocalSetup = () => {
    if (!confirmDiscardEditor()) return
    resetEditor(); setLocalSetupSession(identity); setLocalSetup(true)
  }
  const startNew = () => {
    if (!directoryOwnerCurrent() || connectionSaveBusy(identity)) return
    if (showForm && editing === null) { editorRef.current?.focus(); return }
    if (!confirmDiscardEditor()) return
    const next = providerForm(blank.provider_id) || { ...blank, fields: {}, capabilities: {} }
    editorBaseline.current = { identity, snapshot: connectionEditorSnapshot(next) }
    editorSession.current=crypto.randomUUID();editorDraftSource.current=null
    editingBaseline.current = null; setEditing(null); setRequiresSecretReentry(false); setLocalSetup(false); setShowForm(true); setStep(1)
    setForm(next); setKey(''); setError('')
  }
  const changeMethod = (method: AuthMethodId) => {
    if (method === form.auth_method_id || !directoryOwnerCurrent() || connectionSaveBusy(identity)) return
    if (key && !window.confirm(t('connections.discardKey'))) return
    if (!directoryOwnerCurrent()) return
    setKey(''); setForm({ ...form, auth_method_id: method })
  }
  const draftSnapshot:ConnectionDraftInput={id:editorSession.current,editing,form:{version:1,provider_id:form.provider_id,protocol_id:form.protocol_id,auth_method_id:form.auth_method_id,fields:{...form.fields},label:form.label,default_model_id:form.default_model_id,enabled:form.enabled,capabilities:{...form.capabilities}},baselineFingerprint:editingBaseline.current,baselineSnapshot:editorBaseline.current.snapshot,source:editorDraftSource.current,step,requiresSecretReentry:requiresSecretReentry||Boolean(key)}
  const draftOnLeave=useRef({identity,generation:testViewGeneration,showForm,dirty:editorDirty,value:draftSnapshot})
  draftOnLeave.current={identity,generation:testViewGeneration,showForm,dirty:editorDirty,value:draftSnapshot}
  useEffect(()=>()=>{
    const state=draftOnLeave.current,owner=useAuthStore.getState()
    if(state.identity!==identity||!owner.token||`${owner.user?.id||0}:${owner.epoch}`!==identity||!state.showForm||!state.dirty)return
    keepConnectionDraft(identity,state.generation,state.value)
  },[identity])
  const parkEditor=()=>{
    if(!removalOwner()||connectionSaveBusy(identity)||draftReadLock.current||!editorDirty)return
    if(keepConnectionDraft(identity,testViewGeneration,draftSnapshot)){draftOnLeave.current={...draftOnLeave.current,showForm:false};resetEditor(true)}
  }
  const resumeEditorDraft=async()=>{
    const original=readConnectionDraft(identity)
    if(!original||!removalOwner()||showForm||connectionSaveBusy(identity)||draftReadLock.current)return
    const token=Symbol('connection draft read');draftReadLock.current=token;setDraftReading(true);setDraftFailure(null)
    const valid=()=>removalOwner()&&draftReadLock.current===token&&readConnectionDraft(identity)===original&&!connectionSaveBusy(identity)
    try{
      const provider=descriptorFor(original.form.provider_id),method=provider?.methods.find(value=>value.method_id===original.form.auth_method_id)
      if(!provider||!provider.protocols.includes(original.form.protocol_id)||!method||!isUsableAuthMethod(method)){if(valid())setDraftFailure('unavailable');return}
      if(original.editing!==null){
        const result=await connectionsQuery.refetch()
        if(!valid())return
        if(result.isError){setDraftFailure('read');return}
        const current=result.data?.find(row=>row.id===original.editing)
        if(!current){setDraftFailure('changed');return}
        const currentInput:VersionedConnectionInput={version:1,provider_id:current.provider_id||current.provider_kind,protocol_id:current.protocol_id||descriptorFor(current.provider_id||current.provider_kind)?.protocols[0]||'openai_chat_completions',auth_method_id:legacyMethod(current),fields:legacyFields(current),label:current.label,default_model_id:current.default_model_id,enabled:current.enabled,capabilities:{...current.capabilities.declared}}
        if(connectionEditorSnapshot(currentInput)!==original.baselineSnapshot||(current.config_revision??null)!==original.source?.config||(current.auth_grant_revision??null)!==original.source?.auth){setDraftFailure('changed');return}
      }
      if(!valid())return
      editorSession.current=original.id;editorDraftSource.current=original.source
      editingBaseline.current=original.baselineFingerprint;editorBaseline.current={identity,snapshot:original.baselineSnapshot}
      setLocalSetup(false);setEditing(original.editing);setForm({...original.form,fields:{...original.form.fields},capabilities:{...original.form.capabilities}});setKey('');setRequiresSecretReentry(original.requiresSecretReentry)
      setStep(original.requiresSecretReentry&&original.form.auth_method_id==='api_key'?2:Math.min(3,original.step));setError('');setShowInputIssues(false);setShowForm(true)
    }catch{if(valid())setDraftFailure('read')}
    finally{if(draftReadLock.current===token){draftReadLock.current=null;if(removalOwner())setDraftReading(false)}}
  }
  const useDraftAsNew=()=>{
    const original=readConnectionDraft(identity)
    if(!original||!removalOwner()||showForm||connectionSaveBusy(identity)||draftReadLock.current)return
    const provider=descriptorFor(original.form.provider_id),method=provider?.methods.find(value=>value.method_id===original.form.auth_method_id)
    if(!provider||!provider.protocols.includes(original.form.protocol_id)||!method||!isUsableAuthMethod(method)){setDraftFailure('unavailable');return}
    // Explicitly consume this parked input into a new editor, without carrying
    // the old connection ID, revisions, stored credentials or Agent references.
    if(readConnectionDraft(identity)!==original||!forgetConnectionDraft(identity,testViewGeneration,original.id))return
    editorSession.current=crypto.randomUUID();editorDraftSource.current=null
    editingBaseline.current=null;editorBaseline.current={identity,snapshot:connectionEditorSnapshot({...blank,fields:{},capabilities:{}})}
    setLocalSetup(false);setEditing(null);setForm({...original.form,fields:{...original.form.fields},capabilities:{...original.form.capabilities}});setKey('')
    setRequiresSecretReentry(original.form.auth_method_id==='api_key');setStep(1);setError('');setDraftFailure(null);setShowInputIssues(false);setShowForm(true)
  }
  const discardParkedDraft=()=>{if(!removalOwner()||showForm||connectionSaveBusy(identity)||draftReadLock.current)return;forgetConnectionDraft(identity,testViewGeneration);setDraftFailure(null)}
  const nameLength = editorTextLength(form.label)
  const modelLength = editorTextLength(form.default_model_id)
  const keyTooLong = editorTextLength(key) > 32_768
  const nameIssue: MessageKey | null = !form.label.trim() ? 'editor.requiredField' : nameLength > 100 ? 'editor.nameLimit' : null
  const modelIssue: MessageKey | null = !validEditorModelId(form.default_model_id) ? 'editor.modelFormat' : null
  const keyIssue: MessageKey | null = keyTooLong ? 'connections.keyLengthLimit'
    : form.auth_method_id === 'api_key' && (editing === null || requiresSecretReentry) && !key.trim() ? 'editor.requiredField' : null
  useEffect(() => { setShowInputIssues(false); setInvalidFocus(null) }, [identity, showForm, editing, form.provider_id])
  const validStep = (stage: number) => {
    const root = formRef.current
    if (!root || !directoryOwnerCurrent() || connectionSaveBusy(identity) || saveLock.current) return false
    const local = stage === 2 && keyIssue ? keyInput.current
      : stage === 3 && nameIssue ? root.querySelector<HTMLInputElement>('[data-connection-field="name"]')
        : stage === 3 && modelIssue ? root.querySelector<HTMLInputElement>('[data-connection-field="model"] input[required]') : null
    const controls = root.querySelectorAll<HTMLInputElement | HTMLSelectElement>(`[data-step="${stage}"] input, [data-step="${stage}"] select`)
    const invalid = local || Array.from(controls).find(control => !control.disabled && !control.checkValidity())
    if (!invalid) return true
    setShowInputIssues(true)
    setInvalidFocus({ field: invalid, root, owner: identity, editing, form, stage, origin: document.activeElement, native: !local })
    setStep(stage)
    return false
  }
  const nextStep = () => {
    if (connectionSaveBusy(identity) || !selectedProvider || step === 2 && !formUsable || !validStep(step)) return
    if (step === 1 && editing === null) setForm((current) => ({ ...current,
      label: current.label || providerDisplayName(selectedProvider, t),
      default_model_id: current.default_model_id || selectedProvider.model_source.static_model_ids.find((id) => !selectedProvider.model_source.static_model_protocols?.[id] || selectedProvider.model_source.static_model_protocols[id] === current.protocol_id) || '' }))
    setStep((current) => Math.min(4, current + 1))
  }
  useEffect(() => {
    if (!showForm) return
    const request = invalidFocus && invalidFocus.stage === step && invalidFocus !== handledInvalidFocus.current ? invalidFocus : null
    const frame = requestAnimationFrame(() => {
      if (request) {
        handledInvalidFocus.current = request
        const field = request.field
        if (request.owner !== identity || !directoryOwnerCurrent() || request.editing !== editing
          || currentEditorForm.current !== request.form || formRef.current !== request.root || !field.isConnected
          || !request.root.contains(field) || field.disabled || connectionSaveBusy(identity) || saveLock.current
          || document.querySelector('[role="dialog"][aria-modal="true"]')
          || ![request.origin, document.body, editorRef.current].includes(document.activeElement)) return
        let parent = field.parentElement
        while (parent && parent !== request.root) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement }
        if (!field.getClientRects().length || !request.native && field.getAttribute('aria-invalid') !== 'true') return
        field.focus({ preventScroll: true }); field.scrollIntoView({ block: 'nearest' })
        if (request.native) field.reportValidity()
        return
      }
      if (!directoryOwnerCurrent()) return
      if (requiresSecretReentry && step === 2) keyInput.current?.focus()
      else editorRef.current?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [showForm, step, requiresSecretReentry, invalidFocus, identity, editing])
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (formIme.isSubmitBlocked(event)) return
    if (!directoryOwnerCurrent() || connectionSaveBusy(identity) || saveLock.current) return
    if (step < 4) { nextStep(); return }
    for (const stage of [1, 2, 3]) if (!validStep(stage)) return
    if (requiresSecretReentry && form.auth_method_id === 'api_key' && !key.trim()) {
      setError('connections.reenterBeforeSave')
      return
    }
    if (!formUsable) return
    if (saveFeedback?.identity === identity && saveFeedback.phase === 'unknown' && !window.confirm(t('connections.repeatUncertainSave'))) return
    if (!directoryOwnerCurrent() || saveLock.current) return
    const feedback: ConnectionSaveFeedback = { identity, attempt: nextConnectionSaveAttempt(identity), phase: 'saving',
      target: { id: editing, label: form.label, providerId: form.provider_id, modelId: form.default_model_id },
      hadKey: form.auth_method_id === 'api_key' && Boolean(key),
      requestDetails: { authMethodId: form.auth_method_id, protocolId: form.protocol_id, enabled: form.enabled ?? true,
        requestedOptions: options.filter(option => Boolean(form.capabilities?.[option])),
        keyHandling: form.auth_method_id === 'api_key' && key ? 'included'
          : form.auth_method_id === 'api_key' && editing !== null ? 'retain' : 'none' } }
    const data = { ...form, api_key: form.auth_method_id === 'api_key' ? key || undefined : undefined }
    if(!setSaveFeedback(feedback))return
    saveLock.current = identity; setError(''); setShowInputIssues(false)
    if(feedback.hadKey)setRequiresSecretReentry(true)
    setKey('')
    save.mutate({ data, editing, identity, baselineFingerprint: editingBaseline.current, feedback, generation:testViewGeneration, draftSession:editorSession.current })
  }

  const matchedIds = useMemo(() => {
    const matches = directoryMatcher(directoryQuery)
    return new Set(connections.filter(connection => matches([connection.label, connection.provider_id, connection.provider_kind,
      providerLabel(connection), connection.default_model_id, String(connection.id), `#${connection.id}`], connection.id)).map(connection => connection.id))
  }, [connections, directoryQuery, catalogQuery.data, t])
  const visibleIds = new Set(connections.filter(connection => {
    const view = disconnectViews[connection.id]
    return matchedIds.has(connection.id) || expanded[connection.id] || editing === connection.id || deleting === connection.id
      || disconnecting === connection.id || disconnectFocus?.id === connection.id
      || (view?.kind === 'operation' && isDisconnectPending(view.response))
      || connectionTestActive(identity, connection.id) || (unlock.isPending && unlock.variables?.id === connection.id)
      || (remove.isPending && remove.variables?.target.id === connection.id)
      || unlockStates[connection.id] === 'pending' || unlockStates[connection.id] === 'busy'
  }).map(connection => connection.id))
  const keptCount = visibleIds.size - matchedIds.size
  useEffect(() => {
    if (!pendingSavedFocus) return
    if (pendingSavedFocus.identity !== identity || showForm || localSetup || deleting !== null || disconnecting !== null
      || testInFlight || unlock.isPending || remove.isPending) { setPendingSavedFocus(null); return }
    if (directoryQuery || !connectionsQuery.isSuccess || isFetching || connectionsQuery.dataUpdatedAt === pendingSavedFocus.snapshotVersion
      || !visibleIds.has(pendingSavedFocus.id) || !directoryOwnerCurrent()) return
    focusSavedEditorCard('connection', pendingSavedFocus.id); setPendingSavedFocus(null)
  }, [pendingSavedFocus, identity, showForm, localSetup, deleting, disconnecting, testInFlight, unlock.isPending, remove.isPending,
    directoryQuery, connectionsQuery.isSuccess, connectionsQuery.dataUpdatedAt, isFetching, visibleIds])

  useEffect(()=>{
    if(!savedFind||savedFind.phase!=='moving'||savedFind.identity!==identity||savedFindLock.current!==savedFind.token||!removalOwner())return
    if(directoryQuery)return
    const card=document.getElementById(`connection-card-${savedFind.id}`)
    const blocked=!savedFind.origin?.isConnected||!!document.querySelector('[role="dialog"][aria-modal="true"]')||!(document.activeElement===savedFind.origin||document.activeElement===document.body)
    const visible=visibleIds.has(savedFind.id)&&!!card?.getClientRects().length
    savedFindLock.current=null
    if(!blocked&&visible){focusSavedEditorCard('connection',savedFind.id);setSavedFind({...savedFind,phase:'found'})}
    else setSavedFind({...savedFind,phase:'not_moved'})
  },[savedFind,identity,directoryQuery,visibleIds])
  const methodCopy: MethodCopy = {
    methodLegend: t('connections.authMethod'), protocol: t('connections.protocol'), details: t('connections.ui.advanced'),
    methodNames: { api_key: t('connections.methodApiKey'), local_none: t('connections.methodLocalNone'),
      oauth_browser: t('connections.methodOauthBrowser'), oauth_device: t('connections.methodOauthDevice'),
      native_chain: t('connections.methodNativeChain') },
    usageUnverified: t('connections.usageUnverified'), eligibilityUnknown: t('connections.eligibilityUnknown'),
    unavailable: t('connections.methodUnavailable'), baseUrl: t('connections.baseUrl'), allowLocalHttp: t('connections.localHttp'),
    fieldNames: { endpoint: t('connections.fieldEndpoint'), resource_endpoint: t('connections.fieldEndpoint'),
      deployment: t('connections.fieldDeployment'),
      api_version: t('connections.fieldApiVersion'), region: t('connections.fieldRegion'), project: t('connections.fieldProject'),
      project_id: t('connections.fieldProject'), profile_name: t('connections.fieldProfile'),
      location: t('connections.fieldLocation') }, otherField: t('connections.fieldOther'),
  }
  const reviewStep = (stage: number) => {
    if (!directoryOwnerCurrent() || connectionSaveBusy(identity) || saveLock.current) return
    setStep(stage)
  }
  const pendingRequestDetails = connectionSaveBusy(identity) && saveFeedback?.identity === identity && saveFeedback.phase === 'saving' ? saveFeedback.requestDetails : undefined
  const saveNotice = <ConnectionSaveNotice feedback={saveFeedback?.identity === identity ? saveFeedback : null} onInspect={inspectConnectionSave} findingSaved={findingSaved} onShowSaved={()=>{if(saveFeedback)void findSavedConnection(saveFeedback)}}
    onDismiss={() => { if (directoryOwnerCurrent()) setSaveFeedback(null) }} />
  return <AppShell>
    <PageHeader title={<span className="heading-with-icon"><Cable aria-hidden="true" size={21} /><span className="heading-with-icon__text">{t('connections.title')}</span></span>}
      backTo="/" width="medium" actions={<><button type="button" className="button button--primary" disabled={connectionSaveBusy(identity) || !catalogQuery.data} onClick={startNew}><Plus size={18} aria-hidden="true" />{t('connections.add')}</button><details className="room-header__menu connections-tools"><summary aria-label={t('connections.ui.more')} title={t('connections.ui.more')}><MoreHorizontal size={19} aria-hidden="true" /></summary><div className="room-header__menu-content connections-tools__content"><Link className="button button--ghost button--compact" to="/usage">{t('usage.title')}</Link><ReadinessCenter/><button type="button" className="button button--ghost button--compact" disabled={connectionSaveBusy(identity)} onClick={event => { const menu = event.currentTarget.closest('details'); if (menu) menu.open = false; startLocalSetup() }}><Server size={18} aria-hidden="true"/>{t('localModels.add')}</button></div></details></>} />
    <main ref={directoryRoot} className="page-frame page-frame--medium page-content stack connections-page connections-page--focused">
      {!showForm && saveNotice}
      {savedFind?.identity===identity&&<div className="notice notice--info stack stack--tight" style={{overflowWrap:'anywhere'}}><p role="status">{t(savedFind.phase==='reading'||savedFind.phase==='moving'?'common.loading':savedFind.phase==='found'?'connections.savedFind.found':savedFind.phase==='missing'?'connections.savedFind.missing':savedFind.phase==='not_moved'?'connections.savedFind.notMoved':'connections.loadFailure',{id:savedFind.id,name:savedFind.name})}</p><p className="metadata">#{savedFind.id} · {savedFind.name}</p><p className="field-hint">{t('connections.savedFind.scope')}</p></div>}
      {parkedDraft&&<section className="notice notice--info stack stack--tight" aria-label={t('connections.draft.title')}><h2 className="section-title">{t('connections.draft.title')}</h2><p>{parkedDraft.editing!==null?`#${parkedDraft.editing} · `:`${t('connections.draft.newTarget')} · `}{parkedDraft.form.label||t('connections.draft.untitled')} · {parkedDraft.form.provider_id} · {parkedDraft.form.default_model_id}</p><p className="field-hint">{t('connections.draft.help')}</p>{draftFailure&&<p role="alert">{t(`connections.draft.${draftFailure}`)}</p>}<ConnectionDraftReader key={`${identity}:${parkedDraft.id}:${parkedDraft.changedAt}`} identity={identity} draft={parkedDraft} provider={descriptorFor(parkedDraft.form.provider_id)} copy={methodCopy} canUse={removalOwner}/><div className="page-actions"><button type="button" className="button button--secondary" disabled={showForm||connectionSaveBusy(identity)||draftReading||!catalogQuery.data} onClick={()=>void resumeEditorDraft()}>{t(draftReading?'common.loading':'connections.draft.resume')}</button><button type="button" className="button button--secondary" disabled={showForm||connectionSaveBusy(identity)||draftReading||!catalogQuery.data} onClick={useDraftAsNew}>{t('connections.draft.asNew')}</button><button type="button" className="text-link" disabled={showForm||connectionSaveBusy(identity)||draftReading} onClick={discardParkedDraft}>{t('connections.draft.discard')}</button></div><p className="field-hint">{t('connections.draft.asNewHelp')}</p></section>}
      {localSetupSession === identity && <div hidden={!localSetup} style={{ display: localSetup ? undefined : 'none' }}><LocalModelSetup key={identity} identity={identity} visible={localSetup} onClose={()=>setLocalSetup(false)} onSaved={c=>{if(!directoryOwnerCurrent())return;void cache.invalidateQueries({queryKey:['connections',identity]});setExpanded(v=>({...v,[c.id]:true}));setLoadedDetails(v=>({...v,[c.id]:true}))}}/></div>}
      {!localSetup && (localSetupSession === identity || localSetupHasWork(localWorkspace)) && <div className="notice notice--info stack stack--tight">
        <p>{t('localModels.retainedSetup')}</p><button type="button" className="button button--secondary button--compact" disabled={connectionSaveBusy(identity)} onClick={startLocalSetup}>{t('localModels.reopenSetup')}</button>
      </div>}
      {catalogQuery.isError && !showForm && <p role="alert" className="notice notice--danger">{t('connections.catalogFailure')} <button type="button" className="text-link" onClick={() => void catalogQuery.refetch()}>{t('common.retry')}</button></p>}
      {firstUse && <section className="notice notice--info first-success-connection" aria-label={t('connections.firstUseTitle')}>
        <h2 className="section-title">{t('connections.firstUseTitle')}</h2><p>{t('connections.firstUseHelp')}</p>
      </section>}
      {showForm && <section ref={editorRef} tabIndex={-1} className="surface connection-editor" aria-label={t('connections.add')}>
        {editorDirty && <p role="status" className="notice notice--info">{t('connections.unsavedInput')}</p>}
        <header className="split"><h2 className="section-title">{editing === null ? t('connections.add') : `${t('common.edit')} #${editing}`}</h2>
          <button type="button" className="icon-button" aria-label={t('common.close')} disabled={connectionSaveBusy(identity)} onClick={closeEditor}><X size={18} /></button></header>
        <ol className="connection-steps" aria-label={t('connections.steps')}>
          {[t('connections.provider'), t('connections.authMethod'), t('connections.stepModel'), t('connections.stepConfirm')].map((label, index) => <li key={index} aria-current={step === index + 1 ? 'step' : undefined}><span>{index + 1}</span>{label}</li>)}
        </ol>
        <form {...formIme.handlers} ref={formRef} noValidate onSubmit={submit} className="form-grid">
          {showInputIssues && (nameIssue || modelIssue || keyIssue) && <p role="alert" className="notice notice--warning">{t('editor.inputIssues')}</p>}
          <fieldset data-step="1" hidden={step !== 1} disabled={connectionSaveBusy(identity)} className="connection-step-fields"><legend className="sr-only">{t('connections.provider')}</legend>
          {catalogQuery.data ? <ProviderCatalog catalog={catalogQuery.data} value={form.provider_id} disabled={editing !== null}
            label={t('connections.provider')} translate={t} onChange={selectProvider} />
            : <p role={catalogQuery.isError ? 'alert' : 'status'} className={catalogQuery.isError ? 'notice notice--danger' : 'section-copy'}>
              {t(catalogQuery.isError ? 'connections.catalogFailure' : 'connections.catalogLoading')}
              {catalogQuery.isError && <button type="button" className="text-link" onClick={() => void catalogQuery.refetch()}>{t('common.retry')}</button>}
            </p>}
          {editing !== null && <span className="field-hint">{t('connections.providerLocked')}</span>}

          </fieldset>
          <fieldset data-step="2" hidden={step !== 2} disabled={connectionSaveBusy(identity)} className="connection-step-fields"><legend className="sr-only">{t('connections.authMethod')}</legend>
          {selectedProvider && <MethodFields provider={selectedProvider} methodId={form.auth_method_id} protocolId={form.protocol_id}
            values={form.fields} focusVersion={methodFocusVersion} copy={methodCopy} onMethodChange={changeMethod}
            onProtocolChange={(protocol: ProtocolId) => changeForm({ ...form, protocol_id: protocol })}
            onValuesChange={(fields) => changeForm({ ...form, fields })} />}

          {formUsable && form.auth_method_id === 'api_key' && <label className="field">{requiresSecretReentry ? t('connections.reenterKey') : <>{t('connections.apiKey')} {editing !== null && <span className="field-hint">{t('connections.keepKey')}</span>}</>}
            <input ref={keyInput} type="password" autoComplete="off" aria-invalid={Boolean(keyIssue && (showInputIssues || keyTooLong))} aria-describedby={keyIssue && (showInputIssues || keyTooLong) ? "connection-key-help" : undefined} required={editing === null || requiresSecretReentry} value={key}
              onChange={(e) => setKey(e.target.value)} />
            {keyIssue && (showInputIssues || keyTooLong) && <span id="connection-key-help" className="metadata metadata--danger">{t(keyIssue)}</span>}</label>}

          </fieldset>
          <fieldset data-step="3" hidden={step !== 3} disabled={connectionSaveBusy(identity)} className="connection-step-fields"><legend className="sr-only">{t('connections.stepModel')}</legend>
          <label className="field">{t('connections.name')}<input data-connection-field="name" required aria-invalid={Boolean(nameIssue && (showInputIssues || nameLength > 100))} aria-describedby="connection-name-help" value={form.label}
            onChange={(e) => changeForm({ ...form, label: e.target.value })} /><span id="connection-name-help" className={nameIssue && (showInputIssues || nameLength > 100) ? 'metadata metadata--danger' : 'sr-only'}>{t(nameIssue && (showInputIssues || nameLength > 100) ? nameIssue : 'editor.nameLimit', { count: nameLength })}</span></label>
          <div data-connection-field="model"><ModelPicker invalid={Boolean(modelIssue && (showInputIssues || modelLength > 200))} describedBy="connection-model-help" searchScope={`${identity}:${editing ?? 'new'}:${form.provider_id}:${form.auth_method_id}:${form.protocol_id}`} compact catalog={selectedProvider ? { observed_at: null, stale: false, truncated: false,
              models: selectedProvider.model_source.static_model_ids.map((id) => ({ id, label: id, source: 'configured' as const })) } : undefined}
            value={form.default_model_id} onChange={(default_model_id) => changeForm({
              ...form, default_model_id,
              protocol_id: selectedProvider?.model_source.static_model_protocols?.[default_model_id] || form.protocol_id,
            })}
            copy={{ knownModel: t('connections.knownModel'), chooseKnownModel: t('connections.chooseKnownModel'), manualModel: t('connections.exactModel'),
              refresh: t('connections.refreshModels'), refreshing: t('connections.refreshingModels'), stale: t('connections.modelCacheStale'),
              truncated: t('connections.modelCacheTruncated'), empty: t('connections.modelCacheEmpty'),
              loadFailure: t('connections.modelLoadFailure'), refreshFailure: t('connections.modelRefreshFailure'),
              configurationChanged: t('connections.modelConfigurationChanged'), retry: t('common.retry'),
              retryRefresh: t('connections.retryModelRefresh') }} /><p id="connection-model-help" className={modelIssue && (showInputIssues || modelLength > 200) ? 'metadata metadata--danger' : 'sr-only'}>{t(modelIssue && (showInputIssues || modelLength > 200) ? modelIssue : 'editor.modelLimit', { count: modelLength })}</p></div>
          <details className="compact-details"><summary>{t('common.details')}</summary><div className="form-grid">
          {form.provider_id === 'chatgpt' && <p role="note" className="notice notice--info">{t('connections.chatgptScope')}</p>}
          <fieldset className="stack stack--tight"><legend className="section-title">{t('connections.capabilities')}</legend>
            <span className="field-hint">{t('connections.capabilitiesHelp')}</span>
            {options.map((option) => <label key={option} className="check-row"><input type="checkbox"
              checked={Boolean(form.capabilities?.[option])} disabled={form.provider_id === 'chatgpt' && option !== 'vision' && option !== 'tools'}
              onChange={(e) => changeForm({ ...form, capabilities: { ...form.capabilities, [option]: e.target.checked } })} /> {t(capabilityLabels[option])}</label>)}
          </fieldset>
          </div></details>

          <label className="check-row"><input type="checkbox" checked={form.enabled} onChange={(e) => changeForm({ ...form, enabled: e.target.checked })} /> {t('connections.use')}</label>
          </fieldset>
          {step === 4 && <div className="stack stack--tight">
            <h3 className="section-title">{t('connections.stepConfirm')}</h3>
            <p className="field-hint">{t(pendingRequestDetails ? 'connections.reviewPendingRequest' : 'connections.reviewDraft')}</p>
            <dl className="connection-confirm"><dt>{t('connections.provider')}</dt><dd>{selectedProvider && providerDisplayName(selectedProvider, t)}</dd><dt>{t('connections.authMethod')}</dt><dd>{t(authMethodLabel(form.auth_method_id))}</dd>
              <dt>{t('connections.name')}</dt><dd>{form.label}</dd><dt>{t('connections.defaultModel')}</dt><dd>{form.default_model_id}</dd><dt>{t('connections.use')}</dt><dd>{t(form.enabled ? 'connections.enabled' : 'connections.disabled')}</dd></dl>
            {selectedProvider && <MethodFieldsReview provider={selectedProvider} protocolId={form.protocol_id} values={form.fields} copy={methodCopy} />}
            <dl className="connection-confirm">
              <dt>{t('connections.capabilities')}</dt><dd>{options.filter(option => form.capabilities?.[option]).map(option => t(capabilityLabels[option])).join(', ') || t('connections.reviewRequestsNone')}</dd>
              <dt>{t('connections.reviewCredential')}</dt><dd>{t(pendingRequestDetails ? connectionSentKeyLabel(pendingRequestDetails.keyHandling) : form.auth_method_id !== 'api_key' ? 'connections.reviewOtherAuth'
                : key ? 'connections.reviewKeySupplied' : editing !== null && !requiresSecretReentry ? 'connections.reviewKeyRetained' : 'connections.reviewKeyMissing')}</dd>
            </dl>
            <div className="page-actions">
              {[{ stage: 1, label: t('connections.provider') }, { stage: 2, label: t('connections.authMethod') }, { stage: 3, label: t('connections.stepModel') }].map(item =>
                <button key={item.stage} type="button" className="button button--secondary button--compact" disabled={connectionSaveBusy(identity)} onClick={() => reviewStep(item.stage)}>{t('common.edit')} · {item.label}</button>)}
            </div>
            <p className="notice notice--info">{t('connections.saveThenCheck')}</p>
          </div>}
          <div className="page-actions">
            {step > 1 && <button type="button" className="button button--secondary" disabled={connectionSaveBusy(identity)} onClick={() => setStep((current) => current - 1)}>{t('connections.backStep')}</button>}
            {step < 4 ? <button type="button" className="button button--primary" disabled={connectionSaveBusy(identity) || !selectedProvider || step === 2 && !formUsable} onClick={nextStep}>{t('connections.nextStep')}</button>
              : <button type="submit" className="button button--primary" disabled={connectionSaveBusy(identity) || !formUsable}><Save aria-hidden="true" size={17} />{connectionSaveBusy(identity) ? t('common.saving') : t('common.save')}</button>}
            <button type="button" className="button button--secondary" disabled={connectionSaveBusy(identity)||!editorDirty} onClick={parkEditor}>{t('connections.draft.park')}</button>
            <button type="button" className="button button--ghost" disabled={connectionSaveBusy(identity)} onClick={closeEditor}>{t('common.cancel')}</button>
          </div>
          {error && <p role="alert" className="notice notice--danger">{t(error)}</p>}
          {saveNotice}
        </form>
      </section>}

      <section className="stack connections-list" aria-label={t('connections.savedList')}>
        <h2 className="section-title">{t('connections.savedList')}</h2>
        <DirectorySearch compact exactId ref={directoryFinder} value={directoryQuery} onChange={changeDirectoryQuery} hint={t('directory.connectionHint')}
          shown={visibleIds.size} total={connections.length} loading={isLoading} known={connectionsQuery.data !== undefined} controls="connection-directory-cards" />
        {directoryQuery.trim() && keptCount > 0 && <p role="status" className="notice notice--info">{t('directory.keptConnections', { count: keptCount })}</p>}
        {isLoading && <p role="status" className="section-copy">{t('connections.loading')}</p>}
        {isError && <p role="alert" className="notice notice--danger inline-feedback">{t('connections.loadFailure')} <button type="button" className="danger-link" disabled={isFetching} onClick={() => void connectionsQuery.refetch()}><RotateCcw aria-hidden="true" size={15} />{isFetching ? t('connections.retrying') : t('common.retry')}</button></p>}
        {!isLoading && !isError && connections.length === 0 && <p className="section-copy">{t('connections.empty')}</p>}
        <details className="compact-details connections-guide"><summary>{t('connections.ui.guide')}</summary><p className="field-hint">{t('connections.workflow')}</p><p className="field-hint">{t('connections.agentNext.scope')}</p></details>
        <div id="connection-directory-cards" className="stack">
        {!isLoading && !isError && connections.length > 0 && matchedIds.size === 0 && <div className="notice notice--info stack stack--tight">
          <p>{t('directory.noMatches')}</p><button type="button" className="button button--secondary button--compact" onClick={clearDirectorySearch}>{t('directory.clear')}</button>
        </div>}
        {connections.map((connection) => {
          const testWorkspace = readConnectionTests(identity, connection.id)
          const testedModel = effectiveTestModel(connection, testWorkspace)
          const preparedModel = typeof testedModel==='string'?testedModel.trim():''
          const canPrepareAgent = connection.enabled&&validEditorModelId(preparedModel)
          const preparedDifferent = preparedModel!==(typeof connection.default_model_id==='string'?connection.default_model_id.trim():'')
          const authHint = authSessionHint(readAuthSession(identity, connection.id))
          const disconnectView = disconnectViews[connection.id]
          const disconnectActive = disconnectView?.kind === 'operation' && isDisconnectPending(disconnectView.response)
          return <article key={connection.id} id={`connection-card-${connection.id}`} tabIndex={-1} hidden={!visibleIds.has(connection.id)} style={{ display: visibleIds.has(connection.id) ? undefined : 'none' }} className="surface stack stack--tight connection-card">
          <h3 className="section-title heading-with-icon connection-card__heading">{connection.is_local ? <Server aria-hidden="true" size={18} /> : <Cable aria-hidden="true" size={18} />}<span className="connection-card__label">{connection.label}</span>{saveFeedback?.identity === identity && saveFeedback.phase === 'success' && saveFeedback.target.id === connection.id && <span className="metadata"> · {t('editor.justSaved')}</span>}</h3>
          <p className="metadata connection-card__binding"><span className="technical" title={t('connections.defaultModel')}>{connection.default_model_id}</span></p>
          <div className="connection-statuses">
            <span className={connection.enabled ? 'status-text status-text--success' : 'status-text status-text--warning'}><CheckCircle2 aria-hidden="true" size={15} />{t(connection.enabled ? 'connections.enabled' : 'connections.disabled')}</span>
            {connection.is_local && <span className="status-text"><Server aria-hidden="true" size={15} />{t('localModels.ui.onDevice')}</span>}
            {connection.auth_required && !connection.has_secret && <span className="status-text status-text--error"><KeyRound aria-hidden="true" size={15} />{t('connections.keyMissing')}</span>}
          </div>
          {authHint && <p className="field-hint">{t(authHint)}</p>}
          {canPrepareAgent&&preparedDifferent&&<p className="metadata" style={{overflowWrap:'anywhere'}}>{t('connections.agentNext.selected',{model:preparedModel})}</p>}
          {!canPrepareAgent&&<p className="field-hint">{t(connection.enabled?'connections.agentNext.modelMissing':'connections.agentNext.disabled')}</p>}
          <div className="connection-card__footer">
          <details className="connection-checks compact-details" open={expanded[connection.id] || undefined}
            onToggle={(event) => { const open = event.currentTarget.open; if (open) setLoadedDetails((current) => current[connection.id] ? current : { ...current, [connection.id]: true }); setExpanded((current) => current[connection.id] === open ? current : { ...current, [connection.id]: open }) }}>
            <summary>{t('connections.configure')}</summary>
            {loadedDetails[connection.id] && <div className="stack"><details className="compact-details"><summary>{t('connections.ui.checkHelp')}</summary><p className="field-hint">{t('connections.distinctChecks')}</p></details>
          <SavedModelPicker connection={connection} identity={identity} value={testWorkspace.model ?? connection.default_model_id}
            onChange={(modelId) => {
              if (directoryOwnerCurrent()) selectConnectionTestModel(identity, connection.id, testViewGeneration, modelId)
            }} />
          <ModelFeatures key={`features:${identity}:${connection.id}:${testedModel}:${connection.config_revision??0}:${connection.auth_grant_revision??0}`} connectionId={connection.id} modelId={testedModel} identity={identity} revision={`${connection.config_revision??0}:${connection.auth_grant_revision??0}`} enabled={connection.enabled}/>
          <SavedAuthPanel visible={!!expanded[connection.id] && visibleIds.has(connection.id)} connection={connection} provider={descriptorFor(connection.provider_id || connection.provider_kind)} identity={identity} />
          <ConnectionTestControls key={`test-controls:${identity}:${connection.id}`} connection={connection} identity={identity} visible={!!expanded[connection.id] && visibleIds.has(connection.id)} />            </div>}
          </details>
          <div className="page-actions connection-card__primary-actions">{canPrepareAgent&&<Link className="button button--secondary button--compact" to="/agents" state={agentCreationState(identity,testViewGeneration,connection.id,preparedModel)} aria-label={`${t('agents.fromConnection.prepare')} · ${connection.label} · ${preparedModel}`} onClick={event=>{if(!directoryOwnerCurrent()||directoryViewGeneration()!==testViewGeneration)event.preventDefault()}}>{t('agents.fromConnection.prepare')}</Link>}<button className="button button--ghost button--compact" disabled={connectionSaveBusy(identity)} onClick={() => edit(connection)}><Pencil aria-hidden="true" size={15} />{t('common.edit')}</button></div>
          <details className="compact-details connection-card__management"><summary>{t('connections.ui.manage')}</summary><div className="stack stack--tight"><p className="metadata">#{connection.id} · {providerLabel(connection)} · {t(authMethodLabel(legacyMethod(connection)))} · {t(!connection.auth_required ? 'connections.keyNotRequired' : connection.has_secret ? 'connections.keySaved' : 'connections.keyMissing')}</p><div className="page-actions"><Link className="text-link" to={agentConnectionHref(connection.id)} onClick={event=>{if(!directoryOwnerCurrent())event.preventDefault()}}>{t('agents.connectionFilter.open')}</Link><button type="button" className="text-link" disabled={remove.isPending} onClick={()=>openInactive(connection)}>{t('inactiveRefs.title')}</button>
            {connection.auth_required && connection.has_secret && <button className="button button--secondary button--compact" onClick={() => unlock.mutate({ id: connection.id, identity })} disabled={unlock.isPending}><KeyRound aria-hidden="true" size={15} />{t('connections.unlock')}</button>}
            <button ref={(element) => { disconnectButtonRefs.current[connection.id] = element }} className="button button--secondary button--compact"
              disabled={disconnectBusy || disconnectActive}
              onClick={() => { setDisconnecting(connection.id); setRevokeRemote(false); setDisconnectStartError(false) }}>
              <Unplug aria-hidden="true" size={15} />{t('connections.disconnect')}
            </button>
            <button className="button button--danger button--compact" disabled={remove.isPending}
              onClick={() => openRemoval(connection)}><Trash2 aria-hidden="true" size={15} />{t('common.delete')}</button></div>
          {connection.capabilities.verified.models && <details><summary className="metadata">{t('connections.verifiedModels')}</summary><p className="technical">{Object.entries(connection.capabilities.verified.models).map(([model, verified]) => `${model} (${options.filter((option) => verified[option]).map((option) => t(capabilityLabels[option])).join(', ') || t('connections.noVerifiedOptions')})`).join('; ')}</p></details>}
          </div></details></div>
          {disconnecting === connection.id && <div role="dialog" aria-label={t('connections.disconnectDialog', { name: connection.label })} className="notice notice--warning stack stack--tight">
            <p>{t('connections.disconnectConfirm')}</p>
            <label className="check-row"><input ref={disconnectDialogFocus} type="checkbox" checked={revokeRemote}
              disabled={disconnectBusy} onChange={(event) => setRevokeRemote(event.target.checked)} /> {t('connections.disconnectRemote')}</label>
            <div className="page-actions">
              <button type="button" className="button button--secondary" disabled={disconnectBusy} onClick={() => closeDisconnectDialog(connection.id)}>{t('common.cancel')}</button>
              <button type="button" className="button button--danger" disabled={disconnectBusy} onClick={() => void startDisconnect(connection.id)}>{t('connections.disconnect')}</button>
            </div>
            {disconnectBusy && <p role="status">{t('connections.disconnecting')}</p>}
            {disconnectStartError && <p role="alert">{t('connections.disconnectFailure')}</p>}
          </div>}
          <ConnectionTestStatus key={`test-status:${identity}:${connection.id}`} connection={connection} identity={identity} />
          {unlockStates[connection.id] && <p role="status" className={unlockStates[connection.id] === 'ready' ? 'status-text status-text--success' : 'status-text status-text--warning'}>{t(unlockStates[connection.id] === 'ready' ? 'connections.credentialReady' : unlockStates[connection.id] === 'pending' ? 'connections.keychainWaiting' : unlockStates[connection.id] === 'busy' ? 'connections.keychainBusy' : 'connections.unlockFailure')}</p>}
          {disconnectView?.kind === 'immediate' && <div className="notice notice--success stack stack--tight">
            <p role="status">{t('connections.disconnectSucceeded')}</p>
            <p>{t(disconnectRevokeLabels[disconnectView.result.revoke_state])}</p>
          </div>}
          {disconnectView?.kind === 'operation' && isDisconnectPending(disconnectView.response) && <div className="notice notice--info stack stack--tight">
            <p ref={(element) => { disconnectStatusRefs.current[connection.id] = element }} role="status"
              tabIndex={disconnectView.response.state === 'finalizing' ? -1 : undefined}>{t(disconnectView.response.state === 'pending' ? 'connections.disconnectPending' : 'connections.disconnectFinalizing')}</p>
            {disconnectView.response.state === 'pending' && <button ref={(element) => { disconnectCancelRefs.current[connection.id] = element }} type="button" className="button button--secondary button--compact"
              disabled={disconnectView.cancelling} onClick={() => void cancelDisconnect(connection.id, disconnectView.response as DisconnectPending)}>
              {t(disconnectView.cancelling ? 'connections.disconnectCancelling' : 'connections.disconnectCancel')}
            </button>}
            {disconnectView.statusError && <p role="alert">{t('connections.disconnectStatusFailure')} <button type="button" className="danger-link"
              onClick={() => retryDisconnectStatus(connection.id, disconnectView.response as DisconnectPending)}>{t('common.retry')}</button></p>}
          </div>}
          {disconnectView?.kind === 'operation' && !isDisconnectPending(disconnectView.response) && disconnectView.response.state === 'succeeded' && <div className="notice notice--success stack stack--tight">
            <p role="status">{t('connections.disconnectSucceeded')}</p>
            <p>{t(disconnectRevokeLabels[disconnectView.response.result.revoke_state])}</p>
          </div>}
          {disconnectView?.kind === 'operation' && !isDisconnectPending(disconnectView.response) && disconnectView.response.state !== 'succeeded' && <div className="notice notice--warning stack stack--tight">
            <p role="status">{t(disconnectView.response.state === 'cancelled' ? 'connections.disconnectCancelled' : 'connections.disconnectRestored')}</p>
            <p>{t(disconnectCredentialLabels[disconnectView.response.result.credential_state])}</p>
          </div>}
        </article>})}
        </div>
      </section>
    <InactiveReferenceHistory identity={identity}/>
    <ConnectionRemovalHistory identity={identity}/>
    <ConnectionSaveHistory identity={identity} findingSaved={findingSaved} onShowSaved={feedback=>void findSavedConnection(feedback)}/>
    </main>
    {removing&&deleting!==null&&<ConnectionRemoveDialog target={removing.target} current={connections.find(row=>row.id===deleting)}
      ready={connectionsQuery.isSuccess&&!connectionsQuery.isFetching&&!cache.getQueryState(['connections',identity])?.isInvalidated}
      pending={remove.isPending||!!removeLock.current} blocked={removalBlocked(deleting)} reading={removalRead?.target===removing.target&&removalRead.phase==='reading'}
      readFailed={removalRead?.target===removing.target&&removalRead.phase==='error'} failure={removalFailure}
      canAdopt={connectionsQuery.isSuccess&&!connectionsQuery.isFetching&&!cache.getQueryState(['connections',identity])?.isInvalidated&&!!connections.find(row=>row.id===deleting)&&(!removalFailure||removalRead?.target===removing.target&&removalRead.phase==='ready')&&(!sameConnectionRemovalTarget(removing.target,connections.find(row=>row.id===deleting))||!!removalFailure&&removalRead?.target===removing.target&&removalRead.phase==='ready')}
      onInactive={()=>{const row=connections.find(value=>value.id===deleting);if(row)openInactive(row)}} canUse={removalOwner} onClose={closeRemoval} onRead={()=>void readRemoval()} onAdopt={adoptRemoval} onConfirm={confirmRemoval}/>}
    {inactiveTarget&&inactiveTarget.identity===identity&&inactiveTarget.generation===testViewGeneration&&<InactiveAgentReferences key={`${identity}:${inactiveTarget.id}`} connection={inactiveTarget} identity={identity} canUse={()=>removalOwner()&&inactiveTarget.identity===identity&&inactiveTarget.generation===testViewGeneration} onClose={()=>{if(removalOwner())setInactiveTarget(null)}}/>}
  </AppShell>
}
