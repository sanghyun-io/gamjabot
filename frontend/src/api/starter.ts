import api from '../lib/api'

export type StarterRole = 'proposer' | 'reviewer'
export interface StarterAgentIntent {
  role: StarterRole
  name: string
  connection_id: number
  model_id: string
}
export interface StarterIntent {
  project_title: string
  room_title: string
  agents: [StarterAgentIntent, StarterAgentIntent]
}
export interface StarterPreviewAgent extends StarterAgentIntent {
  requested_name: string
  test_history_id: number
  config_revision: number
  config_fingerprint: string
}
export interface StarterPreview {
  plan_hash: string
  project_title: string
  room_title: string
  agents: [StarterPreviewAgent, StarterPreviewAgent]
  policy: { max_rounds: 2; max_tool_calls: 0; max_executor_seconds: 60 }
}
export interface StarterResult {
  project_id: number
  room_id: number
  agents: { proposer: number; reviewer: number }
  replayed?: boolean
}

export const starterApi = {
  preview: async (intent: StarterIntent) => (await api.post<StarterPreview>('/starter/preview', intent)).data,
  apply: async (intent: StarterIntent & { plan_hash: string }, idempotencyKey: string) =>
    (await api.post<StarterResult>('/starter/apply', intent, { headers: { 'Idempotency-Key': idempotencyKey } })).data,
  lastResult: async () => (await api.get<StarterResult | null>('/starter/last-result')).data,
}
