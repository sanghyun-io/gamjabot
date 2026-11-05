// @vitest-environment jsdom
import axios from 'axios'
import { afterEach, expect, it, vi } from 'vitest'
import { useAuthStore } from '../stores/authStore'
import { clearSessionOnly, markSessionOnly, suppressAutomaticRestore } from './sessionOnlyLogin'
import { ensureSessionContinuity } from './sessionContinuity'

const owner = { id: 1, email: 'owner@example.invalid', username: 'Owner' }
const endpoint = { base_url: 'http://127.0.0.1:1', session_token: 'synthetic' }
const token = (seconds: number) => `synthetic.${btoa(JSON.stringify({ sub: '1', exp: Date.now() / 1000 + seconds }))}.signature`
afterEach(() => { useAuthStore.getState().logout(); sessionStorage.clear(); vi.restoreAllMocks() })

it('keeps the current session only and does not restore its saved login at expiry', async () => {
  const post = vi.spyOn(axios, 'post')
  const currentToken = token(20)
  useAuthStore.getState().setAuth(currentToken, owner, { sessionOnly: true })
  const snapshot = useAuthStore.getState()
  expect(await ensureSessionContinuity(endpoint, snapshot)).toBe(currentToken)
  expect(post).not.toHaveBeenCalled()
  useAuthStore.getState().setAuth(token(-1), owner, { sessionOnly: true })
  await expect(ensureSessionContinuity(endpoint, useAuthStore.getState())).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  expect(useAuthStore.getState().token).toBeNull()
  expect(post).not.toHaveBeenCalled()
})

it('keeps the non-secret automatic restore suppression across a same-store reload', () => {
  expect(suppressAutomaticRestore()).toBe(false)
  expect(markSessionOnly()).toBe(true)
  expect(suppressAutomaticRestore()).toBe(true)
  clearSessionOnly()
  expect(suppressAutomaticRestore()).toBe(false)
})
