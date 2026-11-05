// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Register from './Register'
import api from '../lib/api'
import { I18nProvider } from '../i18n'

vi.mock('../lib/api', () => ({ default: { post: vi.fn() } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('shows an explicit local-account success handoff instead of silently redirecting', async () => {
  vi.mocked(api.post).mockResolvedValue({ data: {} })
  render(<I18nProvider systemLanguages={() => ['en']}><MemoryRouter><Register /></MemoryRouter></I18nProvider>)
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'owner@example.test' } })
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'owner' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct horse battery staple' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

  expect((await screen.findByRole('status')).textContent).toContain('Your local account is ready')
  expect(screen.getByRole('link', { name: 'Continue to sign in' }).getAttribute('href')).toBe('/login?registered=1')
  expect(screen.queryByLabelText('Password')).toBeNull()
  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1))
})
