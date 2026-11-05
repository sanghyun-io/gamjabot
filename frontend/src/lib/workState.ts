export type WorkPhase = 'idle' | 'queued' | 'thinking' | 'peer_waiting' | 'approval_waiting' |
  'tool_waiting' | 'tool_running' | 'completed' | 'passed' | 'failed' | 'uncertain' |
  'paused' | 'stopped' | 'cancelled' | 'interrupted' | 'unknown'
export type WorkEventRef = { event_id: string; seq: number }
export type WorkState = {
  version: 1; room_id: number; agent_id: number; scope: 'current' | 'previous' | 'none'; phase: WorkPhase
  trigger_event_id?: string | null; generation?: number | null; round?: number | null; updated_at?: string | null
  request?: WorkEventRef | null; event?: WorkEventRef | null; result?: WorkEventRef | null
  action_id?: string | null; peer_agent_ids: number[]
}
const phases = new Set<WorkPhase>(['idle', 'queued', 'thinking', 'peer_waiting', 'approval_waiting',
  'tool_waiting', 'tool_running', 'completed', 'passed', 'failed', 'uncertain', 'paused', 'stopped',
  'cancelled', 'interrupted', 'unknown'])
const uuid = (value: unknown): value is string => typeof value === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const ref = (value: unknown): value is WorkEventRef => record(value) && uuid(value.event_id) && positive(value.seq)

/** Reject explicit malformed data instead of falling back to an old actor's result. */
export function validateWorkState(value: unknown, roomId: number, agentId: number): WorkState | null {
  if (!record(value) || value.version !== 1 || value.room_id !== roomId || value.agent_id !== agentId ||
    !positive(value.room_id) || !positive(value.agent_id) || typeof value.phase !== 'string' ||
    !phases.has(value.phase as WorkPhase) || typeof value.scope !== 'string' || !['current', 'previous', 'none'].includes(value.scope)) return null
  for (const key of ['trigger_event_id', 'action_id']) if (value[key] != null && !uuid(value[key])) return null
  for (const key of ['generation', 'round']) if (value[key] != null && !positive(value[key])) return null
  for (const key of ['request', 'event', 'result']) if (value[key] != null && !ref(value[key])) return null
  if (value.updated_at != null && (typeof value.updated_at !== 'string' || !Number.isFinite(Date.parse(value.updated_at)))) return null
  const peers = value.peer_agent_ids
  if (!Array.isArray(peers) || peers.length > 64 || !peers.every(positive) || peers.includes(agentId) || new Set(peers).size !== peers.length) return null
  if (value.scope !== 'none' && (!uuid(value.trigger_event_id) || !ref(value.request) || value.request.event_id !== value.trigger_event_id)) return null
  if (['thinking', 'peer_waiting', 'approval_waiting', 'tool_waiting', 'tool_running'].includes(value.phase) &&
    (value.scope !== 'current' || !positive(value.generation) || !positive(value.round))) return null
  if (['completed', 'passed', 'failed'].includes(value.phase) && (!ref(value.result) || !positive(value.generation) || !positive(value.round))) return null
  if (value.phase === 'peer_waiting' && (!ref(value.result) || !peers.length)) return null
  if (['approval_waiting', 'tool_running'].includes(value.phase) && !uuid(value.action_id)) return null
  if (value.phase === 'uncertain' && !ref(value.result) && !uuid(value.action_id)) return null
  return {
    version: 1, room_id: roomId, agent_id: agentId, scope: value.scope as WorkState['scope'], phase: value.phase as WorkPhase,
    trigger_event_id: value.trigger_event_id as string | null | undefined,
    generation: value.generation as number | null | undefined, round: value.round as number | null | undefined,
    updated_at: value.updated_at as string | null | undefined, request: value.request as WorkEventRef | null | undefined,
    event: value.event as WorkEventRef | null | undefined, result: value.result as WorkEventRef | null | undefined,
    action_id: value.action_id as string | null | undefined, peer_agent_ids: [...peers],
  }
}
