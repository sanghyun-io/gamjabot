import { conversationsApi, type Conversation } from '../api/conversations'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from './directoryViewMemory'
import { queryClient } from './queryClient'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { beginRoomAction, settleRoomAction, type RoomActionCommand, type RoomClonePreview } from './roomActionWorkspace'
export function roomActionOwnerCurrent(identity: string, generation: number) { const owner = useAuthStore.getState(); return !!owner.token && generation === directoryViewGeneration() && `${owner.user?.id || 0}:${owner.epoch}` === identity }
async function send(identity: string, generation: number, original: RoomActionCommand) {
  if (!roomActionOwnerCurrent(identity, generation)) return
  try {
    const result = original.kind === 'rename' ? await conversationsApi.update(original.source.id, original.body.title) : await conversationsApi.clone(original.source.id, { project_id: original.body.project_id!, title: original.body.title })
    if (!roomActionOwnerCurrent(identity, generation)) return
    if (result.user_id !== useAuthStore.getState().user?.id || !Number.isSafeInteger(result.id) || result.id <= 0 || result.title !== original.body.title
      || result.project_id !== (original.kind === 'rename' ? original.source.project_id : original.body.project_id) || original.kind === 'rename' && result.id !== original.source.id || original.kind === 'clone' && result.id === original.source.id) {
      settleRoomAction(identity, generation, original, { phase: 'unknown', code: 'response_target_mismatch' }); return
    }
    settleRoomAction(identity, generation, original, { phase: 'succeeded', result })
    void queryClient.invalidateQueries({ queryKey: ['room-action-source', identity, original.source.id] }); void queryClient.invalidateQueries({ queryKey: ['rooms'] }); void queryClient.invalidateQueries({ queryKey: ['conversation', original.source.id] }); void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  } catch (error) {
    if (!roomActionOwnerCurrent(identity, generation)) return
    const code = (error as { response?: { data?: { detail?: { code?: unknown } } } })?.response?.data?.detail?.code
    settleRoomAction(identity, generation, original, { phase: editorSaveFailure(error) === 'rejected' ? 'rejected' : 'unknown', code: typeof code === 'string' ? code : undefined })
  }
}
// Rename and clone have no supplied server idempotency/CAS key. Every explicit
// new command is kept separately; never replay an unknown command automatically.
export function requestRoomAction(identity: string, generation: number, source: Conversation, kind: 'rename' | 'clone', preview?: RoomClonePreview, targetProjectName?: string) {
  if (!roomActionOwnerCurrent(identity, generation) || source.user_id !== useAuthStore.getState().user?.id) return null
  const original = beginRoomAction(identity, generation, source, kind, preview, targetProjectName)
  if (original) void send(identity, generation, original)
  return original
}
