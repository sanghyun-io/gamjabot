import api from '../lib/api'
import type { Conversation } from './conversations'

export interface RoomTemplateWrite {
  name: string
  room_title: string
  agent_ids: number[]
  max_rounds: number
  max_tool_calls: number
  max_executor_seconds: number
}

export interface RoomTemplate extends RoomTemplateWrite {
  id: number
  user_id: number
  project_id: number
  created_at: string
  updated_at: string | null
}

const base = (projectId: number) => `/projects/${projectId}/room-templates`

export const roomTemplatesApi = {
  list: async (projectId: number) => (await api.get<RoomTemplate[]>(base(projectId))).data,
  create: async (projectId: number, value: RoomTemplateWrite) =>
    (await api.post<RoomTemplate>(base(projectId), value)).data,
  update: async (projectId: number, templateId: number, value: RoomTemplateWrite) =>
    (await api.put<RoomTemplate>(`${base(projectId)}/${templateId}`, value)).data,
  delete: async (projectId: number, templateId: number) => {
    await api.delete(`${base(projectId)}/${templateId}`)
  },
  createRoom: async (projectId: number, templateId: number) =>
    (await api.post<Conversation>(`${base(projectId)}/${templateId}/rooms`, {})).data,
}
