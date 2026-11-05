import { invoke } from '@tauri-apps/api/core'
import type { GeneratedSaveOutcome } from './generatedFileSave'
import type { WorkspaceSaveReceipt } from './workspaceSaveWorkspace'

type Destination = { selection_id: string; filename: string; replacing: boolean }
const MAX_BYTES = 20 * 1024 * 1024
const noWriteErrors = new Set(['export_unavailable', 'export_busy', 'export_canceled', 'export_invalid_payload',
  'export_destination_unavailable', 'export_destination_changed', 'export_save_failed', 'export_selection_expired'])

// Binary data never passes through a text decoder. No renderer-selected path,
// live file reread, persistent body cache, or automatic write retry is used.
export async function saveVerifiedBinaryFile(buffer: ArrayBuffer, file: { filename: string; size_bytes: number; sha256: string },
  panelTitle: string, current: () => boolean): Promise<GeneratedSaveOutcome> {
  if (!current()) return { phase: 'cancelled' }
  const frozen = buffer.slice(0), context = crypto.randomUUID(), exportId = crypto.randomUUID()
  let selection: Destination | null = null, writing = false
  const discard = async () => { if (selection) try { await invoke('discard_workspace_export', { selectionId: selection.selection_id, context }) } catch { /* No write from a selection. */ } }
  try {
    if (!Number.isSafeInteger(file.size_bytes) || file.size_bytes < 0 || file.size_bytes > MAX_BYTES
      || frozen.byteLength !== file.size_bytes || !/^[a-f0-9]{64}$/.test(file.sha256)) throw 'export_invalid_payload'
    const digest = await crypto.subtle.digest('SHA-256', frozen)
    if (Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('') !== file.sha256) throw 'export_invalid_payload'
    if (!current()) return { phase: 'cancelled' }
    // Small chunks avoid argument-stack limits for the advertised 20 MiB size.
    const bytes = new Uint8Array(frozen), chunks: string[] = []
    for (let index = 0; index < bytes.length; index += 8192) chunks.push(String.fromCharCode(...bytes.subarray(index, index + 8192)))
    const contentBase64 = btoa(chunks.join(''))
    if (await invoke<string>('workspace_export_transport') !== 'native') throw 'export_unavailable'
    if (!current()) return { phase: 'cancelled' }
    selection = await invoke<Destination | null>('choose_binary_file_export', { context, exportId, title: panelTitle, filename: file.filename })
    if (!selection) return { phase: 'cancelled' }
    if (!/^[a-f0-9]{32}$/.test(selection.selection_id) || typeof selection.filename !== 'string' || typeof selection.replacing !== 'boolean') throw 'export_transport_unknown'
    if (!current()) { await discard(); return { phase: 'cancelled' } }
    writing = true
    const receipt = await invoke<WorkspaceSaveReceipt>('save_binary_file_export', { selectionId: selection.selection_id, context, exportId, contentBase64, expectedSha256: file.sha256 })
    if (!receipt || typeof receipt.path !== 'string' || !receipt.path || receipt.bytes !== frozen.byteLength
      || receipt.replaced !== selection.replacing || typeof receipt.durability_unconfirmed !== 'boolean'
      || receipt.reveal_id != null && !/^[a-f0-9]{32}$/.test(receipt.reveal_id)) throw 'export_transport_unknown'
    return { phase: 'saved', filename: selection.filename, receipt }
  } catch (error) {
    await discard()
    return { phase: error === 'export_canceled' ? 'cancelled' : !writing || typeof error === 'string' && noWriteErrors.has(error) ? 'failed' : 'unknown' }
  }
}
