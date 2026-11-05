import {useMemo, useRef, useState} from 'react'
import type {RoomEvent} from '../api/events'
import {useI18n} from '../i18n'
import {useClipboardText} from '../lib/useClipboardText'
import ClipboardTextFallback from './ClipboardTextFallback'

export default function FailureReference({event, canUse}: {event: RoomEvent; canUse: () => boolean}) {
  const {t} = useI18n(), root = useRef<HTMLDetailsElement>(null), manualTrigger = useRef<HTMLButtonElement>(null)
  const [expanded, setExpanded] = useState(false)
  const targetValid = event.kind === 'failure' && Number.isSafeInteger(event.room_id) && event.room_id > 0
    && Number.isSafeInteger(event.seq) && event.seq > 0 && typeof event.event_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(event.event_id)
  const code = typeof event.payload.code === 'string' && /^[a-z][a-z0-9_]{0,79}$/.test(event.payload.code) ? event.payload.code : null
  const diagnostic = event.payload.diagnostic && typeof event.payload.diagnostic === 'object' ? event.payload.diagnostic as Record<string, unknown> : null
  const reference = typeof diagnostic?.correlation_id === 'string' && /^[0-9a-f]{32}$/.test(diagnostic.correlation_id) ? diagnostic.correlation_id : null
  const source = `/chat/${event.room_id}?${new URLSearchParams({seq: String(event.seq), event: event.event_id})}`
  // Explicit allowlist: no message/content, actor names, provider URLs or raw diagnostics.
  const text = targetValid ? JSON.stringify({room_id: event.room_id, seq: event.seq, event_id: event.event_id,
    ...(code ? {code} : {}), ...(reference ? {correlation_id: reference} : {}), source}, null, 2) : ''
  const context = useMemo(() => ({}), [text])
  const valid = () => targetValid && canUse() && !!root.current?.open && !!root.current.isConnected && !!root.current.getClientRects().length && !root.current.closest('[inert], [aria-hidden="true"]')
  const copy = useClipboardText<{text: string}>(context, item => valid() && item.text === text)
  const closeManual = () => {
    if (!copy.manualAllowed()) return
    copy.closeManual()
    if (valid() && manualTrigger.current?.isConnected) manualTrigger.current.focus({preventScroll: true})
  }
  if (!targetValid) return null
  return <details ref={root} className="compact-details failure-reference" onToggle={event => {
    if (event.target !== event.currentTarget) return
    const open = event.currentTarget.open
    if (open && !canUse()) {event.currentTarget.open = false; setExpanded(false); copy.invalidate(); return}
    setExpanded(open)
    if (!open) copy.invalidate()
  }}>
    <summary>{t('chat.failureReference.title')}</summary>
    {expanded && <div className="stack stack--tight">
    <p className="field-hint">{t('chat.failureReference.help')}</p>
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={copy.busy} onClick={() => void copy.copy({text})}>{t(copy.busy ? 'common.loading' : 'chat.failureReference.copy')}</button>
      <button ref={manualTrigger} type="button" className="text-link" onClick={() => copy.openManual({text})}>{t('chat.failureReference.manual')}</button>
    </div>
    {copy.result && <p role={copy.result.status === 'copied' ? 'status' : 'alert'} className="metadata">{t(copy.result.status === 'copied' ? 'chat.failureReference.copied' : 'chat.clipboard.failure')}</p>}
    {copy.manual && <ClipboardTextFallback text={copy.manual.text} label={t('chat.failureReference.title')} caption={t('chat.failureReference.help')}
      canUse={copy.manualAllowed} onClose={closeManual} onInteract={copy.touchManual} busy={copy.busy}
      onRetry={() => {if (copy.manual) void copy.copy(copy.manual)}} rows={7}/>}
    </div>}
  </details>
}
