// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RoomScheduleSettings from './RoomScheduleSettings'
import { schedulesApi, type RoomSchedule } from '../api/schedules'

vi.mock('../api/schedules', () => ({ schedulesApi: { get: vi.fn(), put: vi.fn() } }))
let authState = { user: { id: 1 }, epoch: 1 }
vi.mock('../stores/authStore', () => ({ useAuthStore: Object.assign(() => authState, { getState: () => authState }) }))

const saved: RoomSchedule = { max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60 }

afterEach(() => {
  cleanup(); vi.clearAllMocks(); authState = { user: { id: 1 }, epoch: 1 }
})

function view(props: { active?: boolean; epoch?: number; roomId?: number } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const component = (next = props) => <QueryClientProvider client={client}>
    <RoomScheduleSettings roomId={next.roomId ?? 501} active={next.active ?? true} authEpoch={next.epoch ?? 1} />
  </QueryClientProvider>
  const rendered = render(component())
  return { ...rendered, show: (next: typeof props) => rendered.rerender(component(next)) }
}

it('stays collapsed by default and distinguishes loading from canonical saved values', async () => {
  let resolve!: (value: RoomSchedule) => void
  vi.mocked(schedulesApi.get).mockReturnValue(new Promise((done) => { resolve = done }))
  view()
  const options = screen.getByText('Room options').closest('details') as HTMLDetailsElement
  expect(options.open).toBe(false)
  fireEvent.click(screen.getByText('Room options'))
  expect(screen.getByRole('status').textContent).toContain('Loading run policy')
  expect(screen.queryByText(/2 rounds/)).toBeNull()
  resolve(saved)
  expect(await screen.findByText('Saved policy: 2 rounds · 8 tool calls · 60 seconds')).toBeTruthy()
})

it('validates strict integer ranges and sends all three fields together', async () => {
  vi.mocked(schedulesApi.get).mockResolvedValue(saved)
  vi.mocked(schedulesApi.put).mockResolvedValue({ max_rounds: 4, max_tool_calls: 0, max_executor_seconds: 3600 })
  view()
  fireEvent.click(screen.getByText('Room options'))
  const rounds = await screen.findByRole('spinbutton', { name: 'Collaboration rounds' })
  expect(rounds.closest('form')?.noValidate).toBe(true)
  fireEvent.change(rounds, { target: { value: '1.5' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save run policy' }))
  expect((await screen.findByRole('alert')).textContent).toContain('Enter whole numbers')
  expect(schedulesApi.put).not.toHaveBeenCalled()

  fireEvent.change(rounds, { target: { value: '4' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Tool uses' }), { target: { value: '0' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Tool work time (seconds)' }), { target: { value: '3600' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save run policy' }))
  await waitFor(() => expect(schedulesApi.put).toHaveBeenCalledWith(501, {
    max_rounds: 4, max_tool_calls: 0, max_executor_seconds: 3600,
  }))
  expect(await screen.findByText('Saved. New requests will use this policy.')).toBeTruthy()
})

it('preserves local edits and the old saved policy when save fails, then retries explicitly', async () => {
  vi.mocked(schedulesApi.get).mockResolvedValue(saved)
  vi.mocked(schedulesApi.put).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ ...saved, max_rounds: 3 })
  view()
  fireEvent.click(screen.getByText('Room options'))
  const rounds = await screen.findByRole('spinbutton', { name: 'Collaboration rounds' }) as HTMLInputElement
  fireEvent.change(rounds, { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save run policy' }))
  expect(await screen.findByText('Could not save the run policy. Your edits are still here.')).toBeTruthy()
  expect(rounds.value).toBe('3')
  expect(screen.getByText('Saved policy: 2 rounds · 8 tool calls · 60 seconds')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry saving run policy' }))
  await waitFor(() => expect(schedulesApi.put).toHaveBeenCalledTimes(2))
})

it('shows a truthful load failure with retry and ignores a late previous-owner response', async () => {
  vi.mocked(schedulesApi.get).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(saved)
  const rendered = view()
  fireEvent.click(screen.getByText('Room options'))
  expect(await screen.findByText('Run policy is unavailable.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry loading run policy' }))
  expect(await screen.findByText('Saved policy: 2 rounds · 8 tool calls · 60 seconds')).toBeTruthy()
  rendered.unmount()

  let finishOld!: (value: RoomSchedule) => void
  vi.mocked(schedulesApi.get).mockReset().mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve }))
    .mockResolvedValueOnce({ max_rounds: 4, max_tool_calls: 4, max_executor_seconds: 40 })
  const second = view()
  authState = { user: { id: 2 }, epoch: 2 }
  second.rerender(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <RoomScheduleSettings roomId={501} active authEpoch={2} />
  </QueryClientProvider>)
  fireEvent.click(screen.getByText('Room options'))
  expect(await screen.findByText('Saved policy: 4 rounds · 4 tool calls · 40 seconds')).toBeTruthy()
  finishOld({ max_rounds: 1, max_tool_calls: 1, max_executor_seconds: 1 })
  await waitFor(() => expect(screen.queryByText('Saved policy: 1 rounds · 1 tool calls · 1 seconds')).toBeNull())
})

it('loads archived policy read-only and never offers a mutation', async () => {
  vi.mocked(schedulesApi.get).mockResolvedValue(saved)
  view({ active: false })
  fireEvent.click(screen.getByText('Room options'))
  expect(await screen.findByText('This closed room’s saved policy is read-only.')).toBeTruthy()
  expect((screen.getByRole('spinbutton', { name: 'Collaboration rounds' }) as HTMLInputElement).disabled).toBe(true)
  expect(screen.queryByRole('button', { name: 'Save run policy' })).toBeNull()
  expect(schedulesApi.put).not.toHaveBeenCalled()
})

it('resets edits on a room switch and ignores a late save from the previous room', async () => {
  let finishOld!: (value: RoomSchedule) => void
  vi.mocked(schedulesApi.get).mockImplementation(async (roomId) => roomId === 501
    ? saved
    : { max_rounds: 4, max_tool_calls: 12, max_executor_seconds: 90 })
  vi.mocked(schedulesApi.put).mockReturnValue(new Promise((resolve) => { finishOld = resolve }))
  const rendered = view()
  fireEvent.click(screen.getByText('Room options'))
  const oldRounds = await screen.findByRole('spinbutton', { name: 'Collaboration rounds' }) as HTMLInputElement
  fireEvent.change(oldRounds, { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save run policy' }))
  await waitFor(() => expect(schedulesApi.put).toHaveBeenCalledWith(501, { max_rounds: 3, max_tool_calls: 8, max_executor_seconds: 60 }))

  rendered.show({ roomId: 602 })
  expect(await screen.findByText('Saved policy: 4 rounds · 12 tool calls · 90 seconds')).toBeTruthy()
  expect((screen.getByRole('spinbutton', { name: 'Collaboration rounds' }) as HTMLInputElement).value).toBe('4')

  finishOld({ max_rounds: 3, max_tool_calls: 8, max_executor_seconds: 60 })
  await waitFor(() => {
    expect(screen.queryByText('Saved policy: 3 rounds · 8 tool calls · 60 seconds')).toBeNull()
    expect(screen.queryByText('Saved. New requests will use this policy.')).toBeNull()
  })
})
