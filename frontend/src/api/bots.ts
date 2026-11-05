import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { validAvatar, type AvatarConfig } from '../lib/avatarRecipe'

export type PrimaryBot = {
  version: 1; revision: number; agent_id: number | null
  agent: { id: number; name: string; description: string; model_id: string | null; avatar?: AvatarConfig | null; is_active: boolean } | null
  conversation: { room_id: number; project_id: number; href: string; active: boolean } | null
  availability: 'unset' | 'available' | 'agent_inactive' | 'room_inactive' | 'project_inactive'
}
export type BotConversation = {
  version: 1; agent_id: number; room_id: number; project_id: number; href: string; created: boolean; replayed: boolean
}
export type BotConversationMetadata = { version: 1; room_id: number; personal_agent_id: number | null; can_change_members: boolean }
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const primaryAvailability = new Set(['unset', 'available', 'agent_inactive', 'room_inactive', 'project_inactive'])
const ownerSnapshot = () => { const value = useAuthStore.getState(); return { id: value.user?.id, epoch: value.epoch } }
function unchanged(before: ReturnType<typeof ownerSnapshot>) {
  const current = useAuthStore.getState()
  if (!current.token || current.user?.id !== before.id || current.epoch !== before.epoch) throw new Error('Bot owner changed')
}
function primaryResult(value: PrimaryBot): PrimaryBot {
  if (!value || value.version !== 1 || !Number.isSafeInteger(value.revision) || value.revision < 0
    || !primaryAvailability.has(value.availability) || value.agent_id !== null && !positive(value.agent_id)
    || value.agent && (value.agent.id !== value.agent_id || typeof value.agent.name !== 'string'
      || typeof value.agent.description !== 'string' || typeof value.agent.is_active !== 'boolean'
      || value.agent.model_id !== null && typeof value.agent.model_id !== 'string'
      || value.agent.avatar != null && !validAvatar(value.agent.avatar))
    || value.conversation && (!positive(value.conversation.room_id) || !positive(value.conversation.project_id)
      || value.conversation.href !== `/chat/${value.conversation.room_id}` || typeof value.conversation.active !== 'boolean')) throw new Error('Primary Bot response unavailable')
  return value
}
export const botsApi = {
  metadata: async (roomId: number): Promise<BotConversationMetadata> => {
    const before = ownerSnapshot(), { data } = await api.get<BotConversationMetadata>(`/bots/conversations/${roomId}`)
    unchanged(before)
    if (!data || data.version !== 1 || data.room_id !== roomId || typeof data.can_change_members !== 'boolean'
      || data.personal_agent_id !== null && (!positive(data.personal_agent_id) || data.can_change_members)) throw new Error('Bot conversation identity unavailable')
    return data
  },
  primary: async (): Promise<PrimaryBot> => {
    const before = ownerSnapshot(), { data } = await api.get<PrimaryBot>('/bots/primary')
    unchanged(before); return primaryResult(data)
  },
  setPrimary: async (agentId: number | null, revision: number): Promise<PrimaryBot> => {
    const before = ownerSnapshot(), { data } = await api.put<PrimaryBot>('/bots/primary', { agent_id: agentId, expected_revision: revision })
    unchanged(before)
    const value = primaryResult(data)
    if (value.agent_id !== agentId) throw new Error('Primary Bot target changed')
    return value
  },
  conversation: async (agentId: number, replaceClosedRoomId?: number): Promise<BotConversation> => {
    const before = ownerSnapshot()
    const { data } = await api.post<BotConversation>(`/bots/${agentId}/conversation`,
      replaceClosedRoomId === undefined ? {} : { replace_closed_room_id: replaceClosedRoomId })
    unchanged(before)
    if (!data || data.version !== 1 || data.agent_id !== agentId || !positive(data.room_id) || !positive(data.project_id)
      || data.href !== `/chat/${data.room_id}` || typeof data.created !== 'boolean' || typeof data.replayed !== 'boolean') throw new Error('Bot conversation response unavailable')
    return data
  },
}
