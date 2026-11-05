import { invoke } from '@tauri-apps/api/core'
import type { WorkspaceSaveReceipt } from './workspaceSaveWorkspace'

type Destination = { selection_id: string; filename: string; replacing: boolean }
export type GeneratedSaveOutcome = { phase: 'saved'; filename: string; receipt: WorkspaceSaveReceipt }
  | { phase: 'cancelled' | 'failed' | 'unknown' }
const noWriteErrors = new Set(['export_unavailable', 'export_busy', 'export_canceled', 'export_invalid_payload',
  'export_destination_unavailable', 'export_destination_changed', 'export_save_failed', 'export_selection_expired'])

// The renderer never chooses a filesystem path. This explicit save invocation
// holds the frozen full body only until the native chooser/write settles.
export async function saveVerifiedGeneratedFile(buffer: ArrayBuffer, file: { filename: string; size_bytes: number; sha256: string },
  panelTitle: string, current: () => boolean): Promise<GeneratedSaveOutcome> {
  if (!current()) return { phase: 'cancelled' }
  const frozen = buffer.slice(0), context = crypto.randomUUID(), exportId = crypto.randomUUID()
  let selection: Destination | null = null, writing = false
  const discard = async () => {
    if (selection) try { await invoke('discard_workspace_export', { selectionId: selection.selection_id, context }) } catch { /* Selection alone writes nothing. */ }
  }
  try {
    if (!Number.isSafeInteger(file.size_bytes) || file.size_bytes < 0 || file.size_bytes > 4_000_000
      || frozen.byteLength !== file.size_bytes || !/^[a-f0-9]{64}$/.test(file.sha256)) throw 'export_invalid_payload'
    // ignoreBOM preserves an initial UTF-8 BOM in the string passed to Rust.
    // The display decoder may omit it; saving must retain all original bytes.
    const content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(frozen)
    const roundtrip = new TextEncoder().encode(content), original = new Uint8Array(frozen)
    if (roundtrip.byteLength !== frozen.byteLength || roundtrip.some((byte, index) => byte !== original[index])) throw 'export_invalid_payload'
    const digest = await crypto.subtle.digest('SHA-256', frozen)
    if (Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('') !== file.sha256) throw 'export_invalid_payload'
    if (!current()) return { phase: 'cancelled' }
    if (await invoke<string>('workspace_export_transport') !== 'native') throw 'export_unavailable'
    if (!current()) return { phase: 'cancelled' }
    selection = await invoke<Destination | null>('choose_generated_file_export', { context, exportId, title: panelTitle, filename: file.filename })
    if (!selection) return { phase: 'cancelled' }
    if (!/^[a-f0-9]{32}$/.test(selection.selection_id) || typeof selection.filename !== 'string' || typeof selection.replacing !== 'boolean') throw 'export_transport_unknown'
    // Native panels legitimately take focus. Check Owner/epoch/view after the
    // panel closes, rather than document.hasFocus(). Never replay a write.
    if (!current()) { await discard(); return { phase: 'cancelled' } }
    writing = true
    const receipt = await invoke<WorkspaceSaveReceipt>('save_generated_file_export', { selectionId: selection.selection_id, context, exportId, content })
    if (!receipt || typeof receipt.path !== 'string' || !receipt.path || receipt.bytes !== frozen.byteLength
      || receipt.replaced !== selection.replacing || typeof receipt.durability_unconfirmed !== 'boolean'
      || receipt.reveal_id != null && !/^[a-f0-9]{32}$/.test(receipt.reveal_id)) throw 'export_transport_unknown'
    return { phase: 'saved', filename: selection.filename, receipt }
  } catch (error) {
    await discard()
    return { phase: error === 'export_canceled' ? 'cancelled' : !writing || typeof error === 'string' && noWriteErrors.has(error) ? 'failed' : 'unknown' }
  }
}
