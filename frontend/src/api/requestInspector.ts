import api from '../lib/api'

export type RequestExecutionPhase = 'queued' | 'running' | 'waiting_approval' | 'completed' | 'cancelled' | 'interrupted' | 'unknown'
export type RequestOutcomeStatus = 'pending' | 'succeeded' | 'partial' | 'failed' | 'unknown'
export type RequestOutcomeCode = 'response' | 'passed' | 'failure_connection' | 'failure_context' | 'failure_tool' | 'failure_permission' | 'failure_uncertain' | 'failure_other' | 'failure_teammate_result_incomplete'
export type RequestActionCode = 'action_process' | 'action_files' | 'action_browser' | 'action_screen' | 'action_input' | 'action_other'

export interface RequestInspectorResponse {
  version: 1
  room_id: number
  room_title: string
  read_only: boolean
  request: { event_id: string; seq: number; created_at: string; actor_name_snapshot: string; preview: string; href: string }
  execution: {
    phase: RequestExecutionPhase
    selected_agents: Array<{ agent_id: number; name_snapshot: string | null; live_state: 'running' | 'waiting' | 'paused' | 'idle' | 'stopped' | 'unknown' | null }>
    selected_agent_count: number
    policy: { max_rounds: number; max_tool_calls: number; max_executor_seconds: number } | null
    current_round: number | null
  }
  completion: {
    state: RequestExecutionPhase
    outcome_status: RequestOutcomeStatus
    result_available: boolean
    completed_rounds: number
    pending_agent_count: number
  }
  outcomes: Array<{
    event_id: string; seq: number; round: number; agent_id: number; actor_type?: 'agent' | 'system'; agent_name_snapshot: string | null
    kind: 'message' | 'failure' | 'passed'; phase: 'succeeded' | 'failed' | 'passed'; summary_code: RequestOutcomeCode
    model_use: { connection_id: number; model_id: string; provider_kind: string; watermark_seq: number } | null
    href: string
  }>
  outcome_count: number
  outcomes_truncated: boolean
  actions: Array<{
    action_id: string; agent_id: number; state: 'pending' | 'waiting' | 'approved' | 'started' | 'completed' | 'failed' | 'denied' | 'cancelled' | 'uncertain' | 'unknown'
    phase: 'queued' | 'approval' | 'running' | 'completed' | 'failed' | 'denied' | 'cancelled' | 'uncertain' | 'unknown'
    summary_code: RequestActionCode; requested_at: string | null; started_at: string | null; completed_at: string | null
    result_event_id: string | null; result_seq: number | null; href: string
  }>
  collaboration_checks?: Array<{ event_id: string; seq: number; agent_id: number; kind: 'teammates' | 'bots'; state: 'completed' | 'failed'; shown_count: number | null; has_more: boolean | null }>
  collaboration_check_count?: number
  collaboration_checks_truncated?: boolean
  action_count: number
  actions_truncated: boolean
}

export const requestInspectorApi = {
  get: async (roomId: number, triggerEventId: string) => (await api.get<RequestInspectorResponse>(
    `/conversations/${roomId}/requests/${triggerEventId}`)).data,
}
