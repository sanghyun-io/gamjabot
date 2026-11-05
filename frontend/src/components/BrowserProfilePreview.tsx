import { useId, useLayoutEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'

export type ProfilePreview = Readonly<{ id: string; url: string; received: string }>
export default function BrowserProfilePreview({ preview, stale, pending, canCapture, onCapture, onClose }: {
  preview: ProfilePreview; stale: boolean; pending: boolean; canCapture: boolean; onCapture: () => void; onClose: () => void
}) {
  const { t, formatDateTime } = useI18n(), [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [actual, setActual] = useState(false), [size, setSize] = useState<{ width: number; height: number } | null>(null)
  const viewport = useRef<HTMLDivElement>(null), imageId = useId(), helpId = useId()
  const savedPosition = useRef({ left: 0, top: 0 }), focusIntent = useRef<{ source: string; origin: HTMLButtonElement } | null>(null)
  const toggleSize = (origin: HTMLButtonElement) => {
    if (phase !== 'ready' || !size || !origin.isConnected) return
    origin.focus({ preventScroll: true })
    if (actual) {
      if (viewport.current) savedPosition.current = { left: viewport.current.scrollLeft, top: viewport.current.scrollTop }
      focusIntent.current = null; setActual(false)
    } else { focusIntent.current = { source: preview.url, origin }; setActual(true) }
  }
  useLayoutEffect(() => {
    const intent = focusIntent.current; focusIntent.current = null
    const target = viewport.current
    if (!actual || phase !== 'ready' || !intent || intent.source !== preview.url || !target?.isConnected
      || !target.getClientRects().length || target.closest('[hidden], [inert], [aria-hidden="true"]')) return
    target.scrollLeft = savedPosition.current.left; target.scrollTop = savedPosition.current.top
    if (document.visibilityState === 'visible' && document.hasFocus() && document.activeElement === intent.origin) target.focus({ preventScroll: true })
  }, [actual, phase, preview.url])
  return <figure className="browser-profile-preview stack stack--tight">
    <figcaption className="metadata">{t(stale || phase === 'error' ? 'browserProfiles.stale' : 'browserProfiles.snapshot')} · {formatDateTime(preview.received, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</figcaption>
    {phase === 'loading' && <p role="status">{t('browserProfiles.previewPanel.loading')}</p>}
    {phase === 'error' && <p role="alert" className="notice notice--warning">{t('browserProfiles.previewPanel.decodeFailed')}</p>}
    <div ref={viewport} id={imageId} role={actual ? 'region' : undefined} tabIndex={actual ? 0 : undefined} aria-label={actual ? t('browserProfiles.previewAlt') : undefined} aria-describedby={actual ? helpId : undefined}
      style={{ minWidth: 0, maxHeight: actual ? 'min(65dvh, 720px)' : undefined, overflow: actual ? 'auto' : 'hidden', overscrollBehavior: 'contain' }}
      onKeyDown={event => {
        // Preserve native scrolling, without forwarding reading keys to room controls.
        if (actual && event.target === event.currentTarget && !event.altKey && !event.ctrlKey && !event.metaKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229
          && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) event.stopPropagation()
      }}>
      <img src={preview.url} alt={t('browserProfiles.previewAlt')} hidden={phase === 'error'} draggable={false}
        style={{ display: phase === 'error' ? 'none' : 'block', width: actual && size ? size.width : '100%', height: actual && size ? size.height : 'auto', boxSizing: actual ? 'content-box' : undefined, maxWidth: actual ? 'none' : undefined, maxHeight: actual ? 'none' : undefined }}
        onLoad={event => { const image = event.currentTarget; if (image.naturalWidth > 0 && image.naturalHeight > 0) { setSize({ width: image.naturalWidth, height: image.naturalHeight }); setPhase('ready') } else { focusIntent.current = null; setActual(false); setPhase('error') } }}
        onError={() => { focusIntent.current = null; setActual(false); setPhase('error') }}/>
    </div>
    {phase === 'ready' && <><div><button type="button" className="button button--secondary button--compact" aria-pressed={actual} aria-controls={imageId} onClick={event => toggleSize(event.currentTarget)}>{t(actual ? 'browserProfiles.previewPanel.fit' : 'browserProfiles.previewPanel.actual')}</button></div>
      <p id={helpId} className="field-hint">{t('browserProfiles.previewPanel.panHelp')}</p></>}
    {pending && <p role="status" className="metadata">{t('browserProfiles.previewPanel.pending')}</p>}
    <p className="field-hint">{t('browserProfiles.previewPanel.snapshotHelp')}</p>
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={!canCapture} onClick={onCapture}>{t('browserProfiles.previewPanel.capture')}</button>
      <button type="button" className="button button--secondary button--compact" onClick={onClose}>{t('browserProfiles.previewPanel.close')}</button>
    </div>
    {pending && <p className="field-hint">{t('browserProfiles.previewPanel.closeHelp')}</p>}
  </figure>
}
