import api from '../lib/api'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { useAuthStore } from '../stores/authStore'
import type { BotMessageRef } from './botMessages'
import { executionContext, sameToolRootRef, validToolRootRef } from '../lib/toolExecution'

export interface ExecutionBinding {
  version: 1; root_scope_id: string; scope_id: string; owner_id: number
  root_room_id: number; root_request_event_id: string; execution_room_id: number; agent_id: number; run_generation: number
  job_id: string | null; phase: 'response' | 'return' | null; context_mode: 'user_room' | 'delegated'; deadline_at: string
  lineage: Array<{ scope_id: string; epoch: number }>
}
export interface RoutineBinding { version:1; owner_id:number; agent_id:number; room_id:number; routine_id:string; run_id:string; config_revision:number; generation:number; root_event_id:string; origin:'scheduled'|'test'|'webhook'; scheduled_at:string }
export interface McpActionBinding { server_id:string;config_revision:number;binding_revision:number;tool_id:string;tool_name:string;schema_sha256:string;owner_id:number;agent_id:number;root_event_id:string;generation:number;transport_sha256:string;input_sha256:string;transport_type:'http'|'stdio';executable?:string;teammate_binding?:{handoff_id:string;phase:'response'|'return'}|null }
export interface SlackBinding {version:1;owner_id:number;agent_id:number;room_id:number;run_id:string;config_revision:number;credential_revision:number;generation:number;root_event_id:string;workspace_id:string;channel_id:string;sender_id:string;event_id:string;thread_ts:string}
export interface ToolExecutionFields {
  slack_binding?:SlackBinding | null;
  mcp_binding?:McpActionBinding | null;
  routine_binding?: RoutineBinding | null;
  execution_binding?: ExecutionBinding | null; root_ref?: BotMessageRef | null
  scope_status?: 'active' | 'revoked' | 'expired' | 'unknown' | null; revocation_state?: 'pending' | 'settled' | 'unknown' | null
}
export interface RootToolActionsProjection { version: 1; message_id: string; root_ref: BotMessageRef; actions: ToolActivity[] }

export interface ToolActivity extends ToolExecutionFields {
  action_id: string
  room_id: number
  agent_id: number
  trigger_event_id: string | null
  family: string
  target: string
  expected_effect: string
  state: string
  scope: string | null
  grant_id: number | null
  requested_at: string
  started_at: string | null
  completed_at: string | null
  review_id?: string | null
  review_status?: string
  approval_owner_id?: number | null
  approved_at?: string | null
  send_image_to_model?: boolean | null
  owner_draft?: { revision: number; pending: boolean; applied: boolean } | null
}

export interface ActionPreview extends ToolExecutionFields {
  action_id: string
  family: string
  target: string
  scope_target: string
  scope_description: string
  expected_effect: string
  workflow_id: string | null
  arguments: Record<string, unknown>
  payload_digest: string
  review_id: string
  review?: unknown
  approval_policy: {
    scopes: ('once' | 'room_session')[]
    unverified_context_required: boolean
    start_within_seconds: number | null
  }
}

export interface RoomGrant {
  id: number
  room_id: number
  agent_id: number
  scope: string
  families: string[]
  targets: string[]
  decision: string
  expires_at: string | null
}

export interface BroadMode {
  enabled: boolean
  scope: string | null
  expires_at: string | null
}

export interface DesktopLease {
  active: boolean
  owner: { room_id: number; agent_id: number; workflow_id: string } | null
  another_profile_or_room: boolean
}

type ArtifactTransport = 'native' | 'http'
const MAX_NATIVE_ARTIFACT_BYTES = 10_000_000

interface ArtifactRequestError extends Error {
  response?: { status?: number }
}

function fixedArtifactError(value: unknown): ArtifactRequestError {
  const code = typeof value === 'string' ? value : 'artifact_unavailable'
  const error = new Error('Screenshot unavailable') as ArtifactRequestError
  if (code === 'artifact_removed') error.response = { status: 410 }
  else if (code === 'access_denied') error.response = { status: 403 }
  return error
}

function nativeArtifactBytes(value: unknown): ArrayBuffer | Uint8Array<ArrayBuffer> {
  if (value instanceof ArrayBuffer && value.byteLength <= MAX_NATIVE_ARTIFACT_BYTES) return value
  if (Array.isArray(value) && value.length <= MAX_NATIVE_ARTIFACT_BYTES) {
    for (let index = 0; index < value.length; index += 1) {
      const byte = value[index]
      if (!Number.isFinite(byte) || !Number.isInteger(byte) || byte < 0 || byte > 255) {
        throw fixedArtifactError('artifact_unavailable')
      }
    }
    return Uint8Array.from(value)
  }
  throw fixedArtifactError('artifact_unavailable')
}

async function artifactTransport(): Promise<ArtifactTransport> {
  if (!isTauri()) return 'http'
  let transport: unknown
  try {
    transport = await invoke('screenshot_artifact_transport')
  } catch (error) {
    throw fixedArtifactError(error)
  }
  if (transport !== 'native' && transport !== 'http') throw fixedArtifactError(transport)
  return transport
}

function nativeArtifactArgs(roomId: number, actionId: string, eventSeq: number) {
  const bearerToken = useAuthStore.getState().token
  if (!bearerToken) throw fixedArtifactError('access_denied')
  return { roomId, actionId, eventSeq, bearerToken }
}

function aborted(): DOMException {
  return new DOMException('The operation was aborted', 'AbortError')
}

export const toolsApi = {
  // This is the whole original Owner request tree, including sibling and nested Bots.
  rootActions: async (messageId: string, rootRef: BotMessageRef): Promise<RootToolActionsProjection> => {
    const before = useAuthStore.getState(), owner = before.user?.id, epoch = before.epoch
    if (!before.token || !owner || !validToolRootRef(rootRef, owner)) throw new Error('Root action owner changed')
    const { data } = await api.get<RootToolActionsProjection>(`/bots/messages/${messageId}/actions`)
    const after = useAuthStore.getState()
    if (!after.token || after.user?.id !== owner || after.epoch !== epoch || !data || data.version !== 1 || data.message_id !== messageId
      || !validToolRootRef(data.root_ref, owner) || !sameToolRootRef(data.root_ref, rootRef) || !Array.isArray(data.actions)
      || new Set(data.actions.map(action => action.action_id)).size !== data.actions.length
      || !data.actions.every(action => typeof action.family === 'string' && typeof action.target === 'string' && typeof action.expected_effect === 'string' && typeof action.state === 'string' && executionContext(action, owner).kind === 'scoped' && !!action.root_ref && sameToolRootRef(action.root_ref, rootRef))) throw new Error('Root action projection changed or unavailable')
    return data
  },
  activity: async (roomId: number) => (await api.get<ToolActivity[]>(`/tools/rooms/${roomId}/activity`)).data,
  preview: async (roomId: number, actionId: string) => (await api.get<ActionPreview>(
    `/tools/rooms/${roomId}/actions/${actionId}/preview`)).data,
  screenshot: async (roomId: number, actionId: string, eventSeq: number, signal?: AbortSignal) => {
    if (signal?.aborted) throw aborted()
    if (await artifactTransport() === 'http') return (await api.get<Blob>(
      `/tools/rooms/${roomId}/actions/${actionId}/screenshot`, { responseType: 'blob', signal })).data
    let bytes: unknown
    try {
      bytes = await invoke<unknown>('read_screenshot_artifact', nativeArtifactArgs(roomId, actionId, eventSeq))
    } catch (error) {
      throw fixedArtifactError(error)
    }
    if (signal?.aborted) throw aborted()
    return new Blob([nativeArtifactBytes(bytes)], { type: 'image/png' })
  },
  deleteScreenshot: async (roomId: number, actionId: string, eventSeq: number) => {
    if (await artifactTransport() === 'http') {
      await api.delete(`/tools/rooms/${roomId}/actions/${actionId}/screenshot`)
      return
    }
    try {
      await invoke('delete_screenshot_artifact', nativeArtifactArgs(roomId, actionId, eventSeq))
    } catch (error) {
      throw fixedArtifactError(error)
    }
  },
  decide: async (roomId: number, actionId: string, decision: 'approve' | 'deny', scope?: 'once' | 'room_session', reviewId?: string, acknowledgeUnverified = false, sendImageToModel = false) =>
    (await api.post(`/tools/rooms/${roomId}/actions/${actionId}/decision`, { decision, scope, review_id: reviewId, acknowledge_unverified_context: acknowledgeUnverified, send_image_to_model: sendImageToModel })).data,
  grants: async (roomId: number) => (await api.get<RoomGrant[]>(`/tools/rooms/${roomId}/grants`)).data,
  revoke: async (roomId: number, grantId: number) => { await api.post(`/tools/rooms/${roomId}/grants/${grantId}/revoke`) },
  broadMode: async () => (await api.get<BroadMode>('/tools/broad-mode')).data,
  setBroadMode: async (enabled: boolean, expires_minutes?: number, always = false) =>
    (await api.put<BroadMode>('/tools/broad-mode', { enabled, expires_minutes, always })).data,
  lease: async () => (await api.get<DesktopLease>('/tools/desktop/lease')).data,
}
