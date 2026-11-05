import ResultTextFind, { type ResultTextFindHandle, type ResultFindMemory } from './ResultTextFind'
import { useMemo, useRef, type Ref } from 'react'
import { useI18n } from '../i18n'
import { useClipboardText } from '../lib/useClipboardText'
import ClipboardTextFallback from './ClipboardTextFallback'
export default function ResultContentCopy({ content, version, canCopy, findRef, copyContext='result-content', findMemory }: { content: string; version: number; canCopy: () => boolean; findRef?: Ref<ResultTextFindHandle>; copyContext?: string; findMemory?: ResultFindMemory }) {
  const { t } = useI18n()
  const context = useMemo(() => ({}), [copyContext, content, version])
  const copy = useClipboardText<{ text: string; version: number }>(context, item => canCopy() && item.version === version && item.text === content)
  const snapshot = () => ({ text: content, version })
  const manualTrigger = useRef<HTMLButtonElement>(null)
  const closeManual = () => {
    if (!copy.manualAllowed()) return
    copy.closeManual()
    if (canCopy() && manualTrigger.current?.isConnected && manualTrigger.current.getClientRects().length) manualTrigger.current.focus({ preventScroll: true })
  }
  return <div className="stack stack--tight"><div className="page-actions">
    <button type="button" className="button button--secondary button--compact" disabled={copy.busy} onClick={() => void copy.copy(snapshot())}>{copy.busy ? t('common.loading') : t('results.versionCopy.action', { version })}</button>
    <button ref={manualTrigger} type="button" className="text-link" onClick={() => copy.openManual(snapshot())}>{t('results.clipboard.selectText', { version })}</button>
    </div>
    <ResultTextFind ref={findRef} content={content} version={version} canUse={canCopy} memory={findMemory}/>
    {copy.result && <p role={copy.result.status === 'copied' ? 'status' : 'alert'} className="metadata">{t(copy.result.status === 'copied' ? 'results.versionCopy.copied' : 'results.versionCopy.failed', { version: copy.result.item.version })}</p>}
    {copy.manual && <ClipboardTextFallback text={copy.manual.text} label={t('results.versionCopy.original', { version: copy.manual.version })} caption={t('results.clipboard.help')} canUse={copy.manualAllowed} onClose={closeManual} onInteract={copy.touchManual} busy={copy.busy} onRetry={() => { if (copy.manual) void copy.copy(copy.manual) }}/>}
  </div>
}
