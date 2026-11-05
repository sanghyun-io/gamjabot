import { describe, expect, it } from 'vitest'
import { validSkill } from './skills'

describe('demonstration skill provenance', () => {
  const base = { version: 1, skill_id: 'af041000-0000-4000-8000-000000000801', revision: 1, name: 'Example', description: '', content: 'Review', active: false, created_at: '2026-10-31T00:00:00Z', updated_at: '2026-10-31T00:00:00Z' }
  const source = { kind: 'browser_demonstration', demo_id: 'a'.repeat(32), room_id: 1, agent_id: 2, profile_id: 'b'.repeat(32), profile_revision: 1, trust: 'untrusted_hints' }
  it('accepts a bound demonstration source without weakening legacy source checks', () => {
    expect(validSkill({ ...base, source })).toBe(true)
    expect(validSkill({ ...base, source: { ...source, trust: 'verified' } })).toBe(false)
    expect(validSkill({ ...base, source: { room_id: 1, agent_id: 2 } })).toBe(false)
    expect(validSkill({ ...base, source: { room_id: 1, agent_id: 2, root_event_id: base.skill_id, result_event_id: base.skill_id, root_seq: 1, result_seq: 2 } })).toBe(true)
  })
})
