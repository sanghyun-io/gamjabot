import { memo, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { RoomEvent } from '../api/events'
import type { HandoffSourceChoice } from './HandoffSourcePicker'
import { useI18n } from '../i18n'
import ServerTime from './ServerTime'
import HistoryEventLink, { type HistoryEventNavigation } from './HistoryEventLink'
import { readDirectoryView, writeDirectoryView } from '../lib/directoryViewMemory'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'

const PAGE_SIZE = 25
function HandoffSourcePreview({ messages, choices, roomId, canRead, onViewSource, memory }: {
  messages: RoomEvent[]; choices: ReadonlyMap<string, HandoffSourceChoice>; roomId?: number
  canRead?: () => boolean; onViewSource?: HistoryEventNavigation; memory?: { identity: string; generation: number; key: string }
}) {
  const { t } = useI18n(), listId = useId()
  const root = useRef<HTMLDetailsElement>(null)
  const [restored] = useState(() => memory ? readDirectoryView<{ expanded?: unknown; query?: unknown; anchor?: unknown } | null>(memory.identity, memory.key, null) : null)
  const [expanded, setExpanded] = useState(restored?.expanded === true)
  const [anchor, setAnchor] = useState<string | null>(typeof restored?.anchor === 'string' && /^[a-f0-9-]{36}$/i.test(restored.anchor) ? restored.anchor : null)
  const [query, setQuery] = useState(typeof restored?.query === 'string' ? restored.query.slice(0, 200) : '')
  const remember = (open: boolean, text: string, first: string | null) => {
    if (memory) writeDirectoryView(memory.identity, memory.key, { expanded: open, query: text.slice(0, 200), anchor: first }, memory.generation)
  }
  const readable = (message: RoomEvent) => !!canRead?.() && !!root.current?.open && root.current.isConnected
    && message.room_id === roomId && message.kind === 'message' && (message.actor_type === 'user' || message.actor_type === 'agent')
    && choices.get(message.event_id)?.seq === message.seq && messages.includes(message)
  const readingList = useRef<HTMLUListElement>(null), pageIntent = useRef<{ context: object; id: string; origin: HTMLButtonElement } | null>(null)
  const pageContext = useMemo(() => ({}), [roomId, messages, choices, query, memory])
  const matching = useMemo(() => {
    if (!expanded || !query.trim()) return messages
    const matches = directoryMatcher(query)
    return messages.filter(message => matches([String(message.seq), `#${message.seq}`, message.actor_name_snapshot, choices.get(message.event_id)?.excerpt]))
  }, [expanded, messages, choices, query])
  const found = anchor ? matching.findIndex(message => message.event_id === anchor) : 0
  const start = found >= 0 ? found : 0, end = Math.min(start + PAGE_SIZE, matching.length)
  const move = (index: number, origin: HTMLButtonElement) => {
    if (!expanded || !matching.length || !Number.isSafeInteger(index) || !origin.isConnected || origin.disabled) return
    const first = matching[Math.max(0, Math.min(index, matching.length - 1))]?.event_id
    if (!first || first === matching[start]?.event_id) return
    origin.focus({ preventScroll: true }); pageIntent.current = { context: pageContext, id: first, origin }
    setAnchor(first); remember(expanded, query, first)
  }
  useLayoutEffect(() => {
    const intent = pageIntent.current; pageIntent.current = null
    if (!intent || intent.context !== pageContext || intent.id !== matching[start]?.event_id || !expanded || !canRead?.()
        || document.visibilityState !== 'visible' || !document.hasFocus() || document.activeElement !== intent.origin && document.activeElement !== document.body) return
    const list = readingList.current
    if (!list?.isConnected || !list.getClientRects().length || list.closest('[hidden], [inert], [aria-hidden="true"]')) return
    const top = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node => node.getClientRects().length > 0
      && !node.closest('[hidden], [inert], [aria-hidden="true"]') && getComputedStyle(node).visibility !== 'hidden').at(-1)
    if (top && !top.contains(list)) return
    // Focus a reading list, never a source link that a held paging Enter could activate.
    list.focus({ preventScroll: true }); list.scrollIntoView({ block: 'start', inline: 'nearest' })
  }, [anchor, pageContext, expanded, matching[start]?.event_id])
  return <details ref={root} open={expanded} className="handoff-note__included compact-details" onToggle={event => { if (event.target === event.currentTarget) { const open = event.currentTarget.open; if (!open) pageIntent.current = null; setExpanded(open); remember(open, query, anchor) } }}>
    <summary>{t('chat.handoff.pages.title', { count: messages.length })}</summary>
    {expanded && <div className="stack stack--tight">
      <p className="field-hint">{t('chat.handoff.pages.help')}</p>
      {onViewSource && <p className="field-hint">{t('chat.handoff.previewReadHelp')}</p>}
      {memory && <p className="field-hint">{t('chat.handoff.previewMemoryHelp')}</p>}
      <DirectorySearch value={query} onChange={value => { const next = value.slice(0, 200); pageIntent.current = null; setQuery(next); setAnchor(null); remember(expanded, next, null) }} label={t('chat.handoff.sourceSearch.title')} hint={t('chat.handoff.previewFind.hint')}
        shown={matching.length} total={messages.length} known loading={false} controls={listId} preventSubmit />
      <p className="field-hint">{t('chat.handoff.previewFind.scope')}</p>
      {query.trim() && !matching.length && <p role="status">{t('chat.handoff.sourceSearch.noMatches')}</p>}
      <div className="page-actions">
        <button type="button" className="button button--secondary button--compact" disabled={start === 0} aria-controls={listId} onClick={event => move(0, event.currentTarget)}>{t('chat.handoff.pages.first')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={start === 0} aria-controls={listId} onClick={event => move(start - PAGE_SIZE, event.currentTarget)}>{t('chat.handoff.pages.previous')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={end >= matching.length} aria-controls={listId} onClick={event => move(start + PAGE_SIZE, event.currentTarget)}>{t('chat.handoff.pages.next')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={end >= matching.length} aria-controls={listId} onClick={event => move(Math.floor((matching.length - 1) / PAGE_SIZE) * PAGE_SIZE, event.currentTarget)}>{t('chat.handoff.pages.last')}</button>
      </div>
      <p role="status" className="metadata">{t('chat.handoff.pages.range', { from: matching.length ? start + 1 : 0, to: end, total: matching.length })}</p>
      <p className="field-hint">{t('chat.handoff.previewPageReadHelp')}</p>
      <ul ref={readingList} id={listId} tabIndex={-1} aria-label={t('chat.handoff.pages.range', { from: matching.length ? start + 1 : 0, to: end, total: matching.length })} className="handoff-preview-reading-page">{matching.slice(start, end).map(message => <li key={message.event_id}>
        {onViewSource && roomId !== undefined ? <HistoryEventLink roomId={roomId} seq={message.seq} eventId={message.event_id} canUse={() => readable(message)} onNavigate={onViewSource}>
          <span className="sr-only">{t('chat.handoff.readSource',{seq:message.seq})} · </span><strong>#{message.seq} · {message.actor_name_snapshot}</strong>
        </HistoryEventLink> : <strong>#{message.seq} · {message.actor_name_snapshot}</strong>}
        <span>{choices.get(message.event_id)?.excerpt || ''}</span>
        <ServerTime value={message.created_at} compact/>
      </li>)}</ul>
    </div>}
  </details>
}
// Typing the note keeps the same received-message/choice references.
export default memo(HandoffSourcePreview)
