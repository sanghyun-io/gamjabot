import { useMemo, useRef } from 'react'
import { useI18n } from '../../i18n'
import { useAuthStore } from '../../stores/authStore'
import { directoryViewGeneration } from '../../lib/directoryViewMemory'
import { useClipboardText } from '../../lib/useClipboardText'
import ClipboardTextFallback from '../ClipboardTextFallback'

// The selected catalog ID is metadata, not proof of generation or feature support.
export default function SelectedModelIdentity({ scope, modelId }: { scope: string; modelId: string }) {
  const { t } = useI18n(), root = useRef<HTMLDetailsElement>(null)
  const auth = useAuthStore(), ownerId = auth.user?.id, epoch = auth.epoch
  const generation = useRef(directoryViewGeneration()).current
  const context = useMemo(() => ({}), [scope, modelId, ownerId, epoch, generation])
  const current = () => {
    const node = root.current, state = useAuthStore.getState()
    return !!ownerId && state.user?.id === ownerId && state.epoch === epoch && directoryViewGeneration() === generation
      && !!modelId && !!node?.isConnected && node.open && !!node.getClientRects().length
      && !node.closest('[hidden], [inert], [aria-hidden="true"], fieldset:disabled')
  }
  const copy = useClipboardText<{ text: string }>(context, value => current() && value.text === modelId)
  const snapshot = () => ({ text: modelId })
  return <details ref={root} className="compact-details" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <summary>{t('connections.modelIdentity.copy')}</summary>
    <div className="stack stack--tight">
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={copy.busy}
        onClick={() => void copy.copy(snapshot())}>{t(copy.busy ? 'common.loading' : 'connections.modelIdentity.copy')}</button>
      <button type="button" className="text-link" onClick={() => copy.openManual(snapshot())}>{t('connections.modelIdentity.select')}</button>
    </div>
    <p className="field-hint">{t('connections.modelIdentity.help')}</p>
    {copy.result && <p role={copy.result.status === 'copied' ? 'status' : 'alert'} className="metadata">
      {t(copy.result.status === 'copied' ? 'chat.code.copied' : 'chat.clipboard.failure')}</p>}
    {copy.manual && <ClipboardTextFallback rows={3} text={copy.manual.text} label={t('connections.modelIdentity.label')} caption={t('chat.clipboard.manualHelp')}
      canUse={copy.manualAllowed} onClose={copy.closeManualAndRestore} onInteract={copy.touchManual} busy={copy.busy}
      onRetry={() => { if (copy.manual) void copy.copy(copy.manual) }}/>} 
    </div>
  </details>
}
