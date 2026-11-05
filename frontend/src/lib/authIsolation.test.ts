// @vitest-environment jsdom
import { expect, it, vi } from 'vitest'

it('clears cached room and query data whenever the logged-in owner changes', async () => {
  const stored = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', { configurable: true, value: { removeItem: (key: string) => stored.delete(key) } })
  vi.resetModules()
  const { useAuthStore } = await import('../stores/authStore')
  const { roomEventCursor } = await import('./eventCursor')
  const { queryClient } = await import('./queryClient')
  const event = { event_id: 'owner-one', room_id: 901, seq: 1, actor_name_snapshot: 'Owner one', kind: 'message', payload: { content: 'Private' } }
  useAuthStore.getState().setAuth('owner-one-token', { id: 1, email: 'one@example.com', username: 'one' })
  roomEventCursor.add(901, [event] as never)
  queryClient.setQueryData(['conversation', 901], { title: 'Private room' })
  useAuthStore.getState().setAuth('owner-two-token', { id: 2, email: 'two@example.com', username: 'two' })
  expect(roomEventCursor.events(901)).toEqual([])
  expect(queryClient.getQueryData(['conversation', 901])).toBeUndefined()
  expect(useAuthStore.getState().epoch).toBe(2)
})
