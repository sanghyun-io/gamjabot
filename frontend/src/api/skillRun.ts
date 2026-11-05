import api from '../lib/api'
import type { RoomEvent } from './events'

export type SkillRunInput = { bot_id: number; skill_id: string; skill_revision: number; binding_revision: number; question: string; values: Record<string, string> }
export type SkillRunPreview = { basis: string; skill_id: string; skill_revision: number; binding_revision: number; bot_id: number; question: string; proof: Record<string,unknown>; variables: string[]; expanded_reference: string; target: { bot_id:number; bot_name:string; connection_id:number; connection_name:string; model_id:string; provider_id:string } }
export type SkillRunAdapter = {
  preview: (room: number, body: SkillRunInput, signal: AbortSignal) => Promise<SkillRunPreview>
  send: (room: number, body: SkillRunInput, basis: string, key: string, signal: AbortSignal) => Promise<RoomEvent>
  receipt: (room: number, key: string, signal: AbortSignal) => Promise<RoomEvent>
}
export const skillRunApi: SkillRunAdapter = {
  preview: async (room, body, signal) => (await api.post<SkillRunPreview>(`/conversations/${room}/skill-use/preview`, body, { signal })).data,
  send: async (room, body, basis, key, signal) => (await api.post<RoomEvent>(`/conversations/${room}/skill-use`, { ...body, basis, consent: true }, { signal, headers: { 'Idempotency-Key': key } })).data,
  receipt: async (room, key, signal) => (await api.get<RoomEvent>(`/conversations/${room}/skill-use/receipt`, { signal, headers: { 'Idempotency-Key': key } })).data,
}
