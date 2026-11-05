import api from '../lib/api'

export type AttentionSummaryCode = 'approval_process'|'approval_files'|'approval_browser'|'approval_screen'|'approval_input'|'approval_other'
  | 'failure_connection'|'failure_context'|'failure_tool'|'failure_permission'|'failure_uncertain'|'failure_other'

export interface AttentionItem {
  id: string
  kind: 'approval'|'failure'
  occurred_at: string
  room_id: number
  room_title: string
  project_id: number
  project_name: string
  agent_id: number | null
  agent_name: string
  request_event_id: string | null
  request_seq: number | null
  action_id: string | null
  failure_event_id: string | null
  failure_seq: number | null
  summary_code: AttentionSummaryCode
  href: string
}

export interface AttentionPage { items: AttentionItem[]; next_cursor: string | null }

export const attentionApi = {
  list: async (cursor?: string, limit = 50) => (await api.get<AttentionPage>('/attention', {
    params: { limit, ...(cursor ? { cursor } : {}) },
  })).data,
}
