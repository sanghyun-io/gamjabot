import { describe, it, expect } from 'vitest'
import { validSkill } from './skills'
const id = (n: number) => `af041000-0000-4000-8000-${String(n).padStart(12,'0')}`
const skill = {version:1,skill_id:id(1),revision:1,name:'요약',description:'',content:'요약합니다.',active:false,source:{room_id:1,agent_id:8,root_event_id:id(2),root_seq:1,result_event_id:id(3),result_seq:2},created_at:'2026-10-29T01:00:00Z',updated_at:'2026-10-29T01:00:00Z'}
describe('Skill source validation', () => {
  it('accepts an exact source or a manual Skill and rejects malformed provenance', () => {
    expect(validSkill(skill)).toBe(true)
    expect(validSkill({...skill,source:null})).toBe(true)
    expect(validSkill({...skill,source:{...skill.source,result_seq:1}})).toBe(false)
    expect(validSkill({...skill,source:{...skill.source,root_event_id:'invalid'}})).toBe(false)
    expect(validSkill({...skill,revision:0})).toBe(false)
  })
})
