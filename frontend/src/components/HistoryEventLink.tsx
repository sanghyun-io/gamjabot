import { useRef, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

export type HistoryEventNavigation = (seq: number, eventId: string) => void
export default function HistoryEventLink({ roomId, seq, eventId, canUse, onNavigate, children }: {
  roomId: number; seq: number | undefined; eventId: string; canUse: () => boolean
  onNavigate?: HistoryEventNavigation; children: ReactNode
}) {
  const anchor = useRef<HTMLAnchorElement>(null)
  const valid = Number.isSafeInteger(roomId) && roomId > 0 && typeof seq === 'number' && Number.isSafeInteger(seq) && seq > 0
    && typeof eventId === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(eventId)
  if (!valid) return <span className="metadata">{children}</span>
  const query = new URLSearchParams({ seq: String(seq), event: eventId })
  return <Link ref={anchor} className="text-link" to={`/chat/${roomId}?${query.toString()}`} onClick={event => {
    const node = anchor.current
    const modals = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(modal =>
      modal.getClientRects().length > 0 && !modal.closest('[hidden], [inert], [aria-hidden="true"]') && getComputedStyle(modal).visibility !== 'hidden')
    const top = modals.at(-1)
    if (!canUse() || document.visibilityState !== 'visible' || !document.hasFocus() || !node?.isConnected
        || !node.getClientRects().length || node.closest('[hidden], [inert], [aria-hidden="true"]') || top && !top.contains(node)) {
      event.preventDefault(); return
    }
    // Keep normal link behavior for an explicit new-tab gesture; in-place reads use the room's canonical navigation.
    if (!event.defaultPrevented && event.button === 0 && !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey && onNavigate) {
      event.preventDefault(); onNavigate(seq!, eventId)
    }
  }}>{children}</Link>
}
