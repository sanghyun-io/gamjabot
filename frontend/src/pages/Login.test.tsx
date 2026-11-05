// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import Login from './Login'
import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { I18nProvider } from '../i18n'

vi.mock('../lib/localHost', () => ({ getLocalEndpoint: async () => ({
  base_url: 'http://127.0.0.1:9000', session_token: 'synthetic-local-session',
}) }))

const previousAdapter = api.defaults.adapter
afterEach(() => { cleanup(); api.defaults.adapter = previousAdapter })

function renderLogin(props: Parameters<typeof Login>[0] = {}) {
  return render(<I18nProvider systemLanguages={() => ['ko']}><MemoryRouter><Login {...props} /></MemoryRouter></I18nProvider>)
}

it('shows a rejected login without clearing the form or changing the session', async () => {
  api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    const response = { data: { detail: 'Incorrect email or password' }, status: 401,
      statusText: 'Unauthorized', headers: {}, config } as AxiosResponse
    throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, {}, response)
  }
  const before = useAuthStore.getState().epoch
  renderLogin()
  fireEvent.change(screen.getByLabelText('이메일'), { target: { value: 'test@example.com' } })
  fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'wrong-password' } })
  fireEvent.click(screen.getByRole('button', { name: '로그인' }))

  expect(await screen.findByText('로그인에 실패했습니다.')).toBeTruthy()
  expect(screen.queryByText('Incorrect email or password')).toBeNull()
  expect((screen.getByLabelText('이메일') as HTMLInputElement).value).toBe('test@example.com')
  expect((screen.getByLabelText('비밀번호') as HTMLInputElement).value).toBe('wrong-password')
  await waitFor(() => expect(useAuthStore.getState().epoch).toBe(before))
})

it('shows a profile lookup failure after login without reloading the form', async () => {
  api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    if (config.url === '/auth/login') {
      return { data: { access_token: 'synthetic-user-token', token_type: 'bearer' }, status: 200,
        statusText: 'OK', headers: {}, config } as AxiosResponse
    }
    const response = { data: { detail: 'Could not validate credentials' }, status: 401,
      statusText: 'Unauthorized', headers: {}, config } as AxiosResponse
    throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, {}, response)
  }
  const before = useAuthStore.getState().epoch
  renderLogin()
  fireEvent.change(screen.getByLabelText('이메일'), { target: { value: 'test@example.com' } })
  fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'test-password' } })
  fireEvent.click(screen.getByRole('button', { name: '로그인' }))

  expect(await screen.findByText('로그인에 실패했습니다.')).toBeTruthy()
  expect(screen.queryByText('Could not validate credentials')).toBeNull()
  expect((screen.getByLabelText('이메일') as HTMLInputElement).value).toBe('test@example.com')
  expect(useAuthStore.getState().epoch).toBe(before)
})

it('sends remember-device only after explicit checkbox opt-in', async () => {
  const bodies: unknown[] = []
  api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    if (config.url === '/auth/login') {
      bodies.push(JSON.parse(config.data))
      return { data: { access_token: 'synthetic-user-token', token_type: 'bearer' }, status: 200,
        statusText: 'OK', headers: {}, config } as AxiosResponse
    }
    return { data: { id: 1, username: 'owner', email: 'test@example.com' }, status: 200,
      statusText: 'OK', headers: {}, config } as AxiosResponse
  }
  renderLogin()
  fireEvent.change(screen.getByLabelText('이메일'), { target: { value: 'test@example.com' } })
  fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'test-password' } })
  fireEvent.click(screen.getByText(/이 기기에서 자동 로그인/))
  fireEvent.click(screen.getByRole('button', { name: '로그인' }))
  await waitFor(() => expect(bodies).toEqual([{ email: 'test@example.com', password: 'test-password', remember_device: true }]))
})

it('keeps login form visible when secure-store save fails', async () => {
  api.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
    const response = { data: { detail: "Could not save automatic login in this device's secure store" },
      status: 503, statusText: 'Unavailable', headers: {}, config } as AxiosResponse
    throw new AxiosError('Unavailable', 'ERR_BAD_RESPONSE', config, {}, response)
  }
  const before = useAuthStore.getState().epoch
  renderLogin()
  fireEvent.change(screen.getByLabelText('이메일'), { target: { value: 'test@example.com' } })
  fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'test-password' } })
  fireEvent.click(screen.getByText(/이 기기에서 자동 로그인/))
  fireEvent.click(screen.getByRole('button', { name: '로그인' }))
  expect(await screen.findByText('로그인에 실패했습니다.')).toBeTruthy()
  expect(screen.queryByText(/Could not save automatic login/)).toBeNull()
  expect(useAuthStore.getState().epoch).toBe(before)
})
