import api from '../lib/api'
import type { BotMessageRef } from './botMessages'
import { validBotMessageRef } from './botMessages'
import { canonicalEventId } from '../lib/peerReply'
import { validAvatar, type AvatarConfig } from '../lib/avatarRecipe'
import { useAuthStore } from '../stores/authStore'
export type BotCreationReceipt = {
  version: 1; creation_id: string; state: 'created'; root_ref: BotMessageRef; source_agent_id: number; source_room_id: number
  created_agent: { id: number; name: string; description: string; system_prompt: string; connection_id: number; model_id: string; temperature: string; max_tokens: number; avatar: AvatarConfig }
  conversation: { room_id: number; project_id: number; href: string }; request_ref: BotMessageRef; result_ref: BotMessageRef
  model_binding: { connection_id: number; model_id: string; config_revision: number; auth_grant_revision: number }
  readback: { agent_matches: boolean; conversation_matches: boolean; available: boolean }; created_at: string; replayed: boolean
}
export type RoomBotCreations = { version: 1; room_id: number; creations: BotCreationReceipt[]; creations_truncated: boolean }
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const revision = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
export function validBotCreation(value: unknown, roomId: number, ownerId: number): value is BotCreationReceipt {
  if (!object(value) || value.version !== 1 || value.state !== 'created' || !canonicalEventId(value.creation_id)
    || value.source_room_id !== roomId || !positive(value.source_agent_id) || !validBotMessageRef(value.root_ref)
    || value.root_ref.room_id !== roomId || value.root_ref.actor_type !== 'user' || value.root_ref.actor_id !== ownerId || value.root_ref.kind !== 'message'
    || !object(value.created_agent) || !positive(value.created_agent.id) || value.created_agent.id === value.source_agent_id
    || !text(value.created_agent.name) || !text(value.created_agent.description) || !text(value.created_agent.system_prompt)
    || !positive(value.created_agent.connection_id) || !text(value.created_agent.model_id) || typeof value.created_agent.temperature !== 'string'
    || !revision(value.created_agent.max_tokens) || !validAvatar(value.created_agent.avatar)
    || !object(value.conversation) || !positive(value.conversation.room_id) || value.conversation.room_id === roomId || !positive(value.conversation.project_id)
    || value.conversation.href !== `/chat/${value.conversation.room_id}` || !validBotMessageRef(value.request_ref) || !validBotMessageRef(value.result_ref)
    || value.request_ref.room_id !== roomId || value.request_ref.kind !== 'activity' || value.request_ref.actor_type !== 'agent' || value.request_ref.actor_id !== value.source_agent_id
    || value.result_ref.room_id !== roomId || value.result_ref.kind !== 'activity' || value.result_ref.actor_type !== 'system' || value.result_ref.actor_id !== null
    || value.root_ref.seq >= value.request_ref.seq || value.request_ref.seq >= value.result_ref.seq
    || !object(value.model_binding) || value.model_binding.connection_id !== value.created_agent.connection_id || value.model_binding.model_id !== value.created_agent.model_id
    || !revision(value.model_binding.config_revision) || !revision(value.model_binding.auth_grant_revision)
    || !object(value.readback) || typeof value.readback.agent_matches !== 'boolean' || typeof value.readback.conversation_matches !== 'boolean'
    || typeof value.readback.available !== 'boolean' || value.readback.available && (!value.readback.agent_matches || !value.readback.conversation_matches)
    || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at)) || typeof value.replayed !== 'boolean') return false
  return true
}
function owner() {
  const state = useAuthStore.getState(), id = state.user?.id
  if (!state.token || !positive(id)) throw new Error('Bot creation owner unavailable')
  return { id, epoch: state.epoch }
}
function unchanged(before: ReturnType<typeof owner>) {
  const state = useAuthStore.getState()
  if (!state.token || state.user?.id !== before.id || state.epoch !== before.epoch) throw new Error('Bot creation owner changed')
}
export const botCreationsApi = {
  list: async (roomId: number): Promise<RoomBotCreations> => {
    const before = owner(), { data } = await api.get<RoomBotCreations>(`/bots/conversations/${roomId}/creations`)
    unchanged(before)
    if (!data || data.version !== 1 || data.room_id !== roomId || !Array.isArray(data.creations) || data.creations.length > 100
      || !data.creations.every(row => validBotCreation(row, roomId, before.id)) || new Set(data.creations.map(row => row.creation_id)).size !== data.creations.length
      || typeof data.creations_truncated !== 'boolean') throw new Error('Bot creation response unavailable')
    return data
  },
  read: async (creationId: string, roomId: number, rootId: string): Promise<BotCreationReceipt> => {
    const before = owner()
    if (!canonicalEventId(creationId) || !positive(roomId) || !canonicalEventId(rootId)) throw new Error('Bot creation identity unavailable')
    const { data } = await api.get<BotCreationReceipt>(`/bots/creations/${creationId}`)
    unchanged(before)
    if (!validBotCreation(data, roomId, before.id) || data.creation_id !== creationId || data.root_ref.event_id !== rootId) throw new Error('Bot creation response unavailable')
    return data
  },
}
