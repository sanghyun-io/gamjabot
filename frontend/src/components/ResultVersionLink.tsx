import { useMemo, useRef } from 'react'
import { Link } from 'react-router-dom'
import { useI18n } from '../i18n'
import { resultReferenceMarkdown } from '../lib/resultLink'
import { useClipboardText } from '../lib/useClipboardText'
import ClipboardTextFallback from './ClipboardTextFallback'
export default function ResultVersionLink({ title, version, href, canUse, copyContext='result-reference' }: { title: string; version: number; href: string; canUse: () => boolean; copyContext?: string }) {
  const { t } = useI18n()
  const markdown = resultReferenceMarkdown(title, version, href)
  const context = useMemo(() => ({}), [copyContext, title, version, href])
  const copy = useClipboardText<{ text: string; version: number; href: string }>(context, item => canUse() && item.version === version && item.href === href && item.text === markdown)
  const snapshot = () => ({ text: markdown, version, href })
  const manualTrigger = useRef<HTMLButtonElement>(null)
  const closeManual = () => {
    if (!copy.manualAllowed()) return
    copy.closeManual()
    if (canUse() && manualTrigger.current?.isConnected && manualTrigger.current.getClientRects().length) manualTrigger.current.focus({ preventScroll: true })
  }
  return <div className="stack stack--tight"><div className="page-actions"><Link className="text-link" to={href} onClick={event => { if (!canUse()) event.preventDefault() }}>{t('results.versionLink.open')}</Link><button type="button" className="button button--secondary button--compact" disabled={copy.busy} onClick={() => void copy.copy(snapshot())}>{copy.busy ? t('common.loading') : t('results.versionLink.copy')}</button>
    <button ref={manualTrigger} type="button" className="text-link" onClick={() => copy.openManual(snapshot())}>{t('results.clipboard.selectLink')}</button></div>
    <p className="field-hint">{t('results.versionLink.scope')}</p>
    {copy.result && <p role={copy.result.status === 'copied' ? 'status' : 'alert'} className="metadata">{t(copy.result.status === 'copied' ? 'results.versionLink.copied' : 'results.versionLink.failed')}</p>}
    {copy.manual && <ClipboardTextFallback rows={3} text={copy.manual.text} label={t('results.versionLink.manual')} caption={t('results.clipboard.help')} canUse={copy.manualAllowed} onClose={closeManual} onInteract={copy.touchManual} busy={copy.busy} onRetry={() => { if (copy.manual) void copy.copy(copy.manual) }}/>}
  </div>
}
