import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { canonicalEventId } from '../lib/peerReply'

export type TeammateHandoffState = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
export type TeammateReplyState = 'none' | 'pending' | 'running' | 'delivered' | 'failed' | 'cancelled'
export type TeammateHandoff = {
  handoff_id: string; root_request_event_id: string; root_request_seq: number
  parent_handoff_id: string | null; sender_agent_id: number; sender_name: string
  recipient_agent_id: number; recipient_name: string; content: string
  state: TeammateHandoffState; reply_state: TeammateReplyState
  request_event_id: string; request_seq: number
  result_event_id: string | null; result_seq: number | null
  return_event_id: string | null; return_seq: number | null
  depth: number; created_at: string; updated_at: string
}
export type RoomTeammates = {
  version: 1; room_id: number
  teammates: Array<{ agent_id: number; name: string; description: string; model_id: string | null }>
  handoffs: TeammateHandoff[]; handoffs_truncated: boolean
}

const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const states = new Set(['pending', 'running', 'completed', 'failed', 'cancelled'])
const replyStates = new Set(['none', 'pending', 'running', 'delivered', 'failed', 'cancelled'])
const reference = (id: unknown, seq: unknown) => id === null && seq === null || canonicalEventId(id) && positive(seq)

/** Accept recorded identities only; names and nearby messages never establish a handoff. */
function validHandoff(value: unknown): value is TeammateHandoff {
  if (!record(value) || !canonicalEventId(value.handoff_id) || !canonicalEventId(value.root_request_event_id)
    || !positive(value.root_request_seq) || !canonicalEventId(value.request_event_id) || !positive(value.request_seq)
    || value.root_request_seq >= value.request_seq || !positive(value.sender_agent_id) || !positive(value.recipient_agent_id)
    || value.sender_agent_id === value.recipient_agent_id || !positive(value.depth)
    || typeof value.sender_name !== 'string' || typeof value.recipient_name !== 'string' || typeof value.content !== 'string'
    || typeof value.state !== 'string' || !states.has(value.state) || typeof value.reply_state !== 'string' || !replyStates.has(value.reply_state)
    || !reference(value.result_event_id, value.result_seq) || !reference(value.return_event_id, value.return_seq)
    || value.parent_handoff_id !== null && !canonicalEventId(value.parent_handoff_id)
    || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at))
    || typeof value.updated_at !== 'string' || !Number.isFinite(Date.parse(value.updated_at))) return false
  if (positive(value.result_seq) && value.result_seq <= value.request_seq) return false
  if (positive(value.return_seq) && (!positive(value.result_seq) || value.return_seq <= value.result_seq)) return false
  return true
}

export const teammatesApi = {
  list: async (roomId: number): Promise<RoomTeammates> => {
    const before = useAuthStore.getState(), owner = before.user?.id, epoch = before.epoch
    const { data } = await api.get<RoomTeammates>(`/conversations/${roomId}/teammates`)
    const after = useAuthStore.getState()
    if (after.user?.id !== owner || after.epoch !== epoch || !data || data.version !== 1 || data.room_id !== roomId
      || !Array.isArray(data.teammates) || !Array.isArray(data.handoffs) || data.handoffs.length > 100
      || typeof data.handoffs_truncated !== 'boolean' || !data.handoffs.every(validHandoff)
      || new Set(data.handoffs.map(row => row.handoff_id)).size !== data.handoffs.length
      || !data.teammates.every(row => positive(row.agent_id) && typeof row.name === 'string' && typeof row.description === 'string'
        && (row.model_id === null || typeof row.model_id === 'string'))) throw new Error('Teammate response changed or unavailable')
    return data
  },
}
