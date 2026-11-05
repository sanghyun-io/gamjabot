// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import AttentionInbox from './AttentionInbox'
import { attentionApi, type AttentionItem } from '../api/attention'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/attention', () => ({ attentionApi: { list: vi.fn() } }))
const approval: AttentionItem = { id: 'approval:a1', kind: 'approval', occurred_at: '2026-10-05T00:00:00Z', room_id: 7,
  room_title: 'Release room', project_id: 2, project_name: 'Desktop', agent_id: 8, agent_name: 'Ada',
  request_event_id: 'request-1', request_seq: 4, action_id: 'a1', failure_event_id: null, failure_seq: null,
  summary_code: 'approval_browser', href: '/chat/7?seq=4&action=a1' }
const failure: AttentionItem = { ...approval, id: 'failure:f1', kind: 'failure', occurred_at: '2026-10-05T00:01:00Z',
  agent_name: 'Bea', action_id: null, failure_event_id: 'f1', failure_seq: 6, summary_code: 'failure_connection',
  href: '/chat/7?seq=6&failure=f1' }

function Location() { const value = useLocation(); return <p>{value.pathname}{value.search}</p> }

beforeEach(() => { useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' }); vi.mocked(attentionApi.list).mockResolvedValue({ items: [approval, failure], next_cursor: null }) })
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('loads only on demand, shows safe human summaries, and navigates to the exact room item', async () => {
  render(<MemoryRouter><AttentionInbox/><Routes><Route path="*" element={<Location/>}/></Routes></MemoryRouter>)
  expect(attentionApi.list).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Open attention inbox' }))
  expect(await screen.findByText('Approve a browser action')).toBeTruthy()
  expect(screen.getByText('Connection or model needs attention')).toBeTruthy()
  expect(screen.queryByText('approval_browser')).toBeNull()
  fireEvent.click(screen.getByRole('link', { name: /Approve a browser action/ }))
  expect(await screen.findByText('/chat/7?seq=4&action=a1')).toBeTruthy()
})

it('forwards opaque pagination and ignores a late prior-owner page', async () => {
  let finish!: (value: { items: AttentionItem[]; next_cursor: null }) => void
  vi.mocked(attentionApi.list).mockResolvedValueOnce({ items: [approval], next_cursor: 'opaque+cursor==' })
    .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  render(<MemoryRouter><AttentionInbox/></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Open attention inbox' }))
  await screen.findByText('Approve a browser action')
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
  expect(attentionApi.list).toHaveBeenLastCalledWith('opaque+cursor==')
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  act(() => finish({ items: [failure], next_cursor: null }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.queryByText('Connection or model needs attention')).toBeNull()
})

it('shows an explicit retry state without stale rows', async () => {
  vi.mocked(attentionApi.list).mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ items: [], next_cursor: null })
  render(<MemoryRouter><AttentionInbox/></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Open attention inbox' }))
  expect(await screen.findByText('Attention items are unavailable.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('Nothing needs attention right now.')).toBeTruthy()
})
