import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from './directoryViewMemory'
import { botMessageRetriesApi, type BotMessage, type BotMessageRetryPlan, type BotMessageRetryReceipt } from '../api/botMessages'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { queryClient } from './queryClient'
export type BotMessageRetryOperation = {
  key: string; plan: Readonly<BotMessageRetryPlan>; body: Readonly<{ expected_updated_at: string; plan_hash: string }>
  phase: 'sending' | 'unknown' | 'rejected' | 'accepted'; receipt?: BotMessageRetryReceipt; code?: string; reason?: string
}
export type BotMessageRetryWorkspace = { readId: string | null; readPhase: 'idle' | 'loading' | 'ready' | 'error'; plan: BotMessageRetryPlan | null; operation: BotMessageRetryOperation | null }
const empty: BotMessageRetryWorkspace = Object.freeze({ readId: null, readPhase: 'idle', plan: null, operation: null })
const workspaces = new Map<string, BotMessageRetryWorkspace>(), listeners = new Set<() => void>()
const notify = () => { for (const listener of listeners) listener() }
export const botMessageRetryKey = (identity: string, generation: number, messageId: string) => `${identity}:${generation}:${messageId}`
export const readBotMessageRetry = (key: string) => workspaces.get(key) || empty
export const subscribeBotMessageRetry = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
function change(key: string, update: (before: BotMessageRetryWorkspace) => BotMessageRetryWorkspace) {
  const auth = useAuthStore.getState()
  if (!auth.token || !key.startsWith(`${auth.user?.id || 0}:${auth.epoch}:${directoryViewGeneration()}:`)) return false
  const before = readBotMessageRetry(key), next = update(before)
  if (next === before) return false
  workspaces.set(key, next); notify(); return true
}
export function readBotMessageRetryPlan(key: string, message: BotMessage) {
  const readId = crypto.randomUUID()
  if (!change(key, before => before.readPhase === 'loading' || before.operation && before.operation.phase !== 'rejected' ? before
    : { ...before, readId, readPhase: 'loading', plan: null, operation: null })) return
  void (async () => {
    try {
      const plan = await botMessageRetriesApi.preview(message)
      change(key, before => before.readId !== readId ? before : { ...before, readPhase: 'ready', plan })
    } catch { change(key, before => before.readId !== readId ? before : { ...before, readPhase: 'error', plan: null }) }
  })()
}
async function transmit(key: string, request: BotMessageRetryOperation) {
  try {
    const receipt = await botMessageRetriesApi.start(request.plan, request.body, request.key)
    const accepted = change(key, before => before.operation?.key !== request.key || before.operation.phase !== 'sending' ? before : { ...before, operation: { ...request, phase: 'accepted', receipt } })
    if (accepted) for (const queryKey of [['bot-messages'], ['dashboard'], ['members']]) void queryClient.invalidateQueries({ queryKey })
  } catch (error) {
    const detail = (error as { response?: { data?: { detail?: { code?: unknown; reason?: unknown } } } })?.response?.data?.detail
    change(key, before => before.operation?.key !== request.key || before.operation.phase !== 'sending' ? before : { ...before, operation: { ...request,
      phase: editorSaveFailure(error) === 'rejected' ? 'rejected' : 'unknown', code: typeof detail?.code === 'string' ? detail.code : undefined,
      reason: typeof detail?.reason === 'string' ? detail.reason : undefined } })
  }
}
export function startBotMessageRetry(key: string, readId: string | null) {
  let request: BotMessageRetryOperation | undefined
  const admitted = change(key, before => {
    if (before.operation || before.readId !== readId || before.readPhase !== 'ready' || !before.plan?.eligible || !before.plan.plan_hash) return before
    const plan = Object.freeze({ ...before.plan, root_ref: Object.freeze({ ...before.plan.root_ref }) })
    request = { key: crypto.randomUUID(), plan, body: Object.freeze({ expected_updated_at: plan.expected_updated_at, plan_hash: plan.plan_hash! }), phase: 'sending' }
    return { ...before, operation: request }
  })
  if (admitted && request) void transmit(key, request)
}
export function resendBotMessageRetry(key: string) {
  let request: BotMessageRetryOperation | undefined
  const admitted = change(key, before => {
    if (before.operation?.phase !== 'unknown') return before
    request = { ...before.operation, phase: 'sending' }; return { ...before, operation: request }
  })
  if (admitted && request) void transmit(key, request)
}
// Retain a frozen uncertain command across dialog and room changes, never a login change.
useAuthStore.subscribe((current, previous) => {
  if (current.epoch !== previous.epoch || current.user?.id !== previous.user?.id || !current.token) { workspaces.clear(); notify() }
})
