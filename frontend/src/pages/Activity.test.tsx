// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Activity from './Activity'
import { I18nProvider } from '../i18n'

const ko = (children: React.ReactNode) => <I18nProvider systemLanguages={() => ['ko']}>{children}</I18nProvider>

vi.mock('../api/tools', () => ({ toolsApi: { activity: vi.fn(), grants: vi.fn(), broadMode: vi.fn(), lease: vi.fn(), revoke: vi.fn(), setBroadMode: vi.fn() } }))
import { toolsApi } from '../api/tools'

beforeEach(() => {
  vi.mocked(toolsApi.activity).mockResolvedValue([{ action_id: 'a', room_id: 1, agent_id: 7, trigger_event_id: null, family: 'screenshot', target: 'desktop',
    expected_effect: 'capture', state: 'started', scope: 'once', grant_id: 3, requested_at: '', started_at: '', completed_at: null }])
  vi.mocked(toolsApi.grants).mockResolvedValue([{ id: 3, room_id: 1, agent_id: 7, scope: 'room_session', families: ['screenshot'], targets: ['desktop'], decision: 'approved', expires_at: null }])
  vi.mocked(toolsApi.broadMode).mockResolvedValue({ enabled: true, scope: 'broad', expires_at: '2026-09-28T01:00:00Z' })
  vi.mocked(toolsApi.lease).mockResolvedValue({ active: true, owner: null, another_profile_or_room: true })
  vi.mocked(toolsApi.revoke).mockResolvedValue()
  vi.mocked(toolsApi.setBroadMode).mockResolvedValue({ enabled: false, scope: null, expires_at: null })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('keeps daily room context to one settings affordance and reveals private controls only on demand', async () => {
  render(ko(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Activity roomId={1} roomName="Room" agents={[{ id: 7, name: 'Ada' } as never]} onStop={vi.fn()} />
  </QueryClientProvider>))
  const settings = await screen.findByText('방 설정')
  const panel = settings.closest('details')
  expect(panel?.open).toBe(false)
  expect(screen.queryByText(/다른 방에서 데스크톱을 사용 중/)).toBeNull()
  expect(screen.queryByText('이 방의 권한 1개')).toBeNull()
  fireEvent.click(settings)
  expect(panel?.open).toBe(true)
  expect(await screen.findByText(/다른 방에서 데스크톱을 사용 중/)).toBeTruthy()
  expect(screen.queryByText(/workflow/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '무조건 승인 끄기' }))
  await waitFor(() => expect(toolsApi.setBroadMode).toHaveBeenCalledWith(false, undefined, false))
  fireEvent.click(screen.getByRole('button', { name: '권한 취소 Ada' }))
  await waitFor(() => expect(toolsApi.revoke).toHaveBeenCalledWith(1, 3))
})

it('keeps always approve off until the single settings panel is explicitly opened', async () => {
  vi.mocked(toolsApi.broadMode).mockResolvedValue({ enabled: false, scope: null, expires_at: null })
  vi.mocked(toolsApi.setBroadMode).mockResolvedValue({ enabled: true, scope: 'broad', expires_at: null })
  render(ko(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Activity roomId={1} roomName="Room" agents={[]} onStop={vi.fn()} />
  </QueryClientProvider>))
  const settings = await screen.findByText('방 설정')
  const details = settings.closest('details')
  expect(details?.open).toBe(false)
  expect(screen.queryByRole('button', { name: '직접 끌 때까지 무조건 승인' })).toBeNull()
  fireEvent.click(settings)
  expect(details?.open).toBe(true)
  const enable = await screen.findByRole('button', { name: '직접 끌 때까지 무조건 승인' })
  expect(screen.getByText(/직접 끌 때까지 유지/)).toBeTruthy()
  fireEvent.click(enable)
  await waitFor(() => expect(toolsApi.setBroadMode).toHaveBeenCalledWith(true, undefined, true))
})

it('keeps enabled broad controls inside the single settings panel', async () => {
  vi.mocked(toolsApi.broadMode).mockResolvedValue({ enabled: true, scope: 'broad', expires_at: null })
  render(ko(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Activity roomId={1} roomName="Room" agents={[]} onStop={vi.fn()} />
  </QueryClientProvider>))
  const settings = await screen.findByText('방 설정')
  expect(screen.queryByText(/모든 방의 에이전트가 이 컴퓨터의 파일과 화면을 다시 묻지 않고 다룰 수 있습니다/)).toBeNull()
  expect(screen.queryByRole('button', { name: '무조건 승인 끄기' })).toBeNull()
  fireEvent.click(settings)
  await screen.findByText(/모든 방의 에이전트가 명령 실행/)
  fireEvent.click(screen.getByRole('button', { name: '무조건 승인 끄기' }))
  await waitFor(() => expect(toolsApi.setBroadMode).toHaveBeenCalledWith(false, undefined, false))
})

it('links review, approval Owner/time and terminal state in one collapsed action record without inventing legacy approval', async () => {
  const review = '550e8400-e29b-41d4-a716-446655440000'
  vi.mocked(toolsApi.activity).mockResolvedValue([
    { action_id: 'owned-action', room_id: 1, agent_id: 7, trigger_event_id: null, family: 'write_file', target: '/owned', expected_effect: 'write',
      state: 'completed', scope: 'room_session', grant_id: 24, review_id: review, approval_owner_id: 1, approved_at: '2026-10-15T17:00:00Z',
      requested_at: '2026-10-15T16:59:00Z', started_at: '2026-10-15T17:00:01Z', completed_at: '2026-10-15T17:00:02Z' },
    { action_id: 'legacy-action', room_id: 1, agent_id: 7, trigger_event_id: null, family: 'read_file', target: '/legacy', expected_effect: 'read',
      state: 'completed', scope: null, grant_id: null, requested_at: '', started_at: null, completed_at: null },
  ])
  render(ko(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Activity roomId={1} roomName="Room" agents={[{ id: 7, name: 'Ada' } as never]} onStop={vi.fn()} />
  </QueryClientProvider>))
  fireEvent.click(await screen.findByText('방 설정'))
  const records = await screen.findAllByText('승인과 실행 기록')
  expect(records).toHaveLength(2)
  for (const summary of records) expect(summary.closest('details')?.open).toBe(false)
  fireEvent.click(records[1])
  const current = screen.getByText('owned-action').closest('details')!
  expect(current.textContent).toContain(review)
  expect(current.textContent).toContain('#1')
  expect(current.querySelectorAll('dd')).toHaveLength(9)
  expect(current.textContent).toContain('#24')
  const legacy = screen.getByText('legacy-action').closest('details')!
  expect(legacy.textContent).toContain('기록 없음 또는 확인 불가')
  expect(legacy.textContent).not.toContain(review)
})

it('shows the originating grant for reused permission without inventing a fresh manual approver or timestamp', async () => {
  vi.mocked(toolsApi.activity).mockResolvedValue([{ action_id: 'reused-action', room_id: 1, agent_id: 7, trigger_event_id: null,
    family: 'write_file', target: '/owned', expected_effect: 'write', state: 'completed', scope: 'room_session', grant_id: 25,
    review_id: '550e8400-e29b-41d4-a716-446655440000', approval_owner_id: null, approved_at: null,
    requested_at: '', started_at: '2026-10-15T17:22:25Z', completed_at: '2026-10-15T17:22:26Z' }])
  render(ko(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Activity roomId={1} roomName="Room" agents={[]} onStop={vi.fn()} />
  </QueryClientProvider>))
  fireEvent.click(await screen.findByText('방 설정'))
  const detail = (await screen.findByText('승인과 실행 기록')).closest('details')!
  fireEvent.click(within(detail).getByText('승인과 실행 기록'))
  expect(detail.textContent).toContain('#25')
  const approver = within(detail).getByText('승인한 Owner').nextElementSibling
  const approvedAt = within(detail).getByText('승인 시각').nextElementSibling
  expect(approver?.textContent).toBe('기록 없음 또는 확인 불가')
  expect(approvedAt?.textContent).toBe('기록 없음 또는 확인 불가')
})

it('renders UTC-naive server audit times as UTC instants with date and local clock through seconds', async () => {
  const stamps = ['2026-10-16T00:48:12.674149', '2026-10-16T00:48:13.712258', '2026-10-16T00:48:14.782301']
  vi.mocked(toolsApi.activity).mockResolvedValue([{ action_id: 'timed-action', room_id: 1, agent_id: 7, trigger_event_id: null,
    family: 'write_file', target: '/owned', expected_effect: 'write', state: 'completed', scope: 'once', grant_id: 27,
    review_id: '550e8400-e29b-41d4-a716-446655440000', approval_owner_id: 1, approved_at: stamps[0],
    requested_at: '', started_at: stamps[1], completed_at: stamps[2] }])
  render(ko(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Activity roomId={1} roomName="Room" agents={[]} onStop={vi.fn()} />
  </QueryClientProvider>))
  fireEvent.click(await screen.findByText('방 설정'))
  const detail = (await screen.findByText('승인과 실행 기록')).closest('details')!
  fireEvent.click(within(detail).getByText('승인과 실행 기록'))
  const times = Array.from(detail.querySelectorAll('time'))
  expect(times).toHaveLength(3)
  const formatter = new Intl.DateTimeFormat('ko', { dateStyle: 'medium', timeStyle: 'medium' })
  for (const [index, time] of times.entries()) {
    expect(time.dateTime).toBe(`${stamps[index]}Z`)
    expect(time.textContent).toBe(formatter.format(new Date(`${stamps[index]}Z`)))
  }
})

it('shows unavailable for malformed audit timestamps instead of rendering an invalid or rolled date', async () => {
  vi.mocked(toolsApi.activity).mockResolvedValue([{ action_id: 'malformed-time', room_id: 1, agent_id: 7, trigger_event_id: null,
    family: 'write_file', target: '/owned', expected_effect: 'write', state: 'completed', scope: 'once', grant_id: 27,
    approval_owner_id: 1, approved_at: '2026-02-30T00:00:00', requested_at: '', started_at: 'invalid', completed_at: null }])
  render(ko(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Activity roomId={1} roomName="Room" agents={[]} onStop={vi.fn()} />
  </QueryClientProvider>))
  fireEvent.click(await screen.findByText('방 설정'))
  const detail = (await screen.findByText('승인과 실행 기록')).closest('details')!
  for (const label of ['승인 시각', '시작 시각', '종료 시각']) {
    expect(within(detail).getByText(label).nextElementSibling?.textContent).toBe('기록 없음 또는 확인 불가')
  }
  expect(detail.querySelector('time')).toBeNull()
})
