import type { RoomEvent } from '../api/events'

export interface TimelineGroup { key: string; auxiliary: boolean; events: RoomEvent[] }
export function auxiliaryEvent(event: RoomEvent, finalizers: ReadonlyMap<string, number>) {
  return finalizers.has(event.event_id) || event.actor_type === 'system'
    || (event.kind !== 'message' && event.kind !== 'summary')
}
function activityKey(event: RoomEvent): string {
  const value = event.payload
  const agent = typeof value.agent_id === 'number' ? value.agent_id : event.actor_type === 'agent' ? event.actor_id : null
  // Only a recorded identity connects activities. Never infer it from wording or timestamps.
  if (typeof value.trigger_event_id === 'string' && agent !== null) return `${event.room_id}:request:${value.trigger_event_id}:agent:${agent}`
  if (typeof value.action_id === 'string') return `${event.room_id}:action:${value.action_id}`
  return event.event_id
}
export function conversationTimeline(events: RoomEvent[], finalizers: ReadonlyMap<string, number>): TimelineGroup[] {
  const groups: TimelineGroup[] = []
  for (const event of events) {
    const auxiliary = auxiliaryEvent(event, finalizers)
    const key = auxiliary ? activityKey(event) : event.event_id
    const last = groups.at(-1)
    // Authored messages always keep their original position and stable event key.
    if (auxiliary && last?.auxiliary && last.key === key && last.events.at(-1)!.seq + 1 === event.seq) last.events.push(event)
    else groups.push({ key, auxiliary, events: [event] })
  }
  return groups
}
export function activityOutcome(events: RoomEvent[]): RoomEvent {
  // An earlier unresolved/failing action must not disappear behind a later success.
  return events.findLast((event) => event.kind === 'failure' || ['failed', 'denied'].includes(String(event.payload.state)))
    || events.findLast((event) => ['uncertain', 'cancelled', 'partial'].includes(String(event.payload.state)))
    || events.at(-1)!
}
