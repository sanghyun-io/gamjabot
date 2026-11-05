import api from '../lib/api'

export interface RoomSchedule {
  max_rounds: number
  max_tool_calls: number
  max_executor_seconds: number
}

export const schedulesApi = {
  get: async (roomId: number) => (await api.get<RoomSchedule>(`/conversations/${roomId}/schedule`)).data,
  put: async (roomId: number, schedule: RoomSchedule) =>
    (await api.put<RoomSchedule>(`/conversations/${roomId}/schedule`, schedule)).data,
}
