import { useId, useRef } from 'react'
import { Link } from 'react-router-dom'
import { useI18n } from '../i18n'
import { useDialogFocus } from '../lib/useDialogFocus'

export type BrowserProfile = { home_room_id?: number; shared_room_ids?: number[]; id: string; name: string; account_label: string; agent_ids: number[]; origin_urls: string[]; revision: number; controller: 'user' | 'agent' | 'disconnected' | 'settling' | 'parked'; agent_id: number | null; connected: boolean; last_used_at: string | null; expires_in_seconds: number }
export type ProfileAction = 'reset' | 'delete'
export type ProfileActionTarget = Readonly<Omit<BrowserProfile, 'last_used_at' | 'expires_in_seconds' | 'agent_ids' | 'origin_urls' | 'shared_room_ids'> & { agent_ids: readonly number[]; origin_urls: readonly string[]; shared_room_ids: readonly number[] }>
export function profileActionTarget(profile: BrowserProfile): ProfileActionTarget {
  const { id, name, account_label, revision, controller, agent_id, connected, home_room_id } = profile
  return Object.freeze({ id, name, account_label, revision, controller, agent_id, connected, home_room_id, shared_room_ids: Object.freeze([...(profile.shared_room_ids || [])]),
    agent_ids: Object.freeze([...profile.agent_ids]), origin_urls: Object.freeze([...profile.origin_urls]) })
}
export function sameProfileActionTarget(target: ProfileActionTarget, profile: BrowserProfile | undefined) {
  if (!profile) return false
  return target.id === profile.id && target.revision === profile.revision && target.name === profile.name
    && target.account_label === profile.account_label && target.controller === profile.controller
    && target.agent_id === profile.agent_id && target.connected === profile.connected
    && (target.home_room_id ?? null) === (profile.home_room_id ?? null)
    && target.shared_room_ids.length === (profile.shared_room_ids || []).length && target.shared_room_ids.every((id, index) => id === (profile.shared_room_ids || [])[index])
    && target.agent_ids.length === profile.agent_ids.length && target.agent_ids.every((id, index) => id === profile.agent_ids[index])
    && target.origin_urls.length === profile.origin_urls.length && target.origin_urls.every((url, index) => url === profile.origin_urls[index])
}

export default function BrowserProfileActionDialog({ action, target, current, roomId, ready, busy, agentName, canUse, onClose, onConfirm }: {
  action: ProfileAction; target: ProfileActionTarget; current: BrowserProfile | undefined; roomId: number
  ready: boolean; busy: boolean; agentName: (id: number) => string; canUse: () => boolean; onClose: () => void; onConfirm: () => void
}) {
  const { t } = useI18n(), root = useRef<HTMLDivElement>(null), title = useId(), help = useId()
  const close = () => { if (canUse()) onClose() }
  useDialogFocus(root, close)
  const actionable = ready && !busy && sameProfileActionTarget(target, current)
  return <div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack">
    <h2 id={title} className="page-title">{t(action === 'reset' ? 'browserProfiles.actionDialog.resetTitle' : 'browserProfiles.actionDialog.deleteTitle')}</h2>
    <div className="notice notice--info stack stack--tight" style={{ overflowWrap: 'anywhere' }}>
      <strong>{target.name}</strong>
      <p>{t('chat.roomTitle', { id: String(roomId) })} · {t(`browserProfiles.${target.controller}`)}{target.controller === 'agent' && target.agent_id !== null && ` · ${agentName(target.agent_id)}`}</p>
      {target.account_label && <p>{target.account_label} · {t('browserProfiles.notVerified')}</p>}
      <div><strong>{t('browserProfiles.origins')}</strong><ul>{target.origin_urls.map((url, index) => <li key={index}>{url}</li>)}</ul></div>
      <p>{t('browserProfiles.agents')}: {target.agent_ids.map(agentName).join(' · ') || t('browserProfiles.manualOnly')}</p>
      {target.shared_room_ids.length>0&&<div><p>{t('profileSharing.resetImpact',{count:target.shared_room_ids.length})}</p><ul>{target.shared_room_ids.map(id=><li key={id}><Link to={`/chat/${id}`} onClick={event=>{if(!canUse())event.preventDefault()}}>{t('chat.roomTitle',{id:String(id)})}</Link></li>)}</ul></div>}
      <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p><code>{target.id}</code> · r{target.revision}</p></details>
    </div>
    <p id={help}>{t(action === 'reset' ? 'browserProfiles.clearConfirm' : 'browserProfiles.deleteConfirm')}</p>
    <p>{t(action === 'reset' ? 'browserProfiles.actionDialog.resetEffect' : 'browserProfiles.actionDialog.deleteEffect')}</p>
    <p className="field-hint">{t('browserProfiles.actionDialog.limit')}</p>
    {!actionable && <p role="status" className="notice notice--warning">{t('browserProfiles.actionDialog.changed')}</p>}
    <div className="page-actions">
      <button type="button" data-dialog-initial className="button button--secondary" onClick={close}>{t('common.cancel')}</button>
      <button type="button" className="button button--danger" disabled={!actionable} onClick={() => { if (canUse() && actionable) onConfirm() }}>{t(action === 'reset' ? 'browserProfiles.clear' : 'common.delete')}</button>
    </div>
  </div></div>
}
