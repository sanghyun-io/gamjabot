import type { RoomEvent } from '../api/events'

export interface ModelUseSnapshot {
  connectionId: number
  modelId: string
  providerKind: string
  watermarkSeq: number
}

export interface ComparableResponse {
  eventId: string
  seq: number
  actorId: number
  actorName: string
  content: string
  createdAt: string
  modelUse: ModelUseSnapshot
}

export interface ResponseRound {
  round: number
  responses: ComparableResponse[]
}

export interface ResponseComparison {
  triggerEventId: string
  requestSeq: number
  requestContent: string
  requestCreatedAt: string
  rounds: ResponseRound[]
}

function modelUse(value: unknown): ModelUseSnapshot | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const row = value as Record<string, unknown>
  if (!Number.isSafeInteger(row.connection_id) || (row.connection_id as number) <= 0
    || typeof row.model_id !== 'string' || !row.model_id
    || typeof row.provider_kind !== 'string' || !row.provider_kind
    || !Number.isSafeInteger(row.watermark_seq) || (row.watermark_seq as number) < 0) return null
  return { connectionId: row.connection_id as number, modelId: row.model_id,
    providerKind: row.provider_kind, watermarkSeq: row.watermark_seq as number }
}

export function buildResponseComparisons(events: RoomEvent[]): ResponseComparison[] {
  const requests = new Map<string, RoomEvent>()
  for (const event of events) {
    if (event.actor_type === 'user' && event.kind === 'message' && typeof event.payload.content === 'string')
      requests.set(event.event_id, event)
  }

  const grouped = new Map<string, Map<number, ComparableResponse[]>>()
  for (const event of events) {
    if (event.actor_type !== 'agent' || event.kind !== 'message' || typeof event.payload.content !== 'string') continue
    const triggerId = event.payload.trigger_event_id
    const round = event.payload.round
    const snapshot = modelUse(event.payload.model_use)
    const request = typeof triggerId === 'string' ? requests.get(triggerId) : undefined
    if (!request || request.room_id !== event.room_id || !Number.isSafeInteger(round) || (round as number) < 1
      || !Number.isSafeInteger(event.actor_id) || (event.actor_id as number) <= 0 || !snapshot) continue
    let rounds = grouped.get(triggerId as string)
    if (!rounds) { rounds = new Map(); grouped.set(triggerId as string, rounds) }
    let responses = rounds.get(round as number)
    if (!responses) { responses = []; rounds.set(round as number, responses) }
    responses.push({ eventId: event.event_id, seq: event.seq, actorId: event.actor_id as number,
      actorName: event.actor_name_snapshot, content: event.payload.content, createdAt: event.created_at, modelUse: snapshot })
  }

  return Array.from(grouped, ([triggerEventId, rounds]) => {
    const request = requests.get(triggerEventId)!
    return { triggerEventId, requestSeq: request.seq, requestContent: request.payload.content as string,
      requestCreatedAt: request.created_at,
      rounds: Array.from(rounds, ([round, responses]) => ({ round, responses: responses.sort((a, b) => a.seq - b.seq) }))
        .sort((a, b) => a.round - b.round) }
  }).sort((a, b) => b.requestSeq - a.requestSeq)
}
