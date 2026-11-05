import type { ReplyReference } from '../api/events'
import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from 'react'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useDialogFocus } from '../lib/useDialogFocus'
import { Eye, X } from 'lucide-react'
import { contextPreviewsApi, type AgentContextPreview, type ContextPreviewError, type ContextPreviewResponse } from '../api/contextPreviews'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'

const errorKeys: Record<ContextPreviewError, MessageKey> = {
  model_binding_missing: 'chat.context.error.model',
  context_state_missing: 'chat.context.error.state',
  context_input_too_large: 'chat.context.error.input',
  context_memory_too_large: 'chat.context.error.memory',
  context_too_large: 'chat.context.error.size',
}

function AgentProjection({ agent }: { agent: AgentContextPreview }) {
  const { t, formatNumber } = useI18n()
  const recipient = agent.agent_name ? `${agent.agent_name} · Agent #${agent.agent_id}` : `Agent #${agent.agent_id}`
  return <article className="context-preview-agent">
    <header><h3>{recipient}</h3>
      <span className={`context-preview-status context-preview-status--${agent.projection_status}`}>{t(`chat.context.status.${agent.projection_status}`)}</span></header>
    {agent.projection_status === 'unavailable' ? <p role="status" className="notice notice--warning">{t(agent.error_code ? errorKeys[agent.error_code] : 'chat.context.error.generic')}</p> : <>
      {(agent.truncations.length > 0) && <p className="notice notice--warning">{t('chat.context.truncated')}</p>}
      {(agent.non_text.length > 0) && <p className="notice notice--neutral">{t('chat.context.nonText', { count: agent.non_text.length })}</p>}
      <p className="metadata">{t('chat.context.records', { count: agent.source_coverage.included_event_seqs.length })}</p>
      <p className="metadata">{t(agent.source_coverage.draft_included ? 'chat.context.draftIncluded' : 'chat.context.draftNotIncluded')}</p>
      <p className="metadata">{t('chat.context.budgetValue', { used: formatNumber(agent.budget.used_chars), limit: formatNumber(agent.budget.limit_chars), remaining: formatNumber(agent.budget.remaining_chars) })}</p>
      <p className="field-hint">{t('chat.context.characterBudgetHelp')}</p>
      {agent.memory_projection?.bot_private && <p className="metadata">{t('botMemory.preview', { count: agent.memory_projection.bot_private.entries.length })}</p>}
      <div className="context-preview-conversation" aria-label={t('chat.context.supplied', { agent: recipient })}>
        {agent.system_text && <section><strong>{t('chat.context.instructions')}</strong><p>{agent.system_text}</p></section>}
        {agent.messages.map((message, index) => <section key={`${message.role}:${index}`} className={`context-preview-message context-preview-message--${message.role}`}>
          <strong>{t(`chat.context.role.${message.role}`)}</strong><p>{message.content}</p>
        </section>)}
      </div>
      <details className="context-preview-details"><summary>{t('common.technicalDetails')}</summary><dl>
        {agent.memory_projection && <div><dt>{t('memory.context')}</dt><dd>{t('memory.recipient', { agent: agent.memory_projection.agent_id })} · {t('memory.revision', { revision: agent.memory_projection.room_revision })} · {t('memory.agentRevision', { revision: agent.memory_projection.agent_revision })} · {t('memory.floor', { seq: agent.memory_projection.context_after_seq })} · {t('memory.characters', { count: agent.memory_projection.serialized_chars })} · {t(agent.memory_projection.legacy_disabled ? 'memory.disabled' : 'memory.inUse')}<ul>{agent.memory_projection.entries.map((entry) => <li key={entry.id}>{entry.id} · {t('memory.revision', { revision: entry.revision })} · {t(entry.scope === 'room' ? 'memory.room' : 'memory.agent')} {entry.target_agent_id ?? ''}</li>)}</ul></dd></div>}
        <div><dt>{t('agents.exactModel')}</dt><dd>{agent.model_id}</dd></div>
        <div><dt>{t('chat.context.coverage')}</dt><dd>{t('chat.context.coverageValue', { count: agent.source_coverage.included_event_seqs.length, high: agent.source_coverage.committed_high_water })}</dd></div>
        <div><dt>{t('chat.context.included')}</dt><dd>{agent.source_coverage.included_event_seqs.length ? agent.source_coverage.included_event_seqs.map((seq) => `#${seq}`).join(', ') : t('chat.context.none')}</dd></div>
        <div><dt>{t('chat.context.summaries')}</dt><dd>{formatNumber(agent.source_coverage.summarized_ranges.length)}</dd></div>
        {agent.source_coverage.summarized_ranges.map((range, index) => <div key={`${range.kind}:${range.source_start_seq}:${range.source_end_seq}:${index}`}><dt>{t(`chat.context.range.${range.kind}`)}</dt><dd>#{formatNumber(range.source_start_seq)}–#{formatNumber(range.source_end_seq)}</dd></div>)}
        {agent.truncations.map((item) => <div key={item.event_id}><dt>{t('chat.context.shortened', { seq: item.seq })}</dt><dd>{t('chat.context.shortenedValue', { original: formatNumber(item.original_chars), included: formatNumber(item.included_chars), omitted: formatNumber(item.omitted_chars) })}</dd></div>)}

      </dl></details>
    </>}
  </article>
}

function PreviewDialog({ root, onClose, children }: { root: RefObject<HTMLElement | null>; onClose: () => void; children: ReactNode }) {
  const { t } = useI18n()
  // This mounted dialog uses the shared trap; its scoped parent owns focus return.
  useDialogFocus(root, onClose, { restoreFocus: false })
  return <div className="modal-backdrop context-preview-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={root} role="dialog" aria-modal="true" aria-label={t('chat.context.title')} className="context-preview-dialog">{children}</section>
  </div>
}

export default function ContextPreview({ roomId, ownerId, authEpoch, content, replyTo = null, sourceVersion, recipientIdentity, active, visible = true }: {
  roomId: number; ownerId: number; authEpoch: number; content: string; replyTo?: ReplyReference | null; sourceVersion: string; recipientIdentity: string; active: boolean; visible?: boolean
}) {
  const [open, setOpen] = useState(false), [status, setStatus] = useState<'idle' | 'error'>('idle')
  const [preview, setPreview] = useState<ContextPreviewResponse | null>(null), [previewBasis, setPreviewBasis] = useState<string | null>(null)
  const [invalidated, setInvalidated] = useState(false), [reading, setReading] = useState(false)
  const [recipientQuery, setRecipientQuery] = useState(''), [recipientStatus, setRecipientStatus] = useState<'all' | 'ready' | 'unavailable'>('all')
  const recipientListId = useId()
  const currentIdentity = useAuthStore(state => `${state.user?.id || 0}:${state.epoch}`), signedIn = useAuthStore(state => !!state.token)
  const generation = useRef(0), trigger = useRef<HTMLButtonElement>(null), dialog = useRef<HTMLElement>(null), openRef = useRef(false)
  const alive = useRef(true), inFlight = useRef(false), requestBasis = useRef<string | null>(null), focusFrame = useRef<number | null>(null)
  const { t } = useI18n()
  const scopeIdentity = `${ownerId}:${authEpoch}:${roomId}`, openScope = useRef<string | null>(null)
  const projectionIdentity = `${roomId}:${ownerId}:${authEpoch}:${sourceVersion}:${recipientIdentity}:${content}:${replyTo?.event_id || ""}:${replyTo?.seq || 0}:${active}`
  const live = useRef({ roomId, ownerId, authEpoch, active, visible, content, replyTo, hasContent: !!content.trim(), basis: projectionIdentity })
  live.current = { roomId, ownerId, authEpoch, active, visible, content, replyTo, hasContent: !!content.trim(), basis: projectionIdentity }
  const canUse = (forRead = false) => {
    const auth = useAuthStore.getState(), now = live.current
    return alive.current && now.visible && !!auth.token && auth.user?.id === now.ownerId && auth.epoch === now.authEpoch && (!forRead || now.active && now.hasContent)
  }
  const cancelFocus = () => { if (focusFrame.current !== null) { window.cancelAnimationFrame(focusFrame.current); focusFrame.current = null } }
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current++; cancelFocus() } }, [])
  const close = () => {
    const target = trigger.current, boundary = dialog.current, origin = document.activeElement
    const scope = `${live.current.ownerId}:${live.current.authEpoch}:${live.current.roomId}`
    const mayReturn = canUse() && openScope.current === scope && (origin === document.body || !!origin && !!boundary?.contains(origin))
    generation.current++; requestBasis.current = null; openRef.current = false; openScope.current = null; cancelFocus()
    setOpen(false); setPreview(null); setPreviewBasis(null); setInvalidated(false); setStatus('idle'); setRecipientQuery(''); setRecipientStatus('all')
    if (!mayReturn || !target) return
    focusFrame.current = window.requestAnimationFrame(() => {
      focusFrame.current = null
      const now = live.current
      if (!canUse() || `${now.ownerId}:${now.authEpoch}:${now.roomId}` !== scope || target !== trigger.current || !target.isConnected
        || target.matches(':disabled') || !target.getClientRects().length || target.closest('[hidden], [inert], [aria-hidden="true"]')
        || document.visibilityState !== 'visible' || !document.hasFocus() || document.activeElement !== document.body && document.activeElement !== origin
        || document.querySelector('dialog[open], [aria-modal="true"]')) return
      target.focus({ preventScroll: true })
    })
  }
  useEffect(() => {
    if (projectionIdentity !== live.current.basis) return
    cancelFocus()
    if (!canUse() || openRef.current && openScope.current !== scopeIdentity) {
      generation.current++; requestBasis.current = null; openRef.current = false; openScope.current = null
      setOpen(false); setPreview(null); setPreviewBasis(null); setInvalidated(false); setStatus('idle'); setRecipientQuery(''); setRecipientStatus('all')
      return
    }
    if (!openRef.current) return
    const requestChanged = requestBasis.current !== null && requestBasis.current !== projectionIdentity
    if (requestChanged) { generation.current++; requestBasis.current = null; setStatus('idle') }
    if (requestChanged) setInvalidated(true)
  }, [projectionIdentity, currentIdentity, signedIn, visible])
  const load = async () => {
    if (!canUse(true) || !openRef.current || openScope.current !== `${live.current.ownerId}:${live.current.authEpoch}:${live.current.roomId}`) return
    if (inFlight.current) { setInvalidated(true); return }
    const snapshot = live.current
    const run = ++generation.current, expected = snapshot.basis
    requestBasis.current = expected; inFlight.current = true; setReading(true); setStatus('idle'); setInvalidated(false)
    const current = () => canUse(true) && openRef.current && run === generation.current && live.current.basis === expected
    try {
      const value = snapshot.replyTo ? await contextPreviewsApi.create(snapshot.roomId, snapshot.content.trim(), snapshot.replyTo) : await contextPreviewsApi.create(snapshot.roomId, snapshot.content.trim())
      if (!current()) return
      if (value.room_id !== snapshot.roomId || !Array.isArray(value.selected_agent_ids) || !Array.isArray(value.agents)) throw new Error('Preview scope mismatch')
      const selected = new Set(value.selected_agent_ids)
      if (value.selected_agent_ids.some(id => !Number.isSafeInteger(id) || id <= 0) || selected.size !== value.selected_agent_ids.length
        || value.agents.length !== selected.size || new Set(value.agents.map(agent => agent.agent_id)).size !== selected.size
        || value.agents.some(agent => !selected.has(agent.agent_id))) throw new Error('Preview recipient mismatch')
      setPreview(value); setPreviewBasis(expected); setInvalidated(false); setStatus('idle')
    } catch { if (current()) setStatus('error') }
    finally {
      inFlight.current = false
      if (run === generation.current) requestBasis.current = null
      if (alive.current) setReading(false)
    }
  }
  const show = () => { if (!canUse(true)) return; cancelFocus(); openScope.current = `${live.current.ownerId}:${live.current.authEpoch}:${live.current.roomId}`; openRef.current = true; setOpen(true); void load() }
  // Display filtering is local to this received preview, never recipient selection.
  const matchesRecipient = directoryMatcher(recipientQuery)
  const filteredAgents = preview?.agents.filter(agent => (recipientStatus === 'all' || agent.projection_status === recipientStatus)
    && matchesRecipient([agent.agent_name, `Agent #${agent.agent_id}`, String(agent.agent_id)], agent.agent_id)) ?? []
  const filteringRecipients = !!recipientQuery.trim() || recipientStatus !== 'all'
  const scopeCurrent = signedIn && currentIdentity === `${ownerId}:${authEpoch}`
  const needsRefresh = invalidated || preview !== null && previewBasis !== projectionIdentity || open && !preview && !reading && status === 'idle'
  return <><button ref={trigger} type="button" className="button button--ghost button--compact" disabled={!active || !visible || !scopeCurrent || !content.trim()} onClick={show}><Eye size={15} aria-hidden="true" />{t('chat.context.open')}</button>
    {open && visible && scopeCurrent && openScope.current === scopeIdentity && <PreviewDialog root={dialog} onClose={close}>
      <header><div><h2>{t('chat.context.title')}</h2><p>{t('chat.context.help')}</p></div><div className="inline-actions">
        <button type="button" className="button button--secondary button--compact" disabled={reading || !active || !content.trim()} onClick={() => { void load() }}>{t('chat.context.refresh')}</button>
        <button data-dialog-initial type="button" className="icon-button" aria-label={t('common.close')} onClick={close}><X size={18}/></button>
      </div></header>
      <div className="context-preview-dialog__body">
        {needsRefresh && <div className="notice notice--warning stack stack--tight"><p role="status">{t('chat.context.refreshNeeded')}</p><p>{t('chat.context.refreshHelp')}</p></div>}
        {reading && <p role="status">{t('common.loading')}</p>}
        {status === 'error' && <div className="stack stack--tight"><p role="alert" className="notice notice--warning">{t('chat.context.unavailable')}</p>{preview && <p className="field-hint">{t('chat.context.retainedAfterError')}</p>}<button type="button" className="button button--secondary" disabled={reading || !active || !content.trim()} onClick={() => { void load() }}>{t('common.retry')}</button></div>}
        {preview && <><p className="metadata">{t('chat.context.recipientSummary', { total: preview.selected_agent_ids.length, ready: preview.agents.filter(agent => agent.projection_status === 'ready').length, unavailable: preview.agents.filter(agent => agent.projection_status === 'unavailable').length })}</p>{(preview.agents.length > 1 || filteringRecipients) && <div className="stack stack--tight">
            <DirectorySearch value={recipientQuery} onChange={setRecipientQuery} label={t('chat.context.findRecipient')} hint={t('chat.context.recipientSearchHint')} shown={filteredAgents.length} total={preview.agents.length} loading={false} known controls={recipientListId} preventSubmit exactId />
            <label className="field"><span>{t('chat.context.recipientFilter')}</span><select value={recipientStatus} onChange={event => setRecipientStatus(event.target.value as typeof recipientStatus)}>
              <option value="all">{t('switcher.all')}</option><option value="ready">{t('chat.context.status.ready')}</option><option value="unavailable">{t('chat.context.status.unavailable')}</option>
            </select></label>
            <p className="field-hint">{t('chat.context.recipientFilterHelp')}</p>
          </div>}
          <div id={recipientListId} className="context-preview-list">{filteredAgents.map(agent => <AgentProjection key={agent.agent_id} agent={agent}/>)}</div>
          {!filteredAgents.length && <p role="status">{t(preview.agents.length ? 'directory.noMatches' : 'chat.context.empty')}</p>}
          <details className="context-preview-fingerprint"><summary>{t('chat.context.projectionDetails')}</summary><p className="metadata">{t('chat.context.fingerprint')}: <code>{preview.fingerprint}</code></p></details></>}
      </div>
    </PreviewDialog>}
  </>
}
