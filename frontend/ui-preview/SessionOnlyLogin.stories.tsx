import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useMemo, useState } from 'react'
import { AxiosError, type AxiosAdapter } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import api from '../src/lib/api'
import { I18nProvider } from '../src/i18n'
import Login from '../src/pages/Login'

type Scenario = 'ready' | 'mismatch' | 'unavailable' | 'success' | 'storage-error'
function Surface({ scenario }: { scenario: Scenario }) {
  const [ready, setReady] = useState(false)
  const store = useMemo(() => {
    const values = new Map<string, string>()
    return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => {
      if (scenario === 'storage-error') throw new Error('synthetic storage unavailable')
      values.set(key, value)
    }, removeItem: (key: string) => { values.delete(key) } }
  }, [scenario])
  useLayoutEffect(() => {
    const previous = api.defaults.adapter
    api.defaults.adapter = (async config => {
      const respond = (data: unknown, status = 200) => ({ data, status, statusText: 'Synthetic', headers: {}, config })
      if (config.url === '/auth/login/session-only' && config.method === 'post') {
        if (scenario === 'mismatch' || scenario === 'unavailable') {
          const status = scenario === 'mismatch' ? 409 : 404
          throw new AxiosError('synthetic failure', undefined, config, undefined,
            respond({ detail: { code: 'saved_login_owner_mismatch' } }, status))
        }
        return respond({ access_token: 'synthetic-access-token' })
      }
      if (config.url === '/auth/me' && config.method === 'get') return respond({ id: 1, email: 'synthetic@example.invalid', username: 'Preview' })
      throw new Error('Unexpected synthetic request')
    }) satisfies AxiosAdapter
    setReady(true)
    return () => { api.defaults.adapter = previous }
  }, [scenario])
  return <I18nProvider><MemoryRouter initialEntries={['/login']}><Routes>
    <Route path="/login" element={ready ? <Login sessionOnlyStore={store} notice="합성: 저장된 로그인에 접근할 수 없습니다."/> : null}/>
    <Route path="/" element={<p role="status">합성 로그인 완료 · 실제 인증 없음</p>}/>
  </Routes></MemoryRouter></I18nProvider>
}
const meta = { title: 'Product/Session-only login', component: Surface } satisfies Meta<typeof Surface>
export default meta
type Story = StoryObj<typeof meta>
export const Ready: Story = { args: { scenario: 'ready' } }
export const OwnerMismatch: Story = { args: { scenario: 'mismatch' } }
export const Unsupported: Story = { args: { scenario: 'unavailable' } }
export const Success: Story = { args: { scenario: 'success' } }
export const StorageError: Story = { args: { scenario: 'storage-error' } }
