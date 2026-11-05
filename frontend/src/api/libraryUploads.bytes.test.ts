// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import type { AxiosAdapter } from 'axios'
import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { libraryUploadsApi } from './libraryUploads'

vi.mock('../lib/localHost', () => ({ getLocalEndpoint: async () => ({ base_url: 'http://127.0.0.1:1', session_token: 'synthetic-session' }) }))
vi.mock('../lib/sessionContinuity', () => ({ ensureSessionContinuity: async () => undefined }))
const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter; useAuthStore.getState().logout() })

it('unwraps exact PNG bytes from the real Axios response while preserving the download response', async () => {
  useAuthStore.getState().setAuth('synthetic-token', { id: 1, email: 'preview@example.invalid', username: 'Preview' })
  const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aL8AAAAASUVORK5CYII='), c => c.charCodeAt(0)).buffer
  const adapter: AxiosAdapter = async config => {
    expect(config.responseType).toBe('arraybuffer')
    expect(config.url).toBe('/bots/8/library/uploads/11111111-1111-4111-8111-111111111111/file')
    return { data: bytes, status: 200, statusText: 'OK', headers: { 'content-type': 'application/octet-stream' }, config }
  }
  api.defaults.adapter = adapter
  const id = '11111111-1111-4111-8111-111111111111'
  const result = await libraryUploadsApi.fileBytes(8, id)
  expect(result).toBeInstanceOf(ArrayBuffer)
  expect([...new Uint8Array(result)]).toEqual([...new Uint8Array(bytes)])
  const download = await libraryUploadsApi.file(8, id)
  expect(download.data).toBe(bytes)
  expect(download.headers['content-type']).toBe('application/octet-stream')
  api.defaults.adapter = async config => ({ data: new Blob(['not bytes']), status: 200, statusText: 'OK', headers: {}, config })
  await expect(libraryUploadsApi.fileBytes(8, id)).rejects.toThrow('File bytes unavailable')
})
