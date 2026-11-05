import type { ReplyReference } from './events'
import api from '../lib/api'

export type ContextPreviewError = 'model_binding_missing'|'context_state_missing'|'context_input_too_large'|'context_memory_too_large'|'context_too_large'
export interface ContextPreviewCoverage {
  committed_high_water: number
  included_event_seqs: number[]
  summarized_ranges: Array<{ kind: 'extractive_checkpoint'|'owner_checkpoint'; source_start_seq: number; source_end_seq: number }>
  draft_included: boolean
}
export interface AgentContextPreview {
  agent_id: number; agent_name: string; model_id: string|null
  projection_status: 'ready'|'unavailable'; error_code: ContextPreviewError|null
  system_text: string; messages: Array<{ role: 'user'|'assistant'; content: string }>
  source_coverage: ContextPreviewCoverage
  truncations: Array<{ event_id: string; seq: number; original_chars: number; included_chars: number; omitted_chars: number }>
  non_text: Array<{ event_id: string; seq: number; kind: 'screenshot_reference'; supplied: false; image_mime: string; width: number; height: number }>
  memory_projection?: { agent_id: number; room_revision: number; agent_revision: number; context_after_seq: number; legacy_disabled: boolean; entries: Array<{ id: string; revision: number; scope: 'room' | 'agent'; target_agent_id: number | null }>; serialized_chars: number
    bot_private?: { scope: 'bot_private'; revision: number; entries: Array<{ id: string; revision: number; scope: 'bot_private' }> } | null } | null
  budget: { unit: 'characters'; limit_chars: number; used_chars: number; remaining_chars: number }
}
export interface ContextPreviewResponse {
  room_id: number; projected_seq: number; selected_agent_ids: number[]; fingerprint: string; agents: AgentContextPreview[]
}

export const contextPreviewsApi = {
  create: async (roomId: number, content: string, replyTo: ReplyReference | null = null) => (await api.post<ContextPreviewResponse>(`/conversations/${roomId}/context-preview`, { content, reply_to: replyTo }, { timeout: 30000 })).data,
}
