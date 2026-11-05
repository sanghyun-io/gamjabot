import { useMemo, useRef } from 'react'
import type { ModelFeatureReport } from '../../api/connections'
import { useI18n } from '../../i18n'
import { useAuthStore } from '../../stores/authStore'
import { directoryViewGeneration } from '../../lib/directoryViewMemory'
import { useClipboardText } from '../../lib/useClipboardText'
import ClipboardTextFallback from '../ClipboardTextFallback'

export default function FeatureReportCopy({ report }: { report: ModelFeatureReport }) {
  const { t } = useI18n(), root = useRef<HTMLDetailsElement>(null)
  const ownerId = useAuthStore(state => state.user?.id), epoch = useAuthStore(state => state.epoch)
  const generation = useRef(directoryViewGeneration()).current
  // Project the known judgment fields; do not copy the endpoint, arbitrary extra
  // response fields, provider output, current credentials or surrounding UI text.
  const text = useMemo(() => JSON.stringify({
    model_id: report.model_id,
    observed_at: report.observed_at,
    tested_at: report.tested_at,
    actual_generation_requests: report.actual_generation_requests,
    features: Object.fromEntries(Object.entries(report.features).map(([feature, value]) => [feature, {
      reported: value.reported, tested: value.tested, available: value.available, reason: value.reason,
    }])),
  }, null, 2), [report])
  const context = useMemo(() => ({}), [report, ownerId, epoch, generation])
  const current = () => {
    const node = root.current, auth = useAuthStore.getState()
    return !!ownerId && auth.user?.id === ownerId && auth.epoch === epoch && generation === directoryViewGeneration()
      && !!node?.isConnected && node.open && !!node.getClientRects().length
      && !node.closest('[hidden], [inert], [aria-hidden="true"], fieldset:disabled')
  }
  const copy = useClipboardText<{ text: string }>(context, item => current() && item.text === text)
  const snapshot = () => ({ text })
  return <details ref={root} className="compact-details">
    <summary>{t('localModels.reportCopy.title')}</summary>
    <div className="stack stack--tight">
      <p className="field-hint">{t('localModels.reportCopy.scope')}</p>
      <div className="page-actions">
        <button type="button" className="button button--secondary button--compact" disabled={copy.busy}
          onClick={() => void copy.copy(snapshot())}>{t(copy.busy ? 'common.loading' : 'localModels.reportCopy.action')}</button>
        <button type="button" className="text-link" onClick={() => copy.openManual(snapshot())}>{t('localModels.reportCopy.manual')}</button>
      </div>
      {copy.result && <p role={copy.result.status === 'copied' ? 'status' : 'alert'} className="metadata">
        {t(copy.result.status === 'copied' ? 'chat.code.copied' : 'chat.clipboard.failure')}</p>}
      {copy.manual && <ClipboardTextFallback text={copy.manual.text} label={t('localModels.reportCopy.label')} caption={t('chat.clipboard.manualHelp')}
        canUse={copy.manualAllowed} onClose={copy.closeManualAndRestore} onInteract={copy.touchManual} busy={copy.busy}
        onRetry={() => { if (copy.manual) void copy.copy(copy.manual) }}/>} 
    </div>
  </details>
}
