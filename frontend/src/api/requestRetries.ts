import api from '../lib/api'

export type RequestRetryBlockedReason = 'room_read_only' | 'source_invalid' | 'source_not_terminal' | 'failure_superseded'
  | 'teammate_result_incomplete' | 'context_unavailable' | 'room_busy' | 'agent_unavailable' | 'agent_busy' | 'model_unavailable' | 'action_unsettled' | 'action_uncertain' | 'completed_effect' | 'memory_context_changed'

export interface RequestRetryPreflight {
  version: 1
  room_id: number
  failure_event_id: string
  failure_seq: number
  source_request: { event_id: string; seq: number; preview: string; href: string } | null
  agent: { agent_id: number; name_snapshot: string | null; current_name: string | null } | null
  current_model: { connection_id: number; model_id: string; provider_kind: string } | null
  limits: { max_rounds: 1; max_tool_calls: number; max_executor_seconds: number } | null
  teammate_recovery?: ({ version: 1; direct_count: 1; nested_count: number; total_chars: number; single_attempt: true } | { version: 2; direct_count: 1; nested_count: number; total_chars: number; attempt_number: number; single_attempt: false }) | null
  eligible: boolean
  blocked_reason: RequestRetryBlockedReason | null
  plan_hash: string | null
}

export interface RequestRetryResult {
  version: 1
  room_id: number
  source_failure_event_id: string
  source_trigger_event_id: string
  source_request_event_id: string
  retry_event_id: string
  retry_seq: number
  agent_id: number
  replayed: boolean
  href: string
}

export const requestRetriesApi = {
  preflight: async (roomId: number, failureEventId: string) => (await api.get<RequestRetryPreflight>(
    `/conversations/${roomId}/failures/${failureEventId}/retry`)).data,
  start: async (roomId: number, failureEventId: string, planHash: string, idempotencyKey: string) => (await api.post<RequestRetryResult>(
    `/conversations/${roomId}/failures/${failureEventId}/retry`, { plan_hash: planHash },
    { headers: { 'Idempotency-Key': idempotencyKey } })).data,
}
