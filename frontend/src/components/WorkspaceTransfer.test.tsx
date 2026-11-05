// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import WorkspaceTransfer from './WorkspaceTransfer'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/workspaceBlueprint', () => ({ workspaceBlueprintApi: { export: vi.fn(), preview: vi.fn(), apply: vi.fn() } }))
vi.mock('@tauri-apps/api/core', () => ({ isTauri: vi.fn() }))
import { workspaceBlueprintApi } from '../api/workspaceBlueprint'
import { isTauri } from '@tauri-apps/api/core'

const blueprint = { version: 1 as const, connections: [{ ref: 'connection-1', provider_kind: 'openai', label: 'Primary',
  default_model_id: 'exact', base_url: null, allow_local_http: false, auth_required: true, enabled: true, capabilities: { tools: true } }],
agents: [{ ref: 'agent-1', name: 'Ada', description: '', system_prompt: 'Help', connection_ref: 'connection-1', model_id: 'exact', temperature: 'auto', max_tokens: 0 }],
projects: [{ ref: 'project-1', name: 'Research', description: '', agent_refs: ['agent-1'], room_templates: [{ ref: 'template-1', name: 'Launch',
  room_title: 'Launch room', agent_refs: ['agent-1'], max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60 }] }] }
const counts = { connections: 1, agents: 1, projects: 1, project_assignments: 1, room_templates: 1, template_members: 1 }
const preview = { version: 1 as const, plan_hash: 'a'.repeat(64), counts,
  connections: [{ ref: 'connection-1', source_name: 'Primary', target_name: 'Primary (imported 2)' }],
  agents: [{ ref: 'agent-1', source_name: 'Ada', target_name: 'Ada' }],
  projects: [{ ref: 'project-1', source_name: 'Research', target_name: 'Research' }],
  room_templates: [{ ref: 'template-1', project_ref: 'project-1', source_name: 'Launch', target_name: 'Launch' }],
  warnings: [{ code: 'connections_need_credentials' as const, count: 1 }] }

function view() {
  const state = useAuthStore.getState()
  const identity = `${state.user?.id || 0}:${state.epoch}`
  return render(<I18nProvider><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkspaceTransfer identity={identity} authEpoch={state.epoch} />
  </QueryClientProvider></I18nProvider>)
}

beforeEach(() => {
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  vi.mocked(isTauri).mockReturnValue(false)
  vi.mocked(workspaceBlueprintApi.export).mockResolvedValue(blueprint)
  vi.mocked(workspaceBlueprintApi.preview).mockResolvedValue(preview)
  vi.mocked(workspaceBlueprintApi.apply).mockResolvedValue({ plan_hash: preview.plan_hash, counts,
    connections: [{ ref: 'connection-1', id: 10, label: 'Primary (imported 2)' }], agents: [{ ref: 'agent-1', id: 20, name: 'Ada' }],
    projects: [{ ref: 'project-1', id: 30, name: 'Research' }], room_templates: [{ ref: 'template-1', project_ref: 'project-1', id: 40, name: 'Launch' }],
    credentials_unset: 1 })
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); delete (navigator as { clipboard?: Clipboard }).clipboard; act(() => useAuthStore.getState().logout()) })

it('keeps transfer controls collapsed and copies the exact canonical non-secret export', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  view()
  const panel = screen.getByText('Transfer workspace setup').closest('details') as HTMLDetailsElement
  expect(panel.open).toBe(false)
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  expect(screen.getByText(/API keys and chat history are not included/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Copy setup' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(JSON.stringify(blueprint, null, 2) + '\n'))
  const copied = writeText.mock.calls[0][0]
  expect(copied).not.toMatch(/api_key|secret_ref|chat|event|tool_action|credential/i)
  expect(screen.getByText('Workspace setup copied.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Download JSON' })).toBeTruthy()
})

it('does not offer the browser-only download in the native shell', async () => {
  vi.mocked(isTauri).mockReturnValue(true)
  view()
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  expect(screen.queryByRole('button', { name: 'Download JSON' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Copy setup' })).toBeTruthy()
})

it('previews resolved names and counts, never auto-applies, and applies only that exact plan', async () => {
  view()
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  fireEvent.change(screen.getByRole('textbox', { name: 'Or paste setup JSON' }), { target: { value: JSON.stringify(blueprint) } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
  expect(await screen.findByText('Import preview')).toBeTruthy()
  expect(screen.getByText('1 connections')).toBeTruthy()
  expect(screen.getByText('Primary (imported 2)')).toBeTruthy()
  expect(screen.getByText(/need API keys/)).toBeTruthy()
  expect(workspaceBlueprintApi.apply).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Import this preview' }))
  await waitFor(() => expect(workspaceBlueprintApi.apply).toHaveBeenCalledWith(blueprint, 'a'.repeat(64)))
  expect(await screen.findByText('Workspace setup imported.')).toBeTruthy()
  expect(screen.queryByText('Import preview')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Import this preview' })).toBeNull()
  expect(screen.getByText(/Register API keys for 1 imported connections/)).toBeTruthy()
})

it('clears the plan on every edit and ignores a late earlier preview', async () => {
  let finish!: (value: unknown) => void
  vi.mocked(workspaceBlueprintApi.preview).mockReturnValue(new Promise((resolve) => { finish = resolve }) as never)
  view()
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  const editor = screen.getByRole('textbox', { name: 'Or paste setup JSON' })
  fireEvent.change(editor, { target: { value: JSON.stringify(blueprint) } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
  await waitFor(() => expect(workspaceBlueprintApi.preview).toHaveBeenCalledTimes(1))
  fireEvent.change(editor, { target: { value: '{"version":1}' } })
  await act(async () => finish(preview))
  expect(screen.queryByText('Import preview')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Import this preview' })).toBeNull()
})

it('keeps invalid input for repair and never sends or applies it', async () => {
  view()
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  const editor = screen.getByRole('textbox', { name: 'Or paste setup JSON' }) as HTMLTextAreaElement
  fireEvent.change(editor, { target: { value: '{invalid' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
  expect(await screen.findByText('Choose or paste a valid JSON setup.')).toBeTruthy()
  expect(editor.value).toBe('{invalid')
  expect(workspaceBlueprintApi.preview).not.toHaveBeenCalled()
  expect(workspaceBlueprintApi.apply).not.toHaveBeenCalled()
})

it('rejects an oversized file before reading or previewing it', async () => {
  view()
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  const file = new File(['small'], 'large.json', { type: 'application/json' })
  Object.defineProperty(file, 'size', { value: 2 * 1024 * 1024 + 1 })
  fireEvent.change(screen.getByLabelText('Import JSON file'), { target: { files: [file] } })
  expect(await screen.findByText('This setup is larger than 2 MiB.')).toBeTruthy()
  expect(workspaceBlueprintApi.preview).not.toHaveBeenCalled()
})

it('loads a chosen JSON file into the same editable preview flow without applying it', async () => {
  view()
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  const file = new File(['fixture'], 'workspace.json', { type: 'application/json' })
  Object.defineProperty(file, 'text', { value: vi.fn().mockResolvedValue(JSON.stringify(blueprint)) })
  fireEvent.change(screen.getByLabelText('Import JSON file'), { target: { files: [file] } })
  await waitFor(() => expect((screen.getByRole('textbox', { name: 'Or paste setup JSON' }) as HTMLTextAreaElement).value).toBe(JSON.stringify(blueprint)))
  expect(workspaceBlueprintApi.preview).not.toHaveBeenCalled()
  expect(workspaceBlueprintApi.apply).not.toHaveBeenCalled()
})

it('preserves the import text but clears a server-stale plan before retry', async () => {
  vi.mocked(workspaceBlueprintApi.apply).mockRejectedValue(Object.assign(new Error('stale'), {
    response: { status: 409, data: { detail: { code: 'workspace_plan_stale' } } },
  }))
  view()
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  const editor = screen.getByRole('textbox', { name: 'Or paste setup JSON' }) as HTMLTextAreaElement
  const raw = JSON.stringify(blueprint)
  fireEvent.change(editor, { target: { value: raw } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
  await screen.findByText('Import preview')
  fireEvent.click(screen.getByRole('button', { name: 'Import this preview' }))
  expect(await screen.findByText('The workspace changed after this preview. Preview again before importing.')).toBeTruthy()
  expect(editor.value).toBe(raw)
  expect(screen.queryByRole('button', { name: 'Import this preview' })).toBeNull()
})

it('drops a deferred apply result after the owner changes', async () => {
  let finish!: (value: unknown) => void
  vi.mocked(workspaceBlueprintApi.apply).mockReturnValue(new Promise((resolve) => { finish = resolve }) as never)
  view()
  fireEvent.click(screen.getByText('Transfer workspace setup'))
  fireEvent.change(screen.getByRole('textbox', { name: 'Or paste setup JSON' }), { target: { value: JSON.stringify(blueprint) } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview import' }))
  await screen.findByText('Import preview')
  fireEvent.click(screen.getByRole('button', { name: 'Import this preview' }))
  await waitFor(() => expect(workspaceBlueprintApi.apply).toHaveBeenCalledTimes(1))
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  await act(async () => finish({ plan_hash: preview.plan_hash, counts, connections: [], agents: [], projects: [], room_templates: [], credentials_unset: 0 }))
  expect(screen.queryByText('Workspace setup imported.')).toBeNull()
})
