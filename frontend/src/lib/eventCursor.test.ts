import { describe, expect, it } from 'vitest'
import { RoomEventCursor } from './eventCursor'
import type { RoomEvent } from '../api/events'

function event(room_id: number, seq: number, actor_name_snapshot: string): RoomEvent {
  return { event_id: `${room_id}-${seq}`, device_id: null, room_id, seq,
    actor_type: 'agent', actor_id: seq, actor_name_snapshot, kind: 'message',
    payload: { content: actor_name_snapshot }, created_at: '2026-09-28T00:00:00Z', idempotency_key: null }
}

describe('canonical room cursors', () => {
  it('keeps rooms independent and displays a replayed peer event once in sequence order', () => {
    const cursor = new RoomEventCursor()
    cursor.add(1, [event(1, 1, 'A'), event(1, 2, 'B')])
    cursor.add(2, [event(2, 1, 'A')])
    cursor.add(1, [event(1, 2, 'B'), event(1, 3, 'A')])
    expect(cursor.afterSeq(1)).toBe(3)
    expect(cursor.afterSeq(2)).toBe(1)
    expect(cursor.events(1).map((item) => item.actor_name_snapshot)).toEqual(['A', 'B', 'A'])
    expect(cursor.events(2)).toHaveLength(1)
  })

  it('does not advance across a gap or leak an event into another room', () => {
    const cursor = new RoomEventCursor()
    cursor.add(1, [event(1, 2, 'late'), event(2, 1, 'foreign')])
    expect(cursor.afterSeq(1)).toBe(0)
    cursor.add(1, [event(1, 1, 'first')])
    expect(cursor.events(1).map((item) => item.seq)).toEqual([1, 2])
    expect(cursor.events(2)).toEqual([])
  })
})
