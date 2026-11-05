// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import McpServers from './McpServers'
import { mcpApi, type McpServer } from '../api/mcp'
let identity = 0
vi.mock('../lib/useBotScope', () => ({ useBotScope: () => ({ identity: String(identity), ownerId: 1, canUse: () => true }) }))
vi.mock('../i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('../components/McpCredential', () => ({ default: () => null }))
const server: McpServer = { version: 1, server_id: 'b2000000-0000-4000-8000-000000000001', owner_id: 1, revision: 1, name: 'Docs', enabled: true, transport: { type: 'http', url: 'https://example.invalid/mcp' }, credential_ref: null, discovery_state: 'not_discovered' }
const preview = { version: 1 as const, preview_id: 'b2000000-0000-4000-8000-000000000002', server_id: server.server_id, revision: 1, transport: server.transport, expires_at: '2099-10-29T00:00:00Z' }
const receipt = (state: 'running' | 'unknown', revision = 1) => ({ version: 1, operation: 'discover', discovery: { discovery_id: 'b2000000-0000-4000-8000-000000000003', server_id: server.server_id, server_revision: revision, state, failure_code: null } })
async function start() {
 identity++
 vi.spyOn(mcpApi, 'list').mockResolvedValue([server])
 vi.spyOn(mcpApi, 'preview').mockResolvedValue(preview)
 render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><McpServers /></QueryClientProvider>)
 fireEvent.click(await screen.findByText('mcp.inspect'))
 fireEvent.click(await screen.findByText('mcp.discover'))
 await screen.findByText('mcp.read')
}
afterEach(() => { cleanup(); vi.restoreAllMocks() })
describe('discovery result recovery', () => {
 it('unlocks only after a matching terminal unknown receipt, without another discovery', async () => {
  const discover = vi.spyOn(mcpApi, 'discover').mockResolvedValue(receipt('running') as never)
  const command = vi.spyOn(mcpApi, 'command').mockResolvedValue(receipt('unknown') as never)
  await start()
  expect((screen.getByText('mcp.add') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByText('mcp.read'))
  await screen.findByText('mcp.discoveryUnknown')
  await waitFor(() => expect((screen.getByText('mcp.add') as HTMLButtonElement).disabled).toBe(false))
  expect(command).toHaveBeenCalledTimes(1)
  expect(discover).toHaveBeenCalledTimes(1)
 })
 it.each(['missing', 'mismatch'] as const)('keeps writes locked for a %s receipt', async (kind) => {
  const discover = vi.spyOn(mcpApi, 'discover').mockRejectedValue(new Error('response lost'))
  const command = vi.spyOn(mcpApi, 'command')
  if (kind === 'missing') command.mockRejectedValue(new Error('not found'))
  else command.mockResolvedValue(receipt('unknown', 2) as never)
  await start()
  fireEvent.click(screen.getByText('mcp.read'))
  await waitFor(() => expect(command).toHaveBeenCalledTimes(1))
  expect((screen.getByText('mcp.add') as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryByText('mcp.discoveryUnknown')).toBeNull()
  expect(discover).toHaveBeenCalledTimes(1)
 })
})
