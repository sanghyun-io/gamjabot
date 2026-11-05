import api from '../lib/api'

export type RoomIndexStatus = 'active' | 'closed' | 'all'
export interface RoomIndexItem {
  room_id: number; room_title: string; project_id: number; project_name: string
  room_active: boolean; project_active: boolean; effective_active: boolean; read_only: boolean
  pinned: boolean; pinned_at: string | null; last_event_seq: number; last_activity_at: string
  unread_authored: number; waiting_approvals: number; active_failures: number
  attention_count: number; work_status: 'idle' | 'running' | 'waiting' | 'paused'
}
export interface RoomIndexPage { items: RoomIndexItem[]; next_cursor: string | null }

export const roomIndexApi = {
  list: async (params: { status: RoomIndexStatus; project_id?: number; q?: string; limit?: number; cursor?: string }) =>
    (await api.get<RoomIndexPage>('/rooms/index', { params })).data,
  pin: async (roomId: number) => (await api.put<{ room_id: number; pinned: true; pinned_at: string }>(`/rooms/${roomId}/pin`)).data,
  unpin: async (roomId: number) => { await api.delete(`/rooms/${roomId}/pin`) },
}
