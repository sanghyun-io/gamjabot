// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import GrantPrompt from './GrantPrompt'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { toolsApi, type ActionPreview, type ToolActivity } from '../api/tools'

vi.mock('../api/tools', () => ({ toolsApi: { preview: vi.fn(), decide: vi.fn() } }))
const token = '550e8400-e29b-41d4-a716-446655440000'
const nextToken = '650e8400-e29b-41d4-a716-446655440000'
const action: ToolActivity = { action_id: 'a1', room_id: 5, agent_id: 7, trigger_event_id: null,
  family: 'write_file', target: '/fixture', expected_effect: 'Write 4 bytes to file', state: 'waiting',
  scope: null, grant_id: null, requested_at: '', started_at: null, completed_at: null }
const view: ActionPreview = { action_id: 'a1', review_id: token, family: 'write_file', target: '/fixture',
  scope_target: '/fixture', scope_description: 'fixture scope', expected_effect: 'Write 4 bytes to file',
  workflow_id: null, arguments: { path: '/fixture', content: 'private fixture' }, payload_digest: 'fixture',
  approval_policy: { scopes: ['once', 'room_session'], unverified_context_required: false, start_within_seconds: null } }
const props = { roomId: 5, roomName: 'Topic', agentName: 'Ada', action, onRefresh: vi.fn(), onStop: vi.fn() }
const component = (p = props) => <I18nProvider systemLanguages={() => ['en']}><GrantPrompt {...p} /></I18nProvider>
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((r) => { resolve = r }); return { promise, resolve } }
beforeEach(() => {
  useAuthStore.getState().setAuth('synthetic-owner', { id: 1, email: 'owner@test.invalid', username: 'Owner' })
  vi.mocked(toolsApi.preview).mockResolvedValue(view)
  vi.mocked(toolsApi.decide).mockResolvedValue({})
})
afterEach(() => { cleanup(); useAuthStore.getState().logout(); vi.resetAllMocks() })

it('forwards exactly the displayed token once even for duplicate clicks in one tick', async () => {
  const pending = deferred<unknown>()
  vi.mocked(toolsApi.decide).mockReturnValue(pending.promise)
  render(component())
  const once = await screen.findByRole('button', { name: 'Allow Modify file once for Ada' })
  act(() => { fireEvent.click(once); fireEvent.click(once) })
  expect(toolsApi.decide).toHaveBeenCalledTimes(1)
  expect(toolsApi.decide).toHaveBeenCalledWith(5, 'a1', 'approve', 'once', token, false, false)
  expect(toolsApi.preview).toHaveBeenCalledTimes(1)
  expect((screen.getByRole('button', { name: 'Stop work in Topic' }) as HTMLButtonElement).disabled).toBe(false)
  await act(async () => pending.resolve({}))
})

it('hides obsolete room/action preview and ignores its late response', async () => {
  const old = deferred<ActionPreview>()
  vi.mocked(toolsApi.preview).mockReturnValueOnce(old.promise).mockResolvedValueOnce({ ...view, action_id: 'a2', target: '/new' })
  const rendered = render(component())
  rendered.rerender(component({ ...props, roomId: 6, action: { ...action, room_id: 6, action_id: 'a2' } }))
  await screen.findByText('Target: /new')
  await act(async () => old.resolve({ ...view, target: '/old-private' }))
  expect(screen.queryByText('Target: /old-private')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Allow Modify file once for Ada' }))
  await waitFor(() => expect(toolsApi.decide).toHaveBeenCalledWith(6, 'a2', 'approve', 'once', token, false, false))
})

it('drops late decisions after a room change without clearing a new request busy state', async () => {
  const old = deferred<unknown>(), current = deferred<unknown>()
  const refreshOld = vi.fn(), refreshNew = vi.fn()
  vi.mocked(toolsApi.decide).mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
  vi.mocked(toolsApi.preview).mockResolvedValueOnce(view).mockResolvedValueOnce({ ...view, action_id: 'a2', target: '/new' })
  const rendered = render(component({ ...props, onRefresh: refreshOld }))
  fireEvent.click(await screen.findByRole('button', { name: 'Allow Modify file once for Ada' }))
  rendered.rerender(component({ ...props, roomId: 6, action: { ...action, room_id: 6, action_id: 'a2' }, onRefresh: refreshNew }))
  await screen.findByText('Target: /new')
  const once = screen.getByRole('button', { name: 'Allow Modify file once for Ada' }) as HTMLButtonElement
  expect(once.disabled).toBe(false)
  fireEvent.click(once)
  await act(async () => old.resolve({}))
  expect(refreshOld).not.toHaveBeenCalled()
  expect(once.disabled).toBe(true)
  expect(screen.getByText('Target: /new')).toBeTruthy()
  await act(async () => current.resolve({}))
  expect(refreshNew).toHaveBeenCalledTimes(1)
})

it('invalidates previews and late decisions when the authenticated Owner epoch changes', async () => {
  const old = deferred<unknown>(), fresh = deferred<ActionPreview>()
  const refresh = vi.fn()
  vi.mocked(toolsApi.decide).mockReturnValueOnce(old.promise)
  vi.mocked(toolsApi.preview).mockResolvedValueOnce(view).mockReturnValueOnce(fresh.promise)
  render(component({ ...props, onRefresh: refresh }))
  fireEvent.click(await screen.findByRole('button', { name: 'Allow Modify file once for Ada' }))
  act(() => useAuthStore.getState().setAuth('synthetic-other', { id: 2, email: 'other@test.invalid', username: 'Other' }))
  expect(screen.queryByText('Target: /fixture')).toBeNull()
  await act(async () => old.resolve({}))
  expect(refresh).not.toHaveBeenCalled()
  await act(async () => fresh.resolve({ ...view, review_id: nextToken }))
  fireEvent.click(screen.getByRole('button', { name: 'Allow Modify file once for Ada' }))
  await waitFor(() => expect(toolsApi.decide).toHaveBeenLastCalledWith(5, 'a1', 'approve', 'once', nextToken, false, false))
})

it('does not revive a decision from a previous visit after switching A to B to A', async () => {
  const old = deferred<unknown>(), fresh = deferred<unknown>(), refresh = vi.fn()
  vi.mocked(toolsApi.decide).mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise)
  vi.mocked(toolsApi.preview).mockResolvedValueOnce(view)
    .mockResolvedValueOnce({ ...view, action_id: 'b', target: '/B' })
    .mockResolvedValueOnce({ ...view, review_id: nextToken, target: '/fresh-A' })
  const rendered = render(component({ ...props, onRefresh: refresh }))
  fireEvent.click(await screen.findByRole('button', { name: 'Allow Modify file once for Ada' }))
  rendered.rerender(component({ ...props, roomId: 6, action: { ...action, action_id: 'b' }, onRefresh: refresh }))
  await screen.findByText('Target: /B')
  rendered.rerender(component({ ...props, onRefresh: refresh }))
  await screen.findByText('Target: /fresh-A')
  const once = screen.getByRole('button', { name: 'Allow Modify file once for Ada' }) as HTMLButtonElement
  fireEvent.click(once)
  expect(toolsApi.decide).toHaveBeenCalledTimes(2)
  await act(async () => old.resolve({}))
  expect(refresh).not.toHaveBeenCalled()
  expect(once.disabled).toBe(true)
  expect(screen.getByText('Target: /fresh-A')).toBeTruthy()
  await act(async () => fresh.resolve({}))
  expect(refresh).toHaveBeenCalledTimes(1)
})

it('requires current screen consent for each opaque review and exposes no room grant', async () => {
  const opaque: ActionPreview = { ...view, family: 'keyboard_text', target: 'desktop:keyboard',
    approval_policy: { scopes: ['once'], unverified_context_required: true, start_within_seconds: 60 } }
  vi.mocked(toolsApi.preview).mockResolvedValueOnce(opaque).mockResolvedValueOnce({ ...opaque, review_id: nextToken })
  vi.mocked(toolsApi.decide).mockRejectedValueOnce({ response: { status: 409 } })
  render(component())
  const once = await screen.findByRole('button', { name: 'Allow Type text once for Ada' }) as HTMLButtonElement
  expect(once.disabled).toBe(true)
  expect(screen.queryByRole('button', { name: /for this room/ })).toBeNull()
  expect(screen.getByText('Details').closest('details')?.open).toBe(false)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Not verified by the app · I checked the target account and screen.' }))
  expect(once.disabled).toBe(false)
  fireEvent.click(once)
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: 'Reload latest details' }))
  const next = await screen.findByRole('button', { name: 'Allow Type text once for Ada' }) as HTMLButtonElement
  expect(next.disabled).toBe(true)
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(next)
  await waitFor(() => expect(toolsApi.decide).toHaveBeenLastCalledWith(5, 'a1', 'approve', 'once', nextToken, true, false))
})

it('keeps deny and stop available when a preview cannot be inspected', async () => {
  vi.mocked(toolsApi.preview).mockRejectedValueOnce({ response: { status: 409 } })
  const stop = vi.fn()
  render(component({ ...props, onStop: stop }))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: 'Stop work in Topic' }))
  expect(stop).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Deny Modify file for Ada' }))
  await waitFor(() => expect(toolsApi.decide).toHaveBeenCalledWith(5, 'a1', 'deny', undefined, undefined, false, false))
})

it('offers explicit session consent without a 60-second limit and never enables it before acknowledgement', async () => {
  const opaque: ActionPreview = { ...view, family: 'keyboard_text', target: 'desktop:keyboard',
    approval_policy: { scopes: ['once', 'room_session'], unverified_context_required: true, start_within_seconds: 60 } }
  vi.mocked(toolsApi.preview).mockResolvedValueOnce(opaque)
  render(component())
  const session = await screen.findByRole('button', { name: 'Allow Type text for this room for Ada' }) as HTMLButtonElement
  expect(session.textContent).toBe("Don't ask again this session")
  expect(session.disabled).toBe(true)
  expect(screen.getByText('Allow once · start within 60s')).toBeTruthy()
  expect(screen.getByText(/No 60-second start limit/)).toBeTruthy()
  fireEvent.click(session)
  expect(toolsApi.decide).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(session)
  await waitFor(() => expect(toolsApi.decide).toHaveBeenCalledWith(5, 'a1', 'approve', 'room_session', token, true, false))
  expect(toolsApi.preview).toHaveBeenCalledTimes(1)
})

it('keeps server account/connection/review information inside the existing closed details and submits that exact ID', async () => {
  vi.mocked(toolsApi.preview).mockResolvedValue({ ...view, review: { version: 1, captured_at: '2026-10-15T17:00:00Z',
    action: { action_id: 'a1', room_id: 5, agent_id: 7 },
    authority: { owner_id: 1, room_id: 5, agent_id: 7, os_user_status: 'verified', euid: 502,
      proposer: { owner_id: 1, room_id: 5, agent_id: 7, connection_id: 4, provider_id: 'alibaba-coding-plan', model_id: 'qwen3.7-plus', config_revision: 2, auth_grant_revision: 1 } },
    targets: [{ role: 'path', status: 'verified', path: '/fixture' }],
    external_context: { account: 'not_applicable', target_version: 'not_applicable' } } })
  render(component())
  await screen.findByRole('button', { name: 'Allow Modify file once for Ada' })
  const details = screen.getByText('Details').closest('details')
  expect(details?.open).toBe(false)
  fireEvent.click(screen.getByText('Details'))
  expect(screen.getByText('Owner #1 · Agent #7 · Room #5')).toBeTruthy()
  expect(screen.getByText(token)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Allow Modify file once for Ada' }))
  await waitFor(() => expect(toolsApi.decide).toHaveBeenCalledWith(5, 'a1', 'approve', 'once', token, false, false))
})


it('keeps image consent off and resets it for the next capture', async () => {
  const shot = { ...action, family: 'screenshot', target: 'desktop:screen', expected_effect: 'Capture screenshot' }
  const shotView = { ...view, family: 'screenshot', target: 'desktop:screen', scope_target: 'desktop:screen', expected_effect: 'Capture screenshot' }
  vi.mocked(toolsApi.preview).mockResolvedValueOnce(shotView).mockResolvedValueOnce({ ...shotView, action_id: 'a2', review_id: nextToken })
  const rendered = render(component({ ...props, action: shot }))
  const checkbox = await screen.findByRole('checkbox', { name: 'Send this capture to the model for this request' }) as HTMLInputElement
  expect(checkbox.checked).toBe(false)
  fireEvent.click(checkbox)
  rendered.rerender(component({ ...props, action: { ...shot, action_id: 'a2' } }))
  const next = await screen.findByRole('checkbox', { name: 'Send this capture to the model for this request' }) as HTMLInputElement
  expect(next.checked).toBe(false)
  fireEvent.click(next)
  fireEvent.click(screen.getByRole('button', { name: 'Allow Capture screen once for Ada' }))
  await waitFor(() => expect(toolsApi.decide).toHaveBeenCalledWith(5, 'a2', 'approve', 'once', nextToken, false, true))
})
