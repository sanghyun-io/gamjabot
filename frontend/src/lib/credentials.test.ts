// @vitest-environment jsdom
import { expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'

vi.mock('@tauri-apps/api/core', () => ({ isTauri: () => true,
  invoke: vi.fn().mockResolvedValue({ base_url: 'http://127.0.0.1:9000', session_token: 'launch-secret' }) }))

it('keeps user and launch credentials in memory and clears legacy browser storage', async () => {
  const stored = new Map<string, string>()
  const storage = { setItem: (key: string, value: string) => stored.set(key, value),
    getItem: (key: string) => stored.get(key) || null, removeItem: (key: string) => stored.delete(key),
    key: (index: number) => Array.from(stored.keys())[index] || null, get length() { return stored.size } }
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage })
  Object.defineProperty(window, 'sessionStorage', { configurable: true, value: storage })
  window.localStorage.setItem('auth-storage', 'legacy-secret')
  vi.resetModules()
  const { useAuthStore } = await import('../stores/authStore')
  const { getLocalEndpoint } = await import('./localHost')
  useAuthStore.getState().setAuth('user-secret', { id: 1, email: 'u@example.com', username: 'u' })
  expect((await getLocalEndpoint()).session_token).toBe('launch-secret')
  const values = [window.localStorage, window.sessionStorage].flatMap((store) => Array.from({ length: store.length }, (_, index) => store.getItem(store.key(index)!)))
  expect(values.join(' ')).not.toMatch(/legacy-secret|user-secret|launch-secret/)
  expect(window.localStorage.getItem('auth-storage')).toBeNull()
})

it('retries a transient local host startup failure', async () => {
  vi.resetModules()
  vi.mocked(invoke).mockRejectedValueOnce(new Error('starting'))
  const { getLocalEndpoint } = await import('./localHost')
  await expect(getLocalEndpoint()).rejects.toThrow('starting')
  await expect(getLocalEndpoint()).resolves.toEqual({ base_url: 'http://127.0.0.1:9000', session_token: 'launch-secret' })
})
