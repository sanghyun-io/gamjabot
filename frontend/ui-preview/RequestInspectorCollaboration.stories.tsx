import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useMemo, useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import api from '../src/lib/api'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import RequestInspector from '../src/components/RequestInspector'
import type { RequestInspectorResponse } from '../src/api/requestInspector'

type Scenario = 'empty' | 'completed' | 'failed' | 'unknown' | 'teammate-incomplete' | 'teammate-retry-eligible' | 'teammate-retry-blocked' | 'teammate-retry-unknown' | 'teammate-chain-eligible' | 'teammate-chain-blocked' | 'teammate-chain-unknown'
const root = '11111111-1111-4111-8111-111111111111'
function Surface({ scenario }: { scenario: Scenario }) {
  const [epoch, setEpoch] = useState<number | null>(null)
  const isTeammate = scenario.startsWith('teammate-')
  useLayoutEffect(() => {
    useAuthStore.getState().setAuth('synthetic-token', { id: 1, email: 'preview@example.invalid', username: 'Preview' })
    const oldAdapter = api.defaults.adapter
    if (scenario.startsWith('teammate-retry-') || scenario.startsWith('teammate-chain-')) api.defaults.adapter = async config => {
      if (config.url !== '/conversations/501/failures/55555555-5555-4555-8555-555555555555/retry') throw new Error('[UI fixture missing]')
      if (config.method?.toLowerCase() === 'post') throw new Error('synthetic lost response')
      const blocked = scenario === 'teammate-retry-blocked' || scenario === 'teammate-chain-blocked'
      const chain = scenario.startsWith('teammate-chain-')
      return { config, status: 200, statusText: 'OK', headers: {}, data: {version:1,room_id:501,failure_event_id:'55555555-5555-4555-8555-555555555555',failure_seq:4,source_request:blocked?null:{event_id:root,seq:1,preview:'합성 요청',href:''},agent:blocked?null:{agent_id:2,name_snapshot:'감자',current_name:'감자'},current_model:blocked?null:{connection_id:1,model_id:'synthetic',provider_kind:'opencode'},limits:blocked?null:{max_rounds:1,max_tool_calls:8,max_executor_seconds:60},teammate_recovery:blocked?null:chain?{version:2,direct_count:1,nested_count:2,total_chars:4200,attempt_number:2,single_attempt:false}:{version:1,direct_count:1,nested_count:2,total_chars:4200,single_attempt:true},eligible:!blocked,blocked_reason:blocked?'teammate_result_incomplete':null,plan_hash:blocked?null:'a'.repeat(64)} }
    }
    setEpoch(useAuthStore.getState().epoch)
    return () => { api.defaults.adapter = oldAdapter }
  }, [scenario])
  const readRequest = useMemo(() => async (room: number, event: string): Promise<RequestInspectorResponse> => {
    const checks: NonNullable<RequestInspectorResponse['collaboration_checks']> = scenario === 'completed'
      ? [{ event_id: '22222222-2222-4222-8222-222222222222', seq: 3, agent_id: 2, kind: 'teammates', state: 'completed', shown_count: 2, has_more: true },
         { event_id: '33333333-3333-4333-8333-333333333333', seq: 4, agent_id: 2, kind: 'bots', state: 'completed', shown_count: 1, has_more: false }]
      : scenario === 'failed' ? [{ event_id: '44444444-4444-4444-8444-444444444444', seq: 3, agent_id: 2, kind: 'teammates', state: 'failed', shown_count: null, has_more: null }] : []
    return { version: 1, room_id: room, room_title: '합성 대화', read_only: !isTeammate,
      request: { event_id: event, seq: 1, created_at: '2026-11-04T00:00:00Z', actor_name_snapshot: '사용자', preview: '합성 요청', href: '' },
      execution: { phase: 'completed', selected_agents: [{ agent_id: 2, name_snapshot: '감자', live_state: null }], selected_agent_count: 1, policy: null, current_round: 1 },
      completion: { state: 'completed', outcome_status: isTeammate ? 'failed' : 'succeeded', result_available: !isTeammate, completed_rounds: 1, pending_agent_count: 0 },
      outcomes: isTeammate ? [{event_id:'55555555-5555-4555-8555-555555555555',seq:4,round:1,agent_id:2,agent_name_snapshot:'감자',kind:'failure',phase:'failed',summary_code:'failure_teammate_result_incomplete',model_use:null,href:'/chat/501?seq=4'}] : [], outcome_count: isTeammate ? 1 : 0, outcomes_truncated: false, actions: [], action_count: 0, actions_truncated: false,
      ...(scenario === 'unknown' ? {} : { collaboration_checks: checks, collaboration_check_count: checks.length, collaboration_checks_truncated: false }),
    }
  }, [scenario, isTeammate])
  return <MemoryRouter><I18nProvider><main className="page-frame page-content"><p>합성 기록만 표시합니다. 실제 요청이나 모델 호출은 없습니다.</p>
    {epoch !== null && <RequestInspector roomId={501} triggerEventId={root} ownerId={1} authEpoch={epoch} onClose={() => {}} onNavigate={() => {}} readRequest={readRequest}/>}</main></I18nProvider></MemoryRouter>
}
const meta = { title: 'Product/Request inspector collaboration', component: Surface } satisfies Meta<typeof Surface>
export default meta
type Story = StoryObj<typeof meta>
export const Empty: Story = { args: { scenario: 'empty' } }
export const Completed: Story = { args: { scenario: 'completed' } }
export const Failed: Story = { args: { scenario: 'failed' } }
export const Unknown: Story = { args: { scenario: 'unknown' } }

export const TeammateIncomplete: Story = { args: { scenario: 'teammate-incomplete' } }

export const TeammateRetryEligible: Story = { args: { scenario: 'teammate-retry-eligible' } }
export const TeammateRetryBlocked: Story = { args: { scenario: 'teammate-retry-blocked' } }
export const TeammateRetryUnknown: Story = { args: { scenario: 'teammate-retry-unknown' } }

export const TeammateChainEligible: Story = { args: { scenario: 'teammate-chain-eligible' } }
export const TeammateChainBlocked: Story = { args: { scenario: 'teammate-chain-blocked' } }
export const TeammateChainUnknown: Story = { args: { scenario: 'teammate-chain-unknown' } }
