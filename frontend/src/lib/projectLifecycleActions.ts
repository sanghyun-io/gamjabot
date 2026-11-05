import { projectsApi, type ProjectArchiveResult, type ProjectRestoreResult } from '../api/projects'
import { useAuthStore } from '../stores/authStore'
import { queryClient } from './queryClient'
import { directoryViewGeneration } from './directoryViewMemory'
import { projectCommandBlocked } from './projectEditorWorkspace'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { beginProjectLifecycle, settleProjectLifecycle, type ProjectLifecycleCommand } from './projectLifecycleWorkspace'

export function lifecycleOwnerCurrent(identity: string, generation: number) {
  const owner = useAuthStore.getState()
  return !!owner.token && `${owner.user?.id || 0}:${owner.epoch}` === identity && generation === directoryViewGeneration()
}
function matchingResult(original: ProjectLifecycleCommand, result: ProjectArchiveResult | ProjectRestoreResult) {
  if (result.project_id !== original.target.id) return false
  if (original.kind === 'restore') return result.is_active === true && 'closed_rooms' in result && Number.isSafeInteger(result.closed_rooms) && result.closed_rooms >= 0
  return result.is_active === false && 'plan_hash' in result && result.plan_hash === original.plan?.plan_hash
    && Array.isArray(result.cleanup_unconfirmed) && result.cleanup_unconfirmed.every(id => Number.isSafeInteger(id) && id > 0)
    && !!result.counts && Object.entries(original.plan!.counts).every(([key, count]) => (result.counts as unknown as Record<string, number>)[key] === count)
}
async function send(identity: string, generation: number, original: ProjectLifecycleCommand) {
  if (!lifecycleOwnerCurrent(identity, generation)) return
  try {
    const result = original.kind === 'archive' ? await projectsApi.archive(original.target.id, original.plan!.plan_hash) : await projectsApi.restore(original.target.id)
    if (!lifecycleOwnerCurrent(identity, generation)) return
    if (!matchingResult(original, result)) {
      settleProjectLifecycle(identity, generation, original, { phase: 'unknown', code: 'response_target_mismatch' }); return
    }
    settleProjectLifecycle(identity, generation, original, { phase: 'succeeded', result })
    void queryClient.invalidateQueries({ queryKey: ['projects', identity] })
    void queryClient.invalidateQueries({ queryKey: ['project-editor-basis', identity, original.target.id] })
    void queryClient.invalidateQueries({ queryKey: ['rooms'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  } catch (error) {
    if (!lifecycleOwnerCurrent(identity, generation)) return
    const detail = (error as { response?: { data?: { detail?: { code?: string; active_rooms?: number } } } })?.response?.data?.detail
    const code = typeof detail?.code === 'string' ? detail.code : undefined
    const phase = original.kind === 'archive' && code === 'project_archive_plan_stale' ? 'stale' : editorSaveFailure(error) === 'rejected' ? 'rejected' : 'unknown'
    settleProjectLifecycle(identity, generation, original, { phase, code, activeRooms: Number.isSafeInteger(detail?.active_rooms) && detail!.active_rooms! >= 0 ? detail!.active_rooms : undefined })
  }
}
// The UUID is only a UI record. Archive uses the existing exact plan_hash contract;
// restore is a fresh state transition request. Neither operation is automatically replayed.
export function requestProjectLifecycle(identity: string, generation: number, kind: 'archive' | 'restore', target: { id: number; name: string }, previewId?: string, retryOf?: string) {
  if (!lifecycleOwnerCurrent(identity, generation) || projectCommandBlocked(identity)) return null
  const original = beginProjectLifecycle(identity, generation, kind, target, previewId, retryOf)
  if (original) void send(identity, generation, original)
  return original
}
