import { describe, expect, it } from 'vitest'
import { checkinDraftCurrent, validCheckin, validCheckinRuns } from './primaryCheckins'
const base = { version: 1, revision: 1, primary_revision: 3, enabled: true, prompt: 'Hello', interval_minutes: 60, next_run_at: '2026-10-29T09:00:00Z', last_run_id: null, last_state: null, primary_agent_id: 8, availability: 'available' }
const id = 'af038000-0000-4000-8000-000000000001'
describe('primary check-in response validation', () => {
  it('requires the primary selection revision and supported interval', () => {
    expect(validCheckin(base)).toBe(true)
    expect(validCheckin({ ...base, primary_revision: undefined })).toBe(false)
    expect(validCheckin({ ...base, interval_minutes: 10081 })).toBe(false)
  })
  it('blocks an old draft on setting or primary changes but allows time-only refresh', () => {
    const draft = { revision: 1, primary_revision: 3, agent_id: 8 }, primary = { revision: 3, agent_id: 8 }
    expect(checkinDraftCurrent(draft, { ...base, version: 1, availability: 'available', next_run_at: '2026-10-29T10:00:00Z' } as const, primary)).toBe(true)
    expect(checkinDraftCurrent(draft, { ...base, version: 1, availability: 'available', revision: 2 } as const, primary)).toBe(false)
    expect(checkinDraftCurrent(draft, { ...base, version: 1, availability: 'available' } as const, { revision: 4, agent_id: 9 })).toBe(false)
  })
  it('requires a real system source and matching Bot result before linking to an event', () => {
    const root_ref = { room_id: 501, event_id: id, seq: 1, actor_type: 'system', actor_id: null, kind: 'activity' }
    const result_ref = { room_id: 501, event_id: id, seq: 2, actor_type: 'agent', actor_id: 8, kind: 'message' }
    const row = { run_id: id, state: 'completed', scheduled_at: '2026-10-29T08:00:00Z', started_at: null, completed_at: null, root_ref, result_ref, failure_code: null, primary_agent_id: 8, room_id: 501, connection_id: 1, model_id: 'model' }
    expect(validCheckinRuns({ version: 1, runs: [row] })).toBe(true)
    expect(validCheckinRuns({ version: 1, runs: [{ ...row, root_ref: { ...root_ref, actor_type: 'user', actor_id: 1 } }] })).toBe(false)
    expect(validCheckinRuns({ version: 1, runs: [{ ...row, result_ref: { ...result_ref, actor_id: 9 } }] })).toBe(false)
  })
})
