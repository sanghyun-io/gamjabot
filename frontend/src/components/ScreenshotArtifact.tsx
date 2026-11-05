import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Download, Minus, Plus, RotateCcw, Trash2, X } from 'lucide-react'
import { toolsApi } from '../api/tools'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { isTauri } from '@tauri-apps/api/core'
import { saveVerifiedBinaryFile } from '../lib/binaryFileSave'
import type { GeneratedSaveOutcome } from '../lib/generatedFileSave'

interface Props {
  roomId: number
  actionId: string
  eventSeq: number
  width: number
  height: number
  createdAt: string
  compact?: boolean
  readOnly?: boolean
  onRemoved?: () => void
}

export default function ScreenshotArtifact({ roomId, actionId, eventSeq, width, height, createdAt, compact = false, readOnly = false, onRemoved }: Props) {
  const keyboardHelp = useId()
  const host = useRef<HTMLDivElement>(null)
  const dialog = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLDivElement>(null), fullImage = useRef<HTMLImageElement>(null)
  const pendingCenter = useRef<{source: string; x: number; y: number} | null>(null)
  const [viewport, setViewport] = useState({width: 1, height: 1})
  const [naturalSize, setNaturalSize] = useState<{source: string; width: number; height: number} | null>(null)
  const confirmDialog = useRef<HTMLDivElement>(null)
  const cancelConfirmButton = useRef<HTMLButtonElement>(null)
  const deleteButton = useRef<HTMLButtonElement>(null)
  const confirmationWasOpen = useRef(false)
  const returnFocus = useRef<HTMLElement | null>(null)
  const objectUrl = useRef<string | null>(null)
  const imageBlob = useRef<Blob | null>(null), imageResource = useRef<string | null>(null), saveLock = useRef(false), viewerOpen = useRef(false)
  const mounted = useRef(false), deletingLock = useRef(false), liveReadOnly = useRef(readOnly)
  const owner = useRef({ id: useAuthStore.getState().user?.id, epoch: useAuthStore.getState().epoch })
  const resource = `${roomId}:${actionId}:${eventSeq}`
  const liveResource = useRef(resource); liveResource.current = resource
  const saveKey = `gamja.screenshot-save.${owner.current.id}.${owner.current.epoch}.${resource}`
  liveReadOnly.current = readOnly
  const canUse = () => {
    const auth = useAuthStore.getState()
    return mounted.current && !!auth.token && auth.user?.id === owner.current.id && auth.epoch === owner.current.epoch && !!host.current?.isConnected
  }
  const [readAttempt, setReadAttempt] = useState(0)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [visible, setVisible] = useState(false)
  const [src, setSrc] = useState<string | null>(null)
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'error' | 'removed'>('idle')
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveOutcome, setSaveOutcome] = useState<GeneratedSaveOutcome | null>(() => {
    try { return sessionStorage.getItem(saveKey) ? {phase:'unknown'} : null } catch { return {phase:'unknown'} }
  })
  const [zoom, setZoom] = useState(1)
  const [actualSource, setActualSource] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const pan = useRef<{pointerId: number; x: number; y: number; left: number; top: number; source: string} | null>(null)
  const endPan = useCallback(() => {
    const current = pan.current
    pan.current = null
    if (current && canvas.current?.hasPointerCapture(current.pointerId)) {
      try {canvas.current.releasePointerCapture(current.pointerId)} catch { /* Capture may already be lost. */ }
    }
    if (mounted.current) setDragging(false)
  }, [])
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteError, setDeleteError] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const { t, formatDateTime, formatNumber } = useI18n()

  const releaseUrl = () => {
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    objectUrl.current = null
    imageBlob.current = null
    imageResource.current = null
  }

  const priorResource = useRef(resource)
  useEffect(() => {
    if (priorResource.current !== resource) {
      priorResource.current = resource; viewerOpen.current = false; releaseUrl(); setSrc(null); setState('idle'); setOpen(false)
    }
    try { setSaveOutcome(sessionStorage.getItem(saveKey) ? {phase:'unknown'} : null) }
    catch { setSaveOutcome({phase:'unknown'}) }
  }, [resource, saveKey])

  useEffect(() => {
    const target = host.current
    if (!target) return
    if (!('IntersectionObserver' in window)) { setVisible(true); return }
    const observer = new IntersectionObserver((entries) => {
      setVisible(entries.some((entry) => entry.isIntersecting))
    }, { rootMargin: '200px' })
    observer.observe(target)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!visible || state !== 'idle' || !canUse() || deletingLock.current) return
    const controller = new AbortController()
    let disposed = false
    setState('loading')
    void toolsApi.screenshot(roomId, actionId, eventSeq, controller.signal).then((blob) => {
      if (disposed || !canUse()) return
      if (blob.type && blob.type !== 'image/png') throw new Error('Unexpected screenshot type')
      const next = URL.createObjectURL(blob)
      if (disposed || !canUse()) { URL.revokeObjectURL(next); return }
      objectUrl.current = next
      imageBlob.current = blob
      imageResource.current = resource
      setSrc(next)
      setState('ready')
    }).catch((error: { response?: { status?: number }; name?: string }) => {
      if (disposed || !canUse() || error?.name === 'CanceledError' || error?.name === 'AbortError') return
      setState(error?.response?.status === 410 ? 'removed' : 'error')
    })
    return () => { disposed = true; controller.abort() }
  }, [visible, roomId, actionId, eventSeq, readAttempt])

  useEffect(() => () => releaseUrl(), [])

  useEffect(() => {
    if (visible || open || state === 'removed' || deletingLock.current) return
    releaseUrl()
    setSrc(null)
    setState('idle')
  }, [visible, open, state, deleting])

  useEffect(() => {
    if (!open) return
    returnFocus.current = document.activeElement as HTMLElement | null
    const priorOverflow = document.body.style.overflow
    const background = Array.from(document.body.children).filter((node) => node !== dialog.current).map((node) => ({
      node, inert: node.hasAttribute('inert'), hidden: node.getAttribute('aria-hidden'),
    }))
    for (const item of background) { item.node.setAttribute('inert', ''); item.node.setAttribute('aria-hidden', 'true') }
    document.body.style.overflow = 'hidden'
    dialog.current?.focus()
    return () => {
      for (const item of background) {
        if (!item.inert) item.node.removeAttribute('inert')
        if (item.hidden === null) item.node.removeAttribute('aria-hidden')
        else item.node.setAttribute('aria-hidden', item.hidden)
      }
      document.body.style.overflow = priorOverflow
      returnFocus.current?.focus()
    }
  }, [open])

  useEffect(() => {
    if (!open) { confirmationWasOpen.current = false; return }
    if (confirmDelete) cancelConfirmButton.current?.focus({ preventScroll: true })
    else if (confirmationWasOpen.current) deleteButton.current?.focus()
    confirmationWasOpen.current = confirmDelete
  }, [open, confirmDelete])

  useLayoutEffect(() => {
    if (!open || !canvas.current) return
    const element = canvas.current
    const resize = () => {
      const style = getComputedStyle(element)
      const next = {
        width: Math.max(1, element.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0)),
        height: Math.max(1, element.clientHeight - (parseFloat(style.paddingTop) || 0) - (parseFloat(style.paddingBottom) || 0)),
      }
      setViewport(previous => previous.width === next.width && previous.height === next.height ? previous : next)
    }
    resize()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize)
    observer?.observe(element); window.addEventListener('resize', resize)
    return () => {observer?.disconnect(); window.removeEventListener('resize', resize)}
  }, [open])
  const imageSize = naturalSize?.source === src ? naturalSize : {width: width > 0 ? width : viewport.width, height: height > 0 ? height : viewport.height}
  const fit = Math.min(1, viewport.width / imageSize.width, viewport.height / imageSize.height)
  const naturalReady = !!src && naturalSize?.source === src
  const actualSize = naturalReady && actualSource === src
  const maxZoom = Math.max(3, naturalReady ? 1 / fit : 3)
  const currentZoom = actualSize ? 1 / fit : Math.min(maxZoom, zoom)
  const renderedWidth = Math.max(1, actualSize ? imageSize.width : Math.round(imageSize.width * fit * currentZoom))
  const renderedHeight = Math.max(1, actualSize ? imageSize.height : Math.round(imageSize.height * fit * currentZoom))
  const rememberCenter = () => {
    const area = canvas.current, image = fullImage.current
    if (area && image && src) {
      const imageBox = image.getBoundingClientRect(), areaBox = area.getBoundingClientRect()
      if (imageBox.width > 0 && imageBox.height > 0) pendingCenter.current = {
        source: src,
        x: Math.max(0, Math.min(1, (areaBox.left + area.clientWidth / 2 - imageBox.left) / imageBox.width)),
        y: Math.max(0, Math.min(1, (areaBox.top + area.clientHeight / 2 - imageBox.top) / imageBox.height)),
      }
    }
  }
  const changeZoom = (next: number) => {
    if (!canUse()) return
    next = Math.max(.5, Math.min(maxZoom, next))
    if (next === currentZoom && !actualSize) return
    rememberCenter(); setActualSource(null); setZoom(next)
  }
  const showActualSize = () => {
    if (!canUse() || !naturalReady || !src || actualSize) return
    rememberCenter(); setActualSource(src)
  }
  useLayoutEffect(() => {
    const requested = pendingCenter.current
    pendingCenter.current = null
    const area = canvas.current, image = fullImage.current
    if (!open || !canUse() || !requested || requested.source !== src || !area || !image) return
    const imageBox = image.getBoundingClientRect(), areaBox = area.getBoundingClientRect()
    area.scrollLeft += imageBox.left + imageBox.width * requested.x - (areaBox.left + area.clientWidth / 2)
    area.scrollTop += imageBox.top + imageBox.height * requested.y - (areaBox.top + area.clientHeight / 2)
  }, [zoom, actualSize, renderedWidth, renderedHeight, open, src])

  useEffect(() => {
    // Never reuse drag coordinates after the viewport, image, zoom or dialog changes.
    endPan()
    return endPan
  }, [open, confirmDelete, src, zoom, actualSize, viewport.width, viewport.height, endPan])
  const canPan = renderedWidth > viewport.width || renderedHeight > viewport.height
  const close = () => { viewerOpen.current = false; endPan(); pendingCenter.current = null; setOpen(false); setActualSource(null); setZoom(1); setConfirmDelete(false); setDeleteError(false) }
  const saveImage = async () => {
    const blob = imageBlob.current, source = objectUrl.current, savedResource = resource, key = saveKey
    if (!canUse() || !viewerOpen.current || !blob || !source || imageResource.current !== resource || deletingLock.current || saveLock.current || saveOutcome?.phase === 'unknown') return
    const current = () => canUse() && viewerOpen.current && liveResource.current === savedResource
      && imageBlob.current === blob && imageResource.current === savedResource && objectUrl.current === source && !deletingLock.current
    saveLock.current = true; setSaving(true); setSaveOutcome(null)
    let invoked = false, marked = false
    try {
      // Mark unresolved before starting so a remount cannot silently retry a write
      // whose response is lost. A native panel taking focus does not invalidate it.
      if (sessionStorage.getItem(key)) { setSaveOutcome({phase:'unknown'}); return }
      sessionStorage.setItem(key, 'pending')
      marked = true
      if (blob.type !== 'image/png' || blob.size > 10_000_000) throw Error('Invalid screenshot')
      const bytes = await blob.arrayBuffer()
      if (!current()) { sessionStorage.removeItem(key); return }
      const signature = [137,80,78,71,13,10,26,10]
      if (bytes.byteLength !== blob.size || bytes.byteLength < signature.length || !signature.every((value,index) => new Uint8Array(bytes)[index] === value)) throw Error('Invalid screenshot')
      // This digest protects these bytes through the save; it is not a server digest.
      const digest = await crypto.subtle.digest('SHA-256', bytes)
      if (!current()) { sessionStorage.removeItem(key); return }
      const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2,'0')).join('')
      invoked = true
      const outcome = await saveVerifiedBinaryFile(bytes, {filename:`screenshot-${actionId}.png`, size_bytes:bytes.byteLength, sha256}, t('screenshot.save'), current)
      if (outcome.phase !== 'unknown') sessionStorage.removeItem(key)
      if (canUse() && liveResource.current === savedResource) setSaveOutcome(outcome)
    } catch {
      let unknown = invoked || !marked
      if (!unknown) try { sessionStorage.removeItem(key) } catch { unknown = true }
      if (canUse() && liveResource.current === savedResource) setSaveOutcome({phase:unknown ? 'unknown' : 'failed'})
    } finally { saveLock.current = false; if (mounted.current) setSaving(false) }
  }
  const retryRead = () => {
    if (!canUse() || state !== 'error' || deletingLock.current) return
    releaseUrl();setSrc(null);setState('idle');setReadAttempt(value => value + 1)
  }
  const imageFailed = (source: string | null) => {
    if (!canUse() || !source || source !== objectUrl.current) return
    releaseUrl();setSrc(null);setState('error');close()
  }
  const remove = async () => {
    if (!canUse() || deletingLock.current || saveLock.current || liveReadOnly.current || !confirmDelete) return
    deletingLock.current = true;setDeleting(true);setDeleteError(false)
    try {
      await toolsApi.deleteScreenshot(roomId, actionId, eventSeq)
      if (!canUse()) return
      releaseUrl();setSrc(null);setState('removed');onRemoved?.();close()
    } catch {
      if (canUse()) setDeleteError(true)
    } finally {
      deletingLock.current = false
      if (mounted.current) setDeleting(false)
    }
  }
  const dimensions = width > 0 && height > 0 ? `${formatNumber(width)}×${formatNumber(height)}` : t('review.unavailable')
  const label = `${t('screenshot.viewer')} · ${dimensions}`
  useEffect(() => {
    if (!open) return
    const handleViewerKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' && event.key !== 'Tab') return
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229 || !canUse()
          || document.visibilityState !== 'visible' || !document.hasFocus()) return
      const scope = confirmDelete ? confirmDialog.current : dialog.current
      if (!scope?.isConnected || !scope.getClientRects().length || scope.closest('[hidden], [inert], [aria-hidden="true"]')) return
      const modals = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node =>
        node.isConnected && node.getClientRects().length > 0 && !node.closest('[hidden], [inert], [aria-hidden="true"]')
        && getComputedStyle(node).visibility !== 'hidden')
      if (modals.at(-1) !== scope) return
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation()
        // A held key must not cancel confirmation and then close its parent viewer.
        if (event.repeat) return
        if (confirmDelete) setConfirmDelete(false)
        else close()
        return
      }
      const controls = scope ? Array.from(scope.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], [tabindex="0"]')).filter(item => item.getClientRects().length > 0 && !item.closest('[hidden], [inert], [aria-hidden="true"]')) : []
      controls.sort((left, right) => left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1)
      if (!controls.length) return
      const first = controls[0]
      const last = controls[controls.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === scope
          || !scope?.contains(document.activeElement))) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === scope
          || !scope?.contains(document.activeElement))) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener('keydown', handleViewerKey)
    return () => document.removeEventListener('keydown', handleViewerKey)
  }, [open, confirmDelete])

  return <div ref={host} className="attachment stack stack--tight">
    {!compact && <time className="metadata" dateTime={createdAt}>{formatDateTime(createdAt)}</time>}
    {state === 'removed' && <p className="section-copy">{t('screenshot.removed')}</p>}
    {state === 'loading' && <p role="status" className="section-copy">{t('screenshot.loading')}</p>}
    {state === 'error' && <div className="stack stack--tight">
      <p role="status" className="section-copy">{t('screenshot.unavailable')}</p>
      <p className="field-hint">{t('screenshot.retryHelp')}</p>
      <button type="button" className="button button--secondary button--compact" disabled={deleting} onClick={retryRead}>{t('screenshot.retryRead')}</button>
    </div>}
    {deleting && !open && <p role="status">{t('screenshot.removing')}</p>}
    {deleteError && !open && <p role="alert" className="notice notice--danger">{t('screenshot.removeFailure')}</p>}
    {state === 'ready' && src && <button type="button" aria-label={label} disabled={deleting}
      className="attachment__thumb" onClick={() => { if (canUse() && !deletingLock.current) { viewerOpen.current = true; setOpen(true) } }}>
      <img src={src} alt={label} loading="lazy" onError={event => imageFailed(event.currentTarget.getAttribute('src'))}
        draggable={false} />
    </button>}
    {open && src && createPortal(<div ref={dialog} role="dialog" aria-modal="true" aria-label={t('screenshot.viewer')} tabIndex={-1}
      className="viewer"
      onMouseDown={(event) => { if (event.target === event.currentTarget) close() }}>
      <div aria-hidden={confirmDelete || undefined} className={`viewer__toolbar ${confirmDelete ? 'pointer-events-none' : ''}`}>
        <span className="metadata">{dimensions} · {formatNumber(Math.round(currentZoom * 100))}%</span>
        <p id={keyboardHelp} className="viewer__keyboard-help">{t('screenshot.keyboardHelp')} {t('screenshot.dragHelp')} {t('screenshot.originalSizeHelp')}</p>
        <div className="page-actions">
          <button type="button" aria-label={t('screenshot.zoomOut')} onClick={() => changeZoom(currentZoom - .25)}><Minus /></button>
          <button type="button" aria-label={t('screenshot.resetZoom')} onClick={() => changeZoom(1)}><RotateCcw /></button>
          <button type="button" aria-label={t('screenshot.zoomIn')} onClick={() => changeZoom(currentZoom + .25)}><Plus /></button>
          <button type="button" disabled={!naturalReady} aria-pressed={actualSize} title={t('screenshot.originalSizeHelp')} onClick={showActualSize}>{t('screenshot.originalSize')}</button>
          {isTauri() ? <button type="button" aria-label={t('screenshot.save')} disabled={saving || saveOutcome?.phase === 'unknown'} onClick={() => void saveImage()}><Download /></button>
            : <a aria-label={t('screenshot.save')} href={src} download={`screenshot-${actionId}.png`}><Download /></a>}
          {!readOnly && <button ref={deleteButton} type="button" aria-label={t('screenshot.delete')} className="viewer__danger" disabled={deleting || saving} onClick={() => { if (canUse() && !liveReadOnly.current && !deletingLock.current && !saveLock.current) setConfirmDelete(true) }}><Trash2 /></button>}
          <button type="button" aria-label={t('screenshot.close')} onClick={close}><X /></button>
        </div>
      </div>
      {saving && <p role="status">{t('generatedFile.saving')}</p>}
      {saveOutcome && <p role={saveOutcome.phase === 'failed' || saveOutcome.phase === 'unknown' ? 'alert' : 'status'}>{t(`generatedFile.save.${saveOutcome.phase}`)}{saveOutcome.phase === 'saved' && <> {saveOutcome.filename}{saveOutcome.receipt.durability_unconfirmed && <> {t('generatedFile.saveDurability')}</>}</>}</p>}
      {saveOutcome?.phase === 'unknown' && <button type="button" onClick={() => { if (!canUse()) return; try { sessionStorage.removeItem(saveKey); setSaveOutcome(null) } catch { /* Do not enable retry without clearing the marker. */ } }}>{t('generatedFile.saveChecked')}</button>}
      {confirmDelete && <div className="modal-backdrop">
        <div ref={confirmDialog} role="alertdialog" aria-modal="true" aria-label={t('screenshot.delete')} className="modal-panel stack stack--tight">
        <p>{t('screenshot.deleteConfirm')}</p>
        <div className="page-actions">
          <button ref={cancelConfirmButton} type="button" className="button button--secondary" disabled={deleting} onClick={() => setConfirmDelete(false)}>{t('common.cancel')}</button>
          <button type="button" aria-label={t('screenshot.confirmDelete')} className="button button--danger" disabled={deleting || readOnly} onClick={() => { void remove() }}>{deleting ? t('screenshot.removing') : t('screenshot.remove')}</button>
        </div>
        {readOnly && <p role="status" className="field-hint">{t('screenshot.readOnlyDelete')}</p>}
        {deleteError && <p role="alert" className="notice notice--danger">{t('screenshot.removeFailure')}</p>}
        </div>
      </div>}
      <div ref={canvas} role="region" tabIndex={confirmDelete ? -1 : 0} aria-label={t('screenshot.viewer')} aria-describedby={keyboardHelp}
        aria-hidden={confirmDelete || undefined} className={`viewer__canvas ${canPan ? 'viewer__canvas--pannable' : ''} ${dragging ? 'viewer__canvas--dragging' : ''} ${confirmDelete ? 'pointer-events-none' : ''}`}
        onPointerDown={event => {
          const area = event.currentTarget
          if (!canUse() || confirmDelete || !src || event.pointerType !== 'mouse' || event.button !== 0
            || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey
            || area.scrollWidth <= area.clientWidth && area.scrollHeight <= area.clientHeight) return
          try {area.setPointerCapture(event.pointerId)} catch {return}
          pan.current = {pointerId: event.pointerId, x: event.clientX, y: event.clientY, left: area.scrollLeft, top: area.scrollTop, source: src}
          area.focus({preventScroll: true}); setDragging(true)
          event.preventDefault(); event.stopPropagation()
        }}
        onPointerMove={event => {
          const current = pan.current
          if (!current || current.pointerId !== event.pointerId) return
          if (!canUse() || confirmDelete || current.source !== src || !(event.buttons & 1)) {endPan(); return}
          event.currentTarget.scrollLeft = current.left + current.x - event.clientX
          event.currentTarget.scrollTop = current.top + current.y - event.clientY
          event.preventDefault(); event.stopPropagation()
        }}
        onPointerUp={event => {if (pan.current?.pointerId === event.pointerId) endPan()}}
        onPointerCancel={event => {if (pan.current?.pointerId === event.pointerId) endPan()}}
        onLostPointerCapture={event => {if (pan.current?.pointerId === event.pointerId) endPan()}}
        onKeyDown={event => {
          if (event.target !== event.currentTarget || !canUse() || confirmDelete || event.defaultPrevented
            || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || event.altKey || event.ctrlKey || event.metaKey) return
          const directions: Record<string, [number, number]> = {ArrowLeft: [-48, 0], ArrowRight: [48, 0], ArrowUp: [0, -48], ArrowDown: [0, 48]}
          const direction = directions[event.key]
          if (direction) {
            event.preventDefault(); event.stopPropagation()
            event.currentTarget.scrollBy({left: direction[0], top: direction[1], behavior: 'auto'})
          } else if (['+', '=', '-', '_', '0', '1'].includes(event.key)) {
            event.preventDefault(); event.stopPropagation()
            if (event.key === '1') showActualSize()
            else changeZoom(event.key === '0' ? 1 : currentZoom + (event.key === '-' || event.key === '_' ? -.25 : .25))
          }
        }}>
        <div className="viewer__canvas-inner" style={{width: renderedWidth, height: renderedHeight}}>
          <img ref={fullImage} src={src} alt={label} draggable={false} onError={event => imageFailed(event.currentTarget.getAttribute('src'))}
            onLoad={event => {
              const image = event.currentTarget, source = image.getAttribute('src')
              if (canUse() && source && source === objectUrl.current && image.naturalWidth > 0 && image.naturalHeight > 0)
                setNaturalSize({source, width: image.naturalWidth, height: image.naturalHeight})
            }} className="viewer__image" style={{width: renderedWidth, height: renderedHeight}} />
        </div>
      </div>
    </div>, document.body)}
  </div>
}
