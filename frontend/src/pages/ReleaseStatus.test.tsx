// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Dashboard from './Dashboard'
import Devices from './Devices'

vi.mock('../api/agents', () => ({ agentsApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('../api/projects', () => ({ projectsApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('../api/remote', () => ({ remoteApi: { listDevices: vi.fn().mockResolvedValue([
  { device_id: 1, device_name: 'Legacy laptop', is_active: true },
]) } }))
vi.mock('../stores/authStore', () => ({ useAuthStore: () => ({ user: { username: 'Owner' }, logout: vi.fn() }) }))

afterEach(cleanup)

function renderPage(page: React.ReactNode) {
  return render(<MemoryRouter><QueryClientProvider client={new QueryClient()}>{page}</QueryClientProvider></MemoryRouter>)
}

it('shows exact bundle version and the public manual distribution location without a remote connection claim', () => {
  renderPage(<Dashboard />)
  expect(screen.getByText(/Gamja Bot v1\.0\.0/)).toBeTruthy()
  expect(screen.getByText(/sanghyun-io\/gamja-bot-releases/)).toBeTruthy()
  expect(screen.queryByText(/Public downloads are still being prepared/)).toBeNull()
  expect(screen.queryByRole('link', { name: /원격 기기 관리/ })).toBeNull()
  expect(screen.getByLabelText('Mobile continuation unavailable')).toBeTruthy()
})

it('preserves legacy device visibility while connection controls remain unavailable', async () => {
  renderPage(<Devices />)
  expect(await screen.findByText(/Legacy laptop/)).toBeTruthy()
  expect(screen.getByText(/Remote connections and mobile conversation continuation are not available yet/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: /등록/ })).toBeNull()
})
