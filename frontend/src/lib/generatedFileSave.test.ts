// @vitest-environment jsdom
import { createHash, webcrypto } from 'node:crypto'
import { TextDecoder, TextEncoder } from 'node:util'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import { saveVerifiedGeneratedFile } from './generatedFileSave'
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))
const selection = { selection_id: 'a'.repeat(32), filename: 'chosen.csv', replacing: false }
const bytesOf = (text: string) => new TextEncoder().encode(text)
const ref = (bytes: Uint8Array) => ({ filename: 'original.csv', size_bytes: bytes.byteLength, sha256: createHash('sha256').update(bytes).digest('hex') })
beforeEach(() => { vi.stubGlobal('crypto', webcrypto); vi.stubGlobal('TextDecoder', TextDecoder); vi.stubGlobal('TextEncoder', TextEncoder) })
afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals() })
function native(bytes: Uint8Array) {
  vi.mocked(invoke).mockImplementation(async (command) => {
    if (command === 'workspace_export_transport') return 'native'
    if (command === 'choose_generated_file_export') return selection
    if (command === 'save_generated_file_export') return { path: '/synthetic/chosen.csv', bytes: bytes.byteLength, replaced: false, durability_unconfirmed: false }
    if (command === 'discard_workspace_export') return undefined
    throw Error('Unexpected native command')
  })
}
it.each(['\ufeffheader,value\r\n한글,1\0\r\n', ''])('preserves complete UTF-8 bytes including BOM/NUL or empty payload', async content => {
  const bytes = bytesOf(content); native(bytes)
  const outcome = await saveVerifiedGeneratedFile(bytes.buffer as ArrayBuffer, ref(bytes), 'Save file', () => true)
  expect(outcome.phase).toBe('saved')
  const sent = vi.mocked(invoke).mock.calls.find(([command]) => command === 'save_generated_file_export')![1] as { content: string }
  expect([...bytesOf(sent.content)]).toEqual([...bytes])
  expect(vi.mocked(invoke).mock.calls.filter(([command]) => command === 'save_generated_file_export')).toHaveLength(1)
})
it('cancels and discards an unused selection when the Owner/view changes during the chooser', async () => {
  const bytes = bytesOf('csv'); let current = true; native(bytes)
  vi.mocked(invoke).mockImplementation(async command => {
    if (command === 'workspace_export_transport') return 'native'
    if (command === 'choose_generated_file_export') { current = false; return selection }
    return undefined
  })
  expect((await saveVerifiedGeneratedFile(bytes.buffer as ArrayBuffer, ref(bytes), 'Save', () => current)).phase).toBe('cancelled')
  expect(vi.mocked(invoke).mock.calls.some(([command]) => command === 'save_generated_file_export')).toBe(false)
  expect(vi.mocked(invoke).mock.calls.some(([command]) => command === 'discard_workspace_export')).toBe(true)
})
it('does not write after native panel cancellation', async () => {
  const bytes = bytesOf('csv'); vi.mocked(invoke).mockImplementation(async command => command === 'workspace_export_transport' ? 'native' : null)
  expect((await saveVerifiedGeneratedFile(bytes.buffer as ArrayBuffer, ref(bytes), 'Save', () => true)).phase).toBe('cancelled')
  expect(vi.mocked(invoke).mock.calls.some(([command]) => command === 'save_generated_file_export')).toBe(false)
})
it('keeps a lost write response unknown and never retries the write', async () => {
  const bytes = bytesOf('csv'); native(bytes)
  vi.mocked(invoke).mockImplementation(async command => {
    if (command === 'workspace_export_transport') return 'native'
    if (command === 'choose_generated_file_export') return selection
    if (command === 'save_generated_file_export') throw Error('Synthetic lost response')
    return undefined
  })
  expect((await saveVerifiedGeneratedFile(bytes.buffer as ArrayBuffer, ref(bytes), 'Save', () => true)).phase).toBe('unknown')
  expect(vi.mocked(invoke).mock.calls.filter(([command]) => command === 'save_generated_file_export')).toHaveLength(1)
})
it('rejects altered bytes before opening a native chooser', async () => {
  const bytes = bytesOf('csv'); native(bytes)
  expect((await saveVerifiedGeneratedFile(bytes.buffer as ArrayBuffer, { ...ref(bytes), sha256: '0'.repeat(64) }, 'Save', () => true)).phase).toBe('failed')
  expect(invoke).not.toHaveBeenCalled()
})
