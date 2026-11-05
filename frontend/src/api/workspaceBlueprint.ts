import api from '../lib/api'

export interface BlueprintConnectionV1 {
  ref: string
  provider_kind: string
  label: string
  default_model_id: string
  base_url: string | null
  allow_local_http: boolean
  auth_required: boolean
  enabled: boolean
  capabilities: Partial<Record<'max_tokens' | 'temperature' | 'tools' | 'vision', boolean>>
}

export interface BlueprintConnectionV2 {
  ref: string
  label: string
  default_model_id: string
  enabled: boolean
  capabilities: Partial<Record<'max_tokens' | 'temperature' | 'tools' | 'vision', boolean>>
  public_config: {
    version: 1
    provider_id: string
    protocol_id: 'openai_chat_completions' | 'anthropic_messages' | 'gemini_generate_content'
    auth_method_id: 'api_key' | 'local_none' | 'oauth_browser' | 'oauth_device'
    fields: Record<string, string | boolean>
  }
}

export interface BlueprintAgent {
  ref: string
  name: string
  description: string
  system_prompt: string
  connection_ref: string
  model_id: string
  temperature: string
  max_tokens: number
}

export interface BlueprintRoomTemplate {
  ref: string
  name: string
  room_title: string
  agent_refs: string[]
  max_rounds: number
  max_tool_calls: number
  max_executor_seconds: number
}

export interface BlueprintProject {
  ref: string
  name: string
  description: string | null
  agent_refs: string[]
  room_templates: BlueprintRoomTemplate[]
}

export interface WorkspaceBlueprintV1 {
  version: 1
  connections: BlueprintConnectionV1[]
  agents: BlueprintAgent[]
  projects: BlueprintProject[]
}

export interface WorkspaceBlueprintV2 {
  version: 2
  connections: BlueprintConnectionV2[]
  agents: BlueprintAgent[]
  projects: BlueprintProject[]
}

export type WorkspaceBlueprint = WorkspaceBlueprintV1 | WorkspaceBlueprintV2

export interface BlueprintCounts {
  connections: number
  agents: number
  projects: number
  project_assignments: number
  room_templates: number
  template_members: number
}

interface ResolvedName { ref: string; source_name: string; target_name: string }
interface ResolvedTemplateName extends ResolvedName { project_ref: string }

export interface BlueprintPreview {
  version: 1
  plan_hash: string
  counts: BlueprintCounts
  connections: ResolvedName[]
  agents: ResolvedName[]
  projects: ResolvedName[]
  room_templates: ResolvedTemplateName[]
  warnings: Array<{ code: 'connections_need_credentials'; count: number }>
}

export interface BlueprintApplyResult {
  plan_hash: string
  counts: BlueprintCounts
  connections: Array<{ ref: string; id: number; label: string }>
  agents: Array<{ ref: string; id: number; name: string }>
  projects: Array<{ ref: string; id: number; name: string }>
  room_templates: Array<{ ref: string; project_ref: string; id: number; name: string }>
  credentials_unset: number
}

export const workspaceBlueprintApi = {
  export: async () => (await api.get<WorkspaceBlueprint>('/workspace/blueprint')).data,
  preview: async (blueprint: WorkspaceBlueprint) =>
    (await api.post<BlueprintPreview>('/workspace/blueprint/preview', blueprint)).data,
  apply: async (blueprint: WorkspaceBlueprint, planHash: string) =>
    (await api.post<BlueprintApplyResult>('/workspace/blueprint/apply', { blueprint, plan_hash: planHash })).data,
}
