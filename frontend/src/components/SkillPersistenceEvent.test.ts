import { describe, expect, it } from 'vitest'
import type { RoomEvent } from '../api/events'
import type { Skill } from '../api/skills'
import { readyBindingState, matchingSkillReceipt, matchingSkillVersion, skillPersistenceMetadata } from './SkillPersistenceEvent'
const id = (n: number) => `af042000-0000-4000-8000-${String(n).padStart(12,'0')}`
const source = {room_id:501,root_event_id:id(1),root_seq:1,result_event_id:id(2),result_seq:2,agent_id:8}
const meta = {version:1 as const,receipt_id:'c'.repeat(64),operation:'create_draft' as const,state:'saved' as const,agent_id:8,root_request_event_id:id(3),skill_id:id(6),revision:1,name:'회의 정리',description:'요약',content:'결정 사항 정리',active:false,source,creator_binding:null}
const event: RoomEvent = {event_id:id(4),device_id:null,room_id:501,seq:4,actor_type:'system',actor_id:null,actor_name_snapshot:'시스템',kind:'activity',payload:{category:'skill_persistence',status:'saved',trigger_event_id:id(3),skill_persistence:meta},created_at:'2026-10-29T01:00:00Z',idempotency_key:null}
const receipt = {...meta,root_ref:{room_id:501,event_id:id(3),seq:3,actor_type:'user',actor_id:1,kind:'message'},result_ref:{room_id:501,event_id:id(4),seq:4,actor_type:'system',actor_id:null,kind:'activity'},replayed:true}
const version: Skill = {version:1,skill_id:id(6),revision:1,name:meta.name,description:meta.description,content:meta.content,active:false,source,created_at:'2026-10-29T01:00:00Z',updated_at:'2026-10-29T01:00:00Z'}
describe('Skill persistence evidence', () => {
  it('requires exact Owner, event, receipt and snapshot', () => {
    expect(matchingSkillReceipt(event,1,receipt)).toBe(true)
    expect(matchingSkillReceipt(event,2,receipt)).toBe(false)
    expect(matchingSkillReceipt(event,1,{...receipt,result_ref:{...receipt.result_ref,event_id:id(9)}})).toBe(false)
    expect(matchingSkillReceipt(event,1,{...receipt,source:{...source,result_event_id:id(9)}})).toBe(false)
    expect(matchingSkillReceipt(event,1,{...receipt,content:'바뀐 내용'})).toBe(false)
    expect(matchingSkillVersion(meta,version)).toBe(true)
    expect(matchingSkillVersion(meta,{...version,revision:2})).toBe(false)
    expect(matchingSkillVersion(meta,{...version,active:true})).toBe(false)
    expect(skillPersistenceMetadata({...event,actor_type:'agent',actor_id:8})).toBeNull()
    expect(skillPersistenceMetadata({...event,payload:{...event.payload,skill_persistence:{...meta,active:true}}})).toBeNull()
    const ready = {...meta,operation:'create_ready',active:true,creator_binding:{agent_id:8,skill_id:meta.skill_id,revision:1,enabled:true}}
    const readyEvent = {...event,payload:{...event.payload,skill_persistence:ready}}
    expect(matchingSkillReceipt(readyEvent,1,{...receipt,...ready})).toBe(true)
    const readyMeta = skillPersistenceMetadata(readyEvent)!
    const bound = {version:1 as const,agent_id:8,items:[{skill_id:meta.skill_id,revision:1,enabled:true}]}
    expect(readyBindingState(readyMeta,bound)).toBe('same')
    expect(readyBindingState(readyMeta,{...bound,agent_id:9})).toBe('unknown')
    expect(readyBindingState(readyMeta,{...bound,items:[{...bound.items[0],revision:2,enabled:false}]})).toBe('changed')
    expect(readyBindingState(readyMeta,{...bound,items:[{...bound.items[0],revision:0}]})).toBe('unknown')
    expect(readyBindingState(readyMeta,{...bound,items:[{...bound.items[0],skill_id:id(7),revision:2}]})).toBe('unknown')
    expect(readyBindingState(readyMeta,{...bound,items:[{...bound.items[0],enabled:false}]})).toBe('unknown')
    expect(matchingSkillReceipt(readyEvent,1,{...receipt,...ready,creator_binding:{...ready.creator_binding,agent_id:9}})).toBe(false)
    expect(skillPersistenceMetadata({...readyEvent,payload:{...readyEvent.payload,skill_persistence:{...ready,creator_binding:null}}})).toBeNull()
  })
})
