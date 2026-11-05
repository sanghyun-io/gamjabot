import type { StarterIntent, StarterPreview } from '../src/api/starter'

export function createdStarterFixture(intent: StarterIntent, preview: StarterPreview, createdAt: string) {
  const agents = preview.agents.map((agent, index) => ({
    id: 20 + index,
    user_id: 1, created_at: createdAt, updated_at: createdAt,
    name: agent.name,
    description: agent.role === 'proposer' ? '첫 협업 제안 역할' : '첫 협업 검토 역할',
    system_prompt: '',
    connection_id: agent.connection_id,
    model_id: agent.model_id,
    model: agent.model_id,
    temperature: 'auto',
    max_tokens: 0,
    is_active: true,
  }))
  return {
    project: { id: 84, user_id: 1, name: preview.project_title, description: '', is_active: true,
      archived_at: null, created_at: createdAt, updated_at: createdAt, agents },
    room: { id: 584, user_id: 1, project_id: 84, title: preview.room_title, is_active: true,
      created_at: createdAt, updated_at: createdAt },
    agents,
    members: agents.map((agent) => ({ room_id: 584, agent_id: agent.id, is_active: true,
      joined_at: createdAt, paused: false, run_state: 'idle' as const })),
    schedule: preview.policy,
    result: { project_id: 84, room_id: 584, agents: {
      proposer: agents[intent.agents.findIndex((agent) => agent.role === 'proposer')].id,
      reviewer: agents[intent.agents.findIndex((agent) => agent.role === 'reviewer')].id,
    } },
    events: [],
    actions: [],
  }
}
