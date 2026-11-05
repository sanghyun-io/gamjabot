import api from '../lib/api'

export type WorkloadState = 'running'|'waiting'|'paused'|'failed'|'idle'|'unknown'
export interface AgentWorkloadRoom {
  room_id: number; room_title: string; project_id: number; project_name: string; state: WorkloadState
  trigger_event_id: string | null; trigger_seq: number | null; failure_event_id: string | null; failure_seq: number | null
  last_activity_at: string; href: string
}
export interface AgentWorkload {
  agent_id: number; agent_name: string; agent_active: boolean; overall_state: WorkloadState
  counts: Record<WorkloadState, number> & { total_rooms: number }
  rooms: AgentWorkloadRoom[]; rooms_truncated: boolean
}
export interface AgentWorkloadPage { items: AgentWorkload[]; next_cursor: string | null }

export const agentWorkloadsApi = {
  list: async (cursor?: string, agentId?: number, limit = 25) => (await api.get<AgentWorkloadPage>('/agent-workloads', {
    params: { limit, ...(agentId ? { agent_id: agentId } : {}), ...(cursor ? { cursor } : {}) },
  })).data,
}
