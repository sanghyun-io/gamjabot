import { conversationsApi, type Conversation, type RoomLaunch } from '../api/conversations'
import { roomTemplatesApi, type RoomTemplate, type RoomTemplateWrite } from '../api/roomTemplates'
import type { Project } from '../api/projects'
import { useAuthStore } from '../stores/authStore'
import { queryClient } from './queryClient'
import { directoryViewGeneration } from './directoryViewMemory'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { beginRoomSetupCommand, settleRoomSetupCommand, type RoomSetupCommand, type RoomSetupKind } from './roomSetupWorkspace'

export function roomSetupOwnerCurrent(identity: string, generation: number) {
  const owner = useAuthStore.getState()
  return !!owner.token && generation === directoryViewGeneration() && `${owner.user?.id || 0}:${owner.epoch}` === identity
}
export function isRoomSetupResult(value: Conversation | RoomTemplate): value is Conversation { return 'title' in value }
function matchingResult(original: RoomSetupCommand, result: Conversation | RoomTemplate | void) {
  if (original.kind === 'template-delete') return result === undefined
  if (!result || !Number.isSafeInteger(result.id) || result.id <= 0 || result.project_id !== original.target.projectId || result.user_id !== useAuthStore.getState().user?.id) return false
  if (original.kind === 'launch' || original.kind === 'template-room') return isRoomSetupResult(result) && typeof result.title === 'string' && typeof result.is_active === 'boolean'
    && (original.kind !== 'launch' || result.title === (original.body as RoomLaunch).title)
  return !isRoomSetupResult(result) && typeof result.name === 'string' && typeof result.room_title === 'string' && Array.isArray(result.agent_ids) && result.agent_ids.every(id => Number.isSafeInteger(id) && id > 0)
    && Number.isSafeInteger(result.max_rounds) && result.max_rounds >= 1 && result.max_rounds <= 4 && Number.isSafeInteger(result.max_tool_calls) && result.max_tool_calls >= 0 && result.max_tool_calls <= 64 && Number.isSafeInteger(result.max_executor_seconds) && result.max_executor_seconds >= 1 && result.max_executor_seconds <= 3600
    && (original.kind !== 'template-update' || result.id === original.target.templateId)
}
async function send(identity: string, generation: number, original: RoomSetupCommand) {
  if (!roomSetupOwnerCurrent(identity, generation)) return
  try {
    const projectId = original.target.projectId, templateId = original.target.templateId!
    const result = original.kind === 'launch' ? await conversationsApi.launch(original.body as RoomLaunch, original.key!)
      : original.kind === 'template-room' ? await roomTemplatesApi.createRoom(projectId, templateId)
      : original.kind === 'template-delete' ? await roomTemplatesApi.delete(projectId, templateId)
      : original.kind === 'template-create' ? await roomTemplatesApi.create(projectId, original.body as RoomTemplateWrite)
      : await roomTemplatesApi.update(projectId, templateId, original.body as RoomTemplateWrite)
    if (!roomSetupOwnerCurrent(identity, generation)) return
    if (!matchingResult(original, result)) { settleRoomSetupCommand(identity, generation, original, { phase: 'unknown', code: 'response_target_mismatch' }); return }
    settleRoomSetupCommand(identity, generation, original, { phase: 'succeeded', result: result || undefined })
    void queryClient.invalidateQueries({ queryKey: ['room-templates', identity, projectId] })
    void queryClient.invalidateQueries({ queryKey: ['rooms'] })
    void queryClient.invalidateQueries({ queryKey: ['projects', identity] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  } catch (error) {
    if (!roomSetupOwnerCurrent(identity, generation)) return
    const code = (error as { response?: { data?: { detail?: { code?: unknown } } } })?.response?.data?.detail?.code
    settleRoomSetupCommand(identity, generation, original, { phase: editorSaveFailure(error) === 'rejected' ? 'rejected' : 'unknown', code: typeof code === 'string' ? code : undefined })
  }
}
export function requestRoomSetupAction(identity: string, generation: number, project: Project, kind: RoomSetupKind, body?: RoomLaunch | RoomTemplateWrite, template?: RoomTemplate, retryOf?: string) {
  if (!roomSetupOwnerCurrent(identity, generation) || project.user_id !== useAuthStore.getState().user?.id) return null
  if (!retryOf && (kind === 'launch' && (!body || !('project_id' in body) || body.project_id !== project.id) || kind !== 'launch' && kind !== 'template-create' && (!template || template.project_id !== project.id || template.user_id !== project.user_id))) return null
  const original = beginRoomSetupCommand(identity, generation, project, kind, body, template, retryOf)
  if (original) void send(identity, generation, original)
  return original
}
