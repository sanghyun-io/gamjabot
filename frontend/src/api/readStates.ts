import api from '../lib/api'

export interface RoomReadState {
  last_read_seq: number
  updated_at: string | null
}

export const readStatesApi = {
  get: async (roomId: number) => (await api.get<RoomReadState>(`/conversations/${roomId}/read-state`)).data,
  put: async (roomId: number, lastReadSeq: number) =>
    (await api.put<RoomReadState>(`/conversations/${roomId}/read-state`, { last_read_seq: lastReadSeq })).data,
}
