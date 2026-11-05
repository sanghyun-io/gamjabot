import api from '../lib/api'
import type { ReplyReference } from './events'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { beginDraftObservation, observeDraft } from '../lib/composerDraftIndex'

export interface RoomDraft {
  content: string
  reply_to?: ReplyReference | null
  version: number
  idempotency_key: string | null
  updated_at: string | null
}

function observation(room: number) {
  const owner = useAuthStore.getState(), identity = `${owner.user?.id || 0}:${owner.epoch}`, generation = directoryViewGeneration()
  const request = beginDraftObservation(identity, generation, room)
  return (content: string, version: number) => {
    const current = useAuthStore.getState()
    if (current.token && identity === `${current.user?.id || 0}:${current.epoch}`) {
      try { observeDraft(identity, generation, room, request, content, version) } catch { /* Optional navigation cache must not change a completed draft API outcome. */ }
    }
  }
}
export const draftsApi = {
  get: async (roomId: number) => {
    const received = observation(roomId), value = (await api.get<RoomDraft>(`/conversations/${roomId}/draft`)).data
    received(value.content, value.version); return value
  },
  put: async (roomId: number, content: string, expectedVersion: number, replyTo: ReplyReference | null = null) => {
    const received = observation(roomId), value = (await api.put<RoomDraft>(`/conversations/${roomId}/draft`, { content, expected_version: expectedVersion, reply_to: replyTo })).data
    received(value.content, value.version); return value
  },
  clear: async (roomId: number, expectedVersion: number) => {
    const received = observation(roomId)
    await api.delete(`/conversations/${roomId}/draft`, { params: { expected_version: expectedVersion } })
    // Successful DELETE confirms empty at that operation, not current remote state.
    received('', expectedVersion + 1)
  },
}

export type StoredDraftItem = { room_id:number;room_title:string;project_id:number;project_name:string;read_only:boolean;version:number;character_count:number;preview:string;updated_at:string }
export type StoredDraftPage = { items:StoredDraftItem[];next_cursor:string|null }
export const storedDraftsApi = {
  list: async (query:string,status:'active'|'closed'|'all',cursor?:string) => (await api.get<StoredDraftPage>('/rooms/drafts',{params:{q:query,status,limit:50,cursor}})).data,
}
