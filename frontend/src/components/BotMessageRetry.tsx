import { useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import type { Agent } from '../api/agents'
import type { BotMessage, BotMessageRef } from '../api/botMessages'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { useBotScope } from '../lib/useBotScope'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useDialogFocus } from '../lib/useDialogFocus'
import { botMessageRetryKey, readBotMessageRetry, subscribeBotMessageRetry, readBotMessageRetryPlan, startBotMessageRetry, resendBotMessageRetry } from '../lib/botMessageRetryWorkspace'
import HistoryEventLink from './HistoryEventLink'
const blockedKeys: Record<string, MessageKey> = {
  source_room_unavailable: 'retry.blocked.room_read_only', source_not_terminal: 'retry.blocked.source_not_terminal',
  message_not_failed: 'botMessages.retryNotFailed', source_too_large: 'botMessages.retryTooLarge',
  effect_unsettled: 'retry.blocked.action_unsettled', completed_effect: 'retry.blocked.completed_effect', source_bot_unavailable: 'retry.blocked.agent_unavailable',
}
export default function BotMessageRetry({ message, roomId, agents, canUse, onOpen }: {
  message: BotMessage; roomId: number; agents: Agent[]; canUse: () => boolean; onOpen: (reference: BotMessageRef) => void
}) {
  const { t } = useI18n(), scope = useBotScope(), [generation] = useState(directoryViewGeneration)
  const key = botMessageRetryKey(scope.identity, generation, message.message_id), view = `${key}:${roomId}`
  const [openedFor, setOpenedFor] = useState<string | null>(null)
  const workspace = useSyncExternalStore(subscribeBotMessageRetry, () => readBotMessageRetry(key))
  const allowed = () => scope.canUse() && canUse()
  return <>
    <button type="button" className="text-link" onClick={() => {
      if (!allowed()) return
      setOpenedFor(view)
      readBotMessageRetryPlan(key, message)
    }}>{t(workspace.operation?.phase === 'accepted' ? 'botMessages.retryOpenReceipt' : 'botMessages.retryReview')}</button>
    {openedFor === view && <RetryDialog workspaceKey={key} message={message} agents={agents} canUse={allowed}
      onClose={() => setOpenedFor(null)} onOpen={reference => { if (allowed()) { setOpenedFor(null); onOpen(reference) } }} />}
  </>
}
function RetryDialog({ workspaceKey, message, agents, canUse, onClose, onOpen }: {
  workspaceKey: string; message: BotMessage; agents: Agent[]; canUse: () => boolean; onClose: () => void; onOpen: (reference: BotMessageRef) => void
}) {
  const { t } = useI18n(), title = useId(), help = useId(), root = useRef<HTMLDivElement>(null), backdrop = useRef<HTMLDivElement>(null)
  const workspace = useSyncExternalStore(subscribeBotMessageRetry, () => readBotMessageRetry(workspaceKey)), { operation, plan, readPhase } = workspace
  const loading = readPhase === 'loading', sending = operation?.phase === 'sending', uncertain = operation?.phase === 'unknown'
  const receipt = operation?.phase === 'accepted' ? operation.receipt : undefined
  const reason = operation?.reason || plan?.blocked_reason
  const sourceId = operation?.plan.source_agent_id || plan?.source_agent_id
  const sourceName = agents.find(agent => agent.id === sourceId)?.name || (sourceId ? t('members.fallbackName', { id: sourceId }) : '')
  useDialogFocus(root, onClose)
  useLayoutEffect(() => {
    const overflow = document.body.style.overflow
    const background = [...document.body.children].filter(node => node !== backdrop.current).map(node => ({ node, inert: node.hasAttribute('inert'), hidden: node.getAttribute('aria-hidden') }))
    for (const { node } of background) { node.setAttribute('inert', ''); node.setAttribute('aria-hidden', 'true') }
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = overflow; for (const item of background) { if (!item.inert) item.node.removeAttribute('inert'); if (item.hidden === null) item.node.removeAttribute('aria-hidden'); else item.node.setAttribute('aria-hidden', item.hidden) } }
  }, [])
  const read = () => { if (canUse()) readBotMessageRetryPlan(workspaceKey, message) }
  return createPortal(<div ref={backdrop} className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack" style={{ maxWidth: 520 }}>
    <header className="split"><h2 id={title} className="section-title">{t('botMessages.retryTitle')}</h2><button type="button" className="icon-button" aria-label={t('common.close')} onClick={onClose}><X size={20} aria-hidden="true" /></button></header>
    <div className="stack"><p id={help} className="section-copy">{t('botMessages.retryCost')}</p>
      {sourceName && <p className="metadata">{t('botMessages.retrySource', { name: sourceName })}</p>}
      <HistoryEventLink roomId={message.root_ref.room_id} seq={message.root_ref.seq} eventId={message.root_ref.event_id} canUse={canUse}
        onNavigate={() => onOpen(message.root_ref)}>{t('teammates.original')}</HistoryEventLink>
      {loading && <p role="status">{t('common.loading')}</p>}
      {readPhase === 'error' && <p role="alert">{t('botMessages.retryReadFailure')}</p>}
      {!uncertain && reason && <p role="status" className="notice notice--warning">{t(blockedKeys[reason] || 'botMessages.retryBlocked')}</p>}
      {operation?.phase === 'rejected' && <p role="alert">{t('botMessages.retryChanged')}</p>}
      {sending && <p role="status">{t('chat.sending')}</p>}
      {uncertain && <div className="notice notice--warning stack stack--tight"><p role="alert">{t('botMessages.retryUnknown')}</p><p>{t('botMessages.retryReplayHelp')}</p>
        <button type="button" className="button button--secondary" onClick={() => { if (canUse()) resendBotMessageRetry(workspaceKey) }}>{t('botMessages.retryReplay')}</button></div>}
      {receipt && <div className="notice notice--success stack stack--tight"><p role="status">{t('botMessages.retryAccepted')}</p>{receipt.replayed && <p>{t('botMessages.retryReplayed')}</p>}
        <HistoryEventLink roomId={receipt.root_room_id} seq={receipt.retry_seq} eventId={receipt.retry_event_id} canUse={canUse}
          onNavigate={() => onOpen({ room_id: receipt.root_room_id, seq: receipt.retry_seq, event_id: receipt.retry_event_id, actor_type: 'user', actor_id: message.root_ref.actor_id, kind: 'message' })}>{t('botMessages.retryOpen')}</HistoryEventLink></div>}
      <div className="page-actions">
        {!operation && plan?.eligible && <button type="button" className="button button--primary" disabled={loading || !canUse()} onClick={() => { if (canUse()) startBotMessageRetry(workspaceKey, workspace.readId) }}>{t('botMessages.retryStart')}</button>}
        {(!operation || operation.phase === 'rejected') && <button type="button" className="button button--secondary" disabled={loading} onClick={read}>{t('botMessages.retryRead')}</button>}
        <button type="button" className="button button--ghost" onClick={onClose}>{t('common.close')}</button>
      </div>
    </div></div></div>, document.body)
}
