import { useMemo, useRef } from 'react'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { pendingRoomMessages, type PendingRoomMessage } from '../lib/pendingRoomMessages'
import { useClipboardText } from '../lib/useClipboardText'
import ClipboardTextFallback from './ClipboardTextFallback'

// Copy only the submitted request body; never restore a draft or settle/replay a send.
export default function PendingMessageCopy({ item, ownerId, epoch, generation }: {
  item: PendingRoomMessage; ownerId: number; epoch: number; generation: number
}) {
  const { t } = useI18n(), root = useRef<HTMLDivElement>(null)
  const context = useMemo(() => ({}), [ownerId, epoch, generation, item.identity, item.roomId, item.key, item.content, item.outputKind])
  const current = () => {
    const auth = useAuthStore.getState(), node = root.current
    if (auth.user?.id !== ownerId || auth.epoch !== epoch || directoryViewGeneration() !== generation
      || !node?.isConnected || !node.getClientRects().length || node.closest('[hidden], [inert], [aria-hidden="true"]')) return false
    const saved = pendingRoomMessages(item.identity, item.roomId).find(value => value.key === item.key)
    return !!saved && saved.content === item.content && saved.outputKind === item.outputKind && item.content.length > 0
  }
  const copy = useClipboardText<{ text: string; room: number; key: string }>(context,
    value => current() && value.text === item.content && value.room === item.roomId && value.key === item.key)
  const snapshot = () => ({ text: item.content, room: item.roomId, key: item.key })
  return <div ref={root} className="stack stack--tight">
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={!item.content || copy.busy}
        onClick={() => void copy.copy(snapshot())}>{t(copy.busy ? 'common.loading' : 'chat.delivery.copy.action')}</button>
      <button type="button" className="text-link" disabled={!item.content}
        onClick={() => copy.openManual(snapshot())}>{t('chat.delivery.copy.manual')}</button>
    </div>
    <p className="field-hint">{t('chat.delivery.copy.scope')}</p>
    {copy.result && <p role={copy.result.status === 'copied' ? 'status' : 'alert'} className="metadata">
      {t(copy.result.status === 'copied' ? 'chat.code.copied' : 'chat.delivery.copy.failed')}</p>}
    {copy.manual && <ClipboardTextFallback text={copy.manual.text} label={t('chat.delivery.copy.body')} caption={t('chat.clipboard.manualHelp')}
      canUse={copy.manualAllowed} onClose={copy.closeManualAndRestore} onInteract={copy.touchManual} busy={copy.busy}
      onRetry={() => { if (copy.manual) void copy.copy(copy.manual) }}/>} 
  </div>
}
