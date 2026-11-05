// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'

vi.mock('@tauri-apps/api/core', () => ({ isTauri: () => true, invoke: vi.fn() }))

beforeEach(() => { vi.resetModules(); vi.mocked(invoke).mockReset() })

it('a refreshed shell obtains a fresh session exchange at the same API URL', async () => {
  vi.mocked(invoke)
    .mockResolvedValueOnce({ base_url: 'http://127.0.0.1:41001', session_token: 'first-session' })
    .mockResolvedValueOnce({ base_url: 'http://127.0.0.1:41001', session_token: 'next-session' })
  const { getLocalEndpoint } = await import('./localHost')
  const initial = await getLocalEndpoint()
  const refreshed = await getLocalEndpoint({ refresh: true })
  expect(initial.session_token).toBe('first-session')
  expect(refreshed).toEqual({ base_url: initial.base_url, session_token: 'next-session' })
  expect(await getLocalEndpoint()).toEqual(refreshed)
  expect(invoke).toHaveBeenCalledTimes(2)
  expect(invoke).toHaveBeenNthCalledWith(2, 'get_local_endpoint')
})

it('a failed older exchange cannot discard a successful refreshed session', async () => {
  let rejectOld!: (error: Error) => void
  vi.mocked(invoke)
    .mockResolvedValue({ base_url: 'http://127.0.0.1:41001', session_token: 'unexpected-extra-session' })
    .mockReturnValueOnce(new Promise((_, reject) => { rejectOld = reject }))
    .mockResolvedValueOnce({ base_url: 'http://127.0.0.1:41001', session_token: 'current-session' })
  const { getLocalEndpoint } = await import('./localHost')
  const old = getLocalEndpoint().catch(() => undefined)
  const current = await getLocalEndpoint({ refresh: true })
  rejectOld(new Error('obsolete exchange failed'))
  await old
  expect(await getLocalEndpoint()).toEqual(current)
  expect(invoke).toHaveBeenCalledTimes(2)
})
