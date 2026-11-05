import api from '../lib/api'

export type FinalizeBlockedReason = 'room_read_only'|'source_invalid'|'source_not_terminal'|'source_outcome_unknown'|'source_no_useful_result'|'source_all_passed'|'source_all_failed'|'source_too_large'|'action_unsettled'|'action_uncertain'|'completed_effect'|'room_busy'|'agent_not_selected'|'agent_unavailable'|'agent_busy'|'model_unavailable'|'context_too_large'

export interface FinalizePreview {
  version: 1
  room_id: number
  source_request: { event_id: string; seq: number; preview: string; href: string } | null
  source_completion: { state: string; outcome_status: string; result_available: boolean } | null
  outcomes: Array<{ event_id: string; seq: number; round: number; agent_id: number; agent_name_snapshot: string | null
    kind: 'message'|'failure'|'passed'; phase: 'succeeded'|'failed'|'passed'; summary_code: string
    model_use: { connection_id: number; model_id: string; provider_kind: string; watermark_seq: number } | null; href: string }>
  outcome_count: number
  outcomes_truncated: boolean
  excluded_agents: Array<{ agent_id: number; name_snapshot: string | null; reason: string }>
  agent: { agent_id: number; name_snapshot: string | null; current_name: string | null } | null
  current_model: { connection_id: number; model_id: string; provider_kind: string } | null
  limits: { max_rounds: 1; max_tool_calls: 0; max_executor_seconds: number } | null
  source_coverage: { source_request_event_id: string; source_outcome_event_ids: string[]; included_message_event_ids: string[]
    omitted_event_ids: string[]; truncations: Array<{ event_id: string; original_chars: number; included_chars: number; omitted_chars: number }>
    used_chars: number; limit_chars: number } | null
  eligible: boolean
  blocked_reason: FinalizeBlockedReason | null
  plan_hash: string | null
}

export interface FinalizeResult {
  version: 1
  room_id: number
  source_request_event_id: string
  source_outcome_event_ids: string[]
  finalize_event_id: string
  finalize_seq: number
  agent_id: number
  replayed: boolean
  href: string
}

export const requestFinalizeApi = {
  preview: async (roomId: number, sourceRequestEventId: string, agentId: number) =>
    (await api.post<FinalizePreview>(`/conversations/${roomId}/requests/${sourceRequestEventId}/finalize/preview`, { agent_id: agentId })).data,
  execute: async (roomId: number, sourceRequestEventId: string, body: { agent_id: number; plan_hash: string }, idempotencyKey: string) =>
    (await api.post<FinalizeResult>(`/conversations/${roomId}/requests/${sourceRequestEventId}/finalize`, body,
      { headers: { 'Idempotency-Key': idempotencyKey } })).data,
}
