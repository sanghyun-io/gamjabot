import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useState } from 'react'
import { waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import './networkGuard'
import { installNativeFixture } from './native'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { toolsApi } from '../src/api/tools'
import { artifactsApi } from '../src/api/artifacts'
import ScreenshotArtifact from '../src/components/ScreenshotArtifact'

type Outcome = 'saved' | 'cancelled' | 'unknown'
const action = '11111111-1111-4111-8111-111111111111'
// A synthetic one-pixel PNG; no user file or network fetch is involved.
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aL8AAAAASUVORK5CYII='

function ScreenshotSurface({ outcome }: { outcome: Outcome }) {
  const [ready, setReady] = useState(false)
  useLayoutEffect(() => {
    useAuthStore.getState().setAuth('synthetic-preview-token', { id: 1, email: 'preview@example.invalid', username: 'Preview' })
    const originalRead = toolsApi.screenshot, originalDelete = toolsApi.deleteScreenshot, originalList = artifactsApi.list
    toolsApi.screenshot = async () => new Blob([Uint8Array.from(atob(png), c => c.charCodeAt(0))], { type: 'image/png' })
    toolsApi.deleteScreenshot = async () => { throw Error('Synthetic deletion disabled') }
    artifactsApi.list = async () => { throw Error('Synthetic list disabled') }
    const auth = useAuthStore.getState(), key = `gamja.screenshot-save.${auth.user?.id}.${auth.epoch}.501:${action}:1`
    sessionStorage.removeItem(key)
    const restore = installNativeFixture(async (command, args) => {
      if (command === 'workspace_export_transport') return 'native'
      if (command === 'choose_binary_file_export') return outcome === 'cancelled' ? null : { selection_id: 'b'.repeat(32), filename: 'screenshot.png', replacing: false }
      if (command === 'save_binary_file_export') {
        if (outcome === 'unknown') throw Error('Synthetic lost write response')
        return { path: '/synthetic/screenshot.png', bytes: atob(String(args?.contentBase64 ?? '')).length, replaced: false, durability_unconfirmed: false }
      }
      if (command === 'discard_workspace_export') return undefined
      throw Error('Unexpected synthetic command')
    })
    setReady(true)
    return () => { restore(); toolsApi.screenshot = originalRead; toolsApi.deleteScreenshot = originalDelete; artifactsApi.list = originalList; sessionStorage.removeItem(key) }
  }, [outcome])
  return ready ? <I18nProvider><main className="page-frame page-content"><ScreenshotArtifact roomId={501} actionId={action} eventSeq={1} width={1} height={1} createdAt="2026-10-31T00:00:00Z" /></main></I18nProvider> : null
}

const meta = { title: 'Screens/스크린샷 저장', component: ScreenshotSurface } satisfies Meta<typeof ScreenshotSurface>
export default meta
type Story = StoryObj<typeof meta>
const openAndSave = async ({ canvasElement }: { canvasElement: HTMLElement }) => {
  const thumb = await waitFor(() => {
    const element = canvasElement.querySelector<HTMLButtonElement>('.attachment__thumb')
    if (!element) throw Error('Synthetic screenshot not ready')
    return element
  })
  await userEvent.click(thumb)
  const save = await waitFor(() => {
    const buttons = document.querySelectorAll<HTMLButtonElement>('.viewer__toolbar .page-actions button')
    if (!buttons[4]) throw Error('Synthetic save button not ready')
    return buttons[4]
  })
  await userEvent.click(save)
  await waitFor(() => {
    const alert = document.querySelector('.viewer [role="alert"]')
    const status = document.querySelector('.viewer [role="status"]')
    if (!alert && (!status || save.disabled)) throw Error('Synthetic save result not ready')
  })
}
export const NativeSaved: Story = { name: '스크린샷 · 저장 성공 (합성 IPC)', args: { outcome: 'saved' }, play: openAndSave }
export const NativeCancelled: Story = { name: '스크린샷 · 저장 취소 (합성 IPC)', args: { outcome: 'cancelled' }, play: openAndSave }
export const NativeUnknown: Story = { name: '스크린샷 · 결과 불명과 위치 확인 (합성 IPC)', args: { outcome: 'unknown' }, play: openAndSave }
