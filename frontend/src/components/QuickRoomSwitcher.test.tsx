// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import QuickRoomSwitcher from './QuickRoomSwitcher'
import { roomIndexApi } from '../api/roomIndex'
import { projectsApi } from '../api/projects'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/roomIndex', () => ({ roomIndexApi: { list: vi.fn(), pin: vi.fn(), unpin: vi.fn() } }))
vi.mock('../api/projects', () => ({ projectsApi: { list: vi.fn().mockResolvedValue([]) } }))
const row = { room_id: 7, room_title: 'Release room', project_id: 2, project_name: 'Desktop', room_active: true,
  project_active: true, effective_active: true, read_only: false, pinned: false, pinned_at: null,
  last_event_seq: 4, last_activity_at: '2026-10-05T00:00:00Z', unread_authored: 2, waiting_approvals: 0,
  active_failures: 0, attention_count: 2, work_status: 'idle' as const }

beforeEach(() => { useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' }); vi.mocked(roomIndexApi.list).mockResolvedValue({ items: [row], next_cursor: null }); vi.mocked(roomIndexApi.pin).mockResolvedValue({ room_id: 7, pinned: true, pinned_at: '2026-10-05T00:00:00Z' }) })
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('opens on Ctrl-K, navigates with the keyboard, and does not request any read acknowledgement', async () => {
  render(<MemoryRouter initialEntries={['/']}><textarea aria-label="Draft" defaultValue="keep draft" /><QuickRoomSwitcher />
    <Routes><Route path="/chat/:id" element={<p>Opened room</p>} /></Routes></MemoryRouter>)
  const draft = screen.getByRole('textbox', { name: 'Draft' }) as HTMLTextAreaElement
  draft.focus(); draft.setSelectionRange(4, 4)
  fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
  expect(await screen.findByRole('dialog', { name: 'Switch rooms' })).toBeTruthy()
  await screen.findByText('Release room')
  expect(roomIndexApi.list).toHaveBeenCalledWith({ status: 'active', project_id: undefined, q: '', limit: 50 })
  fireEvent.keyDown(screen.getByPlaceholderText('Search room titles'), { key: 'Enter' })
  expect(await screen.findByText('Opened room')).toBeTruthy()
  expect(draft.value).toBe('keep draft')
})

it('announces and scrolls the active keyboard option into view', async () => {
  const scroll = vi.fn()
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll })
  vi.mocked(roomIndexApi.list).mockResolvedValue({ items: [row, { ...row, room_id: 8, room_title: 'Second room' }], next_cursor: null })
  render(<MemoryRouter><QuickRoomSwitcher /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Open room switcher' }))
  const search = screen.getByPlaceholderText('Search room titles')
  await screen.findByText('Second room')
  fireEvent.keyDown(search, { key: 'ArrowDown' })
  expect(search.getAttribute('aria-activedescendant')).toBe('quick-room-8')
  expect(screen.getByRole('option', { name: /Second room/ }).getAttribute('aria-selected')).toBe('true')
  expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
  delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
})

it('restores composer focus and caret on Escape and pins without navigating', async () => {
  render(<MemoryRouter><textarea aria-label="Draft" defaultValue="keep draft" /><QuickRoomSwitcher /></MemoryRouter>)
  const draft = screen.getByRole('textbox', { name: 'Draft' }) as HTMLTextAreaElement
  draft.focus(); draft.setSelectionRange(3, 3)
  fireEvent.keyDown(window, { key: 'k', metaKey: true })
  await screen.findByText('Release room')
  const pin = screen.getByRole('button', { name: 'Pin Release room' })
  fireEvent.click(pin); fireEvent.click(pin)
  await waitFor(() => expect(roomIndexApi.pin).toHaveBeenCalledWith(7))
  expect(roomIndexApi.pin).toHaveBeenCalledTimes(1)
  await waitFor(() => expect(roomIndexApi.list).toHaveBeenCalledTimes(2))
  expect(roomIndexApi.list).toHaveBeenLastCalledWith({ status: 'active', project_id: undefined, q: '', limit: 50 })
  fireEvent.keyDown(screen.getByPlaceholderText('Search room titles'), { key: 'Escape' })
  await waitFor(() => expect(document.activeElement).toBe(draft))
  expect(draft.selectionStart).toBe(3)
})

it('sends opaque pagination cursors unchanged and clears them when the query changes', async () => {
  vi.mocked(roomIndexApi.list).mockResolvedValueOnce({ items: [row], next_cursor: 'opaque==cursor' })
    .mockResolvedValueOnce({ items: [{ ...row, room_id: 8, room_title: 'Second room' }], next_cursor: null })
    .mockResolvedValueOnce({ items: [{ ...row, room_id: 9, room_title: 'Filtered room' }], next_cursor: null })
  render(<MemoryRouter><QuickRoomSwitcher /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Open room switcher' }))
  await screen.findByText('Release room')
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
  await screen.findByText('Second room')
  expect(roomIndexApi.list).toHaveBeenNthCalledWith(2, { status: 'active', project_id: undefined, q: '', limit: 50, cursor: 'opaque==cursor' })
  fireEvent.change(screen.getByPlaceholderText('Search room titles'), { target: { value: 'Filtered' } })
  await screen.findByText('Filtered room')
  expect(roomIndexApi.list).toHaveBeenLastCalledWith({ status: 'active', project_id: undefined, q: 'Filtered', limit: 50 })
})

it('does not open when a foreign aria-modal dialog exists and suppresses default', async () => {
  render(<MemoryRouter><QuickRoomSwitcher /><div role="dialog" aria-modal="true" data-testid="foreign-modal"><button>Foreign</button></div></MemoryRouter>)
  const foreignBtn = screen.getByRole('button', { name: 'Foreign' })
  foreignBtn.focus()
  const keyEvent = new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, cancelable: true })
  window.dispatchEvent(keyEvent)
  expect(keyEvent.defaultPrevented).toBe(true)
  expect(screen.queryByRole('dialog', { name: 'Switch rooms' })).toBeNull()
  expect(roomIndexApi.list).not.toHaveBeenCalled()
})

it('toggles its own dialog normally when no foreign modal exists', async () => {
  const draft = document.createElement('textarea')
  draft.setAttribute('aria-label', 'Draft')
  draft.value = 'keep draft'
  document.body.appendChild(draft)
  draft.focus()
  draft.setSelectionRange(4, 4)
  render(<MemoryRouter><QuickRoomSwitcher /></MemoryRouter>)
  fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
  expect(await screen.findByRole('dialog', { name: 'Switch rooms' })).toBeTruthy()
  expect(roomIndexApi.list).toHaveBeenCalledWith({ status: 'active', project_id: undefined, q: '', limit: 50 })
  fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Switch rooms' })).toBeNull())
  // Wait for requestAnimationFrame to restore focus
  await waitFor(() => expect(document.activeElement).toBe(draft))
  expect(draft.selectionStart).toBe(4)
  document.body.removeChild(draft)
})

it('closes and ignores a late prior-owner index response', async () => {
  let resolve!: (value: { items: Array<typeof row>; next_cursor: null }) => void
  vi.mocked(roomIndexApi.list).mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  render(<MemoryRouter><QuickRoomSwitcher /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Open room switcher' }))
  expect(await screen.findByRole('dialog', { name: 'Switch rooms' })).toBeTruthy()
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  act(() => resolve({ items: [row], next_cursor: null }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.queryByText('Release room')).toBeNull()
})

it('clears prior-owner projects and pending pin locks on identity change', async () => {
  vi.mocked(projectsApi.list).mockResolvedValueOnce([{ id: 11, name: 'Old project', is_active: true }] as never)
    .mockResolvedValueOnce([{ id: 12, name: 'New project', is_active: true }] as never)
  let finishOldPin!: () => void
  vi.mocked(roomIndexApi.pin).mockImplementationOnce(() => new Promise((resolve) => { finishOldPin = () => resolve({ room_id: 7, pinned: true, pinned_at: '2026-10-05T00:00:00Z' }) }))
    .mockResolvedValueOnce({ room_id: 7, pinned: true, pinned_at: '2026-10-05T00:01:00Z' })
  render(<MemoryRouter><QuickRoomSwitcher /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Open room switcher' }))
  expect(await screen.findByRole('option', { name: 'Old project' })).toBeTruthy()
  fireEvent.click(await screen.findByRole('button', { name: 'Pin Release room' }))
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  fireEvent.click(screen.getByRole('button', { name: 'Open room switcher' }))
  expect(await screen.findByRole('option', { name: 'New project' })).toBeTruthy()
  expect(screen.queryByRole('option', { name: 'Old project' })).toBeNull()
  const newOwnerPin = await screen.findByRole('button', { name: 'Pin Release room' })
  expect((newOwnerPin as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(newOwnerPin)
  await waitFor(() => expect(roomIndexApi.pin).toHaveBeenCalledTimes(2))
  act(() => finishOldPin())
})
