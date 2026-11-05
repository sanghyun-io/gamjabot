import type { RoomEvent } from '../api/events'

export type RoomReadingPosition = { seq: number; eventId: string; offset: number; following: boolean }

export function captureRoomReadingPosition(scroller: HTMLElement, events: RoomEvent[], following: boolean): RoomReadingPosition | null {
  const boundary = scroller.getBoundingClientRect()
  for (const node of scroller.querySelectorAll<HTMLElement>('[data-event-seq]')) {
    const rect = node.getBoundingClientRect()
    if (rect.height <= 0 || rect.bottom <= boundary.top || rect.top >= boundary.bottom) continue
    const event = events.find(item => item.seq === Number(node.dataset.eventSeq))
    if (event) return { seq: event.seq, eventId: event.event_id, offset: rect.top - boundary.top, following }
  }
  return null
}

export function restoreRoomReadingPosition(scroller: HTMLElement, events: RoomEvent[], saved: RoomReadingPosition): boolean {
  // Exact event identity; do not substitute another event with a reused sequence.
  const event = events.find(item => item.seq === saved.seq && item.event_id === saved.eventId)
  const node = event ? scroller.querySelector<HTMLElement>(`[data-event-seq="${event.seq}"]`) : null
  if (!node) return false
  let parent = node.parentElement
  while (parent && parent !== scroller) {
    if (parent instanceof HTMLDetailsElement) parent.open = true
    parent = parent.parentElement
  }
  scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - saved.offset
  return true
}
