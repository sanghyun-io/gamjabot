import { invoke, isTauri } from '@tauri-apps/api/core'
import { useEffect, useRef, useState } from 'react'
import { Copy } from 'lucide-react'
import { useI18n } from '../i18n'
import { guiBuildCopyText, guiBuildInfo } from '../lib/guiBuildInfo'

export default function GuiBuildInfo({ version }: { version: string }) {
  const { t } = useI18n()
  const root = useRef<HTMLDetailsElement>(null), historyPending = useRef(false), historyIntent = useRef(0)
  const [historyStatus, setHistoryStatus] = useState<'idle' | 'opening' | 'sent' | 'failed'>('idle')
  const [status, setStatus] = useState<'idle' | 'sending' | 'copied' | 'failed'>('idle')
  const mounted = useRef(false), pending = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const copyText = guiBuildCopyText(version)
  const copy = async () => {
    if (!mounted.current || pending.current || !copyText) return
    pending.current = true; setStatus('sending')
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(copyText)
      if (mounted.current) setStatus('copied')
    } catch { if (mounted.current) setStatus('failed') }
    finally { pending.current = false }
  }
  const historyCurrent = () => mounted.current && !!root.current?.open && root.current.isConnected && !!root.current.getClientRects().length && !root.current.closest('[inert], [aria-hidden="true"]')
  const openHistory = async (thisBuild = false) => {
    if (!isTauri() || !historyCurrent() || historyPending.current || !document.hasFocus()) return
    const intent = ++historyIntent.current
    historyPending.current = true; setHistoryStatus('opening')
    try {
      await invoke('open_release_history', { guiBuild: thisBuild ? guiBuildInfo?.label : null })
      if (historyCurrent() && historyIntent.current === intent) setHistoryStatus('sent')
    } catch {
      if (historyCurrent() && historyIntent.current === intent) setHistoryStatus('failed')
    } finally {
      historyPending.current = false
      if (mounted.current && historyIntent.current !== intent) setHistoryStatus('idle')
    }
  }
  if (!guiBuildInfo) return null
  return <details ref={root} className="compact-details" onToggle={event => {
    if (event.target === event.currentTarget && !event.currentTarget.open) { historyIntent.current++; setHistoryStatus('idle') }
  }}>
    <summary>{t('buildInfo.title')} · {guiBuildInfo.label}</summary>
    <div className="stack stack--tight">
      <p className="field-hint">{t('buildInfo.help')}</p>
      <dl className="result-metadata">
        <dt>{t('buildInfo.version')}</dt><dd>{version}</dd>
        <dt>{t('buildInfo.label')}</dt><dd>{guiBuildInfo.label}</dd>
        <dt>{t('buildInfo.source')}</dt><dd><code style={{ overflowWrap: 'anywhere' }}>{guiBuildInfo.source}</code></dd>
      </dl>
      {isTauri() && <div className="stack stack--tight">
        <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={historyStatus === 'opening'} onClick={() => { void openHistory(true) }}>{t('buildInfo.historyThisBuild', { build: guiBuildInfo.label })}</button>
          <button type="button" className="text-link" disabled={historyStatus === 'opening'} onClick={() => { void openHistory() }}>{t(historyStatus === 'opening' ? 'buildInfo.historyOpening' : 'buildInfo.historyOpen')}</button></div>
        <p className="field-hint">{t('buildInfo.historyThisBuildHelp')}</p>
        <p className="field-hint">{t('buildInfo.historyScope')}</p>
        {historyStatus === 'sent' && <p role="status">{t('buildInfo.historySent')}</p>}
        {historyStatus === 'failed' && <p role="alert">{t('buildInfo.historyFailed')}</p>}
      </div>}
      <p className="field-hint">{t('buildInfo.copyScope')}</p>
      <button type="button" className="button button--secondary button--compact" disabled={status === 'sending'} onClick={() => { void copy() }}>
        <Copy aria-hidden="true" size={16} />{t(status === 'sending' ? 'buildInfo.copying' : 'buildInfo.copy')}
      </button>
      {status === 'copied' && <p role="status">{t('buildInfo.copied')}</p>}
      {status === 'failed' && <><p role="alert">{t('buildInfo.failed')}</p><label className="field">{t('buildInfo.manual')}<textarea readOnly rows={3} spellCheck={false} value={copyText} /></label></>}
    </div>
  </details>
}
