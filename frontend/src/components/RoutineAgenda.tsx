import {useEffect,useMemo,useRef,useState} from 'react'
import {useQueryClient} from '@tanstack/react-query'
import type {Agent} from '../api/agents'
import {routinesApi,type Routine} from '../api/routines'
import {useBotScope} from '../lib/useBotScope'
import {useI18n} from '../i18n'

type Bot=Pick<Agent,'id'|'name'|'is_active'|'user_id'>
type Result={bot:Bot;items:Routine[]|null}
type Props={agents:Bot[];identity:string;ownerId:number;canUse:()=>boolean;load:(agent:number)=>Promise<Routine[]>;manage:(agent:number)=>void;ko:boolean;listStale?:boolean;managementReady?:boolean;managementLoading?:boolean;onRetryManagement?:()=>void;subscribe?:(handler:(agent:number,items:Routine[]|null)=>void)=>()=>void}
const PAGE_SIZE=20
export function RoutineAgendaView({agents,identity,ownerId,canUse,load,manage,ko,listStale=false,managementReady=true,managementLoading=false,onRetryManagement,subscribe}:Props){
 const [viewIdentity]=useState(identity)
 const [open,setOpen]=useState(false),[rows,setRows]=useState<Result[]>([]),[loading,setLoading]=useState(false),[done,setDone]=useState(0),[filter,setFilter]=useState<'all'|'scheduled'|'paused'|'webhook'>('all'),[page,setPage]=useState(0)
 const overrides=useRef(new Map<number,Routine[]|null>())
 const serial=useRef(0),alive=useRef(false),latest=useRef({identity,canUse});latest.current={identity,canUse}
 const signature=agents.map(a=>`${a.id}:${a.user_id}:${a.is_active}:${a.name}`).join('|')
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;serial.current++}},[])
 const read=async()=>{
  if(!latest.current.canUse())return
  const token=++serial.current,who=identity,source=agents.filter(a=>a.is_active&&a.user_id===ownerId),result:Result[]=new Array(source.length);let index=0,count=0
  const current=()=>alive.current&&serial.current===token&&latest.current.identity===who&&latest.current.canUse()
  overrides.current.clear();setLoading(true);setDone(0);setRows([]);setPage(0)
  async function worker(){while(current()){const i=index++;if(i>=source.length)return;let items:Routine[]|null=null;try{items=await load(source[i].id)}catch{/* one Bot may be unavailable */}if(!current())return;result[i]={bot:source[i],items};setDone(++count)}}
  await Promise.all(Array.from({length:Math.min(3,source.length)},()=>worker()))
  if(current()){setRows(result.map(row=>overrides.current.has(row.bot.id)?{...row,items:overrides.current.get(row.bot.id)!}:row));setLoading(false)}
 }
 useEffect(()=>{if(open)void read();else {serial.current++;setLoading(false)}},[open,identity,signature])
 const flattened=useMemo(()=>rows.flatMap(({bot,items})=>(items||[]).map(r=>({bot,r}))).sort((a,b)=>{const x=a.r.enabled&&a.r.next_run_at?Date.parse(a.r.next_run_at):Infinity,y=b.r.enabled&&b.r.next_run_at?Date.parse(b.r.next_run_at):Infinity;return x-y||a.bot.name.localeCompare(b.bot.name)||a.r.name.localeCompare(b.r.name)||a.r.routine_id.localeCompare(b.r.routine_id)}),[rows])
 const shown=flattened.filter(({r})=>filter==='all'||filter==='paused'&&!r.enabled||filter==='webhook'&&r.enabled&&r.schedule.kind==='webhook'||filter==='scheduled'&&r.enabled&&r.schedule.kind!=='webhook')
 const pages=Math.max(1,Math.ceil(shown.length/PAGE_SIZE)),safePage=Math.min(page,pages-1),failed=rows.filter(r=>r.items===null)
 useEffect(()=>subscribe?.((id,items)=>{if(alive.current&&latest.current.identity===viewIdentity&&latest.current.canUse()){overrides.current.set(id,items);setRows(old=>old.map(row=>row.bot.id===id?{...row,items}:row))}}),[subscribe,viewIdentity])
 const manageBot=(id:number)=>{if(latest.current.identity===viewIdentity&&latest.current.canUse())manage(id)}
 const time=(r:Routine)=>{if(!r.enabled)return ko?'일시정지':'Paused';if(r.schedule.kind==='webhook')return ko?'이벤트 대기':'Waiting for event';if(!r.next_run_at)return ko?'다음 실행 시간 확인 필요':'Next run time unavailable';try{return new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short',timeZone:r.schedule.timezone}).format(new Date(r.next_run_at))+' · '+r.schedule.timezone}catch{return ko?'다음 실행 시간 확인 필요':'Next run time unavailable'}}
 if(identity!==viewIdentity)return null
 return <section className="surface stack stack--tight"><div className="page-actions"><h2 className="section-title">{ko?'Bot 루틴 일정판':'Bot routine agenda'}</h2><button type="button" className="button button--secondary" aria-expanded={open} onClick={()=>setOpen(!open)}>{open?(ko?'일정판 닫기':'Close agenda'):(ko?'일정판 열기':'Open agenda')}</button></div>
 {open&&<>{!managementReady&&<p role="status">{managementLoading?(ko?'Bot 목록을 불러오는 중입니다.':'Loading the Bot directory.'):(ko?'Bot 목록을 읽지 못해 루틴 관리로 이동할 수 없습니다.':'Cannot open routine management because the Bot directory is unavailable.')} {!managementLoading&&onRetryManagement&&<button type="button" onClick={onRetryManagement}>{ko?'Bot 목록 다시 읽기':'Retry Bot directory'}</button>}</p>}{listStale&&<p role="alert">{ko?'Bot 목록을 다시 읽지 못해 일정이 완전하지 않을 수 있습니다.':'The Bot list could not be refreshed; the agenda may be incomplete.'}</p>}<p className="field-hint">{ko?'활성 Bot의 저장된 다음 실행을 보여줍니다. 비활성 Bot은 제외하며, 실행이나 완료를 보장하지 않습니다. 시간은 루틴에 설정된 시간대 기준입니다.':'Shows the next saved run for active Bots; inactive Bots are excluded. It does not guarantee execution or completion; times use each routine’s time zone.'}</p><button type="button" className="text-link" disabled={loading} onClick={()=>void read()}>{ko?'일정 다시 확인':'Refresh agenda'}</button>
 {loading&&<p role="status">{ko?`Bot 일정 확인 중 ${done}/${agents.filter(a=>a.is_active&&a.user_id===ownerId).length}`:`Checking Bot schedules ${done}/${agents.filter(a=>a.is_active&&a.user_id===ownerId).length}`}</p>}
 {!loading&&failed.length>0&&<div role="alert"><p>{ko?'일부 Bot의 일정을 읽지 못했습니다. 전체 일정이 아닙니다.':'Some Bot schedules could not be read. This is not the complete agenda.'}</p><ul>{failed.map(x=><li key={x.bot.id}>{x.bot.name}</li>)}</ul><button type="button" onClick={()=>void read()}>{ko?'다시 확인':'Try again'}</button></div>}
 {!loading&&<><label>{ko?'보기':'Show'} <select value={filter} onChange={e=>{setFilter(e.target.value as typeof filter);setPage(0)}}><option value="all">{ko?'전체':'All'}</option><option value="scheduled">{ko?'예정':'Scheduled'}</option><option value="paused">{ko?'일시정지':'Paused'}</option><option value="webhook">{ko?'이벤트':'Event'}</option></select></label>{shown.length===0&&<p>{failed.length?(ko?'읽어 온 일정이 없습니다.':'No schedules were read.'):(ko?'표시할 루틴이 없습니다.':'No routines to show.')}</p>}
 <ul className="stack stack--tight">{shown.slice(safePage*PAGE_SIZE,(safePage+1)*PAGE_SIZE).map(({bot,r})=><li key={`${bot.id}:${r.routine_id}`}><strong>{r.name}</strong> · {bot.name}<p>{time(r)}{r.enabled&&r.availability!=='available'&&<> · {ko?'사용 가능 여부 확인 필요':'Availability needs checking'}</>}</p><button type="button" className="text-link" disabled={!managementReady} onClick={()=>manageBot(bot.id)}>{ko?`${bot.name} 루틴 관리`:`Manage ${bot.name} routines`}</button></li>)}</ul>{pages>1&&<div className="page-actions"><button type="button" disabled={safePage===0} onClick={()=>setPage(safePage-1)}>{ko?'이전':'Previous'}</button><span>{safePage+1} / {pages}</span><button type="button" disabled={safePage+1>=pages} onClick={()=>setPage(safePage+1)}>{ko?'다음':'Next'}</button></div>}</>}
 </>}

 </section>
}
export default function RoutineAgenda({agents,onManage,listStale,managementReady=true,managementLoading=false,onRetryManagement}:{agents:Bot[];onManage:(id:number)=>void;listStale:boolean;managementReady?:boolean;managementLoading?:boolean;onRetryManagement?:()=>void}){
 const scope=useBotScope(),{locale}=useI18n(),cache=useQueryClient()
 const subscribe=useMemo(()=> (handler:(agent:number,items:Routine[]|null)=>void)=>cache.getQueryCache().subscribe(event=>{
  const key=event.query.queryKey
  if(key[0]!=='bot-routines'||key[1]!==scope.identity||typeof key[2]!=='number'||event.type!=='updated')return
  if(event.action.type==='success')handler(key[2],event.query.state.data as Routine[])
  else if(event.action.type==='error')handler(key[2],null)
 }),[cache,scope.identity])
 const load=(id:number)=>cache.fetchQuery({queryKey:['bot-routines',scope.identity,id],queryFn:()=>routinesApi.list(id),staleTime:0})
 return <RoutineAgendaView key={scope.identity} agents={agents} identity={scope.identity} ownerId={scope.ownerId} canUse={scope.canUse} load={load} manage={onManage} ko={locale==='ko'} listStale={listStale} managementReady={managementReady} managementLoading={managementLoading} onRetryManagement={onRetryManagement} subscribe={subscribe}/>
}
