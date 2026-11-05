import type {Meta,StoryObj} from '@storybook/react-vite'
import {useLayoutEffect,useMemo,useState} from 'react'
import userEvent from '@testing-library/user-event'
import {within} from '@testing-library/react'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import {MemoryRouter} from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import {I18nProvider} from '../src/i18n'
import RoutineAgenda from '../src/components/RoutineAgenda'
import BotRoutines from '../src/components/BotRoutines'
import {useAuthStore} from '../src/stores/authStore'
import {routinesApi,type Routine} from '../src/api/routines'
type Scenario='ready'|'partial'|'many'|'empty'|'directory-failure'
const agents=(count:number)=>Array.from({length:count},(_,i)=>({id:i+1,name:`합성 Bot ${i+1}`,is_active:true,user_id:1}))
const row=(id:number):Routine=>({version:1,routine_id:`00000000-0000-4000-8000-${String(id).padStart(12,'0')}`,revision:1,agent_id:id,room_id:501,name:`합성 루틴 ${id}`,instruction:'합성 일정',enabled:id%3!==0,schedule:{kind:'weekly',weekdays:[0],hour:9,minute:0,timezone:id%2?'Asia/Seoul':'America/New_York'},next_run_at:id%3?'2026-11-05T00:00:00Z':null,availability:'available'})
function Surface({scenario}:{scenario:Scenario}){
 const [ready,setReady]=useState(false),[directoryReady,setDirectoryReady]=useState(false),cache=useMemo(()=>new QueryClient({defaultOptions:{queries:{retry:false}}}),[]),bots=useMemo(()=>agents(scenario==='many'?25:scenario==='empty'?0:3),[scenario])
 useLayoutEffect(()=>{const old=useAuthStore.getState(),previous=routinesApi.list;useAuthStore.setState({token:'synthetic',user:{id:1,email:'synthetic@example.invalid',username:'Preview'},epoch:99});routinesApi.list=async id=>{if(scenario==='partial'&&id===2)throw Error('synthetic unavailable');return [row(id)]};setReady(true);return()=>{routinesApi.list=previous;useAuthStore.setState({token:old.token,user:old.user,epoch:old.epoch});cache.clear()}},[scenario,cache])
 return <I18nProvider><QueryClientProvider client={cache}><MemoryRouter><main className="page-frame page-content"><p>합성 일정 · 실제 Bot과 외부 실행 없음</p>{ready&&<><RoutineAgenda agents={bots} listStale={false} managementReady={scenario!=='directory-failure'||directoryReady} onRetryManagement={()=>setDirectoryReady(true)} onManage={id=>{const el=document.getElementById(`synthetic-bot-${id}`);const details=el?.querySelector<HTMLDetailsElement>('details.bot-routines');if(details)details.open=true;details?.scrollIntoView({block:'nearest'})}}/>{bots.map(b=><section id={`synthetic-bot-${b.id}`} key={b.id}><h2>{b.name}</h2><BotRoutines agentId={b.id}/></section>)}</>}</main></MemoryRouter></QueryClientProvider></I18nProvider>
}
const meta={title:'Product/Bot routine agenda',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
const open=async({canvasElement}:{canvasElement:HTMLElement})=>{const c=within(canvasElement);await userEvent.click(await c.findByRole('button',{name:/일정판 열기|Open agenda/}))}
export const Ready:Story={args:{scenario:'ready'},play:open}
export const PartialFailure:Story={args:{scenario:'partial'},play:open}
export const Many:Story={args:{scenario:'many'},play:open}
export const Empty:Story={args:{scenario:'empty'},play:open}
export const DirectoryFailure:Story={args:{scenario:'directory-failure'},play:open}
