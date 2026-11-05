import { useAuthStore } from '../stores/authStore'
import type { BotMemoryCategory, BotMemoryDelete, BotMemoryEdit, BotMemorySource, BotMemoryWrite } from '../api/botMemories'

export type BotMemoryEditor = { id: string; memoryId: string | null; category: BotMemoryCategory; content: string; source: BotMemorySource
  scopeRevision: number; entryRevision: number | null }
export type BotMemoryOperation = { key: string; agentId: number; memoryId: string | null; kind: 'add' | 'edit' | 'remove'
  body: BotMemoryWrite | BotMemoryEdit | BotMemoryDelete; editorId: string | null; content: string
  phase: 'sending' | 'unknown' | 'rejected'; errorCode?: string }
export type BotMemoryWorkspace = { editor: BotMemoryEditor | null; operation: BotMemoryOperation | null; notice: 'saved' | 'removed' | null }
const empty: BotMemoryWorkspace = Object.freeze({ editor: null, operation: null, notice: null })
const workspaces = new Map<string, BotMemoryWorkspace>(), listeners = new Set<() => void>()
const notify = () => { for (const listener of listeners) listener() }
export const botMemoryWorkspaceKey = (identity: string, agentId: number) => `${identity}:${agentId}`
export const readBotMemoryWorkspace = (key: string) => workspaces.get(key) || empty
export const subscribeBotMemoryWorkspace = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
export function updateBotMemoryWorkspace(key: string, change: (value: BotMemoryWorkspace) => BotMemoryWorkspace) {
  const auth = useAuthStore.getState()
  if (!auth.token || !key.startsWith(`${auth.user?.id || 0}:${auth.epoch}:`)) return false
  const current = readBotMemoryWorkspace(key), next = change(current)
  if (next !== current) { workspaces.set(key, next); notify(); return true }
  return false
}
// Drafts and unresolved request keys survive dialog closure, but never another login.
useAuthStore.subscribe((current, previous) => {
  if (current.epoch !== previous.epoch || current.user?.id !== previous.user?.id || !current.token) { workspaces.clear(); notify() }
})
