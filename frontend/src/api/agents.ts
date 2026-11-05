import api from '../lib/api'
import type { AvatarConfig } from '../lib/avatarRecipe'

export interface Agent {
  id: number
  avatar?: AvatarConfig
  user_id: number
  name: string
  description: string
  system_prompt: string
  model: string
  connection_id: number | null
  model_id: string | null
  temperature: string
  max_tokens: number
  is_active: boolean
  created_at: string
  updated_at: string | null
}

export type AgentInput = Pick<Agent, 'name' | 'description' | 'system_prompt' | 'temperature' | 'max_tokens'> & {
  connection_id: number
  model_id: string
  avatar?: AvatarConfig
}

export const agentsApi = {
  avatars: async (): Promise<Record<string, AvatarConfig>> => (await api.get('/agents/avatars')).data,
  list: async () => {
    const response = await api.get<Agent[]>('/agents')
    return response.data
  },

  get: async (id: number) => {
    const response = await api.get<Agent>(`/agents/${id}`)
    return response.data
  },

  create: async (data: AgentInput) => {
    const response = await api.post<Agent>('/agents', data)
    return response.data
  },

  update: async (id: number, data: Partial<AgentInput>) => {
    const response = await api.put<Agent>(`/agents/${id}`, data)
    return response.data
  },

  delete: async (id: number) => {
    await api.delete(`/agents/${id}`)
  },
}


export type InactiveAgentReference=Pick<Agent,'id'|'name'|'connection_id'|'model_id'|'model'|'is_active'|'updated_at'>
export type InactiveAgentReferencePage={items:InactiveAgentReference[];next_after_id:number|null}
export const inactiveAgentReferencesApi={
 list:async(connectionId:number,afterId=0)=>{
  const response=await api.get<InactiveAgentReferencePage>('/agents/inactive-connections',{params:{connection_id:connectionId,after_id:afterId,limit:50}})
  return response.data
 },
 detach:async(row:InactiveAgentReference)=>{
  const response=await api.put<InactiveAgentReference>(`/agents/${row.id}/inactive-connection`,{
   expected_connection_id:row.connection_id,expected_name:row.name,expected_model_id:row.model_id,expected_model:row.model,expected_updated_at:row.updated_at,
  })
  return response.data
 },
}
