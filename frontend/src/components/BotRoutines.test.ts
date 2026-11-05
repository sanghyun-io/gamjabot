import { describe, expect, it } from 'vitest'
import { validSchedule, validCommand, type Routine } from '../api/routines'
describe('routine schedule and command readback', () => {
 const schedule={kind:'weekly' as const,weekdays:[0,1,2,3,4],hour:8,minute:0,timezone:'Asia/Seoul'}
 const routine:Routine={version:1,routine_id:'b0000000-0000-4000-8000-000000000001',revision:1,agent_id:8,room_id:501,name:'계획',instruction:'확인',enabled:true,schedule,next_run_at:null,availability:'available'}
 it('validates schedules',()=>{expect(validSchedule(schedule)).toBe(true);expect(validSchedule({kind:'interval',interval_minutes:5,timezone:'Asia/Seoul'})).toBe(true);expect(validSchedule({kind:'interval',interval_minutes:4,timezone:'Asia/Seoul'})).toBe(false);expect(validSchedule({...schedule,weekdays:[1,0]})).toBe(false);expect(validSchedule({...schedule,timezone:'invalid zone'})).toBe(false)})
 it('matches create readback',()=>{const receipt={version:1,operation:'create',agent_id:8,routine,run:null};expect(validCommand(receipt,8,'create',routine)).toBe(true);expect(validCommand(receipt,9,'create',routine)).toBe(false);expect(validCommand({...receipt,routine:{...routine,instruction:'다른 내용'}},8,'create',routine)).toBe(false)})
 it('rejects test receipt from another room or actor',()=>{const ref={room_id:501,event_id:'b0000000-0000-4000-8000-000000000003',seq:1,actor_type:'system',actor_id:null,kind:'activity'};const run={run_id:'b0000000-0000-4000-8000-000000000002',state:'running',scheduled_at:'2026-10-29T01:00:00Z',started_at:null,completed_at:null,root_ref:ref,result_ref:null,failure_code:null,origin:'test'};const receipt={version:1,operation:'test',agent_id:8,routine:null,run};expect(validCommand(receipt,8,'test',routine)).toBe(true);expect(validCommand({...receipt,run:{...run,root_ref:{...ref,room_id:502}}},8,'test',routine)).toBe(false);expect(validCommand({...receipt,run:{...run,root_ref:{...ref,actor_type:'user'}}},8,'test',routine)).toBe(false)})
})

import { matchingRoutineReceipt } from './RoutinePersistenceEvent'
import type { RoomEvent } from '../api/events'
it('confirms a saved receipt only for the exact Owner root, event, and routine snapshot',()=>{
 const routine:Routine={version:1,routine_id:'b0000000-0000-4000-8000-000000000001',revision:1,agent_id:8,room_id:501,name:'계획',instruction:'확인',enabled:true,schedule:{kind:'interval',interval_minutes:60,timezone:'Asia/Seoul'},next_run_at:null,availability:'available'}
 const root='b0000000-0000-4000-8000-000000000002', result='b0000000-0000-4000-8000-000000000003'
 const meta={version:1,receipt_id:'a'.repeat(64),operation:'create',state:'saved',agent_id:8,root_request_event_id:root,routine_id:routine.routine_id,revision:1,routine}
 const event:RoomEvent={event_id:result,device_id:null,room_id:501,seq:2,actor_type:'system',actor_id:null,actor_name_snapshot:'',kind:'activity',payload:{category:'routine_persistence',status:'saved',trigger_event_id:root,routine_persistence:meta},created_at:'2026-10-29T01:00:00Z',idempotency_key:null}
 const receipt={...meta,root_ref:{room_id:501,event_id:root,seq:1,actor_type:'user',actor_id:1,kind:'message'},result_ref:{room_id:501,event_id:result,seq:2,actor_type:'system',actor_id:null,kind:'activity'},replayed:true}
 expect(matchingRoutineReceipt(event,1,receipt)).toBe(true)
 expect(matchingRoutineReceipt(event,2,receipt)).toBe(false)
 expect(matchingRoutineReceipt(event,1,{...receipt,routine:{...routine,instruction:'다름'}})).toBe(false)
 expect(matchingRoutineReceipt(event,1,{...receipt,result_ref:{...receipt.result_ref,event_id:root}})).toBe(false)
})
