import { useId, useRef } from 'react'
import type { Watch } from '../lib/roomPlanEditorState'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useI18n } from '../i18n'

export type WatchEndTarget = Readonly<Pick<Watch, 'id' | 'revision' | 'state' | 'name' | 'url' | 'interval_minutes' | 'timezone' | 'mode' | 'keyword' | 'stop_after_change'>>
export function watchEndTarget(watch: Watch): WatchEndTarget {
  const { id, revision, state, name, url, interval_minutes, timezone, mode, keyword, stop_after_change } = watch
  return Object.freeze({ id, revision, state, name, url, interval_minutes, timezone, mode, keyword, stop_after_change })
}
export function sameWatchEndTarget(target: WatchEndTarget, current: Watch | undefined) {
  return !!current && current.state !== 'ended' && Object.entries(target).every(([key, value]) => current[key as keyof WatchEndTarget] === value)
}

export default function WatchEndDialog({ target, current, roomId, busy, canUse, onClose, onConfirm }: {
  target: WatchEndTarget; current: Watch | undefined; roomId: number; busy: boolean
  canUse: () => boolean; onClose: () => void; onConfirm: () => void
}) {
  const { t, formatNumber } = useI18n(), root = useRef<HTMLDivElement>(null), title = useId(), help = useId()
  const close = () => { if (canUse()) onClose() }
  useDialogFocus(root, close)
  const actionable = !busy && sameWatchEndTarget(target, current)
  return <div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack">
    <h2 id={title} className="page-title">{t('roomPlans.endDialog.title')}</h2>
    <div className="notice notice--info stack stack--tight" style={{ overflowWrap: 'anywhere' }}>
      <strong>{target.name}</strong>
      <p>{t('chat.roomTitle', { id: String(roomId) })} · {t(`roomPlans.${target.state}`)}</p>
      <p className="technical">{target.url}</p>
      <p>{t(`roomPlans.${target.mode}`)}{target.keyword && ` · ${target.keyword}`}</p>
      <p>{formatNumber(target.interval_minutes)} {t('roomPlans.minutes')} · {target.timezone}</p>
      <label className="check-row"><input type="checkbox" disabled checked={target.stop_after_change}/>{t('roomPlans.stopAfter')}</label>
      <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p><code>{target.id}</code> · r{target.revision}</p></details>
    </div>
    <p id={help}>{t('roomPlans.endConfirm')}</p>
    <p className="field-hint">{t('roomPlans.endDialog.effect')}</p>
    <p className="field-hint">{t('roomPlans.endDialog.received')}</p>
    {!actionable && <p role="status" className="notice notice--warning">{t('roomPlans.endDialog.changed')}</p>}
    <div className="page-actions">
      <button type="button" data-dialog-initial className="button button--secondary" onClick={close}>{t('common.cancel')}</button>
      <button type="button" className="button button--danger" disabled={!actionable} onClick={() => { if (canUse() && actionable) onConfirm() }}>{t('roomPlans.end')}</button>
    </div>
  </div></div>
}
