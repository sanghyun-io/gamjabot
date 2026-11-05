import { expect, it } from 'vitest'
import { resultSource } from './resultSelection'
import type { RoomEvent } from '../api/events'
it('accepts only the exact same-room generated reference from a tool result',()=>{
 const id='11111111-1111-4111-8111-111111111111',ref={version:1,ref:`generated-files/5/${id}.utf8`,filename:'report.csv',size_bytes:3,sha256:'a'.repeat(64),media_type:'text/plain; charset=utf-8',capture_kind:'approved_write_payload'}
 const event={event_id:'22222222-2222-4222-8222-222222222222',room_id:5,actor_type:'tool',actor_id:null,kind:'tool_result',payload:{state:'completed',action_id:id,generated_file:ref}} as unknown as RoomEvent
 expect(resultSource(event,5)).toBe('file')
 expect(resultSource({...event,payload:{...event.payload,generated_file:{...ref,size_bytes:0}}},5)).toBe('file')
 expect(resultSource({...event,payload:{...event.payload,generated_file:{...ref,ref:'/tmp/report.csv'}}},5)).toBeNull()
 expect(resultSource({...event,actor_id:8},5)).toBeNull()
 expect(resultSource({...event,payload:{...event.payload,generated_file:{...ref,extra:true}}},5)).toBeNull()
})
