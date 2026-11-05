import { useLayoutEffect, useRef, useState } from 'react'
import { eventsApi, sameReply, validReply, type ReplyReference, type ReplyThreadItem, type RoomEvent } from '../api/events'
import { assistantVisibleMarkdown } from '../lib/assistantContent'
import { useI18n } from '../i18n'

export default function MessageThread({ roomId, ownerId, root, canUse, canReply, onOpen, onReply }: {
  roomId: number; ownerId: number; root: ReplyReference; canUse: () => boolean
  onOpen: (seq: number, id: string) => void; canReply?: (item: RoomEvent) => boolean; onReply?: (item?: RoomEvent) => void
}) {
  const { t } = useI18n(), alive = useRef(true), request = useRef(0), lock = useRef<number | null>(null)
  const [open, setOpen] = useState(false), [items, setItems] = useState<ReplyThreadItem[]>([])
  const [upper, setUpper] = useState<number | null>(null), [next, setNext] = useState<number | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(false)
  const [depthLimited, setDepthLimited] = useState(false)
  useLayoutEffect(() => { alive.current = true; return () => { alive.current = false; request.current++ } }, [])
  const current = (generation: number) => alive.current && request.current === generation && canUse()
  const read = async (more: boolean) => {
    if (!canUse() || lock.current !== null || more && (upper === null || next === null)) return
    const generation = ++request.current, after = more ? next! : 0, expectedUpper = more ? upper! : null
    lock.current = generation; setBusy(true); setError(false)
    try {
      const page = await eventsApi.thread(roomId, root, more ? { afterSeq: after, upperSeq: expectedUpper! } : undefined)
      if (!current(generation)) return
      if (!page || page.version !== 2 || !sameReply(page.root, root) || !Number.isSafeInteger(page.upper_seq) || page.upper_seq < root.seq
        || expectedUpper !== null && page.upper_seq !== expectedUpper || !Array.isArray(page.items) || page.items.length > 20 || typeof page.has_more !== 'boolean'
        || typeof page.depth_limited !== 'boolean' || expectedUpper !== null && page.depth_limited !== depthLimited
        || (page.has_more ? !Number.isSafeInteger(page.next_after_seq) || page.next_after_seq !== page.items.at(-1)?.event?.seq : page.next_after_seq !== null)) throw new Error('Invalid reply page')
      let previous = Math.max(after, root.seq)
      const ancestors = new Map<string, { seq: number; depth: number }>([[root.event_id, { seq: root.seq, depth: 0 }]])
      if (more) for (const item of items) ancestors.set(item.event.event_id, { seq: item.event.seq, depth: item.depth })
      for (const item of page.items) {
        const event = item?.event, parent = item?.parent
        const ancestor = validReply(parent) ? ancestors.get(parent.event_id) : undefined
        if (!event || event.room_id !== roomId || !validReply({ event_id: event.event_id, seq: event.seq }) || event.seq <= previous || event.seq > page.upper_seq
          || ancestors.has(event.event_id) || event.kind !== 'message' || !(event.actor_type === 'user' && event.actor_id === ownerId || event.actor_type === 'agent' && Number.isSafeInteger(event.actor_id) && event.actor_id! > 0)
          || !ancestor || ancestor.seq !== parent.seq || parent.seq >= event.seq || !Number.isSafeInteger(item.depth) || item.depth < 1 || item.depth > 64 || item.depth !== ancestor.depth + 1
          || !sameReply(event.payload?.reply_to as ReplyReference, parent) || typeof event.payload?.content !== 'string') throw new Error('Invalid reply')
        ancestors.set(event.event_id, { seq: event.seq, depth: item.depth }); previous = event.seq
      }
      setItems(old => more ? [...old, ...page.items] : page.items); setUpper(page.upper_seq); setNext(page.next_after_seq); setDepthLimited(page.depth_limited)
    } catch { if (current(generation)) setError(true) }
    finally { if (lock.current === generation) lock.current = null; if (current(generation)) setBusy(false) }
  }
  const toggle = () => {
    if (!canUse()) return
    if (open) { request.current++; lock.current = null; setBusy(false); setOpen(false); setItems([]); setUpper(null); setNext(null); setError(false); setDepthLimited(false); return }
    setOpen(true); void read(false)
  }
  return <section className="stack stack--tight">
    <button type="button" className="text-link" style={{ justifySelf: 'start', textAlign: 'left' }} aria-expanded={open} onClick={toggle}>{t(open ? 'thread.close' : 'thread.open')}</button>
    {open && <div className="stack stack--tight">
      <p className="metadata">{t('thread.flowHelp')}</p>
      {busy && <p role="status">{t('thread.loading')}</p>}
      {error && <p role="alert">{t('thread.error')} <button type="button" className="text-link" disabled={busy} onClick={() => void read(upper !== null)}>{t('thread.retry')}</button></p>}
      {!busy && !error && upper !== null && !items.length && <p>{t('thread.empty')}</p>}
      {items.map(({ event, parent, depth }) => <article key={event.event_id} className="stack stack--tight" style={{ minWidth: 0, marginInlineStart: Math.min(depth - 1, 3) * 12, borderInlineStart: depth > 1 ? '2px solid var(--line)' : undefined, paddingInlineStart: depth > 1 ? 10 : undefined }}>
        <strong>{event.actor_name_snapshot || t('thread.author')}</strong>
        {depth > 1 && <button type="button" className="text-link metadata" style={{ justifySelf: 'start', textAlign: 'left' }} onClick={() => { if (canUse()) onOpen(parent.seq, parent.event_id) }}>{t('thread.goToParent')}</button>}
        <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{event.actor_type === 'agent' ? assistantVisibleMarkdown(event.payload.content as string) : event.payload.content as string}</div>
        <div className="page-actions">
          <button type="button" className="text-link" onClick={() => { if (canUse()) onOpen(event.seq, event.event_id) }}>{t('thread.goToReply')}</button>
          {onReply && <button type="button" className="text-link" disabled={!!canReply && !canReply(event)} onClick={() => { if (canUse() && (!canReply || canReply(event))) onReply(event) }}>{t('thread.replyToReply')}</button>}
        </div>
      </article>)}
      {next !== null && !error && <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={() => void read(true)}>{t('thread.more')}</button>}
      {depthLimited && <p role="status" className="metadata">{t('thread.depthLimit')}</p>}
      {onReply && <button type="button" className="text-link" style={{ justifySelf: 'start', textAlign: 'left' }} onClick={() => { if (canUse()) onReply() }}>{t('thread.reply')}</button>}
    </div>}
  </section>
}
