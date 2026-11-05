import type { RoomEvent } from '../api/events'

export class RoomEventCursor {
  private rooms = new Map<number, Map<number, RoomEvent>>()
  private contiguous = new Map<number, number>()

  clear(): void {
    this.rooms.clear()
    this.contiguous.clear()
  }

  add(roomId: number, incoming: RoomEvent[]): void {
    let room = this.rooms.get(roomId)
    if (!room) {
      room = new Map()
      this.rooms.set(roomId, room)
    }
    const seen = new Set(Array.from(room.values(), (item) => item.event_id))
    for (const item of incoming) {
      if (item.room_id === roomId && !seen.has(item.event_id) && !room.has(item.seq)) {
        room.set(item.seq, item)
        // Keys only append until clear(). Advance over contiguous keys, not the
        // largest received seq; a future event must never skip an earlier gap.
        let through = this.contiguous.get(roomId) ?? 0
        while (room.has(through + 1)) through++
        this.contiguous.set(roomId, through)
        seen.add(item.event_id)
      }
    }
  }

  events(roomId: number): RoomEvent[] {
    return Array.from(this.rooms.get(roomId)?.values() || []).sort((a, b) => a.seq - b.seq)
  }

  afterSeq(roomId: number): number {
    return this.contiguous.get(roomId) ?? 0
  }
}

export const roomEventCursor = new RoomEventCursor()
