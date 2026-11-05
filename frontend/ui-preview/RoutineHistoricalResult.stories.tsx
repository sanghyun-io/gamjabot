import type {Meta,StoryObj} from '@storybook/react-vite'
import {useLayoutEffect,useMemo,useState} from 'react'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import {MemoryRouter} from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import {I18nProvider} from '../src/i18n'
import RoutinePersistenceEvent from '../src/components/RoutinePersistenceEvent'
import {routinesApi,type Routine,type RoutineRun} from '../src/api/routines'
import {eventsApi,type RoomEvent} from '../src/api/events'
import {useAuthStore} from '../src/stores/authStore'
const id=(n:number)=>`b1000000-0000-4000-8000-${String(n).padStart(12,'0')}`,time='2026-10-30T00:00:00Z'
const routine:Routine={version:1,routine_id:id(4),revision:1,agent_id:8,room_id:501,name:'합성 일일 요약',instruction:'오늘을 요약해',enabled:true,schedule:{kind:'interval',interval_minutes:60,timezone:'Asia/Seoul'},next_run_at:null,availability:'available'}
const ev=(n:number,actor:RoomEvent['actor_type'],actorId:number|null,kind:RoomEvent['kind'],payload:Record<string,unknown>):RoomEvent=>({room_id:501,event_id:id(n),seq:n,actor_type:actor,actor_id:actorId,kind,payload,actor_name_snapshot:'Synthetic',device_id:null,created_at:time,idempotency_key:null})
const ref=(e:RoomEvent)=>({room_id:e.room_id,event_id:e.event_id,seq:e.seq,actor_type:e.actor_type,actor_id:e.actor_id,kind:e.kind})
const root=ev(1,'user',1,'message',{}),meta={version:1,receipt_id:'e'.repeat(64),operation:'test',state:'queued',agent_id:8,root_request_event_id:root.event_id,routine_id:routine.routine_id,revision:1,routine}
const activity=ev(2,'system',null,'activity',{category:'routine_persistence',status:'queued',trigger_event_id:root.event_id,routine_persistence:meta})
const start=ev(3,'system',null,'activity',{category:'owner_routine',status:'started',content:routine.instruction,routine:{version:1,run_id:id(5),routine_id:routine.routine_id,config_revision:1,owner_id:1,agent_id:8,origin:'test',scheduled_at:time}})
const reply=ev(4,'agent',8,'message',{trigger_event_id:start.event_id,routine:{version:1,run_id:id(5)},content:'합성 결과'})
const run:RoutineRun={run_id:id(5),state:'completed',origin:'test',scheduled_at:time,started_at:time,completed_at:time,root_ref:ref(start),result_ref:ref(reply),failure_code:null}
function Surface({unavailable=false}:{unavailable?:boolean}){
 const [ready,setReady]=useState(false),cache=useMemo(()=>new QueryClient({defaultOptions:{queries:{retry:false}}}),[])
 useLayoutEffect(()=>{const old=useAuthStore.getState(),prev={read:routinesApi.read,run:routinesApi.run,persistence:routinesApi.persistence,list:eventsApi.list};useAuthStore.setState({token:'synthetic',user:{id:1,email:'synthetic@example.invalid',username:'Preview'},epoch:99});routinesApi.read=async()=>routine;routinesApi.run=async()=>{if(unavailable)throw Error('unavailable');return run};routinesApi.persistence=async()=>({...meta,root_ref:ref(root),result_ref:ref(activity),replayed:true,test:{state:'started',run,failure_code:null}});eventsApi.list=async(_room,after)=>[root,activity,start,reply].filter(e=>e.seq>after).slice(0,1);setReady(true);return()=>{routinesApi.read=prev.read;routinesApi.run=prev.run;routinesApi.persistence=prev.persistence;eventsApi.list=prev.list;useAuthStore.setState({token:old.token,user:old.user,epoch:old.epoch});cache.clear()}},[cache,unavailable])
 return <I18nProvider><QueryClientProvider client={cache}><MemoryRouter><main className="page-frame page-content"><p>합성 데이터 · 이 시험 이후 20회 이상 실행됨</p>{ready&&<RoutinePersistenceEvent event={activity} allowed visible canUse={()=>true} onOpen={()=>{}}/>}</main></MemoryRouter></QueryClientProvider></I18nProvider>
}
const metaStory={title:'Product/Routine historical result',component:Surface} satisfies Meta<typeof Surface>
export default metaStory
type Story=StoryObj<typeof metaStory>
export const Completed:Story={args:{}}
export const Unavailable:Story={args:{unavailable:true}}
