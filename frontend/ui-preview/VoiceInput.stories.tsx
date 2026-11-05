import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import VoiceInput from '../src/components/VoiceInput'
import type { VoiceAdapter, VoicePhase } from '../src/lib/voiceInput'

// Synthetic states only; no microphone, native command, or network access.
function makeAdapter(phase: VoicePhase): VoiceAdapter {
  return {
    async available() { return phase !== 'unsupported' },
    async action() {},
    async snapshot() { return { phase, text: phase === 'review' ? '내일 오후 세 시에 회의 준비해 줘.' : '' } },
  }
}
const adapters = Object.fromEntries((['unsupported', 'waiting', 'listening', 'stopping', 'review', 'permission_needed', 'error', 'unknown'] as VoicePhase[]).map(phase => [phase, makeAdapter(phase)])) as Record<VoicePhase, VoiceAdapter>
function Surface({ phase }: { phase: VoicePhase }) {
  const [draft, setDraft] = useState('기존 초안')
  return <I18nProvider><main className="page-frame page-content stack"><label>메시지<textarea value={draft} onChange={event => setDraft(event.target.value)} /></label><VoiceInput adapter={adapters[phase]} canUse={() => true} scope={() => "synthetic-draft"} onInsert={text => { setDraft(value => `${value}\n${text}`); return true }} /></main></I18nProvider>
}
const meta = { title: 'Screens/음성 입력', component: Surface } satisfies Meta<typeof Surface>
export default meta
type Story = StoryObj<typeof meta>
const begin = async ({ canvasElement }: { canvasElement: HTMLElement }) => {
  const canvas = within(canvasElement)
  await userEvent.click(await canvas.findByRole('button', { name: /음성 입력|Dictate/ }))
}
export const Unsupported: Story = { args: { phase: 'unsupported' }, play: begin }
export const PermissionNeeded: Story = { args: { phase: 'permission_needed' }, play: begin }
export const Waiting: Story = { args: { phase: 'waiting' }, play: begin }
export const Listening: Story = { args: { phase: 'listening' }, play: begin }
export const Stopping: Story = { args: { phase: 'stopping' }, play: begin }
export const Review: Story = { args: { phase: 'review' }, play: begin }
export const Error: Story = { args: { phase: 'error' }, play: begin }
export const Unknown: Story = { args: { phase: 'unknown' }, play: begin }
