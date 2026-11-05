import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CheckCircle2, X } from 'lucide-react'
import { readinessApi, type ConnectionReadiness, type CredentialObservation, type ReadinessOutcome } from '../api/readiness'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useDirectoryValue } from '../lib/useDirectoryView'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import ServerTime from './ServerTime'
import ReadinessHistoryList from './ReadinessHistoryList'
import ReadinessBindingList from './ReadinessBindingList'
import { useDialogFocus } from '../lib/useDialogFocus'

const observationKeys: Record<CredentialObservation, MessageKey> = {
  not_required: 'readiness.credential.notRequired', unknown: 'readiness.credential.unknown', verified: 'readiness.credential.verified',
  rejected: 'readiness.credential.rejected', missing: 'readiness.credential.missing', locked: 'readiness.credential.locked', unavailable: 'readiness.credential.unavailable',
}
const outcomeKeys: Record<ReadinessOutcome, MessageKey> = {
  connected: 'connections.testSucceeded', missing_secret: 'connections.testMissingSecret', missing_model: 'connections.testFailed',
  secret_store_unavailable: 'connections.testCredentialLocked', invalid_credential: 'connections.testInvalidCredential', unreachable: 'connections.testUnreachable',
  redirect_refused: 'connections.testUnreachable', provider_error: 'connections.testFailed', rate_limited: 'connections.testRateLimited',
  quota_exhausted: 'connections.testQuotaExhausted', invalid_response: 'connections.testFailed', connection_disabled: 'connections.testDisabled', credential_locked: 'connections.testCredentialLocked',
  configuration_changed: 'connections.testConfigurationChanged',
}
function currentOwner(identity: string, generation: number) {
  const state = useAuthStore.getState()
  return !!state.token && `${state.user?.id || 0}:${state.epoch}` === identity && generation === directoryViewGeneration()
}

function ReadinessCard({ item, identity, visible, onViewAgents }: { item: ConnectionReadiness; identity: string; visible: boolean; onViewAgents: () => void }) {
  const [historyOpen, setHistoryOpen] = useState(false), generation = useRef(directoryViewGeneration()).current
  const { t, formatDateTime } = useI18n()
  const valid = () => currentOwner(identity, generation)
  const history = useQuery({ queryKey: ['readiness-history', identity, item.connection_id], enabled: historyOpen,
    retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: async () => {
      if (!valid()) throw new Error('Owner changed')
      const value = await readinessApi.history(item.connection_id)
      if (!valid()) throw new Error('Owner changed')
      if (value.connection_id !== item.connection_id || !Array.isArray(value.items)) throw new Error('History target changed')
      return value
    },
  })
  const basisMatches = !history.data || history.data.current_config_fingerprint === item.config_fingerprint
  const configured = !item.auth_required ? t('connections.keyNotRequired') : item.has_secret ? t('connections.keySaved') : t('connections.keyMissing')
  return <article className="readiness-card" hidden={!visible} style={{ display: visible ? undefined : 'none' }}>
    <header><div><h3>{item.label}</h3><p>#{item.connection_id} · {configured}</p></div><span className={`readiness-state readiness-state--${item.declared_state}`}>{t(item.declared_state === 'declared' ? 'readiness.configured' : item.declared_state === 'disabled' ? 'connections.disabled' : 'connections.keyMissing')}</span></header>
    <p className="readiness-observation"><CheckCircle2 size={15} aria-hidden="true" />{t(observationKeys[item.credential_observation])}</p>
    {item.latest_test ? <p className="metadata">{t(outcomeKeys[item.latest_test.code])} · <ServerTime value={item.latest_test.tested_at}/>{!item.latest_test.config_current && ` · ${t('readiness.previousConfig')}`}</p>
      : <p className="metadata">{t('readiness.neverTested')}</p>}
    <p>{t('readiness.bindingSummary', { ready: item.binding_counts.active_bound, missing: item.binding_counts.active_missing_model, inactive: item.binding_counts.inactive })}</p>
    <details className="readiness-details"><summary>{t('readiness.agentBindings')}</summary><ReadinessBindingList key={`${identity}:${item.connection_id}`} connectionId={item.connection_id} items={item.agent_bindings}
      canUse={() => valid() && visible} onViewAgents={onViewAgents}/>{item.agent_bindings_truncated && <p className="metadata">{t('readiness.bindingsTruncated')}</p>}</details>
    <details className="readiness-details" onToggle={event => { if (event.target === event.currentTarget) setHistoryOpen(event.currentTarget.open) }}><summary>{t('readiness.history')}</summary>
      <div className="stack stack--tight">
        <p className="field-hint">{t('readiness.browse.historyScope')}</p>
        <button type="button" className="button button--secondary button--compact" disabled={history.isFetching} onClick={() => { if (valid()) void history.refetch({ cancelRefetch: false }) }}>{t(history.isFetching ? 'common.loading' : 'readiness.browse.reloadHistory')}</button>
        {history.isFetching && <p role="status">{t('common.loading')}</p>}
        {history.isError && <p role="alert" className="notice notice--warning">{t('readiness.historyFailure')}</p>}
        {history.data && <>
          {history.dataUpdatedAt > 0 && <p className="metadata">{t('readiness.browse.lastReceived', { time: formatDateTime(history.dataUpdatedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
          {(history.isError || history.isFetching) && <p className="notice notice--info">{t('readiness.browse.retained')}</p>}
          {!basisMatches && <p className="notice notice--warning">{t('readiness.browse.basisChanged')}</p>}
          {history.data.items.length ? <ReadinessHistoryList key={`${identity}:${item.connection_id}`} items={history.data.items} basisMatches={basisMatches}
            canUse={() => valid() && visible && historyOpen} outcomeLabel={code => t(outcomeKeys[code])}/> : <p>{t('readiness.noHistory')}</p>}
        </>}
      </div>
    </details>
    <details className="readiness-details"><summary>{t('common.technicalDetails')}</summary><dl>
      <div><dt>{t('connections.exactModel')}</dt><dd>{item.public_config.default_model_id || t('readiness.noModel')}</dd></div>
      <div><dt>{t('readiness.fingerprint')}</dt><dd><code>{item.config_fingerprint}</code></dd></div>
    </dl></details>
  </article>
}

function ReadinessDialog({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLElement>(null), { t } = useI18n()
  useDialogFocus(dialog, onClose, { restoreFocus: false })
  return <section ref={dialog} role="dialog" aria-modal="true" aria-label={t('readiness.title')} className="readiness-dialog">{children}</section>
}

export default function ReadinessCenter() {
  const identity = useAuthStore(state => `${state.user?.id || 0}:${state.epoch}`)
  return <ScopedReadinessCenter key={`${identity}:${directoryViewGeneration()}`} identity={identity} />
}
function ScopedReadinessCenter({ identity }: { identity: string }) {
  const [open, setOpen] = useState(false), generation = useRef(directoryViewGeneration()).current
  const [search, setSearch] = useDirectoryValue<string>(identity, 'readiness.search', '')
  const trigger = useRef<HTMLButtonElement>(null), opened = useRef(false), mounted = useRef(false), closeFrame = useRef<number | null>(null)
  const listId = useId(), { t, formatDateTime } = useI18n()
  const valid = () => currentOwner(identity, generation)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; if (closeFrame.current !== null) cancelAnimationFrame(closeFrame.current) } }, [])
  const query = useQuery({ queryKey: ['readiness', identity], enabled: open, retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: async () => {
      if (!valid()) throw new Error('Owner changed')
      const value = await readinessApi.get()
      if (!valid()) throw new Error('Owner changed')
      if (!Array.isArray(value.connections)) throw new Error('Invalid readiness overview')
      return value
    },
  })
  const close = () => {
    opened.current = false; setOpen(false)
    if (closeFrame.current !== null) cancelAnimationFrame(closeFrame.current)
    closeFrame.current = requestAnimationFrame(() => {
      closeFrame.current = null
      const target = trigger.current
      const modal = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).some(node => node.getClientRects().length > 0 && !node.closest('[hidden], [inert], [aria-hidden="true"]'))
      if (mounted.current && valid() && !opened.current && !modal && document.visibilityState === 'visible' && document.hasFocus()
        && target?.isConnected && target.getClientRects().length > 0 && !target.matches(':disabled') && !target.closest('[hidden], [inert], [aria-hidden="true"]')) target.focus({ preventScroll: true })
    })
  }
  const connections = query.data?.connections || [], matches = directoryMatcher(search)
  const shown = connections.filter(item => matches([String(item.connection_id), `#${item.connection_id}`, item.label, item.provider_kind, item.public_config.default_model_id], item.connection_id))
  const shownIds = new Set(shown.map(item => item.connection_id))
  return <><button ref={trigger} type="button" className="button button--secondary" onClick={() => { if (!valid()) return; opened.current = true; setOpen(true) }}><CheckCircle2 size={18} aria-hidden="true" />{t('readiness.open')}</button>
    {open && <div className="modal-backdrop readiness-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) close() }}><ReadinessDialog onClose={close}><header><div><h2>{t('readiness.title')}</h2><p>{t('readiness.help')}</p></div><button autoFocus type="button" className="icon-button" aria-label={t('common.close')} onClick={close}><X size={18}/></button></header>
      <div className="readiness-dialog__body stack">
        <p className="field-hint">{t('readiness.browse.scope')}</p>
        <button type="button" className="button button--secondary" disabled={query.isFetching} onClick={() => { if (valid()) void query.refetch({ cancelRefetch: false }) }}>{t(query.isFetching ? 'common.loading' : 'readiness.browse.reload')}</button>
        {query.isFetching && <p role="status">{t('common.loading')}</p>}
        {query.isError && <p role="alert" className="notice notice--warning">{t('readiness.loadFailure')}</p>}
        {query.data && <>
          {query.dataUpdatedAt > 0 && <p className="metadata">{t('readiness.browse.lastReceived', { time: formatDateTime(query.dataUpdatedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
          {(query.isError || query.isFetching) && <p className="notice notice--info">{t('readiness.browse.retained')}</p>}
        </>}
        <DirectorySearch exactId value={search} onChange={value => { if (valid()) setSearch(value) }} label={t('readiness.browse.search')} hint={t('readiness.browse.searchHint')}
          shown={shown.length} total={connections.length} known={query.data !== undefined} loading={query.isFetching} controls={listId} />
        {query.data && !connections.length && <p>{t('readiness.empty')}</p>}
        {query.data && connections.length > 0 && !shown.length && <p role="status">{t('readiness.browse.noMatches')}</p>}
        <div id={listId} className="readiness-list">{connections.map(item => <ReadinessCard key={`${identity}:${item.connection_id}`} item={item} identity={identity} visible={shownIds.has(item.connection_id)} onViewAgents={close} />)}</div>
        <div className="page-actions"><Link to="/connections" className="button button--secondary" onClick={close}>{t('connections.title')}</Link></div>
      </div>
    </ReadinessDialog></div>}
  </>
}
