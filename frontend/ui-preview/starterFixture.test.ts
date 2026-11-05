import { expect, it } from 'vitest'
import { createdStarterFixture } from './starterFixture'

it('materializes a newly applied starter as the exact two-role idle room without inherited work', () => {
  const intent = { project_title: '다크 모드', room_title: '첫 협업 결정', agents: [
    { role: 'proposer' as const, name: '기획 담당', connection_id: 1, model_id: 'model-a' },
    { role: 'reviewer' as const, name: '검토 담당', connection_id: 2, model_id: 'model-b' },
  ] }
  const fixture = createdStarterFixture(intent, { plan_hash: 'a'.repeat(64), project_title: '다크 모드 2', room_title: '첫 협업 결정',
    agents: intent.agents.map((agent) => ({ ...agent, requested_name: agent.name, name: `${agent.name} 2`,
      test_history_id: 1, config_revision: 1, config_fingerprint: 'b'.repeat(64) })),
    policy: { max_rounds: 2, max_tool_calls: 0, max_executor_seconds: 60 } }, '2026-10-06T00:00:00Z')

  expect(fixture.project.name).toBe('다크 모드 2')
  expect(fixture.room.title).toBe('첫 협업 결정')
  expect(fixture.agents.map((agent) => [agent.name, agent.connection_id, agent.model_id])).toEqual([
    ['기획 담당 2', 1, 'model-a'], ['검토 담당 2', 2, 'model-b'],
  ])
  expect(fixture.members.map((member) => member.run_state)).toEqual(['idle', 'idle'])
  expect(fixture.schedule).toEqual({ max_rounds: 2, max_tool_calls: 0, max_executor_seconds: 60 })
  expect(fixture.events).toEqual([])
  expect(fixture.actions).toEqual([])
  expect(fixture.result.agents).toEqual({ proposer: 20, reviewer: 21 })
})
