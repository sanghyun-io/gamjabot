import { invoke, isTauri } from '@tauri-apps/api/core'

export type VoicePhase = 'idle' | 'waiting' | 'listening' | 'stopping' | 'review' | 'unsupported' | 'permission_needed' | 'error' | 'unknown'
export type VoiceSnapshot = { phase: VoicePhase; text: string }
export interface VoiceAdapter {
  available(): Promise<boolean>
  action(action: 'start' | 'stop' | 'cancel', token: string): Promise<void>
  snapshot(token: string): Promise<VoiceSnapshot>
}
const phases = new Set<VoicePhase>(['idle', 'waiting', 'listening', 'stopping', 'review', 'unsupported', 'permission_needed', 'error', 'unknown'])
export const nativeVoice: VoiceAdapter = {
  async available() { return isTauri() && await invoke<string>('voice_transport') === 'native' },
  async action(action, token) { await invoke('voice_action', { action, token }) },
  async snapshot(token) {
    const value = await invoke<VoiceSnapshot>('voice_snapshot', { token })
    if (!value || !phases.has(value.phase) || typeof value.text !== 'string' || value.text.length > 65536) throw Error('voice_invalid_snapshot')
    return value
  },
}
