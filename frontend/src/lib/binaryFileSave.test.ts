// @vitest-environment jsdom
import { createHash, webcrypto } from 'node:crypto'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import { saveVerifiedBinaryFile } from './binaryFileSave'
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }))
const selection = { selection_id: 'b'.repeat(32), filename: 'chosen.bin', replacing: false }
const reference = (bytes: Uint8Array) => ({ filename: 'original.bin', size_bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') })
beforeEach(() => { vi.stubGlobal('crypto', webcrypto) })
afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals() })
function native(bytes: Uint8Array) {
  vi.mocked(invoke).mockImplementation(async command => {
    if (command === 'workspace_export_transport') return 'native'
    if (command === 'choose_binary_file_export') return selection
    if (command === 'save_binary_file_export') return { path: '/synthetic/chosen.bin', bytes: bytes.length, replaced: false, durability_unconfirmed: false }
    return undefined
  })
}
it.each([new Uint8Array([0,255,128,254,13,10,0,65]),new Uint8Array()])('preserves arbitrary non-UTF8 or empty bytes through canonical base64', async bytes => {
  native(bytes); const outcome = await saveVerifiedBinaryFile(bytes.buffer as ArrayBuffer, reference(bytes), 'Save file', () => true)
  expect(outcome.phase).toBe('saved')
  const sent = vi.mocked(invoke).mock.calls.find(([command]) => command === 'save_binary_file_export')![1] as {contentBase64:string;expectedSha256:string}
  expect([...Buffer.from(sent.contentBase64,'base64')]).toEqual([...bytes]); expect(sent.expectedSha256).toBe(reference(bytes).sha256)
})
it('rejects modified bytes and the advertised over-size bound before native selection', async () => {
  const bytes = new Uint8Array([1]); native(bytes)
  expect((await saveVerifiedBinaryFile(bytes.buffer as ArrayBuffer,{...reference(bytes),sha256:'0'.repeat(64)},'Save',()=>true)).phase).toBe('failed')
  expect((await saveVerifiedBinaryFile(bytes.buffer as ArrayBuffer,{...reference(bytes),size_bytes:20*1024*1024+1},'Save',()=>true)).phase).toBe('failed')
  expect(invoke).not.toHaveBeenCalled()
})
it('cancels and discards a selected destination if the Owner or view changes', async () => {
  const bytes=new Uint8Array([255]);let current=true
  vi.mocked(invoke).mockImplementation(async command=>{if(command==='workspace_export_transport')return 'native';if(command==='choose_binary_file_export'){current=false;return selection}return undefined})
  expect((await saveVerifiedBinaryFile(bytes.buffer as ArrayBuffer,reference(bytes),'Save',()=>current)).phase).toBe('cancelled')
  expect(vi.mocked(invoke).mock.calls.some(([command])=>command==='save_binary_file_export')).toBe(false)
  expect(vi.mocked(invoke).mock.calls.some(([command])=>command==='discard_workspace_export')).toBe(true)
})
it('treats a lost native write response as unknown without replay', async()=>{
  const bytes=new Uint8Array([0,255]);native(bytes)
  vi.mocked(invoke).mockImplementation(async command=>{if(command==='workspace_export_transport')return 'native';if(command==='choose_binary_file_export')return selection;if(command==='save_binary_file_export')throw Error('Synthetic lost response');return undefined})
  expect((await saveVerifiedBinaryFile(bytes.buffer as ArrayBuffer,reference(bytes),'Save',()=>true)).phase).toBe('unknown')
  expect(vi.mocked(invoke).mock.calls.filter(([command])=>command==='save_binary_file_export')).toHaveLength(1)
})
