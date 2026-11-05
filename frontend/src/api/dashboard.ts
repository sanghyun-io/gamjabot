import api from '../lib/api'

export type DashboardNextAction = 'connection' | 'agent' | 'project' | 'room' | null
export type DashboardWorkStatus = 'idle' | 'running' | 'waiting' | 'paused'

export interface DashboardRoom {
  room_id: number
  personal_agent_id?: number | null
  room_title: string
  project_id: number
  project_name: string
  last_event_seq: number
  last_activity_at: string
  last_authored_seq: number | null
  last_authored_at: string | null
  last_authored_preview: string | null
  unread_authored: number
  waiting_approvals: number
  active_failures: number
  work_status: DashboardWorkStatus
}

export interface DashboardSummary {
  setup: { next_action: DashboardNextAction }
  counts: {
    enabled_connections: number
    ready_agents: number
    ready_projects: number
    active_rooms: number
    unread_authored: number
    waiting_approvals: number
    active_failures: number
  }
  rooms: DashboardRoom[]
}

export const dashboardApi = {
  get: async (limit = 6) => (await api.get<DashboardSummary>('/dashboard', { params: { limit } })).data,
}
