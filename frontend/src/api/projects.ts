import api from '../lib/api'
import type { Agent } from './agents'

export interface Project {
  id: number
  user_id: number
  name: string
  description: string | null
  is_active: boolean
  archived_at?: string | null
  created_at: string
  updated_at: string | null
  room_summary?: { total_rooms: number; active_rooms: number; latest_activity_at: string | null } | null
  agents?: Agent[]
}

export type ProjectStatus = 'active' | 'archived' | 'all'

export interface ProjectArchiveCounts {
  active_rooms: number
  pending_triggers: number
  running_triggers: number
  running_runs: number
  waiting_runs: number
  waiting_approvals: number
  started_actions_uncertain: number
  open_actions_cancelled: number
  grants_revoked: number
}

export interface ProjectArchivePreview {
  project_id: number
  plan_hash: string
  counts: ProjectArchiveCounts
  rooms: Array<{ id: number; title: string; last_event_seq: number; active_work_count: number }>
  rooms_truncated: boolean
}

export interface ProjectArchiveResult {
  project_id: number
  is_active: false
  archived_at: string
  plan_hash: string
  counts: ProjectArchiveCounts
  cleanup_unconfirmed: number[]
}

export interface ProjectRestoreResult {
  project_id: number
  is_active: true
  closed_rooms: number
}

export const projectsApi = {
  list: async (status: ProjectStatus = 'active') => {
    const response = await api.get<Project[]>('/projects', { params: { status } })
    return response.data
  },

  get: async (id: number) => {
    const response = await api.get<Project>(`/projects/${id}`)
    return response.data
  },

  create: async (data: { name: string; description?: string }) => {
    const response = await api.post<Project>('/projects', data)
    return response.data
  },

  update: async (id: number, data: Partial<Project>) => {
    const response = await api.put<Project>(`/projects/${id}`, data)
    return response.data
  },

  delete: async (id: number) => {
    await api.delete(`/projects/${id}`)
  },

  previewArchive: async (id: number) => {
    const response = await api.post<ProjectArchivePreview>(`/projects/${id}/archive/preview`)
    return response.data
  },

  archive: async (id: number, planHash: string) => {
    const response = await api.post<ProjectArchiveResult>(`/projects/${id}/archive`, { plan_hash: planHash })
    return response.data
  },

  restore: async (id: number) => {
    const response = await api.post<ProjectRestoreResult>(`/projects/${id}/restore`)
    return response.data
  },

  addAgent: async (projectId: number, agentId: number) => {
    await api.post(`/projects/${projectId}/agents/${agentId}`)
  },

  removeAgent: async (projectId: number, agentId: number) => {
    await api.delete(`/projects/${projectId}/agents/${agentId}`)
  },
}
