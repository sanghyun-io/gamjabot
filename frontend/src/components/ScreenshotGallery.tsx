import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Image, Images } from 'lucide-react'
import { artifactsApi, type ScreenshotArtifactItem } from '../api/artifacts'
import type { RoomEvent } from '../api/events'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { closeOtherRoomPanels } from '../lib/roomPanels'
import ScreenshotArtifact from './ScreenshotArtifact'

interface Props {
  roomId: number
  ownerId: number
  authEpoch: number
  readOnly: boolean
  knownEvents?: readonly Pick<RoomEvent, 'room_id' | 'seq'>[]
  historyComplete?: boolean
  onNavigateSeq: (seq: number) => void
}
type Intent = { cursor: number; trail: number[] }
type ReceivedPage = Intent & { next: number | null }
const EVENT_LIMIT = 50

export default function ScreenshotGallery({ roomId, ownerId, authEpoch, readOnly, knownEvents = [], historyComplete = false, onNavigateSeq }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [items, setItems] = useState<ScreenshotArtifactItem[]>([])
  const [page, setPage] = useState<ReceivedPage | null>(null)
  const [failed, setFailed] = useState<Intent | null>(null)
  const [state, setState] = useState<'idle'|'loading'|'ready'|'error'>('idle')
  const body = useRef<HTMLDivElement>(null), scrollIntent = useRef(false)
  const panel = useRef<HTMLDetailsElement>(null), mounted = useRef(false)
  const generation = useRef(0), pending = useRef<number | null>(null)
  const identity = `${ownerId}:${authEpoch}:${roomId}`
  const identityRef = useRef(identity);identityRef.current = identity
  const { t } = useI18n()
  const current = () => {
    const auth = useAuthStore.getState()
    return mounted.current && !!auth.token && identity === identityRef.current && identity === `${auth.user?.id || 0}:${auth.epoch}:${roomId}`
  }

  useEffect(() => {
    mounted.current = true;generation.current++;pending.current = null
    if (panel.current) panel.current.open = false
    setExpanded(false);setItems([]);setPage(null);setFailed(null);setState('idle')
    return () => { mounted.current = false;generation.current++;pending.current = null }
  }, [identity])

  useLayoutEffect(() => {
    if (scrollIntent.current && current() && panel.current?.open && body.current) body.current.scrollTop = 0
    scrollIntent.current = false
  }, [page])

  const load = async (intent: Intent) => {
    if (!current() || !panel.current?.open || pending.current !== null) return
    const run = ++generation.current, sentIdentity = identity
    // Navigation keeps only cursor numbers for the visited path, never image
    // bytes or previous pages. A failed request does not advance this path.
    const target = { cursor: intent.cursor, trail: [...intent.trail] }
    pending.current = run;setState('loading');setFailed(null)
    const valid = () => current() && panel.current?.open && generation.current === run && identityRef.current === sentIdentity
    try {
      const value = await artifactsApi.list(roomId, target.cursor, EVENT_LIMIT)
      if (!valid()) return
      if (value.room_id !== roomId || value.after_seq !== target.cursor || !Array.isArray(value.items) || value.items.length > EVENT_LIMIT
        || value.next_after_seq !== null && (!Number.isSafeInteger(value.next_after_seq) || value.next_after_seq <= target.cursor)) throw new Error('Gallery page unavailable')
      setItems([...new Map(value.items.map(item => [`${item.event_seq}:${item.action_id}`, item])).values()].sort((a,b) => b.event_seq-a.event_seq))
      scrollIntent.current = page?.cursor !== target.cursor
      setPage({ ...target, next: value.next_after_seq });setState('ready')
    } catch {
      if (valid()) { setFailed(target);setState('error') }
    } finally {
      if (pending.current === run) pending.current = null
    }
  }
  const close = () => {
    if (panel.current) panel.current.open = false
    generation.current++;pending.current = null;scrollIntent.current = false
    setExpanded(false);setItems([]);setPage(null);setFailed(null);setState('idle')
  }
  const navigate = (seq: number) => { if (current()) { close();onNavigateSeq(seq) } }
  // Only an explicit open/recent action builds the numeric cursor trail.
  // The canonical Chat events are already loaded; no bodies are copied or
  // additional history scan/automatic image capture is requested here.
  const recentIntent = (): Intent => {
    const trail = [0]
    if (!historyComplete) return { cursor: 0, trail }
    let previousSeq = 0
    for (let index=0; index<knownEvents.length; index++) {
      const event = knownEvents[index]
      if (event.room_id !== roomId || !Number.isSafeInteger(event.seq) || event.seq <= previousSeq) return { cursor:0, trail:[0] }
      if (index > 0 && index % EVENT_LIMIT === 0) trail.push(previousSeq)
      previousSeq = event.seq
    }
    return { cursor:trail[trail.length-1], trail }
  }
  const recentStartIndex = Math.floor(Math.max(0,knownEvents.length-1)/EVENT_LIMIT)*EVENT_LIMIT
  const knownRecentCursor = historyComplete && recentStartIndex > 0 ? knownEvents[recentStartIndex-1]?.seq : 0

  const previous = () => {
    if (!page || page.trail.length < 2) return
    const trail = page.trail.slice(0,-1)
    void load({ cursor: trail[trail.length-1], trail })
  }
  const next = () => { if (page?.next !== null && page?.next !== undefined) void load({ cursor: page.next, trail: [...page.trail,page.next] }) }
  const busy = state === 'loading'

  return <details ref={panel} name="room-panel" className="screenshot-gallery compact-details" onToggle={event => {
    if (event.target !== event.currentTarget) return
    closeOtherRoomPanels(event.currentTarget)
    const open = event.currentTarget.open;setExpanded(open)
    if (!open) close()
    else if (state === 'idle') void load(recentIntent())
  }}>
    <summary><Images size={16} aria-hidden="true" />{t('gallery.title')}</summary>
    {expanded && <div ref={body} className="screenshot-gallery__body">
      <p className="field-hint">{t('gallery.pageHelp')}</p>
      <p className="field-hint">{t(historyComplete ? 'gallery.recentHelp' : 'gallery.recentWaiting')}</p>
      {page && <p className="metadata" role="status">{t('gallery.pageNumber', { page: page.trail.length })}</p>}
      {page && state !== 'ready' && <p className="metadata">{t('gallery.previousPage')}</p>}
      <div className="page-actions" style={{position:'sticky',top:0,zIndex:1,background:'var(--bg-canvas)',padding:'.25rem 0'}}>
        <button type="button" className="button button--secondary button--compact" disabled={busy || !page || page.trail.length < 2} onClick={previous}>{t('gallery.previousRange')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={() => void load(page ? {cursor:page.cursor,trail:page.trail} : {cursor:0,trail:[0]})}>{t('gallery.reloadRange')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={busy || page?.next === null || !page} onClick={next}>{t('gallery.nextRange')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={busy || !historyComplete || page?.cursor === knownRecentCursor} onClick={() => void load(recentIntent())}>{t('gallery.recentRange')}</button>
        {page && page.trail.length > 1 && <button type="button" className="text-link" disabled={busy} onClick={() => void load({cursor:0,trail:[0]})}>{t('gallery.firstRange')}</button>}
      </div>
      {busy && <p role="status">{t('gallery.loading')}</p>}
      {state === 'error' && <div className="stack stack--tight"><p role="alert" className="notice notice--warning">{t('gallery.loadFailure')}</p>
        {failed && <button type="button" className="button button--secondary button--compact" onClick={() => void load(failed)}>{t('common.retry')}</button>}</div>}
      {state === 'ready' && items.length === 0 && <p>{t(page?.cursor === 0 && page.next === null ? 'gallery.empty' : 'gallery.emptyRange')}</p>}
      {state === 'ready' && page?.next === null && (items.length > 0 || page.cursor > 0) && <p className="metadata">{t('gallery.endRange')}</p>}
      {items.length > 0 && <div className="screenshot-gallery__grid">{items.map(item => <article key={`${item.event_seq}:${item.action_id}`} className="screenshot-gallery__item">
        <button type="button" className="text-link screenshot-gallery__event" onClick={() => navigate(item.event_seq)}>{t('gallery.event', { seq: item.event_seq })}</button>
        {item.state === 'available' ? <ScreenshotArtifact roomId={roomId} actionId={item.action_id} eventSeq={item.event_seq}
          width={item.width} height={item.height} createdAt={item.created_at} readOnly={readOnly}
          onRemoved={() => { if(current())setItems(rows => rows.map(row => row.action_id === item.action_id ? {...row,state:'removed'} : row)) }} />
          : <div className="screenshot-gallery__placeholder"><Image size={24} aria-hidden="true" /><span>{t(item.state === 'removed' ? 'gallery.removed' : 'gallery.unavailable')}</span></div>}
      </article>)}</div>}
    </div>}
  </details>
}
