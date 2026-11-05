// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import RoutinePersistenceEvent from './RoutinePersistenceEvent'
import { routinesApi, validRun, type Routine, type RoutineRun } from '../api/routines'
import { eventsApi, type RoomEvent } from '../api/events'
import { useAuthStore } from '../stores/authStore'
import { I18nProvider } from '../i18n'
const uid=(n:number)=>`b1000000-0000-4000-8000-${String(n).padStart(12,'0')}`, time='2026-10-30T00:00:00Z'
const routine:Routine={version:1,routine_id:uid(4),revision:1,agent_id:8,room_id:501,name:'Check',instruction:'Check today',enabled:true,schedule:{kind:'interval',interval_minutes:60,timezone:'Asia/Seoul'},next_run_at:null,availability:'available'}
const ev=(n:number,actor:RoomEvent['actor_type'],actorId:number|null,kind:RoomEvent['kind'],payload:Record<string,unknown>):RoomEvent=>({room_id:501,event_id:uid(n),seq:n,actor_type:actor,actor_id:actorId,kind,payload,actor_name_snapshot:'Synthetic',device_id:null,created_at:time,idempotency_key:null})
const ref=(e:RoomEvent)=>({room_id:e.room_id,event_id:e.event_id,seq:e.seq,actor_type:e.actor_type,actor_id:e.actor_id,kind:e.kind})
const root=ev(1,'user',1,'message',{}),meta={version:1,receipt_id:'e'.repeat(64),operation:'test',state:'queued',agent_id:8,root_request_event_id:root.event_id,routine_id:routine.routine_id,revision:1,routine}
const activity=ev(2,'system',null,'activity',{category:'routine_persistence',status:'queued',trigger_event_id:root.event_id,routine_persistence:meta})
const start=ev(3,'system',null,'activity',{category:'owner_routine',status:'started',content:routine.instruction,routine:{version:1,run_id:uid(5),routine_id:routine.routine_id,config_revision:1,owner_id:1,agent_id:8,origin:'test',scheduled_at:time}})
const reply=ev(4,'agent',8,'message',{trigger_event_id:start.event_id,routine:{version:1,run_id:uid(5)},content:'done'})
const failure=ev(5,'agent',8,'failure',{trigger_event_id:start.event_id,routine:{version:1,run_id:uid(5)},code:'routine_failed',generation:3,round:1})
const run=(state:RoutineRun['state'],result_ref:RoutineRun['result_ref']=null):RoutineRun=>({run_id:uid(5),state,origin:'test',scheduled_at:time,started_at:time,completed_at:state==='running'?null:time,root_ref:ref(start),result_ref,failure_code:state==='failed'?'unsupported_option':null})
const receipt=(r:RoutineRun)=>({...meta,root_ref:ref(root),result_ref:ref(activity),replayed:true,test:{state:'started',run:r,failure_code:null}})
beforeEach(()=>{localStorage.removeItem('gamja.locale');useAuthStore.getState().setAuth('synthetic',{id:1,email:'owner@example.invalid',username:'Owner'});vi.spyOn(routinesApi,'read').mockResolvedValue(routine);vi.spyOn(eventsApi,'list').mockImplementation(async(_room,after)=>[root,activity,start,reply,failure].filter(e=>e.seq>after).slice(0,1))})
afterEach(()=>{cleanup();vi.restoreAllMocks()})
function show(){render(<I18nProvider systemLanguages={()=>['en']}><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><MemoryRouter><RoutinePersistenceEvent event={activity} allowed visible canUse={()=>true} onOpen={vi.fn()}/></MemoryRouter></QueryClientProvider></I18nProvider>)}
it('shows a ledger-confirmed failure even when the historical run has no failure event',async()=>{
 const r=run('failed');vi.spyOn(routinesApi,'persistence').mockResolvedValue(receipt(r));vi.spyOn(routinesApi,'run').mockResolvedValue(r);show();expect(await screen.findByText(/requested option/i)).toBeTruthy();expect(screen.queryByText(/The test started/)).toBeNull()
})
it('confirms result event and rejects a mismatched failure reference',async()=>{
 const r=run('completed',ref(reply));vi.spyOn(routinesApi,'persistence').mockResolvedValue(receipt(r));vi.spyOn(routinesApi,'run').mockResolvedValue(r);show();expect(await screen.findByText('Completed')).toBeTruthy();expect(validRun(run('failed',ref(failure)),501,8)).toBe(true);expect(validRun(run('completed',ref(failure)),501,8)).toBe(false)
})

it('refreshes a visible queued receipt into the actual failed run',async()=>{
 Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true})
 const r=run('failed'),read=vi.spyOn(routinesApi,'persistence').mockResolvedValueOnce({...meta,root_ref:ref(root),result_ref:ref(activity),replayed:true,test:{state:'pending',run:null,failure_code:null}}).mockResolvedValue(receipt(r))
 vi.spyOn(routinesApi,'run').mockResolvedValue(r);show();expect(await screen.findByText(/can start after this conversation/)).toBeTruthy();expect(await screen.findByText(/requested option/i,{}, {timeout:7000})).toBeTruthy();expect(read).toHaveBeenCalledTimes(2)
},8000)
it('does not read when the conversation is hidden',()=>{
 const read=vi.spyOn(routinesApi,'persistence');render(<I18nProvider systemLanguages={()=>['en']}><QueryClientProvider client={new QueryClient()}><MemoryRouter><RoutinePersistenceEvent event={activity} allowed visible={false} canUse={()=>false} onOpen={vi.fn()}/></MemoryRouter></QueryClientProvider></I18nProvider>);expect(read).not.toHaveBeenCalled()
})
it('requires a failure event to match the exact run before showing it',async()=>{
 const r=run('failed',ref(failure));vi.spyOn(routinesApi,'persistence').mockResolvedValue(receipt(r));vi.spyOn(routinesApi,'run').mockResolvedValue(r);vi.spyOn(eventsApi,'list').mockRestore()
 vi.spyOn(eventsApi,'list').mockImplementation(async(_room,after)=>[root,activity,start,reply,{...failure,payload:{...failure.payload,routine:{version:1,run_id:uid(99)}}}].filter(e=>e.seq>after).slice(0,1))
 show();expect(await screen.findByText('Could not confirm the result.')).toBeTruthy();expect(screen.queryByText(/requested option/i)).toBeNull()
})
it('opens the exact historical test result after newer history has displaced it',async()=>{
 const r=run('completed',ref(reply));vi.spyOn(routinesApi,'persistence').mockResolvedValue(receipt(r));const exact=vi.spyOn(routinesApi,'run').mockResolvedValue(r)
 const recent=vi.spyOn(routinesApi,'runs').mockResolvedValue(Array.from({length:20},(_,i)=>({...r,run_id:uid(i+10)})))
 show();expect(await screen.findByText('Completed')).toBeTruthy();expect(exact).toHaveBeenCalledWith(routine,r.run_id);expect(recent).not.toHaveBeenCalled();expect(screen.getByRole('link',{name:'View conversation'}).getAttribute('href')).toContain(reply.event_id)
})
