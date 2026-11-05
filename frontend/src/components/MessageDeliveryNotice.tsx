import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { eventsApi, type RoomEvent } from '../api/events'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import PendingMessageCopy from './PendingMessageCopy'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { roomEventCursor } from '../lib/eventCursor'
import { matchesPendingMessage, pendingRoomMessages, updatePendingMessage, type PendingRoomMessage } from '../lib/pendingRoomMessages'

function requestPreview(text: string) {
  const characters = Array.from(text.replace(/\s+/g, ' ').trim())
  return characters.slice(0, 120).join('') + (characters.length > 120 ? '…' : '')
}

export default function MessageDeliveryNotice({ items, ownerId, epoch, generation, busy, onEvents, onRetry, onPark, onFinish, onView, onFocusComposer, onResolveRoutine, canRetry = () => true }: {
  items: readonly PendingRoomMessage[]; ownerId: number; epoch: number; generation: number; busy: boolean;
  canRetry?: (item: PendingRoomMessage) => boolean; onEvents: (batch: RoomEvent[]) => void; onRetry: (item: PendingRoomMessage) => void;
  onResolveRoutine?: (item:PendingRoomMessage)=>Promise<'accepted'|'pending'|'rejected'>;
  onPark: (item: PendingRoomMessage) => Promise<void>; onFinish: (item: PendingRoomMessage) => void; onView: (event: RoomEvent) => void; onFocusComposer: () => void;
}) {
  const { t,locale } = useI18n(), parkedListId = useId()
  const [parkedOpen, setParkedOpen] = useState(false), [query, setQuery] = useState('')
  const alive = useRef(true), lock = useRef(false)
  const panelRef = useRef<HTMLElement>(null)
  const focusRef = useRef<{ key: string | null; element: HTMLElement } | null>(null)
  const parkedRef = useRef<HTMLDetailsElement>(null)
  const [working, setWorking] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ key: string; kind: 'absent' | 'failure' | 'parkFailure' | 'routinePending' } | null>(null)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const current = () => { const auth = useAuthStore.getState(); return alive.current && auth.user?.id === ownerId && auth.epoch === epoch }
  useEffect(() => {
    if (!items.length) return
    const leave = (event: Event) => {
      if (event.target instanceof Node && !panelRef.current?.contains(event.target)) focusRef.current = null
    }
    const focusElsewhere = (event: FocusEvent) => { if (event.target !== document.body) leave(event) }
    const blur = () => { focusRef.current = null }
    document.addEventListener('pointerdown', leave, true)
    document.addEventListener('keydown', leave, true)
    document.addEventListener('focusin', focusElsewhere, true)
    window.addEventListener('blur', blur)
    return () => {
      document.removeEventListener('pointerdown', leave, true)
      document.removeEventListener('keydown', leave, true)
      document.removeEventListener('focusin', focusElsewhere, true)
      window.removeEventListener('blur', blur)
    }
  }, [Boolean(items.length)])
  useLayoutEffect(() => {
    const previous = focusRef.current
    if (!previous || !current()) return
    if (document.visibilityState !== 'visible' || !document.hasFocus()) { focusRef.current = null; return }
    const active = document.activeElement
    if (active && active !== document.body && active !== previous.element) {
      if (!panelRef.current?.contains(active)) focusRef.current = null
      return
    }
    const visible = (node: HTMLElement) => node.isConnected && node.getClientRects().length > 0 && !node.matches(':disabled')
    const focus = (node: HTMLElement) => {
      node.focus({ preventScroll: true })
      const panel = panelRef.current
      if (!panel) return
      const rect = node.getBoundingClientRect(), boundary = panel.getBoundingClientRect()
      if (rect.top < boundary.top) panel.scrollTop += rect.top - boundary.top
      else if (rect.bottom > boundary.bottom) panel.scrollTop += rect.bottom - boundary.bottom
    }
    if (visible(previous.element)) { if (active !== previous.element) focus(previous.element); return }
    const row = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('[data-delivery-key]') || []).find(node => node.dataset.deliveryKey === previous.key)
    if (working === previous.key && row) return
    const next = row && Array.from(row.querySelectorAll<HTMLElement>('button, summary')).find(visible)
    if (next) { focus(next); return }
    const summary = parkedRef.current?.querySelector<HTMLElement>(':scope > summary')
    if (summary && visible(summary)) { focus(summary); return }
    focusRef.current = null
    onFocusComposer()
  }, [items, working, busy, onFocusComposer])

  const check = async (item: PendingRoomMessage) => {
    if (lock.current || !current()) return
    lock.current = true; setWorking(item.key); setNotice(null)
    try {
      if(item.routineRef&&onResolveRoutine){const state=await onResolveRoutine(item);if(current()&&state==='pending')setNotice({key:item.key,kind:'routinePending'});return}
      const cached = roomEventCursor.events(item.roomId).find(event => event.idempotency_key === item.key)
      const accept = (event: RoomEvent) => {
        const matches = matchesPendingMessage(item, event, ownerId)
        updatePendingMessage(item.identity, item.roomId, item.key, matches ? { status: 'confirmed', receipt: event } : { mismatch: true, receipt: event }, generation)
      }
      if (cached) { accept(cached); return }
      let after = item.afterSeq
      for (let page = 0; page < 10; page++) {
        const batch = await eventsApi.list(item.roomId, after, 100)
        if (!current()) return
        if (batch.some((event, index) => event.room_id !== item.roomId || event.seq !== after + index + 1)) throw new Error('Invalid history page')
        onEvents(batch)
        const found = batch.find(event => event.idempotency_key === item.key)
        if (found) { accept(found); return }
        if (batch.length) after = batch[batch.length - 1].seq
        updatePendingMessage(item.identity, item.roomId, item.key, { afterSeq: after }, generation)
        if (batch.length < 100) break
      }
      if (current()) setNotice({ key: item.key, kind: 'absent' })
    } catch { if (current()) setNotice({ key: item.key, kind: 'failure' }) }
    finally { lock.current = false; if (current()) setWorking(null) }
  }
  const park = async (item: PendingRoomMessage, trigger: HTMLButtonElement) => {
    if (lock.current || busy || !current()) return
    const accepted = window.confirm(t('chat.delivery.parkConfirm'))
    if (!current()) return
    // Native confirm may blur the WebView; restore only this explicit action's focus intent.
    if (document.hasFocus() && (document.activeElement === document.body || document.activeElement === trigger)) {
      focusRef.current = { key: item.key, element: trigger }
      if (!accepted) trigger.focus({ preventScroll: true })
    }
    if (!accepted) return
    lock.current = true; setWorking(item.key); setNotice(null)
    try { await onPark(item) }
    catch { if (current()) setNotice({ key: item.key, kind: 'parkFailure' }) }
    finally { lock.current = false; if (current()) setWorking(null) }
  }
  const viewReceipt = (item: PendingRoomMessage) => {
    if (!current()) return
    const live = pendingRoomMessages(item.identity, item.roomId).find(record => record.key === item.key)
    if (live?.receipt && matchesPendingMessage(live, live.receipt, ownerId)) onView(live.receipt)
  }
  const actionable = items.filter(item => !item.parked || item.status === 'sending' || item.status === 'confirmed' && item.settlementFailed)
    .sort((a, b) => Number(a.parked) - Number(b.parked))
  const parked = items.filter(item => !actionable.includes(item))
  const match = directoryMatcher(query)
  const found = parkedOpen && query.trim() ? parked.filter(item => match([item.content])) : parked
  const foundKeys = new Set(found.map(item => item.key))
  const renderItem = (item: PendingRoomMessage, hidden = false) => <div hidden={hidden} style={hidden ? { display: 'none' } : undefined} key={item.key} data-delivery-key={item.key} className="message-delivery__item stack stack--tight">
      <p role="status">{item.routineRef&&item.status==='unknown'?(locale==='ko'?'메시지 접수 여부를 확인하지 못했습니다. 기록을 확인하거나 같은 요청을 다시 보낼 수 있습니다.':'Message receipt is unconfirmed. Check the record or retry the same request.'):t(item.status === 'confirmed' ? 'chat.delivery.confirmed' : item.status === 'sending' ? 'chat.sending' : item.parked ? 'chat.delivery.parked' : item.replyTo ? 'reply.unknown' : 'chat.sendUnconfirmed')}</p>
      {item.parked && <p className="metadata">{t('chat.delivery.preview', { text: requestPreview(item.content) })}</p>}
      <details><summary>{t('chat.delivery.original')}</summary><pre className="technical">{item.content}</pre><PendingMessageCopy item={item} ownerId={ownerId} epoch={epoch} generation={generation}/><p className="metadata">{t('chat.delivery.requestType')}: {t(item.outputKind === 'auto' ? 'localModels.autoText' : item.outputKind === 'text' ? 'localModels.textOnly' : item.outputKind === 'image_generation' ? 'chat.delivery.imageRequest' : 'chat.delivery.speechRequest')}</p><p className="metadata">{t('chat.delivery.requestTypeHelp')}</p></details>
      {item.status === 'unknown' && item.replyTo && !canRetry(item) && <p className="metadata">{t('reply.stopChanged')}</p>}
      {item.mismatch && <p role="alert">{t('chat.delivery.mismatch')}</p>}
      <div className="inline-actions">
        {item.receipt && matchesPendingMessage(item, item.receipt, ownerId) && <button type="button" className="button button--secondary button--compact" onClick={() => viewReceipt(item)}>{t('chat.delivery.view')}</button>}
        {item.status === 'confirmed' && item.settlementFailed && <button type="button" className="button button--secondary button--compact" disabled={busy || !!working} onClick={() => onFinish(item)}>{t('chat.delivery.finish')}</button>}
        {item.status === 'unknown' && <>
          <button type="button" className="button button--secondary button--compact" disabled={!!working} onClick={() => { void check(item) }}>{working === item.key ? t('common.loading') : t('chat.delivery.check')}</button>
          <button type="button" className="button button--secondary button--compact" disabled={busy || !!working || item.mismatch || !canRetry(item) || items.some(other => other.status === 'sending' || other.key !== item.key && !other.parked && other.status === 'unknown')} onClick={() => {
            const currentItem = pendingRoomMessages(item.identity, item.roomId).find(record => record.key === item.key)
            if (current() && currentItem?.status === 'unknown' && !currentItem.mismatch && canRetry(currentItem)) onRetry(currentItem)
          }}>{t('chat.delivery.retry')}</button>
          {!item.parked && !item.routineRef && <button type="button" className="button button--ghost button--compact" disabled={busy || !!working} onClick={(event) => { void park(item, event.currentTarget) }}>{t('chat.delivery.park')}</button>}
        </>}
      </div>
      {notice?.key === item.key && <p role="status" className="metadata">{notice.kind==='routinePending'?(locale==='ko'?'접수 여부를 아직 확인할 수 없습니다. 같은 요청을 다시 확인하거나 같은 요청을 재전송할 수 있습니다.':'The request remains unconfirmed. Check again or retry the same request.') :t(notice.kind === 'absent' ? 'chat.delivery.absent' : notice.kind === 'failure' ? 'chat.delivery.checkFailure' : 'chat.delivery.parkFailure')}</p>}
    </div>
  if (!items.length) return null
  return <section ref={panelRef} className="message-delivery notice notice--warning" aria-label={t('chat.delivery.title')}
    onFocusCapture={(event) => { const element = event.target as HTMLElement; focusRef.current = { key: element.closest<HTMLElement>('[data-delivery-key]')?.dataset.deliveryKey || null, element } }}
    onBlurCapture={(event) => {
      const destination = event.relatedTarget
      const source = event.target
      // Disabling/removing a focused control may blur to the document body before
      // layout recovery. Explicit outside interaction and window blur still cancel.
      const controlBecameUnavailable = source instanceof HTMLElement
        && (!source.isConnected || source.matches(':disabled') || source.getClientRects().length === 0)
      const transientDocumentFocus = controlBecameUnavailable
        && (destination === document.body || destination === document.documentElement)
      if (destination instanceof Node && !panelRef.current?.contains(destination) && !transientDocumentFocus) focusRef.current = null
    }}>
    <div className="message-delivery__heading"><strong>{t('chat.delivery.title')}</strong><span className="metadata" role="status">{t('chat.delivery.counts', { active: actionable.length, parked: parked.length })}</span></div>
    {actionable.length > 0 && <div className="message-delivery__items">{actionable.map(item => renderItem(item))}</div>}
    {parked.length > 0 && <details ref={parkedRef} className="message-delivery__parked" open={parkedOpen}
      onToggle={event => { if (event.target === event.currentTarget && current()) setParkedOpen(event.currentTarget.open) }}>
      <summary>{t('chat.delivery.parkedList', { count: parked.length })}</summary>
      {parkedOpen && <div className="stack stack--tight">
        <DirectorySearch value={query} onChange={value => { if (current()) setQuery(value) }} label={t('chat.delivery.findParked')}
          hint={t('chat.delivery.findParked')} shown={found.length} total={parked.length} known loading={false} controls={parkedListId} maxLength={200} preventSubmit/>
        <p className="field-hint">{t('chat.delivery.findScope')}</p>
        {!found.length && <p role="status" className="metadata">{t('directory.noMatches')}</p>}
      </div>}
      <div id={parkedListId} className="message-delivery__items">{parked.map(item => renderItem(item, !foundKeys.has(item.key)))}</div>
    </details>}
    <details className="message-delivery__help"><summary>{t('chat.delivery.details')}</summary><p className="metadata">{t('chat.delivery.session')}</p></details>
  </section>
}
