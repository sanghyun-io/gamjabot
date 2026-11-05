// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GrantPrompt from './GrantPrompt'
import RecoveryBanner from './RecoveryBanner'
import { I18nProvider } from '../i18n'

const ko = (children: React.ReactNode) => <I18nProvider systemLanguages={() => ['ko']}>{children}</I18nProvider>

vi.mock('../api/tools', () => ({ toolsApi: { preview: vi.fn(), decide: vi.fn() } }))
import { toolsApi, type ActionPreview } from '../api/tools'
import { eventsApi } from '../api/events'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'

vi.mock('../api/events', () => ({ eventsApi: { send: vi.fn() } }))
const recoveryScope = () => ({ identity: `1:${useAuthStore.getState().epoch}`, generation: directoryViewGeneration(), ownerId: 1, failureKey: null, events: [] })

const action = { action_id: 'a1', room_id: 5, agent_id: 7, trigger_event_id: null, family: 'write_file', target: '/tmp/report.txt',
  expected_effect: 'replace report', state: 'waiting', scope: null, grant_id: null, requested_at: '', started_at: null, completed_at: null }
const preview: ActionPreview = { review_id: '550e8400-e29b-41d4-a716-446655440000', approval_policy: { scopes: ['once', 'room_session'], unverified_context_required: false, start_within_seconds: null }, action_id: 'a1', family: 'write_file', target: '/tmp/report.txt', scope_target: '/tmp/report.txt',
  scope_description: 'Agent may write different contents to this file in this room.', expected_effect: 'replace report',
  workflow_id: null, arguments: { path: '/tmp/report.txt', content: 'secret sample' }, payload_digest: 'digest' }

beforeEach(() => {
  useAuthStore.getState().setAuth('synthetic-owner', { id: 1, email: 'owner@example.invalid', username: 'owner' })
  vi.mocked(toolsApi.preview).mockResolvedValue(preview); vi.mocked(toolsApi.decide).mockResolvedValue({})
  vi.mocked(eventsApi.send).mockImplementation(async (roomId, content, key) => ({ event_id: 'continuation-1', room_id: roomId, seq: 1, device_id: null, actor_type: 'user', actor_id: 1, actor_name_snapshot: 'Owner', kind: 'message', payload: { content }, created_at: '', idempotency_key: key }))
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('shows exact action and wider session scope before sending the chosen grant', async () => {
  const refresh = vi.fn()
  render(ko(<GrantPrompt roomId={5} roomName="Topic" agentName="Ada" action={action} onRefresh={refresh} onStop={vi.fn()} />))
  await screen.findByRole('button', { name: /Ada의 파일 수정 작업을 이 방에서 허용/ })
  expect(screen.getByText(/Agent may write different contents/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /Ada의 파일 수정 작업을 이 방에서 허용/ }))
  await waitFor(() => expect(toolsApi.decide).toHaveBeenCalledWith(5, 'a1', 'approve', 'room_session', preview.review_id, false))
  expect(refresh).toHaveBeenCalled()
})

it('keeps consent targets readable while retaining fingerprints and supports pointer scroll', async () => {
  vi.mocked(toolsApi.preview).mockResolvedValueOnce({ ...preview, family: 'pointer_scroll',
    target: 'desktop:pointer@320,240 [interaction sha256:abcdef1234567890]', scope_target: 'desktop:pointer',
    expected_effect: 'Pointer scroll', arguments: { x: 320, y: 240, delta_y: 600 } })
  render(ko(<GrantPrompt roomId={5} roomName="Topic" agentName="Ada" action={{ ...action, family: 'pointer_scroll' }}
    onRefresh={vi.fn()} onStop={vi.fn()} />))

  expect(await screen.findByRole('heading', { name: 'Ada · 포인터 스크롤' })).toBeTruthy()
  expect(screen.getByText('대상: 화면 위치 320, 240')).toBeTruthy()
  expect(screen.queryByText('이 위치에서 화면을 스크롤합니다.')).toBeNull()
  const details = screen.getByText('자세히').closest('details')!
  expect(details.open).toBe(false)
  expect(details.textContent).toContain('interaction sha256:abcdef1234567890')
})

it('requires new inspection after a stale decision and never retains an approval preview', async () => {
  vi.mocked(toolsApi.decide).mockRejectedValueOnce({ response: { status: 409 } })
  const refresh = vi.fn()
  render(ko(<GrantPrompt roomId={5} roomName="Topic" agentName="Ada" action={action} onRefresh={refresh} onStop={vi.fn()} />))
  fireEvent.click(await screen.findByRole('button', { name: /Ada의 파일 수정 작업을 한 번 허용/ }))
  await screen.findByText(/승인 내용이 바뀌었습니다/)
  expect(screen.queryByRole('button', { name: /Ada의 파일 수정 작업을 한 번 허용/ })).toBeNull()
  expect(refresh).toHaveBeenCalled()
})

it('blocks continuation of uncertain effects until external state inspection is acknowledged', async () => {
  render(ko(<QueryClientProvider client={new QueryClient()}><RecoveryBanner {...recoveryScope()} roomId={5} uncertain={[{ ...action, state: 'uncertain' }]}
    failure={null} onContinue={vi.fn()} /></QueryClientProvider>))
  const button = screen.getByRole('button', { name: '확인 후 방 작업 계속하기' }) as HTMLButtonElement
  expect(button.disabled).toBe(true)
  fireEvent.click(screen.getByRole('checkbox'))
  expect(button.disabled).toBe(false)
  fireEvent.click(button)
  await waitFor(() => expect(eventsApi.send).toHaveBeenCalledTimes(1))
})

it('requires fresh inspection whenever the room or uncertain action set changes', () => {
  const client = new QueryClient()
  const banner = (roomId: number, actionId: string) => ko(<QueryClientProvider client={client}>
    <RecoveryBanner {...recoveryScope()} roomId={roomId} uncertain={[{ ...action, action_id: actionId, state: 'uncertain' }]} failure={null} onContinue={vi.fn()} />
  </QueryClientProvider>)
  const rendered = render(banner(5, 'action-A'))
  const continueButton = () => screen.getByRole('button', { name: '확인 후 방 작업 계속하기' }) as HTMLButtonElement
  fireEvent.click(screen.getByRole('checkbox'))
  expect(continueButton().disabled).toBe(false)
  rendered.rerender(banner(5, 'action-B'))
  expect(continueButton().disabled).toBe(true)
  fireEvent.click(screen.getByRole('checkbox'))
  expect(continueButton().disabled).toBe(false)
  rendered.rerender(banner(6, 'action-B'))
  expect(continueButton().disabled).toBe(true)
  rendered.rerender(banner(5, 'action-A'))
  expect(continueButton().disabled).toBe(true)
})
