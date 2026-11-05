import api from '../lib/api'
import { CanceledError } from 'axios'
import { useAuthStore } from '../stores/authStore'
import type { WorkState } from '../lib/workState'

export interface Conversation {
  id: number
  user_id: number
  project_id: number
  title: string
  is_active: boolean
  created_at: string
  updated_at: string | null
}

export interface RoomMember {
  room_id: number
  agent_id: number
  is_active: boolean
  joined_at: string
  resume_status?: string | null
  paused?: boolean
  run_state?: 'idle' | 'running' | 'waiting' | 'paused' | 'stopped'
  work_state?: WorkState | null
}

export interface Message {
  id: number
  conversation_id: number
  agent_id: number | null
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
}

export interface RoomLaunch {
  project_id: number
  title: string
  agent_ids: number[]
  max_rounds: number
  max_tool_calls: number
  max_executor_seconds: number
}

export const conversationsApi = {
  listByProject: async (projectId: number, status: 'active' | 'closed' | 'all' = 'active') => {
    const response = await api.get<Conversation[]>(`/conversations/project/${projectId}`, { params: { status } })
    return response.data
  },

  get: async (id: number) => {
    const response = await api.get<Conversation>(`/conversations/${id}`)
    return response.data
  },

  create: async (data: { project_id: number; title: string }) => {
    const response = await api.post<Conversation>('/conversations', data)
    return response.data
  },

  launch: async (data: RoomLaunch, idempotencyKey: string) =>
    (await api.post<Conversation>('/conversations/launch', data, { headers: { 'Idempotency-Key': idempotencyKey } })).data,

  delete: async (id: number) => {
    await api.delete(`/conversations/${id}`)
  },

  update: async (id: number, title: string) =>
    (await api.patch<Conversation>(`/conversations/${id}`, { title })).data,

  clone: async (id: number, data: { project_id: number; title: string }) =>
    (await api.post<Conversation>(`/conversations/${id}/clone`, data)).data,

  listMembers: async (id: number) => {
    const before = useAuthStore.getState()
    const owner = before.user?.id
    const epoch = before.epoch
    const response = await api.get<RoomMember[]>(`/conversations/${id}/members`)
    const after = useAuthStore.getState()
    if (after.user?.id !== owner || after.epoch !== epoch || !Array.isArray(response.data) ||
      response.data.some((member) => member.room_id !== id || !member.is_active ||
        !Number.isSafeInteger(member.agent_id) || member.agent_id <= 0)) throw new CanceledError('Room status changed')
    return response.data
  },
  addMember: async (id: number, agentId: number) => (await api.post<RoomMember>(`/conversations/${id}/members/${agentId}`)).data,
  removeMember: async (id: number, agentId: number) => { await api.delete(`/conversations/${id}/members/${agentId}`) },
  pauseMember: async (id: number, agentId: number) => { await api.post(`/conversations/${id}/members/${agentId}/pause`) },
  resumeMember: async (id: number, agentId: number) => { await api.post(`/conversations/${id}/members/${agentId}/resume`) },
  cancel: async (id: number, onScopeStop?: (stopId: string) => void) => {
    const response = await api.post(`/conversations/${id}/cancel`)
    const stopId = response.headers?.['x-gamja-scope-stop-id']
    if (typeof stopId === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(stopId)) onScopeStop?.(stopId)
  },
  stop: async (id: number) => { await api.post(`/conversations/${id}/stop`) },
  continue: async (id: number, content: string) => (await api.post(`/conversations/${id}/continue`, { content })).data,

  listMessages: async (conversationId: number) => {
    const response = await api.get<Message[]>(`/conversations/${conversationId}/messages`)
    return response.data
  },

  sendMessage: async (conversationId: number, content: string) => {
    const response = await api.post<Message[]>(`/conversations/${conversationId}/messages`, {
      content,
    })
    return response.data
  },
}
