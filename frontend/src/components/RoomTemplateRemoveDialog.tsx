import { useId, useRef } from 'react'
import type { Agent } from '../api/agents'
import type { RoomTemplate } from '../api/roomTemplates'
import { useI18n } from '../i18n'
import { useDialogFocus } from '../lib/useDialogFocus'

export function sameTemplateRemoval(target: RoomTemplate, current: RoomTemplate | undefined) {
  return !!current && target.id === current.id && target.user_id === current.user_id && target.project_id === current.project_id
    && target.name === current.name && target.room_title === current.room_title && target.updated_at === current.updated_at
    && target.created_at === current.created_at && target.max_rounds === current.max_rounds
    && target.max_tool_calls === current.max_tool_calls && target.max_executor_seconds === current.max_executor_seconds
    && target.agent_ids.length === current.agent_ids.length && target.agent_ids.every((id, index) => id === current.agent_ids[index])
}

export default function RoomTemplateRemoveDialog({ target, current, projectName, agents, ready, blocked, canUse, onClose, onConfirm }: {
  target: RoomTemplate; current: RoomTemplate | undefined; projectName: string; agents: Agent[]
  ready: boolean; blocked: boolean; canUse: () => boolean; onClose: () => void; onConfirm: () => void
}) {
  const { t } = useI18n(), root = useRef<HTMLDivElement>(null), title = useId(), help = useId()
  const close = () => { if (canUse()) onClose() }
  useDialogFocus(root, close)
  const unchanged = sameTemplateRemoval(target, current)
  const actionable = ready && unchanged && !blocked
  return <div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack">
    <h2 id={title} className="page-title">{t('projects.templates.removeDialog.title')}</h2>
    <div className="notice notice--info stack stack--tight">
      <strong>{target.name} · #{target.id}</strong>
      <p>{projectName} · #{target.project_id}</p>
      <p>{t('projects.templates.removeDialog.room', { title: target.room_title })}</p>
      <p>{t('projects.cloneSettings', { rounds: target.max_rounds, tools: target.max_tool_calls, seconds: target.max_executor_seconds })}</p>
      <details className="compact-details"><summary>{target.agent_ids.length ? t('projects.templates.memberCount', { count: target.agent_ids.length }) : t('projects.templates.noMembers')}</summary>
        <ul>{target.agent_ids.map(id => <li key={id}>{agents.find(agent => agent.id === id)?.name || t('projects.templates.missingMember', { id })} · #{id}</li>)}</ul>
      </details>
    </div>
    <p id={help}>{t('projects.templates.deleteNamed', { name: target.name })}</p>
    <p className="field-hint">{t('projects.templates.removeDialog.saved')}</p>
    {(!ready || !unchanged || blocked) && <p role="status" className="notice notice--warning">{t('projects.templates.removeDialog.unavailable')}</p>}
    <div className="page-actions">
      <button type="button" data-dialog-initial className="button button--secondary" onClick={close}>{t('common.cancel')}</button>
      <button type="button" className="button button--danger" disabled={!actionable} onClick={() => { if (canUse() && actionable) onConfirm() }}>{t('common.delete')}</button>
    </div>
  </div></div>
}
