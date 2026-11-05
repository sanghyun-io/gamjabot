import { afterEach, expect, it, vi } from 'vitest'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { skipAutoRestoreOnStartup } from './startupLoginMode'

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(), isTauri: vi.fn() }))
afterEach(() => { vi.resetAllMocks(); vi.useRealTimers() })

it('skips on native true and unknown; only explicit false or non-native preserves automatic restore', async () => {
  vi.mocked(isTauri).mockReturnValue(true)
  vi.mocked(invoke).mockResolvedValueOnce({ skip_auto_restore: true }).mockResolvedValueOnce({ skip_auto_restore: false })
    .mockResolvedValueOnce({ skip_auto_restore: 'true' }).mockRejectedValueOnce(new Error('old endpoint unavailable'))
  expect(await skipAutoRestoreOnStartup()).toBe(true)
  expect(await skipAutoRestoreOnStartup()).toBe(false)
  expect(await skipAutoRestoreOnStartup()).toBe(true)
  expect(await skipAutoRestoreOnStartup()).toBe(true)
  vi.mocked(isTauri).mockReturnValue(false)
  expect(await skipAutoRestoreOnStartup()).toBe(false)
  expect(invoke).toHaveBeenCalledTimes(4)
})

it('suppresses restore when the native command does not answer', async () => {
  vi.useFakeTimers()
  vi.mocked(isTauri).mockReturnValue(true)
  vi.mocked(invoke).mockImplementation(() => new Promise(() => {}))
  const result = skipAutoRestoreOnStartup()
  await vi.advanceTimersByTimeAsync(2000)
  expect(await result).toBe(true)
})
