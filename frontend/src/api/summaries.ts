import api from '../lib/api'
import type { RoomEvent } from './events'

export interface HandoffNoteWrite {
  source_start_seq: number
  source_end_seq: number
  content: string
}

export const summariesApi = {
  create: async (roomId: number, value: HandoffNoteWrite, idempotencyKey: string) =>
    (await api.post<RoomEvent>(`/conversations/${roomId}/summaries`, value,
      { headers: { 'Idempotency-Key': idempotencyKey } })).data,
}
