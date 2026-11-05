// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import App from './App'
import api from './lib/api'
import { useAuthStore } from './stores/authStore'
import { skipAutoRestoreOnStartup } from './lib/startupLoginMode'

vi.mock('./lib/startupLoginMode', () => ({ skipAutoRestoreOnStartup: vi.fn().mockResolvedValue(false) }))

vi.mock('./lib/localHost', () => ({ getLocalEndpoint: async () => ({
  base_url: 'http://127.0.0.1:9000', session_token: 'synthetic-local-session',
}) }))
vi.mock('./pages/Dashboard', () => ({ default: () => <p>Restored dashboard</p> }))
vi.mock('./pages/Connections', () => ({ default: () => <button onClick={() => {
  void api.get('/connections').catch(() => {})
}}>Load connections</button> }))

const previousAdapter = api.defaults.adapter
afterEach(() => { cleanup(); api.defaults.adapter = previousAdapter; useAuthStore.getState().logout() })

function ok(config: InternalAxiosRequestConfig, data: unknown): AxiosResponse {
  return { config, data, status: 200, statusText: 'OK', headers: {} }
}

it('recovers an expired protected session silently without resending the protected request', async () => {
  const user = { id: 1, email: 'owner@example.com', username: 'owner' }
  useAuthStore.getState().logout()
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(`${config.method}:${config.url}`)
    if (config.url === '/auth/restore') return ok(config, { access_token: calls.length === 1 ? 'expired-jwt' : 'renewed-jwt' })
    if (config.url === '/connections') throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, {}, {
      config, data: {}, status: 401, statusText: 'Unauthorized', headers: {},
    })
    if (config.method === 'post' && config.url === '/auth/restore/interactive')
      return ok(config, { state: 'pending', unlock_id: 'unlock-request-123456789012' })
    if (config.method === 'get' && config.url === '/auth/restore/interactive')
      return ok(config, { state: 'ready', access_token: 'renewed-jwt' })
    if (config.url === '/auth/me') {
      expect(['Bearer expired-jwt', 'Bearer renewed-jwt']).toContain(config.headers.Authorization)
      return ok(config, user)
    }
    throw new Error(`unexpected request ${config.method}:${config.url}`)
  }
  render(<MemoryRouter initialEntries={['/connections']}><App /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Load connections' }))
  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy()
  expect(screen.queryByText('Starting the local service…')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Continue with saved login' }))
  expect(await screen.findByText('Restored dashboard', {}, { timeout: 1500 })).toBeTruthy()
  expect(calls).toEqual([
    'post:/auth/restore', 'get:/auth/me', 'get:/connections', 'post:/auth/restore', 'get:/auth/me',
  ])
})

it('validates a restored token with /auth/me before showing the signed-in app', async () => {
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(config.url!)
    if (config.url === '/auth/restore') return ok(config, { access_token: 'restored-jwt' })
    expect(config.headers.Authorization).toBe('Bearer restored-jwt')
    return ok(config, { id: 1, email: 'owner@example.com', username: 'owner' })
  }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
  expect(await screen.findByText('Restored dashboard')).toBeTruthy()
  expect(calls).toEqual(['/auth/restore', '/auth/me'])
  expect(useAuthStore.getState().token).toBe('restored-jwt')
})

it('does not resurrect a session when logout happens while restore is in flight', async () => {
  let completeRestore!: (response: AxiosResponse) => void
  let capturedConfig!: InternalAxiosRequestConfig
  api.defaults.adapter = async (config) => {
    if (config.url === '/auth/restore') {
      capturedConfig = config
      return new Promise<AxiosResponse>((resolve) => { completeRestore = resolve })
    }
    return ok(config, { id: 1, email: 'owner@example.com', username: 'owner' })
  }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
  await waitFor(() => expect(completeRestore).toBeDefined())
  useAuthStore.getState().logout()
  completeRestore(ok(capturedConfig, { access_token: 'late-jwt' }))
  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy()
  expect(useAuthStore.getState().token).toBeNull()
})

it('turns one stale remembered-login restore into a normal manual sign-in prompt', async () => {
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(config.url!)
    const response = { data: { detail: { action: 'sign_in_again' } },
      status: 401, statusText: 'Unauthorized', headers: {}, config } as AxiosResponse
    throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, {}, response)
  }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)

  expect(await screen.findByText('Your saved login expired. Sign in again with your email and password.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Continue with saved login' })).toBeNull()
  await waitFor(() => expect(calls).toEqual(['/auth/restore']))
  expect(screen.queryByText(/Keychain|terminal|security\s+command/i)).toBeNull()
})

it.each(['credential_locked', 'secret_store_unavailable'])(
  'keeps explicit recovery available without promising credential replacement after %s',
  async (code) => {
    const calls: string[] = []
    api.defaults.adapter = async (config) => {
      calls.push(config.url!)
      const response = { data: { detail: { code, correlation_id: 'a'.repeat(32) } },
        status: 503, statusText: 'Unavailable', headers: {}, config } as AxiosResponse
      throw new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', config, {}, response)
    }
    render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)

    expect(await screen.findByText(
      'Your saved login can’t be accessed right now. Try again or open Report a problem.',
    )).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy()
    expect((screen.getByRole('checkbox', { name: /Sign in automatically on this device/ }) as HTMLInputElement).checked).toBe(false)
    expect(screen.getByRole('button', { name: 'Continue with saved login' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Report a problem' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Report a problem' }).closest('[role="status"]')).toBeTruthy()
    expect(screen.queryByText(/save it again/i)).toBeNull()
    expect(screen.queryByText(new RegExp(code))).toBeNull()
    expect(screen.queryByText(/Keychain|terminal|security\s+command/i)).toBeNull()
    await waitFor(() => expect(calls).toEqual(['/auth/restore']))
    fireEvent.click(screen.getByRole('button', { name: 'Continue with saved login' }))
    await waitFor(() => expect(calls).toEqual(['/auth/restore', '/auth/restore']))
    expect(screen.getByText('Your saved login can’t be accessed right now. Try again or open Report a problem.')).toBeTruthy()
  },
)

it.each([
  ['malformed response', (config: InternalAxiosRequestConfig) => {
    const response = { data: { detail: { code: 'secret_store_unavailable', correlation_id: 'not-valid' } },
      status: 503, statusText: 'Unavailable', headers: {}, config } as AxiosResponse
    return new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', config, {}, response)
  }],
  ['network failure', (config: InternalAxiosRequestConfig) => new AxiosError('Network Error', 'ERR_NETWORK', config)],
] as const)('does not label a %s as an expired or repairable saved login', async (_label, failure) => {
  api.defaults.adapter = async (config) => { throw failure(config) }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)

  expect(await screen.findByRole('button', { name: 'Continue with saved login' })).toBeTruthy()
  expect(screen.queryByText(/saved login expired/i)).toBeNull()
  expect(screen.queryByText(/select .*sign in automatically/i)).toBeNull()
})

it('does not show a late restore failure after the auth owner epoch changes', async () => {
  let rejectRestore!: (error: unknown) => void
  let capturedConfig!: InternalAxiosRequestConfig
  api.defaults.adapter = async (config) => {
    capturedConfig = config
    return new Promise<AxiosResponse>((_resolve, reject) => { rejectRestore = reject })
  }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
  await waitFor(() => expect(rejectRestore).toBeDefined())
  useAuthStore.getState().logout()
  const response = { data: { detail: { code: 'secret_store_unavailable', correlation_id: 'b'.repeat(32) } },
    status: 503, statusText: 'Unavailable', headers: {}, config: capturedConfig } as AxiosResponse
  rejectRestore(new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', capturedConfig, {}, response))

  expect(await screen.findByRole('button', { name: 'Continue with saved login' })).toBeTruthy()
  expect(screen.queryByText(/select .*sign in automatically/i)).toBeNull()
})

it('handles interactive reentry without polling an absent unlock request', async () => {
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(`${config.method}:${config.url}`)
    if (config.url === '/auth/restore') {
      const response = { data: { detail: 'Restore unavailable' },
        status: 503, statusText: 'Unavailable', headers: {}, config } as AxiosResponse
      throw new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', config, {}, response)
    }
    if (config.method === 'post' && config.url === '/auth/restore/interactive')
      return ok(config, { state: 'reentry_required', action: 'sign_in_again' })
    throw new Error(`unexpected request ${config.method}:${config.url}`)
  }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)

  fireEvent.click(await screen.findByRole('button', { name: 'Continue with saved login' }))
  expect(await screen.findByText('The saved login is unavailable.')).toBeTruthy()
  expect(screen.queryByText(/saved login expired/i)).toBeNull()
  await new Promise((resolve) => window.setTimeout(resolve, 550))
  expect(calls).toEqual(['post:/auth/restore', 'post:/auth/restore', 'post:/auth/restore/interactive'])
  expect(screen.queryByRole('button', { name: 'Continue with saved login' })).toBeNull()
  expect(screen.queryByText('Your saved login is unavailable. Sign in again.')).toBeNull()
})

it('polls a genuine interactive handle until the saved login is ready', async () => {
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(`${config.method}:${config.url}`)
    if (config.url === '/auth/restore') {
      const response = { data: { detail: 'Restore unavailable' },
        status: 503, statusText: 'Unavailable', headers: {}, config } as AxiosResponse
      throw new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', config, {}, response)
    }
    if (config.method === 'post' && config.url === '/auth/restore/interactive')
      return ok(config, { state: 'pending', unlock_id: 'unlock-request-123456789012' })
    if (config.method === 'get' && config.url === '/auth/restore/interactive') {
      expect(config.params).toEqual({ unlock_id: 'unlock-request-123456789012' })
      return ok(config, { state: 'ready', access_token: 'interactive-jwt' })
    }
    if (config.url === '/auth/me') {
      expect(config.headers.Authorization).toBe('Bearer interactive-jwt')
      return ok(config, { id: 1, email: 'owner@example.com', username: 'owner' })
    }
    throw new Error(`unexpected request ${config.method}:${config.url}`)
  }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)

  fireEvent.click(await screen.findByRole('button', { name: 'Continue with saved login' }))
  expect(await screen.findByText('Restored dashboard', {}, { timeout: 1500 })).toBeTruthy()
  expect(calls).toEqual([
    'post:/auth/restore',
    'post:/auth/restore',
    'post:/auth/restore/interactive',
    'get:/auth/restore/interactive',
    'get:/auth/me',
  ])
})

it('rejects a malformed interactive handle without issuing a follow-up request', async () => {
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(`${config.method}:${config.url}`)
    if (config.url === '/auth/restore') {
      const response = { data: { detail: 'Restore unavailable' },
        status: 503, statusText: 'Unavailable', headers: {}, config } as AxiosResponse
      throw new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', config, {}, response)
    }
    if (config.method === 'post' && config.url === '/auth/restore/interactive')
      return ok(config, { state: 'pending', unlock_id: 'invalid handle with spaces' })
    throw new Error(`unexpected request ${config.method}:${config.url}`)
  }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)

  fireEvent.click(await screen.findByRole('button', { name: 'Continue with saved login' }))
  expect(await screen.findByText('The saved login is unavailable.')).toBeTruthy()
  await new Promise((resolve) => window.setTimeout(resolve, 550))
  expect(calls).toEqual(['post:/auth/restore', 'post:/auth/restore', 'post:/auth/restore/interactive'])
})

it.each(['restore', 'profile'] as const)('does not resurrect another owner after a delayed manual %s response', async (phase) => {
  const calls: string[] = []
  let complete!: (response: AxiosResponse) => void
  let pendingConfig!: InternalAxiosRequestConfig
  api.defaults.adapter = async (config) => {
    calls.push(config.url!)
    if (calls.length === 1) throw new AxiosError('Network Error', 'ERR_NETWORK', config)
    if (config.url === (phase === 'restore' ? '/auth/restore' : '/auth/me')) {
      pendingConfig = config
      return new Promise<AxiosResponse>((resolve) => { complete = resolve })
    }
    return ok(config, { access_token: 'manual-jwt' })
  }
  render(<MemoryRouter><App /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Continue with saved login' }))
  await waitFor(() => expect(complete).toBeDefined())
  useAuthStore.getState().logout()
  complete(ok(pendingConfig, phase === 'restore' ? { access_token: 'late-jwt' }
    : { id: 1, email: 'owner@example.com', username: 'owner' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Continue with saved login' }).hasAttribute('disabled')).toBe(false))
  expect(useAuthStore.getState().token).toBeNull()
  expect(calls).toEqual(phase === 'restore' ? ['/auth/restore', '/auth/restore']
    : ['/auth/restore', '/auth/restore', '/auth/me'])
})

it.each(['network', 'unauthorized', 'profile unavailable'] as const)('does not start interactive unlock after a manual %s failure', async (failure) => {
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(config.url!)
    if (calls.length === 1) throw new AxiosError('Network Error', 'ERR_NETWORK', config)
    if (failure === 'profile unavailable' && config.url === '/auth/restore') return ok(config, { access_token: 'manual-jwt' })
    if (failure === 'network') throw new AxiosError('Network Error', 'ERR_NETWORK', config)
    const response = { config, data: {}, status: failure === 'unauthorized' ? 401 : 503,
      statusText: 'Unavailable', headers: {} }
    throw new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', config, {}, response)
  }
  render(<MemoryRouter><App /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Continue with saved login' }))
  expect(await screen.findByText('The saved login is unavailable.')).toBeTruthy()
  expect(calls).toEqual(failure === 'profile unavailable' ? ['/auth/restore', '/auth/restore', '/auth/me']
    : ['/auth/restore', '/auth/restore'])
  expect(screen.queryByText(/saved login expired/i)).toBeNull()
})

it('does not start legacy unlock after a delayed silent-store failure changes owners', async () => {
  let reject!: (error: unknown) => void
  let pendingConfig!: InternalAxiosRequestConfig
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(config.url!)
    if (calls.length === 1) throw new AxiosError('Network Error', 'ERR_NETWORK', config)
    pendingConfig = config
    return new Promise<AxiosResponse>((_resolve, fail) => { reject = fail })
  }
  render(<MemoryRouter><App /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Continue with saved login' }))
  await waitFor(() => expect(reject).toBeDefined())
  useAuthStore.getState().logout()
  reject(new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', pendingConfig, {}, {
    config: pendingConfig, data: {}, status: 503, statusText: 'Unavailable', headers: {},
  }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Continue with saved login' }).hasAttribute('disabled')).toBe(false))
  expect(calls).toEqual(['/auth/restore', '/auth/restore'])
  expect(screen.queryByText(/saved login expired/i)).toBeNull()
})

it('restores the same owner after explicit retry when credential access becomes available', async () => {
  const calls: string[] = []
  api.defaults.adapter = async (config) => {
    calls.push(config.url!)
    if (calls.length === 1) {
      const response = { data: { detail: { code: 'secret_store_unavailable', correlation_id: 'a'.repeat(32) } },
        status: 503, statusText: 'Unavailable', headers: {}, config } as AxiosResponse
      throw new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', config, {}, response)
    }
    if (config.url === '/auth/restore') return ok(config, { access_token: 'recovered-jwt' })
    expect(config.url).toBe('/auth/me')
    expect(config.headers.Authorization).toBe('Bearer recovered-jwt')
    return ok(config, { id: 1, email: 'owner@example.com', username: 'owner' })
  }
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Continue with saved login' }))
  expect(await screen.findByText('Restored dashboard')).toBeTruthy()
  expect(calls).toEqual(['/auth/restore', '/auth/restore', '/auth/me'])
  expect(useAuthStore.getState().user?.id).toBe(1)
  expect(screen.queryByText(/saved login can’t be accessed/i)).toBeNull()
})

it('does not restore a saved login after a session-only window reload', async () => {
  sessionStorage.setItem('gamja.session-only-login.v1', '1')
  useAuthStore.getState().logout()
  const calls: string[] = []
  api.defaults.adapter = async config => { calls.push(String(config.url)); throw new Error('No real network') }
  try {
    const first = render(<MemoryRouter initialEntries={['/login']}><App /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy()
    expect(calls).toEqual([])
    first.unmount()
    render(<MemoryRouter initialEntries={['/login']}><App /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy()
    expect(calls).toEqual([])
  } finally { sessionStorage.removeItem('gamja.session-only-login.v1') }
})

it('opens manual login without restoring when the native startup mode explicitly skips it', async () => {
  sessionStorage.removeItem('gamja.session-only-login.v1')
  useAuthStore.getState().logout()
  vi.mocked(skipAutoRestoreOnStartup).mockResolvedValueOnce(true)
  const calls: string[] = []
  api.defaults.adapter = async config => { calls.push(String(config.url)); throw new Error('No real network') }
  render(<MemoryRouter initialEntries={['/login']}><App /></MemoryRouter>)
  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeTruthy()
  expect(screen.getByRole('checkbox', { name: 'Keep the saved login and sign in only for this window' })).toBeTruthy()
  expect(calls).toEqual([])
})
