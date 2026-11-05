import { useId, useRef } from 'react'
import type { Agent } from '../api/agents'
import { useI18n } from '../i18n'
import { useDialogFocus } from '../lib/useDialogFocus'

// Only the received, displayed target metadata is captured, never its prompt.
export type AgentRemovalTarget = Pick<Agent, 'id' | 'user_id' | 'name' | 'description' | 'connection_id' | 'model_id' | 'model' | 'updated_at' | 'is_active'>
export function removalTarget(agent: Agent): AgentRemovalTarget {
  const { id, user_id, name, description, connection_id, model_id, model, updated_at, is_active } = agent
  return { id, user_id, name, description, connection_id, model_id, model, updated_at, is_active }
}
export function sameRemovalTarget(target: AgentRemovalTarget, current: Agent | undefined) {
  return !!current && current.is_active && Object.entries(target).every(([key, value]) => current[key as keyof AgentRemovalTarget] === value)
}

export default function AgentRemoveDialog({ target, current, connectionLabel, ready, blocked, pending, canUse, onAdopt, onConfirm, onClose }: {
  target: AgentRemovalTarget; current: Agent | undefined; connectionLabel?: string; ready: boolean; blocked: boolean; pending: boolean
  canUse: () => boolean; onAdopt: () => void; onConfirm: () => void; onClose: () => void
}) {
  const { t } = useI18n(), root = useRef<HTMLDivElement>(null), title = useId(), help = useId()
  const close = () => { if (canUse()) onClose() }
  useDialogFocus(root, close)
  const unchanged = sameRemovalTarget(target, current)
  const actionable = ready && unchanged && !blocked
  return <div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack">
    <h2 id={title} className="page-title">{t('agents.removeDialog.title')} · {target.name} · #{target.id}</h2>
    {target.description && <p className="section-copy">{target.description}</p>}
    <div className="notice notice--info stack stack--tight">
      <p>{t('agents.connection', { value: target.connection_id ? connectionLabel ? `${connectionLabel} · #${target.connection_id}` : t('agents.savedConnectionNumber', { id: String(target.connection_id) }) : t('agents.noConnection') })}</p>
      <p>{t('agents.model', { value: target.model_id || target.model })}</p>
    </div>
    <p id={help}>{t('agents.removeConfirmNamed', { name: target.name })}</p>
    {!ready || !current?.is_active ? <p role="status" className="notice notice--warning">{t('agents.removeDialog.unavailable')}</p>
      : !unchanged && <p role="status" className="notice notice--warning">{t('agents.removeDialog.changed')}</p>}
    {blocked && <p role="status" className="field-hint">{t(pending ? 'agents.workspace.pending' : 'agents.workspace.blocked')}</p>}
    <div className="page-actions">
      <button type="button" data-dialog-initial className="button button--secondary" onClick={close}>{t('common.cancel')}</button>
      {ready && current?.is_active && !unchanged && <button type="button" className="button button--secondary" onClick={() => { if (canUse()) onAdopt() }}>{t('agents.removeDialog.refresh')}</button>}
      <button type="button" className="button button--danger" disabled={!actionable} onClick={() => { if (canUse() && actionable) onConfirm() }}>{t('common.delete')}</button>
    </div>
  </div></div>
}
