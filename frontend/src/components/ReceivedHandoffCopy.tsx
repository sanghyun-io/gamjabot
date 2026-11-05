import { useMemo, useRef } from 'react'
import type { RoomEvent } from '../api/events'
import { useI18n } from '../i18n'
import { useClipboardText } from '../lib/useClipboardText'
import ClipboardTextFallback from './ClipboardTextFallback'

export default function ReceivedHandoffCopy({ event, scope, canUse }: { event: RoomEvent; scope: string; canUse: () => boolean }) {
  const { t } = useI18n(), root = useRef<HTMLDivElement>(null)
  const text = typeof event.payload.content === 'string' ? event.payload.content : ''
  const eligible = event.kind === 'summary' && event.actor_type === 'system' && event.payload.owner_checkpoint === true
    && Number.isSafeInteger(event.room_id) && event.room_id > 0 && Number.isSafeInteger(event.seq) && event.seq > 0
    && typeof event.event_id === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(event.event_id) && text.length > 0
  const context = useMemo(() => ({}), [scope, event.room_id, event.event_id, event.seq, text])
  const current = () => eligible && canUse() && !!root.current?.isConnected && !!root.current.getClientRects().length
    && !root.current.closest('[hidden], [inert], [aria-hidden="true"]')
  const copy = useClipboardText<{ text: string; room: number; id: string; seq: number }>(context, item => current()
    && item.text === text && item.room === event.room_id && item.id === event.event_id && item.seq === event.seq)
  const snapshot = () => ({ text, room: event.room_id, id: event.event_id, seq: event.seq })
  return <div ref={root} className="stack stack--tight">
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={!eligible || copy.busy} onClick={() => void copy.copy(snapshot())}>{t(copy.busy ? 'common.loading' : 'chat.handoff.copy.action')}</button>
      <button type="button" className="text-link" disabled={!eligible} onClick={() => copy.openManual(snapshot())}>{t('chat.handoff.copy.manual')}</button>
    </div>
    <p className="field-hint">{t('chat.handoff.copy.scope')}</p>
    {copy.result && <p role={copy.result.status === 'copied' ? 'status' : 'alert'} className="metadata">{t(copy.result.status === 'copied' ? 'chat.code.copied' : 'chat.handoff.copy.failed')}</p>}
    {copy.manual && <ClipboardTextFallback text={copy.manual.text} label={t('chat.handoff.copy.body')} caption={t('chat.clipboard.manualHelp')}
      canUse={copy.manualAllowed} onClose={copy.closeManualAndRestore} onInteract={copy.touchManual} busy={copy.busy}
      onRetry={() => { if (copy.manual) void copy.copy(copy.manual) }}/>} 
  </div>
}
