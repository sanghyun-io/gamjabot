// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Connections from './Connections'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import firstSuccessWire from '../test-fixtures/firstSuccessTask3Wire.json'

vi.mock('../api/connections', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/connections')>()
  return { ...actual, connectionsApi: {
    list: vi.fn(), catalog: vi.fn(), revisions: vi.fn(), models: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn(), disconnect: vi.fn(), disconnectStatus: vi.fn(), cancelDisconnect: vi.fn(), authMethods: vi.fn(), startAuth: vi.fn(), authStatus: vi.fn(), cancelAuth: vi.fn(), test: vi.fn(), testPreview: vi.fn(), unlock: vi.fn(), unlockStatus: vi.fn(),
  } }
})
import * as connectionsModule from '../api/connections'
const { connectionsApi } = connectionsModule

const catalog = { version: 1 as const, providers: [
  { provider_id: 'openai', label_key: 'connections.providers.openai', icon: 'openai', protocols: ['openai_chat_completions'], fields: [],
    model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] },
    methods: [
      { method_id: 'api_key', code_state: 'implemented', delivery_phase: 'D1', external_eligibility: 'verified', selectable: true },
      { method_id: 'oauth_browser', code_state: 'absent', delivery_phase: 'D3', external_eligibility: 'unknown', selectable: false },
    ], technical_compatibility: 'supported', usage_classification: 'verified', default_base_url: 'https://api.openai.com/v1' },
  { provider_id: 'compatible', label_key: 'connections.providers.compatible', icon: 'compatible', protocols: ['openai_chat_completions'],
    fields: [
      { field_id: 'base_url', kind: 'url', required: true, max_length: 2048 },
      { field_id: 'allow_local_http', kind: 'boolean', required: false },
    ], model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] },
    methods: [
      { method_id: 'api_key', code_state: 'implemented', delivery_phase: 'D1', external_eligibility: 'verified', selectable: true },
      { method_id: 'local_none', code_state: 'implemented', delivery_phase: 'D1', external_eligibility: 'verified', selectable: true },
    ], technical_compatibility: 'supported', usage_classification: 'verified', default_base_url: null },
  { provider_id: 'alibaba-coding-plan', label_key: 'connections.providers.alibaba-coding-plan', icon: 'alibaba-coding-plan',
    protocols: ['openai_chat_completions', 'anthropic_messages'], fields: [],
    model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] },
    methods: [{ method_id: 'api_key', code_state: 'implemented', delivery_phase: 'D3', external_eligibility: 'unknown', selectable: false }],
    technical_compatibility: 'supported', usage_classification: 'unverified', default_base_url: null },
  { provider_id: 'opencode-go', label_key: 'connections.providers.opencode-go', icon: 'opencode-go',
    protocols: ['openai_chat_completions', 'anthropic_messages'], fields: [],
    model_source: { strategy: 'configured', manual_id: true, static_model_ids: ['glm-5.3-flash', 'qwen3.7-plus'],
      static_model_protocols: { 'glm-5.3-flash': 'openai_chat_completions', 'qwen3.7-plus': 'anthropic_messages' } },
    methods: [{ method_id: 'api_key', code_state: 'implemented', delivery_phase: 'D1', external_eligibility: 'verified', selectable: true }],
    technical_compatibility: 'supported', usage_classification: 'unverified', default_base_url: 'https://opencode.ai/zen/go/v1' },
] }

const azureApiKeyProvider = {
  provider_id: 'azure-openai', label_key: 'connections.providers.azure-openai', icon: 'azure-openai',
  protocols: ['azure_openai'], fields: [
    { field_id: 'resource_endpoint', kind: 'url', required: true, max_length: 2048 },
    { field_id: 'api_version', kind: 'text', required: true, max_length: 16 },
  ],
  model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] },
  methods: [{ method_id: 'api_key', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true }],
  technical_compatibility: 'supported', usage_classification: 'verified', default_base_url: null,
} satisfies connectionsModule.ProviderDescriptor

const rows = [
  { id: 7, owner_id: 1, provider_kind: 'compatible', label: 'Native Keychain A', base_url: 'http://127.0.0.1:51701/v1',
    default_model_id: 'fixture-model', enabled: true, auth_required: true, has_secret: true, is_local: true,
    version: 1, provider_id: 'compatible', protocol_id: 'openai_chat_completions', auth_method_id: 'api_key',
    fields: { base_url: 'http://127.0.0.1:51701/v1', allow_local_http: true }, config_revision: 3, auth_grant_revision: 2, credential_state: 'present',
    capabilities: { declared: { tools: true }, verified: { models: {} } } },
  { id: 8, owner_id: 1, provider_kind: 'compatible', label: 'Native Keychain B', base_url: 'http://127.0.0.1:51701/v1',
    default_model_id: 'fixture-model', enabled: true, auth_required: true, has_secret: true, is_local: true,
    capabilities: { declared: { tools: true }, verified: { models: {} } } },
] as const

function renderPage(entry = '/connections', languages = ['ko']) {
  render(<I18nProvider systemLanguages={() => languages}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[entry]}><Connections /></MemoryRouter>
  </QueryClientProvider></I18nProvider>)
}

async function runTextTest(row: HTMLElement) {
  fireEvent.click(await within(row).findByRole('button', { name: '정확한 텍스트 모델 테스트' }))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function installTimerHarness() {
  let nextId = 1_000_000
  const scheduled = new Map<number, { callback: TimerHandler; delay: number }>()
  const realSetTimeout = window.setTimeout.bind(window)
  const realClearTimeout = window.clearTimeout.bind(window)
  vi.spyOn(window, 'setTimeout').mockImplementation(((callback: TimerHandler, delay = 0) => {
    if (delay !== 1000 && delay !== 2000) return realSetTimeout(callback, delay)
    const id = nextId++
    scheduled.set(id, { callback, delay })
    return id
  }) as typeof window.setTimeout)
  vi.spyOn(window, 'clearTimeout').mockImplementation((id) => {
    if (!scheduled.delete(Number(id))) realClearTimeout(id)
  })
  return {
    scheduled,
    runNext: async () => {
      const next = [...scheduled.entries()].sort(([left], [right]) => left - right)[0]
      if (!next) throw new Error('No timer scheduled')
      scheduled.delete(next[0])
      await act(async () => {
        if (typeof next[1].callback === 'function') await next[1].callback()
      })
    },
  }
}

beforeEach(() => {
  useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' })
  vi.mocked(connectionsApi.list).mockResolvedValue(rows as never)
  vi.mocked(connectionsApi.catalog).mockResolvedValue(catalog as never)
  vi.mocked(connectionsApi.revisions).mockResolvedValue({ version: 1, config_revision: 3, auth_grant_revision: 2 } as never)
  vi.mocked(connectionsApi.models).mockResolvedValue({ observed_at: null, stale: false, truncated: false,
    models: [{ id: 'cached-model', label: 'Cached model', source: 'discovery' }] } as never)
  vi.mocked(connectionsApi.create).mockResolvedValue(rows[0] as never)
  vi.mocked(connectionsApi.update).mockResolvedValue(rows[1] as never)
  vi.mocked(connectionsApi.delete).mockResolvedValue(undefined)
  vi.mocked(connectionsApi.disconnect).mockResolvedValue({ credential_state: 'missing', revoke_state: 'skipped' } as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [] } as never)
  vi.mocked(connectionsApi.testPreview).mockImplementation(async (_id, modelId, profile) => ({ profile, model_id: modelId, max_request_count: profile === 'text' ? 1 : 7 }))
  vi.stubGlobal('confirm', vi.fn(() => false))
})

it('accepts only closed auth methods and browser/device operation shapes', () => {
  const parseMethods = (connectionsModule as unknown as { parseAuthMethodsResponse: (value: unknown) => unknown }).parseAuthMethodsResponse
  const parseOperation = (connectionsModule as unknown as { parseAuthOperationResponse: (value: unknown, status: number) => unknown }).parseAuthOperationResponse
  const operationId = 'a'.repeat(64)

  expect(parseMethods({ version: 1, methods: [{ method_id: 'oauth_browser', available: true }] })).toEqual({
    version: 1, methods: [{ method_id: 'oauth_browser', available: true }],
  })
  expect(parseOperation({ version: 1, operation_id: operationId, state: 'awaiting_user', expires_at: null,
    browser_url: 'https://login.example.test/authorize' }, 200)).toMatchObject({ state: 'awaiting_user' })
  expect(parseOperation({ version: 1, operation_id: operationId, state: 'awaiting_user', expires_at: null,
    verification_uri: 'https://login.example.test/device', user_code: 'ABCD-EFGH', poll_interval: 2 }, 200)).toMatchObject({ user_code: 'ABCD-EFGH' })
  expect(() => parseMethods({ version: 1, methods: [{ method_id: 'oauth_browser', available: true, raw: 'TOKEN' }] })).toThrow('Invalid auth methods response')
  expect(() => parseOperation({ version: 1, operation_id: operationId, state: 'awaiting_user', expires_at: null,
    browser_url: 'https://owner:secret@login.example.test/authorize' }, 200)).toThrow('Invalid auth operation response')
  expect(() => parseOperation({ version: 1, operation_id: operationId, state: 'succeeded',
    result: { credential_state: 'present' }, token: 'TOKEN' }, 200)).toThrow('Invalid auth operation response')
})

it('keeps credential authorization separate from model Test and restores keyboard focus', async () => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
  const operationId = 'b'.repeat(64)
  const openaiProvider = { ...catalog.providers[0], methods: [
    catalog.providers[0].methods[0],
    { method_id: 'oauth_device', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true },
  ] }
  const row = { ...rows[0], provider_kind: 'openai', provider_id: 'openai', is_local: false,
    auth_method_id: 'oauth_device', credential_state: 'authorizing', has_secret: false }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [openaiProvider] } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([row] as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [{ method_id: 'oauth_device', available: true }] } as never)
  vi.mocked(connectionsApi.test).mockResolvedValue({ ok: true, code: 'connected', message: '' })
  vi.mocked(connectionsApi.startAuth).mockResolvedValue({ version: 1, operation_id: operationId, state: 'awaiting_user', expires_at: null,
    verification_uri: 'https://login.example.test/device', user_code: 'ABCD-EFGH', poll_interval: 1 } as never)
  vi.mocked(connectionsApi.authStatus).mockResolvedValue({ version: 1, operation_id: operationId, state: 'succeeded',
    result: { credential_state: 'present' } } as never)
  const timers = installTimerHarness()
  renderPage()
  const article = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await runTextTest(article)
  expect(await within(article).findByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  const start = await within(article).findByRole('button', { name: '로그인 시작' })
  fireEvent.click(start)
  expect(await within(article).findByText('ABCD-EFGH')).toBeTruthy()
  expect(within(article).getByRole('link', { name: 'https://login.example.test/device' })).toBeTruthy()
  expect(within(article).getByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  await timers.runNext()
  expect(await within(article).findByText('자격 증명이 저장되었습니다. 정확한 모델 테스트는 별도로 실행하세요.')).toBeTruthy()
  expect(within(article).getByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  await waitFor(() => expect(document.activeElement).toBe(start))
})

it('locks method selection and preserves the operation controls while auth is active', async () => {
  const operationId = 'e'.repeat(64)
  const openaiProvider = { ...catalog.providers[0], methods: [catalog.providers[0].methods[0],
    { method_id: 'oauth_browser', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true },
    { method_id: 'oauth_device', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true }] }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [openaiProvider] } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ ...rows[0], provider_kind: 'openai', provider_id: 'openai', is_local: false }] as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [
    { method_id: 'oauth_browser', available: true }, { method_id: 'oauth_device', available: true },
  ] } as never)
  vi.mocked(connectionsApi.startAuth).mockResolvedValue({ version: 1, operation_id: operationId, state: 'awaiting_user', expires_at: null,
    browser_url: 'https://login.example.test/authorize' } as never)
  renderPage()
  const article = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(await within(article).findByRole('button', { name: '로그인 시작' }))
  expect(await within(article).findByText('https://login.example.test/authorize')).toBeTruthy()
  const browser = within(article).getByRole('radio', { name: '브라우저로 로그인' }) as HTMLInputElement
  const device = within(article).getByRole('radio', { name: '기기 코드로 로그인' }) as HTMLInputElement
  expect(browser.checked).toBe(true)
  expect(browser.disabled).toBe(true)
  expect(device.disabled).toBe(true)
  expect(within(article).getByText('https://login.example.test/authorize')).toBeTruthy()
  expect(within(article).getByRole('button', { name: '로그인 취소' })).toBeTruthy()
  expect((within(article).getByRole('button', { name: '로그인 시작' }) as HTMLButtonElement).disabled).toBe(true)
  expect(connectionsApi.startAuth).toHaveBeenCalledTimes(1)
})

it('shows finalizing as committed cleanup and does not offer cancellation', async () => {
  const openaiProvider = { ...catalog.providers[0], methods: [catalog.providers[0].methods[0],
    { method_id: 'oauth_browser', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true }] }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [openaiProvider] } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ ...rows[0], provider_kind: 'openai', provider_id: 'openai', is_local: false }] as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [{ method_id: 'oauth_browser', available: true }] } as never)
  vi.mocked(connectionsApi.startAuth).mockResolvedValue({ version: 1, operation_id: '1'.repeat(64), state: 'finalizing', expires_at: null } as never)
  renderPage()
  const article = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(await within(article).findByRole('button', { name: '로그인 시작' }))
  expect(await within(article).findByText('저장된 자격 증명을 확인하고 정리하는 중입니다.')).toBeTruthy()
  expect(within(article).queryByRole('button', { name: '로그인 취소' })).toBeNull()
})

it('refreshes connection truth when auth start immediately succeeds', async () => {
  const openaiProvider = { ...catalog.providers[0], methods: [catalog.providers[0].methods[0],
    { method_id: 'native_chain', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true }] }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [openaiProvider] } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ ...rows[0], provider_kind: 'openai', provider_id: 'openai', is_local: false }] as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [{ method_id: 'native_chain', available: true }] } as never)
  vi.mocked(connectionsApi.startAuth).mockResolvedValue({ version: 1, operation_id: '2'.repeat(64), state: 'succeeded',
    result: { credential_state: 'present' } } as never)
  renderPage()
  const article = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(await within(article).findByRole('button', { name: '로그인 시작' }))
  expect(await within(article).findByText('자격 증명이 저장되었습니다. 정확한 모델 테스트는 별도로 실행하세요.')).toBeTruthy()
  await waitFor(() => expect(connectionsApi.list).toHaveBeenCalledTimes(2))
})

it('refreshes connection truth when cancel observes an already succeeded operation', async () => {
  const operationId = '3'.repeat(64)
  const openaiProvider = { ...catalog.providers[0], methods: [catalog.providers[0].methods[0],
    { method_id: 'oauth_browser', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true }] }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [openaiProvider] } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ ...rows[0], provider_kind: 'openai', provider_id: 'openai', is_local: false }] as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [{ method_id: 'oauth_browser', available: true }] } as never)
  vi.mocked(connectionsApi.startAuth).mockResolvedValue({ version: 1, operation_id: operationId, state: 'awaiting_user', expires_at: null,
    browser_url: 'https://login.example.test/authorize' } as never)
  vi.mocked(connectionsApi.cancelAuth).mockResolvedValue({ version: 1, operation_id: operationId, state: 'succeeded',
    result: { credential_state: 'present' } } as never)
  renderPage()
  const article = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(await within(article).findByRole('button', { name: '로그인 시작' }))
  fireEvent.click(await within(article).findByRole('button', { name: '로그인 취소' }))
  expect(await within(article).findByText('자격 증명이 저장되었습니다. 정확한 모델 테스트는 별도로 실행하세요.')).toBeTruthy()
  await waitFor(() => expect(connectionsApi.list).toHaveBeenCalledTimes(2))
})

it('discards a late auth start result after the owner epoch changes', async () => {
  const pending = deferred<unknown>()
  const openaiProvider = { ...catalog.providers[0], methods: [
    catalog.providers[0].methods[0],
    { method_id: 'oauth_browser', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true },
  ] }
  const row = { ...rows[0], provider_kind: 'openai', provider_id: 'openai', is_local: false }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [openaiProvider] } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([row] as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [{ method_id: 'oauth_browser', available: true }] } as never)
  vi.mocked(connectionsApi.startAuth).mockImplementation(() => pending.promise as never)
  renderPage()
  const article = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(await within(article).findByRole('button', { name: '로그인 시작' }))
  await waitFor(() => expect(connectionsApi.startAuth).toHaveBeenCalledTimes(1))
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  pending.resolve({ version: 1, operation_id: 'c'.repeat(64), state: 'awaiting_user', expires_at: null,
    browser_url: 'https://login.example.test/authorize' })
  await act(async () => { await Promise.resolve() })
  expect(screen.queryByText('https://login.example.test/authorize')).toBeNull()
})

it('keeps an active auth operation visible and retries a failed status read explicitly', async () => {
  const operationId = 'd'.repeat(64)
  const openaiProvider = { ...catalog.providers[0], methods: [catalog.providers[0].methods[0],
    { method_id: 'oauth_browser', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true }] }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [openaiProvider] } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ ...rows[0], provider_kind: 'openai', provider_id: 'openai', is_local: false }] as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [{ method_id: 'oauth_browser', available: true }] } as never)
  vi.mocked(connectionsApi.startAuth).mockResolvedValue({ version: 1, operation_id: operationId, state: 'awaiting_user', expires_at: null,
    browser_url: 'https://login.example.test/authorize' } as never)
  vi.mocked(connectionsApi.authStatus).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({
    version: 1, operation_id: operationId, state: 'succeeded', result: { credential_state: 'present' },
  } as never)
  const timers = installTimerHarness()
  renderPage()
  const article = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  const start = await within(article).findByRole('button', { name: '로그인 시작' })
  fireEvent.click(start)
  expect(await within(article).findByText('https://login.example.test/authorize')).toBeTruthy()
  expect(start).toHaveProperty('disabled', true)
  await timers.runNext()
  const alert = await within(article).findByRole('alert')
  expect(within(alert).getByRole('button', { name: '다시 시도' })).toBeTruthy()
  expect(within(article).getByText('https://login.example.test/authorize')).toBeTruthy()
  fireEvent.click(within(alert).getByRole('button', { name: '다시 시도' }))
  await waitFor(() => expect(timers.scheduled.size).toBeGreaterThan(0))
  for (let attempt = 0; attempt < 3 && vi.mocked(connectionsApi.authStatus).mock.calls.length < 2; attempt += 1) await timers.runNext()
  expect(connectionsApi.authStatus).toHaveBeenCalledTimes(2)
  expect(await within(article).findByText('자격 증명이 저장되었습니다. 정확한 모델 테스트는 별도로 실행하세요.')).toBeTruthy()
  expect(connectionsApi.startAuth).toHaveBeenCalledTimes(1)
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); vi.unstubAllGlobals() })

it('renders the catalog and keeps technically compatible but ineligible methods unavailable', async () => {
  renderPage()
  const provider = await screen.findByLabelText('제공자') as HTMLSelectElement
  expect(connectionsApi.catalog).toHaveBeenCalledTimes(1)

  fireEvent.change(provider, { target: { value: 'compatible' } })
  expect(screen.getAllByText('OpenAI 호환').length).toBeGreaterThan(0)

  fireEvent.change(provider, { target: { value: 'alibaba-coding-plan' } })

  expect(screen.getAllByText('Alibaba Coding Plan').length).toBeGreaterThan(0)
  expect(screen.getByText('사용 분류 미확인')).toBeTruthy()
  expect(screen.queryByText('지원됨')).toBeNull()
  expect((screen.getByRole('button', { name: '저장' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryByLabelText('API 키')).toBeNull()
})

it('atomically selects the catalog protocol for a known Go model while responses stay hidden', async () => {
  renderPage()
  const provider = await screen.findByLabelText('제공자') as HTMLSelectElement
  fireEvent.change(provider, { target: { value: 'opencode-go' } })

  const form = screen.getByRole('heading', { name: '연결 추가' }).closest('section')!
  const knownModels = within(form).getByLabelText('확인된 모델') as HTMLSelectElement
  expect(within(knownModels).getByRole('option', { name: 'glm-5.3-flash' })).toBeTruthy()
  expect(within(knownModels).getByRole('option', { name: 'qwen3.7-plus' })).toBeTruthy()
  expect(within(knownModels).queryByRole('option', { name: 'gpt-6-luna' })).toBeNull()
  fireEvent.change(within(form).getByLabelText('연결 이름'), { target: { value: 'Go explicit' } })
  fireEvent.change(knownModels, { target: { value: 'qwen3.7-plus' } })
  fireEvent.change(within(form).getByLabelText('API 키'), { target: { value: 'secret' } })
  expect(within(form).getByLabelText('프로토콜')).toHaveProperty('value', 'anthropic_messages')
  fireEvent.click(within(form).getByRole('button', { name: '저장' }))

  await waitFor(() => expect(connectionsApi.create).toHaveBeenCalledWith({
    version: 1, provider_id: 'opencode-go', protocol_id: 'anthropic_messages',
    auth_method_id: 'api_key', fields: {}, label: 'Go explicit',
    default_model_id: 'qwen3.7-plus', api_key: 'secret', enabled: true, capabilities: {},
  }))
})

it.each(['openai_chat_completions', 'anthropic_messages'])('offers one Coding Plan key and configured models in the eligible Core (%s)', async (protocol) => {
  const base = catalog.providers.find((provider) => provider.provider_id === 'alibaba-coding-plan')!
  const scoped = { ...base, usage_scope: 'personal_interactive',
    methods: [{ method_id: 'api_key', code_state: 'implemented', delivery_phase: 'D1',
      external_eligibility: 'verified', selectable: true }],
    model_source: { strategy: 'configured', manual_id: false,
      static_model_ids: ['qwen3.7-plus', 'qwen3.6-plus', 'kimi-k2.5', 'glm-5', 'MiniMax-M2.5', 'qwen3.5-plus'] },
  }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [scoped] } as never)
  renderPage()
  const provider = await screen.findByLabelText('제공자')
  fireEvent.change(provider, { target: { value: 'alibaba-coding-plan' } })
  const form = screen.getByRole('heading', { name: '연결 추가' }).closest('section')!
  expect(within(form).getAllByLabelText('API 키')).toHaveLength(1)
  expect(within(form).queryByLabelText('기본 URL')).toBeNull()
  const models = within(form).getByLabelText('확인된 모델')
  expect(within(models).getByRole('option', { name: 'MiniMax-M2.5' })).toBeTruthy()
  expect(within(models).queryByRole('option', { name: 'gpt-6-luna' })).toBeNull()
  fireEvent.change(within(form).getByLabelText('연결 이름'), { target: { value: 'Personal plan' } })
  fireEvent.change(models, { target: { value: 'qwen3.7-plus' } })
  fireEvent.change(within(form).getByLabelText('프로토콜'), { target: { value: protocol } })
  fireEvent.change(within(form).getByLabelText('API 키'), { target: { value: 'synthetic-key' } })
  fireEvent.click(within(form).getByRole('button', { name: '저장' }))
  await waitFor(() => expect(connectionsApi.create).toHaveBeenCalledWith({
    version: 1, provider_id: 'alibaba-coding-plan', protocol_id: protocol, auth_method_id: 'api_key',
    fields: {}, label: 'Personal plan', default_model_id: 'qwen3.7-plus',
    api_key: 'synthetic-key', enabled: true, capabilities: {},
  }))
})

it('keeps the explicit protocol when an unknown Go model is entered manually', async () => {
  renderPage()
  fireEvent.change(await screen.findByLabelText('제공자'), { target: { value: 'opencode-go' } })
  const form = screen.getByRole('heading', { name: '연결 추가' }).closest('section')!
  fireEvent.change(within(form).getByLabelText('프로토콜'), { target: { value: 'anthropic_messages' } })
  fireEvent.change(within(form).getByLabelText('연결 이름'), { target: { value: 'Future Go' } })
  fireEvent.change(within(form).getByLabelText('정확한 모델 ID'), { target: { value: 'future-owner-model' } })
  fireEvent.change(within(form).getByLabelText('API 키'), { target: { value: 'secret' } })
  fireEvent.click(within(form).getByRole('button', { name: '저장' }))

  await waitFor(() => expect(connectionsApi.create).toHaveBeenCalledWith({
    version: 1, provider_id: 'opencode-go', protocol_id: 'anthropic_messages',
    auth_method_id: 'api_key', fields: {}, label: 'Future Go',
    default_model_id: 'future-owner-model', api_key: 'secret', enabled: true, capabilities: {},
  }))
})

it('shows the three cloud providers with their catalog icons and exact public field labels', async () => {
  const nativeMethod = { method_id: 'native_chain', code_state: 'implemented', delivery_phase: 'D2',
    external_eligibility: 'verified', selectable: true } satisfies connectionsModule.AuthMethodDescriptor
  const cloudProviders = [
    { provider_id: 'azure-openai', label_key: 'connections.providers.azure-openai', icon: 'azure-openai',
      protocols: ['azure_openai'], fields: [{ field_id: 'resource_endpoint', kind: 'url', required: true, max_length: 2048 }],
      model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] }, methods: [nativeMethod],
      technical_compatibility: 'supported', usage_classification: 'verified', default_base_url: null },
    { provider_id: 'amazon-bedrock', label_key: 'connections.providers.amazon-bedrock', icon: 'amazon-bedrock',
      protocols: ['aws_bedrock_converse'], fields: [{ field_id: 'profile_name', kind: 'text', required: true, max_length: 128 }],
      model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] }, methods: [nativeMethod],
      technical_compatibility: 'supported', usage_classification: 'verified', default_base_url: null },
    { provider_id: 'google-vertex', label_key: 'connections.providers.google-vertex', icon: 'google-vertex',
      protocols: ['vertex_generate_content'], fields: [{ field_id: 'project_id', kind: 'text', required: true, max_length: 128 }],
      model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] }, methods: [nativeMethod],
      technical_compatibility: 'supported', usage_classification: 'verified', default_base_url: null },
  ] satisfies connectionsModule.ProviderDescriptor[]
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [...catalog.providers, ...cloudProviders] } as never)
  renderPage('/connections', ['en'])
  const provider = await screen.findByLabelText('Provider') as HTMLSelectElement

  expect(within(provider).getByRole('option', { name: 'Azure OpenAI' })).toBeTruthy()
  expect(within(provider).getByRole('option', { name: 'Amazon Bedrock' })).toBeTruthy()
  expect(within(provider).getByRole('option', { name: 'Google Vertex AI' })).toBeTruthy()

  fireEvent.change(provider, { target: { value: 'azure-openai' } })
  expect(document.querySelector('[data-provider-icon="azure-openai"]')).toBeTruthy()
  expect(screen.getByLabelText('Service endpoint')).toBeTruthy()

  fireEvent.change(provider, { target: { value: 'amazon-bedrock' } })
  expect(document.querySelector('[data-provider-icon="amazon-bedrock"]')).toBeTruthy()
  expect(screen.getByLabelText('Profile name')).toBeTruthy()

  fireEvent.change(provider, { target: { value: 'google-vertex' } })
  expect(document.querySelector('[data-provider-icon="google-vertex"]')).toBeTruthy()
  expect(screen.getByLabelText('Project')).toBeTruthy()
})

it('defaults the fixed Azure API version without exposing an editable field', async () => {
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [...catalog.providers, azureApiKeyProvider] } as never)
  renderPage('/connections', ['en'])

  fireEvent.change(await screen.findByLabelText('Provider'), { target: { value: 'azure-openai' } })
  expect(screen.queryByRole('textbox', { name: 'API version' })).toBeNull()
  const versionDetails = screen.getByText('API version').closest('details') as HTMLDetailsElement
  expect(versionDetails.open).toBe(false)
  expect(versionDetails.textContent).toContain('v1')

  fireEvent.change(screen.getByLabelText('Service endpoint'), { target: { value: 'https://potato.openai.azure.com' } })
  fireEvent.change(screen.getByLabelText('Connection name'), { target: { value: 'Azure primary' } })
  fireEvent.change(screen.getByLabelText('Exact model ID'), { target: { value: 'deployment-test' } })
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'secret' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(connectionsApi.create).toHaveBeenCalledWith({
    version: 1, provider_id: 'azure-openai', protocol_id: 'azure_openai', auth_method_id: 'api_key',
    fields: { api_version: 'v1', resource_endpoint: 'https://potato.openai.azure.com' },
    label: 'Azure primary', default_model_id: 'deployment-test', api_key: 'secret', enabled: true, capabilities: {},
  }))
})

it('keeps a persisted Azure API version unchanged while editing', async () => {
  const persisted = { ...rows[0], id: 17, provider_kind: 'azure-openai', provider_id: 'azure-openai',
    protocol_id: 'azure_openai', auth_method_id: 'api_key', label: 'Azure preview', is_local: false,
    fields: { resource_endpoint: 'https://potato.openai.azure.com', api_version: 'preview' } }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [azureApiKeyProvider] } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([persisted] as never)
  renderPage('/connections', ['en'])

  const row = (await screen.findByRole('heading', { name: 'Azure preview' })).closest('article')!
  fireEvent.click(within(row).getByRole('button', { name: 'Edit' }))
  expect(screen.queryByRole('textbox', { name: 'API version' })).toBeNull()
  expect(screen.getByText('API version').closest('details')?.textContent).toContain('preview')
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(connectionsApi.update).toHaveBeenCalledWith(17, expect.objectContaining({
    fields: { resource_endpoint: 'https://potato.openai.azure.com', api_version: 'preview' },
  })))
})

it('uses the catalog label key in a non-Latin locale and hides unknown machine identifiers', async () => {
  const unknown = { ...catalog.providers[0], provider_id: 'future-new-vendor',
    label_key: 'connections.providers.future-new-vendor', icon: 'future-new-vendor' }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [...catalog.providers, unknown] } as never)
  renderPage('/connections', ['ja'])
  const provider = await screen.findByLabelText('プロバイダー') as HTMLSelectElement

  fireEvent.change(provider, { target: { value: 'compatible' } })
  expect(screen.getAllByText('OpenAI 互換').length).toBeGreaterThan(0)

  fireEvent.change(provider, { target: { value: 'future-new-vendor' } })
  expect(screen.getAllByText('その他のプロバイダー').length).toBeGreaterThan(0)
  expect(screen.queryByText('Future New Vendor')).toBeNull()
  expect(document.body.textContent).not.toContain('connections.providers.future-new-vendor')
})

it('resets secret and provider fields without interrupting provider keyboard selection', async () => {
  renderPage()
  const provider = await screen.findByLabelText('제공자') as HTMLSelectElement
  const secret = screen.getByLabelText('API 키') as HTMLInputElement
  fireEvent.change(secret, { target: { value: 'must-not-survive' } })

  provider.focus()
  fireEvent.change(provider, { target: { value: 'compatible' } })

  const apiKeyMethod = screen.getByRole('radio', { name: 'API 키 인증' }) as HTMLInputElement
  expect(document.activeElement).toBe(provider)
  expect((screen.getByLabelText('OpenAI 호환 기본 URL') as HTMLInputElement).value).toBe('')
  fireEvent.click(screen.getByRole('radio', { name: '키 없음' }))
  expect(screen.queryByLabelText('API 키')).toBeNull()
  fireEvent.click(apiKeyMethod)
  expect((await screen.findByLabelText('API 키') as HTMLInputElement).value).toBe('')
})

it('filters localized provider names without changing selection or secrets until selection', async () => {
  renderPage()
  const provider = await screen.findByLabelText('제공자') as HTMLSelectElement
  const secret = screen.getByLabelText('API 키') as HTMLInputElement
  fireEvent.change(secret, { target: { value: 'must-remain-private' } })

  const search = screen.getByRole('searchbox', { name: '제공자 검색' }) as HTMLInputElement
  search.focus()
  fireEvent.change(search, { target: { value: '알리바바' } })

  expect(document.activeElement).toBe(search)
  expect(provider.value).toBe('openai')
  expect(secret.value).toBe('must-remain-private')
  expect(within(provider).getByRole('option', { name: 'Alibaba Coding Plan' })).toBeTruthy()
  expect(within(provider).queryByRole('option', { name: 'OpenAI 호환' })).toBeNull()

  for (const query of ['Alibaba', 'alibaba-coding-plan', 'OpenAI 호환']) {
    fireEvent.change(search, { target: { value: query } })
    expect(within(provider).queryAllByRole('option')).toHaveLength(1)
  }
  expect(within(provider).getByRole('option', { name: 'OpenAI 호환' })).toBeTruthy()

  fireEvent.change(search, { target: { value: '알리바바' } })

  provider.focus()
  fireEvent.change(provider, { target: { value: 'alibaba-coding-plan' } })
  expect(document.activeElement).toBe(provider)
  expect(screen.queryByLabelText('API 키')).toBeNull()
})

it('shows an empty provider search result while preserving the selected provider', async () => {
  renderPage()
  const provider = await screen.findByLabelText('제공자') as HTMLSelectElement
  fireEvent.change(screen.getByRole('searchbox', { name: '제공자 검색' }), {
    target: { value: '일치하지 않는 이름' },
  })

  expect(provider.value).toBe('openai')
  expect(screen.getByText('일치하는 제공자가 없습니다.')).toBeTruthy()
  expect(within(provider).queryAllByRole('option')).toHaveLength(0)
})

it('submits the accepted top-level v1 envelope without declaring unselected capabilities', async () => {
  renderPage()
  await screen.findByLabelText('제공자')
  fireEvent.change(screen.getByLabelText('연결 이름'), { target: { value: 'Primary' } })
  fireEvent.change(screen.getByLabelText('정확한 모델 ID'), { target: { value: 'gpt-exact' } })
  fireEvent.change(screen.getByLabelText('API 키'), { target: { value: 'secret' } })
  fireEvent.click(screen.getByRole('button', { name: '저장' }))

  await waitFor(() => expect(connectionsApi.create).toHaveBeenCalledWith({
    version: 1, provider_id: 'openai', protocol_id: 'openai_chat_completions', auth_method_id: 'api_key', fields: {},
    label: 'Primary', default_model_id: 'gpt-exact', api_key: 'secret', enabled: true, capabilities: {},
  }))
})

it('reads cached models, refreshes only on explicit action, and tests the exact selected model', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue({ ok: true, code: 'connected', message: '' })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await waitFor(() => expect(connectionsApi.models).toHaveBeenCalledWith(7, false))
  expect(connectionsApi.models).not.toHaveBeenCalledWith(7, true, expect.anything())

  fireEvent.click(within(row).getByRole('button', { name: '모델 목록 새로고침' }))
  await waitFor(() => expect(connectionsApi.revisions).toHaveBeenCalledWith(7))
  await waitFor(() => expect(connectionsApi.models).toHaveBeenCalledWith(7, true, { configRevision: 3, authGrantRevision: 2 }))
  expect(connectionsApi.test).not.toHaveBeenCalled()

  fireEvent.change(within(row).getByLabelText('수동 모델 ID'), { target: { value: 'manual-exact' } })
  await runTextTest(row)
  await waitFor(() => expect(connectionsApi.test).toHaveBeenCalledWith(7, 'manual-exact', 'text'))
})

it('shows a bounded cached-model error and retries without exposing raw details', async () => {
  vi.mocked(connectionsApi.models).mockRejectedValueOnce({ response: { data: { detail: { code: 'provider_error', message: 'RAW-UPSTREAM-DETAIL' } } } })
    .mockResolvedValue({ observed_at: null, stale: false, truncated: false,
      models: [{ id: 'recovered-model', label: 'Recovered model', source: 'configured' }] } as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!

  const alert = await within(row).findByRole('alert')
  expect(alert.textContent).toContain('모델 목록을 불러오지 못했습니다.')
  expect(alert.textContent).not.toContain('RAW-UPSTREAM-DETAIL')
  fireEvent.click(within(alert).getByRole('button', { name: '다시 시도' }))

  expect(await within(row).findByRole('option', { name: 'Recovered model' })).toBeTruthy()
  expect(connectionsApi.models).toHaveBeenCalledWith(7, false)
})

it('maps a refresh revision conflict to safe guidance and retries explicitly', async () => {
  vi.mocked(connectionsApi.models).mockImplementation(async (_id, refresh) => {
    if (!refresh) return { observed_at: null, stale: false, truncated: false, models: [] } as never
    throw { response: { data: { detail: { code: 'configuration_changed', message: 'RAW-CONFLICT-DETAIL' } } } }
  })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(within(row).getByRole('button', { name: '모델 목록 새로고침' }))

  const alert = await within(row).findByRole('alert')
  expect(alert.textContent).toContain('연결 설정이 변경되었습니다. 현재 설정을 다시 불러온 뒤 새로고침하세요.')
  expect(alert.textContent).not.toContain('RAW-CONFLICT-DETAIL')
  vi.mocked(connectionsApi.models).mockResolvedValue({ observed_at: null, stale: false, truncated: false,
    models: [{ id: 'after-retry', label: 'After retry', source: 'discovery' }] } as never)
  fireEvent.click(within(alert).getByRole('button', { name: '새로고침 다시 시도' }))

  expect(await within(row).findByRole('option', { name: 'After retry' })).toBeTruthy()
})

it('shows a retryable refresh error when current revisions cannot be loaded', async () => {
  vi.mocked(connectionsApi.revisions).mockRejectedValueOnce(new Error('offline')).mockResolvedValue({
    version: 1, config_revision: 3, auth_grant_revision: 2,
  } as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(within(row).getByRole('button', { name: '모델 목록 새로고침' }))

  const alert = await within(row).findByRole('alert')
  expect(alert.textContent).toContain('모델 목록을 새로고치지 못했습니다.')
  fireEvent.click(within(alert).getByRole('button', { name: '새로고침 다시 시도' }))
  await waitFor(() => expect(connectionsApi.revisions).toHaveBeenCalledTimes(2))
})

it('marks a prior test stale when the selected model changes', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue({ ok: true, code: 'connected', message: '' })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await runTextTest(row)
  expect(await within(row).findByText('모델 테스트에 성공했습니다.')).toBeTruthy()

  fireEvent.change(within(row).getByLabelText('수동 모델 ID'), { target: { value: 'other-model' } })

  expect(within(row).queryByText('모델 테스트에 성공했습니다.')).toBeNull()
  expect(within(row).getByText('모델이나 설정이 변경되었습니다. 다시 테스트하세요.')).toBeTruthy()
})

it('preserves the prior Test result when changed edits are cancelled', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue({ ok: true, code: 'connected', message: '' })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await runTextTest(row)
  expect(await within(row).findByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  fireEvent.click(within(row).getByRole('button', { name: '수정' }))
  fireEvent.change(screen.getByLabelText('정확한 모델 ID'), { target: { value: 'unsaved-model' } })

  expect(within(row).getByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '수정 취소' }))

  expect(within(row).getByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  expect(within(row).queryByText('모델이나 설정이 변경되었습니다. 다시 테스트하세요.')).toBeNull()
})

it('marks the prior Test stale only after a fingerprint-changing edit is saved', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue({ ok: true, code: 'connected', message: '' })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await runTextTest(row)
  expect(await within(row).findByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  fireEvent.click(within(row).getByRole('button', { name: '수정' }))
  fireEvent.change(screen.getByLabelText('정확한 모델 ID'), { target: { value: 'saved-model' } })
  fireEvent.click(screen.getByRole('button', { name: '저장' }))

  expect(await within(row).findByText('모델이나 설정이 변경되었습니다. 다시 테스트하세요.')).toBeTruthy()
  expect(within(row).queryByText('모델 테스트에 성공했습니다.')).toBeNull()
})

it('preserves the prior Test result when an unchanged edit is saved', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue({ ok: true, code: 'connected', message: '' })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await runTextTest(row)
  expect(await within(row).findByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  fireEvent.click(within(row).getByRole('button', { name: '수정' }))
  fireEvent.click(screen.getByRole('button', { name: '저장' }))

  await waitFor(() => expect(connectionsApi.update).toHaveBeenCalledTimes(1))
  expect(within(row).getByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  expect(within(row).queryByText('모델이나 설정이 변경되었습니다. 다시 테스트하세요.')).toBeNull()
})

it('preserves the prior Test result when only the connection name is saved', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue({ ok: true, code: 'connected', message: '' })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await runTextTest(row)
  expect(await within(row).findByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  fireEvent.click(within(row).getByRole('button', { name: '수정' }))
  fireEvent.change(screen.getByLabelText('연결 이름'), { target: { value: 'Renamed connection' } })
  fireEvent.click(screen.getByRole('button', { name: '저장' }))

  await waitFor(() => expect(connectionsApi.update).toHaveBeenCalledWith(7, expect.objectContaining({ label: 'Renamed connection' })))
  expect(within(row).getByText('모델 테스트에 성공했습니다.')).toBeTruthy()
  expect(within(row).queryByText('모델이나 설정이 변경되었습니다. 다시 테스트하세요.')).toBeNull()
})

it('shows a recoverable error when saved connections cannot be loaded', async () => {
  vi.mocked(connectionsApi.list)
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(rows as never)

  renderPage()

  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toContain('저장된 연결을 불러오지 못했습니다.')
  fireEvent.click(within(alert).getByRole('button', { name: '다시 시도' }))
  expect(await screen.findByRole('heading', { name: /Native Keychain A/ })).toBeTruthy()
  expect(connectionsApi.list).toHaveBeenCalledTimes(2)
})

it('requires an in-page choice even when native confirm is unavailable, and only deletes the selected row after approval', async () => {
  vi.mocked(connectionsApi.list).mockResolvedValueOnce(rows as never).mockResolvedValue([rows[1]] as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(within(row).getByRole('button', { name: '삭제' }))
  const dialog = screen.getByRole('dialog', { name: 'Native Keychain A 삭제' })
  expect(connectionsApi.delete).not.toHaveBeenCalled()
  fireEvent.click(within(dialog).getByRole('button', { name: '취소' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(connectionsApi.delete).not.toHaveBeenCalled()
  fireEvent.click(within(row).getByRole('button', { name: '삭제' }))
  fireEvent.click(within(screen.getByRole('dialog', { name: 'Native Keychain A 삭제' })).getByRole('button', { name: '삭제' }))
  await waitFor(() => expect(connectionsApi.delete).toHaveBeenCalledTimes(1))
  expect(vi.mocked(connectionsApi.delete).mock.calls[0][0]).toBe(7)
  await waitFor(() => expect(screen.queryByRole('heading', { name: /Native Keychain A/ })).toBeNull())
  expect(screen.getByRole('heading', { name: /Native Keychain B/ })).toBeTruthy()
  expect(window.confirm).not.toHaveBeenCalled()
})

it('keeps the choice and an error visible when deletion fails', async () => {
  vi.mocked(connectionsApi.delete).mockRejectedValue({ response: { data: { detail: 'Connection is still bound to an Agent.' } } })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(within(row).getByRole('button', { name: '삭제' }))
  const dialog = screen.getByRole('dialog', { name: 'Native Keychain A 삭제' })
  fireEvent.click(within(dialog).getByRole('button', { name: '삭제' }))
  expect((await within(dialog).findByRole('alert')).textContent).toContain('연결을 삭제하지 못했습니다.')
  expect(screen.getByRole('heading', { name: /Native Keychain A/ })).toBeTruthy()
})

it('announces an in-flight delete and prevents a second request', async () => {
  let finish!: () => void
  vi.mocked(connectionsApi.delete).mockImplementation(() => new Promise<void>((resolve) => { finish = resolve }))
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(within(row).getByRole('button', { name: '삭제' }))
  const dialog = screen.getByRole('dialog', { name: 'Native Keychain A 삭제' })
  expect(within(dialog).getByText(/저장된 자격 증명을 삭제할까요\?/)).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: '삭제' }))
  expect(await within(dialog).findByRole('status')).toHaveProperty('textContent', '연결을 삭제하는 중…')
  const confirm = within(dialog).getByRole('button', { name: '삭제' }) as HTMLButtonElement
  expect(confirm.disabled).toBe(true)
  fireEvent.click(confirm)
  expect(connectionsApi.delete).toHaveBeenCalledTimes(1)
  await act(async () => finish())
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
})

it('requires and submits a replacement API key for the exact recovery connection', async () => {
  renderPage('/connections?edit=8')

  expect(await screen.findByRole('heading', { name: '수정 #8' })).toBeTruthy()
  expect((screen.getByLabelText('연결 이름') as HTMLInputElement).value).toBe('Native Keychain B')
  const key = screen.getByLabelText('API 키 다시 입력') as HTMLInputElement
  await waitFor(() => expect(document.activeElement).toBe(key))
  expect(key.required).toBe(true)
  fireEvent.change(key, { target: { value: '   ' } })
  fireEvent.click(screen.getByRole('button', { name: '저장' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '저장하기 전에 API 키를 다시 입력해 주세요.')
  expect(connectionsApi.update).not.toHaveBeenCalled()
  fireEvent.change(key, { target: { value: 'replacement-secret' } })
  fireEvent.click(screen.getByRole('button', { name: '저장' }))
  await waitFor(() => expect(connectionsApi.update).toHaveBeenCalledWith(8, expect.objectContaining({ api_key: 'replacement-secret' })))
})

it('keeps the API key optional for an ordinary existing-connection edit', async () => {
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain B/ })).closest('article')!
  fireEvent.click(within(row).getByRole('button', { name: '수정' }))

  const key = screen.getByLabelText(/API 키.*비워 두면 현재 키를 유지합니다/) as HTMLInputElement
  expect(screen.getByRole('heading', { name: '수정 #8' })).toBeTruthy()
  expect(key.required).toBe(false)
  expect(document.activeElement).not.toBe(key)
  fireEvent.click(screen.getByRole('button', { name: '저장' }))
  await waitFor(() => expect(connectionsApi.update).toHaveBeenCalledWith(8, expect.objectContaining({ api_key: undefined })))
})

it('clears a typed secret and identity-bound dialogs when the owner changes', async () => {
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  fireEvent.click(within(row).getByRole('button', { name: '수정' }))
  const key = screen.getByLabelText(/API 키.*비워 두면 현재 키를 유지합니다/) as HTMLInputElement
  fireEvent.change(key, { target: { value: 'prior-owner-secret' } })
  fireEvent.click(within(row).getByRole('button', { name: '삭제' }))
  expect(screen.getByRole('dialog')).toBeTruthy()

  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect((await screen.findByLabelText('API 키') as HTMLInputElement).value).toBe('')
  expect(screen.getByRole('heading', { name: '연결 추가' })).toBeTruthy()
})

it('keeps an unknown connection-test response in closed technical details', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue({
    ok: false, code: 'synthetic_unknown_code', message: 'Synthetic backend detail',
  })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await runTextTest(row)

  expect(await within(row).findByRole('status')).toHaveProperty(
    'textContent', '모델 테스트에 성공하지 못했습니다. 연결 설정을 확인해 주세요.',
  )
  const details = within(row).getByText('기술 정보').closest('details')!
  expect(details.open).toBe(false)
  expect(details.textContent).toContain('synthetic_unknown_code · Synthetic backend detail')
  expect(within(row).getByRole('status').textContent).not.toContain('Synthetic backend detail')
})

it('reports an exact Go usage exhaustion without marking the connection test successful', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue({
    ok: false, code: 'quota_exhausted',
    message: 'OpenCode Go usage is exhausted. Wait for usage to recover and check the connection settings.',
  })
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  await runTextTest(row)

  expect(await within(row).findByRole('status')).toHaveProperty(
    'textContent', 'OpenCode Go 사용량이 소진되었습니다. 사용량이 회복될 때까지 기다리고 연결 설정을 확인해 주세요.',
  )
  expect(within(row).queryByText('모델 테스트에 성공했습니다.')).toBeNull()
})

it('runs the first-use text profile only after an explicit click and reports actual provider requests', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue(firstSuccessWire.text_test_result as never)
  renderPage('/connections?first=1', ['en'])
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  expect(connectionsApi.test).not.toHaveBeenCalled()
  expect(await within(row).findByText(/Text check: at most 1 provider request/)).toBeTruthy()
  const fullDetails = within(row).getByText('Full capability check limits').closest('details')!
  expect(fullDetails.open).toBe(false)
  fireEvent.click(within(row).getByText('Full capability check limits'))
  expect(fullDetails.open).toBe(true)
  expect(within(row).getByText(/Full capability check: at most 7/)).toBeTruthy()
  expect(within(row).getByRole('button', { name: 'Run full capability check' })).toBeTruthy()
  fireEvent.click(within(row).getByRole('button', { name: 'Test exact text model' }))
  await waitFor(() => expect(connectionsApi.test).toHaveBeenCalledWith(7, 'fixture-model', 'text'))
  expect((await within(row).findByRole('status')).textContent).toContain('1 provider request')
  expect(firstSuccessWire.text_test_result.actual_request_count).toBeLessThanOrEqual(firstSuccessWire.text_test_preview.max_request_count)
})

it('uses previewed text and full profiles for an ordinary saved connection instead of an implicit full Test', async () => {
  vi.mocked(connectionsApi.test).mockResolvedValue(firstSuccessWire.text_test_result as never)
  renderPage('/connections', ['en'])
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!

  expect(connectionsApi.test).not.toHaveBeenCalled()
  expect(await within(row).findByText(/Text check: at most 1 provider request/)).toBeTruthy()
  const fullDetails = within(row).getByText('Full capability check limits').closest('details')!
  expect(fullDetails.open).toBe(false)
  expect(within(row).queryByRole('button', { name: 'Test model' })).toBeNull()

  fireEvent.click(within(row).getByRole('button', { name: 'Test exact text model' }))
  await waitFor(() => expect(connectionsApi.test).toHaveBeenCalledWith(7, 'fixture-model', 'text'))

  fireEvent.click(within(row).getByText('Full capability check limits'))
  fireEvent.click(within(row).getByRole('button', { name: 'Run full capability check' }))
  await waitFor(() => expect(connectionsApi.test).toHaveBeenCalledWith(7, 'fixture-model', 'full'))
})

it('accepts only the closed disconnect response shapes and rejects unknown raw fields', () => {
  const parseStart = (connectionsModule as typeof connectionsModule & {
    parseDisconnectStartResponse?: (data: unknown, status: number) => unknown
  }).parseDisconnectStartResponse
  const parseOperation = (connectionsModule as typeof connectionsModule & {
    parseDisconnectOperationResponse?: (data: unknown, status: number) => unknown
  }).parseDisconnectOperationResponse
  expect(parseStart).toBeTypeOf('function')
  expect(parseOperation).toBeTypeOf('function')
  expect(parseStart?.({ credential_state: 'missing', revoke_state: 'skipped' }, 200)).toEqual({
    credential_state: 'missing', revoke_state: 'skipped',
  })
  expect(parseOperation?.({ version: 1, operation_id: 'a'.repeat(64), state: 'pending', expires_at: null, poll_interval: 1 }, 202))
    .toEqual({ version: 1, operation_id: 'a'.repeat(64), state: 'pending', expires_at: null, poll_interval: 1 })
  expect(() => parseStart?.({ credential_state: 'missing', revoke_state: 'skipped', raw_detail: 'RAW-TOKEN-MARKER' }, 200)).toThrow('Invalid disconnect response')
  expect(() => parseOperation?.({ version: 1, operation_id: 'a'.repeat(64), state: 'pending', expires_at: null, poll_interval: 1, phase: 'old_retained' }, 202)).toThrow('Invalid disconnect response')
  expect(() => parseOperation?.({ version: 1, operation_id: 'a'.repeat(64), state: 'pending', expires_at: null, poll_interval: 301 }, 202)).toThrow('Invalid disconnect response')
})

it('keeps metadata delete separate and sends an idempotent disconnect request with keyboard focus at 390px', async () => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
  const start = deferred<{ credential_state: 'missing'; revoke_state: 'skipped' }>()
  vi.mocked(connectionsApi.disconnect).mockImplementation(() => start.promise as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  const trigger = within(row).getByRole('button', { name: '연결 해제' })
  fireEvent.click(trigger)

  const dialog = screen.getByRole('dialog', { name: 'Native Keychain A 연결 해제' })
  const revoke = within(dialog).getByRole('checkbox', { name: '제공자에서도 권한 취소 시도' })
  await waitFor(() => expect(document.activeElement).toBe(revoke))
  fireEvent.click(within(dialog).getByRole('button', { name: '취소' }))
  expect(document.activeElement).toBe(trigger)
  expect(connectionsApi.disconnect).not.toHaveBeenCalled()

  fireEvent.click(trigger)
  const reopened = screen.getByRole('dialog', { name: 'Native Keychain A 연결 해제' })
  fireEvent.click(within(reopened).getByRole('checkbox', { name: '제공자에서도 권한 취소 시도' }))
  fireEvent.click(within(reopened).getByRole('button', { name: '연결 해제' }))
  await waitFor(() => expect(connectionsApi.disconnect).toHaveBeenCalledWith(7, true, expect.stringMatching(/^[0-9a-f]{32}$/)))
  expect(within(reopened).getByRole('status').textContent).toContain('연결 해제를 시작하는 중…')
  expect((trigger as HTMLButtonElement).disabled).toBe(true)
  await act(async () => { start.resolve({ credential_state: 'missing', revoke_state: 'skipped' }) })
  expect(connectionsApi.delete).not.toHaveBeenCalled()
  expect(await within(row).findByText('연결이 해제되었습니다.')).toBeTruthy()
  expect(within(row).getByText('제공자 권한은 변경하지 않았습니다.')).toBeTruthy()
  expect(within(row).getByRole('button', { name: '삭제' })).toBeTruthy()
  await waitFor(() => expect(document.activeElement).toBe(trigger))
})

it('polls the exact operation at the server interval and hides cancel while finalizing', async () => {
  const operationId = 'b'.repeat(64)
  const start = deferred<{ version: 1; operation_id: string; state: 'pending'; expires_at: null; poll_interval: 1 }>()
  vi.mocked(connectionsApi.disconnect).mockImplementation(() => start.promise as never)
  vi.mocked(connectionsApi.disconnectStatus)
    .mockResolvedValueOnce({ version: 1, operation_id: operationId, state: 'finalizing', expires_at: null, poll_interval: 2 } as never)
    .mockResolvedValueOnce({ version: 1, operation_id: operationId, state: 'succeeded',
      result: { credential_state: 'missing', revoke_state: 'revoked' } } as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  const timers = installTimerHarness()
  const trigger = within(row).getByRole('button', { name: '연결 해제' })
  fireEvent.click(trigger)
  fireEvent.click(within(screen.getByRole('dialog', { name: 'Native Keychain A 연결 해제' })).getByRole('button', { name: '연결 해제' }))
  expect(await within(screen.getByRole('dialog', { name: 'Native Keychain A 연결 해제' })).findByRole('status')).toBeTruthy()
  expect((trigger as HTMLButtonElement).disabled).toBe(true)
  await act(async () => { start.resolve({ version: 1, operation_id: operationId,
    state: 'pending', expires_at: null, poll_interval: 1 }) })

  expect(await within(row).findByText('연결 해제를 준비하고 있습니다. 완료될 때까지 이 연결은 사용할 수 없습니다.')).toBeTruthy()
  const cancel = within(row).getByRole('button', { name: '연결 해제 취소' })
  await waitFor(() => expect(document.activeElement).toBe(cancel))
  await waitFor(() => expect([...timers.scheduled.values()].map((item) => item.delay)).toContain(1000))
  await timers.runNext()
  const finalizingStatus = await within(row).findByText('로컬 연결 해제를 마무리하고 있습니다.')
  expect(within(row).queryByRole('button', { name: '연결 해제 취소' })).toBeNull()
  expect(finalizingStatus.getAttribute('tabindex')).toBe('-1')
  await waitFor(() => expect(document.activeElement).toBe(finalizingStatus))
  await waitFor(() => expect([...timers.scheduled.values()].map((item) => item.delay)).toContain(2000))
  await timers.runNext()
  expect(await within(row).findByText('연결이 해제되었습니다.')).toBeTruthy()
  expect(within(row).getByText('제공자 권한을 취소했습니다.')).toBeTruthy()
  await waitFor(() => expect(document.activeElement).toBe(trigger))
  expect(connectionsApi.disconnectStatus).toHaveBeenNthCalledWith(1, 7, operationId)
  expect(connectionsApi.disconnectStatus).toHaveBeenNthCalledWith(2, 7, operationId)
})

it('moves focus to the enabled trigger only after a confirmed terminal cancel commits', async () => {
  const operationId = 'f'.repeat(64)
  const start = deferred<{ version: 1; operation_id: string; state: 'pending'; expires_at: null; poll_interval: 1 }>()
  const cancelRequest = deferred<{ version: 1; operation_id: string; state: 'cancelled'; result: {
    credential_state: 'locked'; revoke_state: 'skipped' }; error_code: 'operation_cancelled' }>()
  vi.mocked(connectionsApi.disconnect).mockImplementation(() => start.promise as never)
  vi.mocked(connectionsApi.cancelDisconnect).mockImplementation(() => cancelRequest.promise as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  const trigger = within(row).getByRole('button', { name: '연결 해제' })
  fireEvent.click(trigger)
  fireEvent.click(within(screen.getByRole('dialog', { name: 'Native Keychain A 연결 해제' })).getByRole('button', { name: '연결 해제' }))
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(true))
  await act(async () => { start.resolve({ version: 1, operation_id: operationId,
    state: 'pending', expires_at: null, poll_interval: 1 }) })
  const cancel = await within(row).findByRole('button', { name: '연결 해제 취소' })
  fireEvent.click(cancel)
  await waitFor(() => expect((cancel as HTMLButtonElement).disabled).toBe(true))
  expect(document.activeElement).not.toBe(trigger)
  await act(async () => { cancelRequest.resolve({ version: 1, operation_id: operationId, state: 'cancelled',
    result: { credential_state: 'locked', revoke_state: 'skipped' }, error_code: 'operation_cancelled' }) })
  expect(await within(row).findByText('연결 해제가 취소되었습니다.')).toBeTruthy()
  await waitFor(() => expect(document.activeElement).toBe(trigger))
})

it('does not claim cancellation until the server returns confirmed terminal truth', async () => {
  const operationId = 'c'.repeat(64)
  vi.mocked(connectionsApi.disconnect).mockResolvedValue({ version: 1, operation_id: operationId,
    state: 'pending', expires_at: null, poll_interval: 1 } as never)
  vi.mocked(connectionsApi.cancelDisconnect).mockResolvedValue({ version: 1, operation_id: operationId,
    state: 'pending', expires_at: null, poll_interval: 2 } as never)
  vi.mocked(connectionsApi.disconnectStatus).mockResolvedValue({ version: 1, operation_id: operationId, state: 'cancelled',
    result: { credential_state: 'locked', revoke_state: 'skipped' }, error_code: 'operation_cancelled' } as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  const timers = installTimerHarness()
  fireEvent.click(within(row).getByRole('button', { name: '연결 해제' }))
  fireEvent.click(within(screen.getByRole('dialog', { name: 'Native Keychain A 연결 해제' })).getByRole('button', { name: '연결 해제' }))
  fireEvent.click(await within(row).findByRole('button', { name: '연결 해제 취소' }))

  await waitFor(() => expect(connectionsApi.cancelDisconnect).toHaveBeenCalledWith(7, operationId))
  expect(within(row).queryByText('연결 해제가 취소되었습니다.')).toBeNull()
  expect(within(row).getByText('연결 해제를 준비하고 있습니다. 완료될 때까지 이 연결은 사용할 수 없습니다.')).toBeTruthy()
  await waitFor(() => expect([...timers.scheduled.values()].map((item) => item.delay)).toContain(2000))
  await timers.runNext()
  expect(await within(row).findByText('연결 해제가 취소되었습니다.')).toBeTruthy()
  expect(within(row).getByText('자격 증명 저장소가 잠겨 있습니다.')).toBeTruthy()
  expect(document.body.textContent).not.toContain('operation_cancelled')
})

it('discards a stale poll response and clears the operation when the owner changes', async () => {
  const operationId = 'd'.repeat(64)
  let finishStatus!: (value: unknown) => void
  vi.mocked(connectionsApi.disconnect).mockResolvedValue({ version: 1, operation_id: operationId,
    state: 'pending', expires_at: null, poll_interval: 1 } as never)
  vi.mocked(connectionsApi.disconnectStatus).mockImplementation(() => new Promise((resolve) => { finishStatus = resolve }) as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  const timers = installTimerHarness()
  const trigger = within(row).getByRole('button', { name: '연결 해제' })
  fireEvent.click(trigger)
  fireEvent.click(within(screen.getByRole('dialog', { name: 'Native Keychain A 연결 해제' })).getByRole('button', { name: '연결 해제' }))
  await within(row).findByText('연결 해제를 준비하고 있습니다. 완료될 때까지 이 연결은 사용할 수 없습니다.')
  await waitFor(() => expect(document.activeElement).toBe(within(row).getByRole('button', { name: '연결 해제 취소' })))
  const poll = timers.runNext()
  await waitFor(() => expect(connectionsApi.disconnectStatus).toHaveBeenCalledWith(7, operationId))

  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  await waitFor(() => expect(screen.queryByText('연결 해제를 준비하고 있습니다. 완료될 때까지 이 연결은 사용할 수 없습니다.')).toBeNull())
  finishStatus({ version: 1, operation_id: operationId, state: 'succeeded',
    result: { credential_state: 'missing', revoke_state: 'revoked' } })
  await poll
  expect(screen.queryByText('연결이 해제되었습니다.')).toBeNull()
  expect(document.activeElement).not.toBe(trigger)
  expect(timers.scheduled.size).toBe(0)
})

it('shows only mapped restored state when a terminal failure follows pending cleanup', async () => {
  const operationId = 'e'.repeat(64)
  vi.mocked(connectionsApi.disconnect).mockResolvedValue({ version: 1, operation_id: operationId,
    state: 'pending', expires_at: null, poll_interval: 1 } as never)
  vi.mocked(connectionsApi.disconnectStatus).mockResolvedValue({ version: 1, operation_id: operationId, state: 'failed',
    result: { credential_state: 'refresh_required', revoke_state: 'skipped' }, error_code: 'credential_recovery_required',
  } as never)
  renderPage()
  const row = (await screen.findByRole('heading', { name: /Native Keychain A/ })).closest('article')!
  const timers = installTimerHarness()
  fireEvent.click(within(row).getByRole('button', { name: '연결 해제' }))
  fireEvent.click(within(screen.getByRole('dialog', { name: 'Native Keychain A 연결 해제' })).getByRole('button', { name: '연결 해제' }))
  await within(row).findByText('연결 해제를 준비하고 있습니다. 완료될 때까지 이 연결은 사용할 수 없습니다.')
  await timers.runNext()

  expect(await within(row).findByText('연결 해제를 완료하지 못했습니다. 기존 자격 증명 상태를 확인했습니다.')).toBeTruthy()
  expect(within(row).getByText('자격 증명을 새로 고쳐야 합니다.')).toBeTruthy()
  expect(document.body.textContent).not.toContain('credential_recovery_required')
  expect(document.body.textContent).not.toContain('RAW')
})

it('defaults ChatGPT tools on and image off while allowing both declarations', async () => {
  const chatgpt = { ...catalog.providers[0], provider_id: 'chatgpt', label_key: 'connections.providers.chatgpt', icon: 'chatgpt', protocols: ['openai_responses'], methods: [{ method_id: 'oauth_browser', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true }] }
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ ...catalog, providers: [...catalog.providers, chatgpt] } as never)
  vi.mocked(connectionsApi.authMethods).mockResolvedValue({ version: 1, methods: [{ method_id: 'oauth_browser', available: true }] } as never)
  renderPage('/connections?first=1', ['en'])
  fireEvent.change(await screen.findByLabelText('Provider'), { target: { value: 'chatgpt' } })
  fireEvent.click(screen.getByRole('button', { name: 'Next' }))
  fireEvent.click(screen.getByRole('button', { name: 'Next' }))
  const vision = screen.getByRole('checkbox', { name: 'Image understanding', hidden: true }) as HTMLInputElement
  expect(vision.disabled).toBe(false)
  for (const label of ['Max tokens', 'Temperature']) expect((screen.getByRole('checkbox', { name: label, hidden: true }) as HTMLInputElement).disabled).toBe(true)
  const tools = screen.getByRole('checkbox', { name: 'Tool use', hidden: true }) as HTMLInputElement
  expect(tools.disabled).toBe(false)
  expect(tools.checked).toBe(true)
  fireEvent.click(tools)
  expect(tools.checked).toBe(false)
  expect(vision.checked).toBe(false)
  fireEvent.click(vision)
  expect(vision.checked).toBe(true)
})
