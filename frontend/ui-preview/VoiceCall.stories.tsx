import type { Meta, StoryObj } from '@storybook/react-vite'
import { MemoryRouter } from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { VoiceCallPanel } from '../src/components/VoiceCall'
import type { CallPhase, CallTarget, CallView, VoiceCallUIController } from '../src/lib/voiceCallController'
import type { VoiceCallRecord } from '../src/api/voiceCalls'

// A static injected controller: no OS, microphone, IPC or network calls.
const target: CallTarget = { ownerId: 1, epoch: 1, roomId: 501, agentId: 7, title: '감자', generation: 0, canBegin: () => true }
const call: VoiceCallRecord = { id: '11111111-1111-4111-8111-111111111111', owner_id: 1, room_id: 501, agent_id: 7, status: 'active', started_at: '2026-11-03T00:00:00Z', ended_at: null, duration_ms: 65000, turns: [{ client_turn_id: '22222222-2222-4222-8222-222222222222', text: '내일 일정을 알려줘.', user_event_ref: { event_id:'33333333-3333-4333-8333-333333333333', seq: 1 }, phase:'completed', reply_event_ref:{ event_id:'44444444-4444-4444-8444-444444444444', seq:2 }, reply_text:'오후 세 시에 회의가 있습니다.', replies:[{event_ref:{event_id:'44444444-4444-4444-8444-444444444444',seq:2},text:'오후 세 시에 회의가 있습니다.'}] }] }
function Surface({ phase, notice }: { phase: CallPhase; notice?: string }) {
  const view: CallView = { phase, target: phase === 'confirm' ? null : target, proposed: phase === 'confirm' ? target : null, call: phase === 'confirm' ? null : { ...call, status: phase === 'ended' ? 'ended' : 'active' }, muted: phase === 'muted', notice: notice ?? (phase === 'approval' ? 'approval' : phase === 'unknown' ? 'turn_unknown' : ''), voices:[{id:'synthetic',name:'가상 목소리',language:'ko-KR'}], voice:'', language:'ko-KR', rate:0.5 }
  const controller: VoiceCallUIController = { subscribe: () => () => {}, snapshot: () => view, confirm() {}, dismiss() {}, hangup() {}, mute() {}, resume() {}, refresh() {}, settings() {} }
  return <I18nProvider><MemoryRouter><VoiceCallPanel controller={controller}/></MemoryRouter></I18nProvider>
}
const meta = { title:'Screens/음성 통화', component:Surface } satisfies Meta<typeof Surface>
export default meta
type Story = StoryObj<typeof meta>
export const Start: Story = { args:{phase:'confirm'} }
export const Listening: Story = { args:{phase:'listening'} }
export const Responding: Story = { args:{phase:'responding'} }
export const Speaking: Story = { args:{phase:'speaking'} }
export const Muted: Story = { args:{phase:'muted'} }
export const Approval: Story = { args:{phase:'approval'} }
export const Unknown: Story = { args:{phase:'unknown'} }
export const Ended: Story = { args:{phase:'ended'} }
export const EndUnknown: Story = { args:{phase:'unknown', notice:'end_unknown'} }
