import { describe, it, expect } from 'vitest'
import type { RoomEvent } from '../api/events'
import { memoryChangeMetadata } from './MemoryChangeEvent'
import { validMemoryPolicy, validCommonList } from '../api/memorySharing'
const id = 'af039000-0000-4000-8000-000000000001'
const m = { version:1,receipt_id:'a'.repeat(64),scope:'owner_common',operation:'edit',state:'saved',agent_id:8,root_request_event_id:id,content:'Asia/Seoul',memory_id:id,revision:2,entry_revision:2,effective:'next_request',history_retained:true,kind:'timezone',origin:{agent_id:8,source_kind:'event',room_id:501,event_id:id,seq:1} }
const event: RoomEvent = { device_id:null,actor_name_snapshot:'시스템',created_at:'2026-10-29T00:00:00Z',idempotency_key:null,event_id:'af039000-0000-4000-8000-000000000002',room_id:501,seq:2,actor_type:'system',actor_id:null,kind:'activity',payload:{category:'owner_common_memory',status:'saved',trigger_event_id:id,bot_persistence:m} }
describe('shared memory evidence', () => {
  it('recognizes only a system activity tied to the Owner request', () => {
    expect(memoryChangeMetadata(event)).not.toBeNull()
    expect(memoryChangeMetadata({...event,actor_type:'agent',actor_id:8})).toBeNull()
    expect(memoryChangeMetadata({...event,payload:{...event.payload,trigger_event_id:'wrong'}})).toBeNull()
  })
  it('checks Bot identity and keeps direct and event origins distinct', () => {
    expect(validMemoryPolicy({version:1,agent_id:8,revision:0,mode:'canonical_only'},8)).toBe(true)
    expect(validMemoryPolicy({version:1,agent_id:9,revision:0,mode:'owner_rooms'},8)).toBe(false)
    const common = {version:1,scope:'owner_common',revision:1,items:[{fact_id:id,kind:'name',content:'이름',revision:1,origin:{agent_id:8,source_kind:'manual',room_id:null,event_id:null,seq:null},visibility:'owner_bots',created_at:'2026-10-29T01:23:45Z',updated_at:'2026-10-29T01:23:45Z'}]}
    expect(validCommonList(common)).toBe(true)
    expect(validCommonList({...common,items:[{...common.items[0],origin:{...common.items[0].origin,source_kind:'event'}}]})).toBe(false)
  })
})
