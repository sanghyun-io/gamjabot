import api from '../lib/api'
import type { ReplyReference } from './events'

export type CallTurnPhase = 'queued' | 'running' | 'waiting_approval' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'unknown'
export type VoiceCallTurn = { client_turn_id: string; text: string; user_event_ref: ReplyReference; phase: CallTurnPhase; reply_event_ref: ReplyReference | null; reply_text: string | null; replies: Array<{ event_ref: ReplyReference; text: string }> }
export type VoiceCallRecord = { id: string; owner_id: number; room_id: number; agent_id: number; status: 'active' | 'ended' | 'interrupted'; started_at: string; ended_at: string | null; duration_ms: number | null; turns: VoiceCallTurn[] }
const base = (room: number) => `/conversations/${room}/voice-calls`
export const voiceCallsApi = {
  list: async (room: number) => (await api.get<{ calls: VoiceCallRecord[] }>(base(room))).data,
  create: async (room: number, clientCallId: string, agentId: number) => (await api.post<VoiceCallRecord>(base(room), { client_call_id: clientCallId, agent_id: agentId })).data,
  get: async (room: number, id: string, options?: { signal?:AbortSignal; timeout?:number }) => (await api.get<VoiceCallRecord>(`${base(room)}/${id}`, options)).data,
  turn: async (room: number, id: string, clientTurnId: string, text: string) => (await api.post<VoiceCallRecord>(`${base(room)}/${id}/turns`, { client_turn_id: clientTurnId, text })).data,
  end: async (room: number, id: string) => (await api.post<VoiceCallRecord>(`${base(room)}/${id}/end`)).data,
  heartbeat: async (room: number, id: string, options?: { signal?:AbortSignal; timeout?:number }) => (await api.post<VoiceCallRecord>(`${base(room)}/${id}/heartbeat`, undefined, options)).data,
}
