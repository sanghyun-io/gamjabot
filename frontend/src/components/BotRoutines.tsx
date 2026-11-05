import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { routinesApi, validCommand, validSchedule, type Routine, type RoutineInput, type RoutineRun, type RoutineSchedule } from '../api/routines'
import { eventsApi } from '../api/events'
import { useAuthStore } from '../stores/authStore'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'
import ServerTime from './ServerTime'
import RoutineWebhook from './RoutineWebhook'

type Draft = { name:string; instruction:string; enabled:boolean; kind:'interval'|'weekly'|'webhook'; interval:string; weekdays:number[]; time:string; timezone:string; base:Routine | null }
type Pending = { kind:'create'|'update'|'test'; phase:'sending'|'unknown'|'conflict'; key:string; body:RoutineInput | null; routine:Routine | null }
const operations = new Map<string,Pending>()
useAuthStore.subscribe((current,previous)=>{if(!current.token || current.epoch!==previous.epoch || current.user?.id!==previous.user?.id) operations.clear()})
const zone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Seoul' } catch { return 'Asia/Seoul' } }
const blank = ():Draft => ({name:'',instruction:'',enabled:true,kind:'weekly',interval:'60',weekdays:[0,1,2,3,4],time:'08:00',timezone:zone(),base:null})
const draftOf = (r:Routine):Draft => ({name:r.name,instruction:r.instruction,enabled:r.enabled,kind:r.schedule.kind,interval:r.schedule.kind==='interval'?String(r.schedule.interval_minutes):'60',weekdays:r.schedule.kind==='weekly'?[...r.schedule.weekdays]:[0,1,2,3,4],time:r.schedule.kind==='weekly'?`${String(r.schedule.hour).padStart(2,'0')}:${String(r.schedule.minute).padStart(2,'0')}`:'08:00',timezone:r.schedule.kind==='webhook'?zone():r.schedule.timezone,base:r})
function toInput(d:Draft):RoutineInput | null {
  const time=/^(\d{2}):(\d{2})$/.exec(d.time)
  const schedule:RoutineSchedule = d.kind==='webhook' ? {kind:'webhook'} : d.kind==='interval' ? {kind:'interval',interval_minutes:/^\d+$/.test(d.interval)?Number(d.interval):NaN,timezone:d.timezone.trim()} : {kind:'weekly',weekdays:[...d.weekdays].sort((a,b)=>a-b),hour:time?Number(time[1]):NaN,minute:time?Number(time[2]):NaN,timezone:d.timezone.trim()}
  const name=d.name.trim(),instruction=d.instruction.trim()
  return name && name.length<=120 && instruction && instruction.length<=4000 && validSchedule(schedule) ? {name,instruction,enabled:d.enabled,schedule} : null
}
const status=(e:unknown)=>(e as {response?:{status?:number;data?:{detail?:{code?:string}}}}).response
export default function BotRoutines({ agentId }: { agentId:number }) {
  const {t}=useI18n(),scope=useBotScope(),cache=useQueryClient(),key=`${scope.identity}:${agentId}`,lock=useRef(false)
  const [open,setOpen]=useState(false),[draft,setDraft]=useState<Draft|null>(null),[invalid,setInvalid]=useState(false)
  const [pending,setPendingState]=useState<Pending|null>(()=>operations.get(key)||null),[notice,setNotice]=useState<'created'|'createdWebhook'|'updated'|'testStarted'|'rejected'|'busy'|'room'|null>(null)
  const setPending=(value:Pending|null)=>{if(value)operations.set(key,value);else operations.delete(key);setPendingState(value)}
  useEffect(()=>{setPendingState(operations.get(key)||null);setDraft(null);setNotice(null);return()=>{const p=operations.get(key);if(p?.phase==='sending')operations.set(key,{...p,phase:'unknown'})}},[key])
  const queryKey=['bot-routines',scope.identity,agentId] as const
  const query=useQuery({queryKey,enabled:open,retry:false,queryFn:async()=>{const value=await routinesApi.list(agentId);if(!scope.canUse())throw new Error('Owner changed');return value}})
  const changed=!!draft?.base && !!query.data && !query.data.some(r=>r.routine_id===draft.base!.routine_id && r.revision===draft.base!.revision)
  const receive = async (op:Pending, value:unknown) => {
    if(!scope.canUse() || !op.body && op.kind==='create' || !op.routine && op.kind==='test') throw new Error('Routine result unavailable')
    if(op.kind==='update') throw new Error('Routine read required')
    if(!validCommand(value,agentId,op.kind,op.kind==='create'?op.body!:op.routine!)) throw new Error('Routine result unavailable')
    if(op.kind==='test' && op.routine) {
      const run=value.run!, ref=run.root_ref!
      const rows=await eventsApi.list(ref.room_id,ref.seq-1,1), event=rows.find(row=>row.seq===ref.seq && row.event_id===ref.event_id)
      const meta=event?.payload.routine as Record<string,unknown> | undefined
      if(!scope.canUse() || !event || event.room_id!==ref.room_id || event.actor_type!=='system' || event.actor_id!==null || event.kind!=='activity' || event.payload.category!=='owner_routine' || event.payload.status!=='started' || event.payload.content!==op.routine.instruction || !meta || meta.version!==1 || meta.run_id!==run.run_id || meta.routine_id!==op.routine.routine_id || meta.config_revision!==op.routine.revision || meta.owner_id!==scope.ownerId || meta.agent_id!==agentId || meta.origin!=='test' || meta.scheduled_at!==run.scheduled_at) throw new Error('Routine result unavailable')
    }
    setPending(null);setDraft(null);setNotice(op.kind==='test'?'testStarted':op.body?.schedule.kind==='webhook'?'createdWebhook':'created');void cache.invalidateQueries({queryKey})
    if(op.kind==='test' && op.routine) void cache.invalidateQueries({queryKey:['routine-runs',scope.identity,agentId,op.routine.routine_id]})
  }
  const perform = async (op:Pending) => {
    lock.current=true;setPending(op);setNotice(null)
    try {
      if(op.kind==='create') await receive(op,await routinesApi.create(agentId,op.body!,op.key))
      else if(op.kind==='test') await receive(op,await routinesApi.test(op.routine!,op.key))
      else { const saved=await routinesApi.update(agentId,op.routine!.routine_id,op.routine!.revision,op.body!);if(scope.canUse()){cache.setQueryData<Routine[]>(queryKey,rows=>rows?.map(r=>r.routine_id===saved.routine_id?saved:r));setPending(null);setDraft(null);setNotice('updated');void cache.invalidateQueries({queryKey})} }
    } catch(e) { if(scope.canUse()){const response=status(e), code=response?.data?.detail?.code; if(response?.status===409 && code==='routine_revision_conflict') setPending({...op,phase:'conflict'}); else if(response?.status===409 && ['routine_room_unavailable','routine_room_busy','routine_unavailable','routine_rate_limited','routine_limit'].includes(code || '')) {setPending(null);setNotice(code==='routine_room_busy'?'busy':code==='routine_room_unavailable'?'room':'rejected')} else setPending({...op,phase:'unknown'})} }
    finally {lock.current=false}
  }
  const save=(event:FormEvent)=>{event.preventDefault();if(!scope.canUse()||lock.current||pending||!draft||!query.isSuccess||query.isFetching||changed)return;const body=toInput(draft);if(!body){setInvalid(true);return}setInvalid(false);void perform({kind:draft.base?'update':'create',phase:'sending',key:crypto.randomUUID(),body,routine:draft.base})}
  const test=(r:Routine)=>{if(!scope.canUse()||lock.current||pending||!query.isSuccess||query.isFetching||!query.data.some(x=>x.routine_id===r.routine_id&&x.revision===r.revision))return;void perform({kind:'test',phase:'sending',key:crypto.randomUUID(),body:null,routine:r})}
  const recover=async()=>{if(!scope.canUse()||lock.current||!pending||pending.phase==='sending')return;const op=pending;lock.current=true
    try {
      if(op.phase==='unknown' && (op.kind==='create'||op.kind==='test')) {const data=await routinesApi.command(agentId,op.key);await receive(op,data)}
      else {if(op.routine)await routinesApi.read(agentId,op.routine.routine_id);const result=await query.refetch({cancelRefetch:false});if(scope.canUse()&&result.isSuccess){setPending(null);setDraft(null);setNotice(null)}}
    } catch { /* An unavailable read never repeats the write or test. */ } finally {lock.current=false} }
  const setEnabled=(r:Routine,enabled:boolean)=>{if(!scope.canUse()||pending||lock.current||query.isFetching||!query.isSuccess||!query.data.some(x=>x.routine_id===r.routine_id&&x.revision===r.revision))return;void perform({kind:'update',phase:'sending',key:crypto.randomUUID(),body:{name:r.name,instruction:r.instruction,schedule:r.schedule,enabled},routine:r})}
  const days=['monday','tuesday','wednesday','thursday','friday','saturday','sunday'] as const
  return <details className="bot-routines" open={open} onToggle={event=>setOpen(event.currentTarget.open)}><summary>{t('routine.title')}</summary>{open && <div className="stack stack--tight bot-routines__body">
    <p className="metadata">{t('routine.intro')}</p>
    {query.isPending && <p role="status">{t('common.loading')}</p>}{query.isError && <p role="alert">{t('routine.unavailable')} <button type="button" className="text-link" onClick={()=>{if(scope.canUse())void query.refetch({cancelRefetch:false})}}>{t('routine.readAgain')}</button></p>}
    {pending && <p role={pending.phase==='sending'?'status':'alert'}>{t(pending.phase==='sending'?'routine.saving':pending.phase==='conflict'?'routine.conflict':'routine.unknown')}{pending.phase!=='sending' && <> <button type="button" className="text-link" onClick={()=>{void recover()}}>{t('routine.check')}</button>{pending.kind==='test' && <span className="metadata"> · {t('routine.checkOnly')}</span>}</>}</p>}
    {notice && <p role="status">{t(notice==='created'?'routine.saved':notice==='createdWebhook'?'webhook.saved':notice==='updated'?'routine.updated':notice==='testStarted'?'routine.testStarted':notice==='busy'?'routine.busy':notice==='room'?'routine.unavailableRoom':'routine.rejected')}</p>}
    {query.isSuccess && !query.data.length && !draft && <p className="metadata">{t('routine.empty')}</p>}
    {!draft && <div><button type="button" className="button button--secondary button--compact" disabled={!!pending || !query.isSuccess || query.isFetching || (query.data?.length||0)>=50} onClick={()=>{if(scope.canUse()){setDraft(blank());setNotice(null)}}}>{t('routine.create')}</button></div>}
    {draft && <form className="stack stack--tight bot-routine-form" onSubmit={save}>
      <label className="field">{t('routine.name')}<input value={draft.name} maxLength={120} disabled={!!pending} onChange={e=>setDraft({...draft,name:e.target.value})}/></label>
      <label className="field">{t('routine.instruction')}<textarea value={draft.instruction} rows={3} maxLength={4000} disabled={!!pending} onChange={e=>setDraft({...draft,instruction:e.target.value})}/></label>
      <label className="field">{t('routine.schedule')}<select value={draft.kind} disabled={!!pending} onChange={e=>setDraft({...draft,kind:e.target.value as Draft['kind']})}><option value="weekly">{t('routine.weekly')}</option><option value="interval">{t('routine.interval')}</option><option value="webhook">{t('webhook.kind')}</option></select></label>
      {draft.kind==='webhook' ? <p className="field-hint">{t('webhook.scheduleHelp')}</p> : draft.kind==='interval' ? <label className="field">{t('routine.minutes')}<input type="number" min="5" max="10080" step="1" value={draft.interval} disabled={!!pending} onChange={e=>setDraft({...draft,interval:e.target.value})}/></label> : <><fieldset className="routine-weekdays"><legend>{t('routine.weekdays')}</legend>{days.map((day,i)=><label key={day}><input type="checkbox" checked={draft.weekdays.includes(i)} disabled={!!pending} onChange={e=>setDraft({...draft,weekdays:e.target.checked?[...draft.weekdays,i]:draft.weekdays.filter(x=>x!==i)})}/>{t(`routine.${day}`)}</label>)}</fieldset><label className="field">{t('routine.time')}<input type="time" value={draft.time} disabled={!!pending} onChange={e=>setDraft({...draft,time:e.target.value})}/></label></>}
      {draft.kind!=='webhook'&&<label className="field">{t('routine.timezone')}<input value={draft.timezone} disabled={!!pending} onChange={e=>setDraft({...draft,timezone:e.target.value})}/></label>}
      <label className="routine-toggle"><input type="checkbox" checked={draft.enabled} disabled={!!pending} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>{t('routine.enabled')}</label>
      {(invalid || !toInput(draft) && !!draft.name && !!draft.instruction) && <p role="alert">{t('routine.invalid')}</p>}{changed && <p role="status">{t('routine.conflict')}</p>}
      <div className="page-actions"><button className="button button--primary button--compact" disabled={!!pending || changed || query.isFetching || !toInput(draft)}>{t('routine.save')}</button><button type="button" className="button button--ghost button--compact" disabled={!!pending} onClick={()=>setDraft(null)}>{t('routine.cancel')}</button></div>
    </form>}
    {query.data?.map(r=><RoutineRow key={r.routine_id} routine={r} disabled={!!pending || !!draft || query.isFetching} onEdit={()=>{if(scope.canUse())setDraft(draftOf(r))}} onEnable={value=>setEnabled(r,value)} onTest={()=>test(r)} onRefresh={()=>{void query.refetch({cancelRefetch:false})}} />)}
  </div>}</details>
}
function RoutineRow({routine,disabled,onEdit,onEnable,onTest,onRefresh}:{routine:Routine;disabled:boolean;onEdit:()=>void;onEnable:(value:boolean)=>void;onTest:()=>void;onRefresh:()=>void}) {
  const {t}=useI18n(),scope=useBotScope(),[open,setOpen]=useState(false)
  const runs=useQuery({queryKey:['routine-runs',scope.identity,routine.agent_id,routine.routine_id],enabled:open,retry:false,refetchInterval:open?5000:false,refetchIntervalInBackground:false,queryFn:async()=>{const data=await routinesApi.runs(routine);if(!scope.canUse())throw new Error('Owner changed');return data}})
  const failure=(code:string|null)=>code==='model_feature_unavailable'?'routine.failureModel':['provider_invalid_response','model_identity_unconfirmed'].includes(code||'')?'routine.failureResponse':code==='context_too_large'?'routine.failureContext':code==='memory_context_changed'?'routine.failureChanged':code==='bot_configuration_changed'?'routine.failureConfig':['routine_rate_limited','routine_capacity_busy'].includes(code||'')?'routine.failureLimit':code==='routine_room_busy'?'routine.busy':['routine_revision_conflict','routine_unavailable'].includes(code||'')?'routine.failureChanged':code==='routine_tool_unavailable'?'routine.failureTool':code==='routine_action_not_completed'?'routine.failureAction':code==='executor_budget_exhausted'?'routine.failureTime':'routine.failureOther'
  const state=(run:RoutineRun)=>run.state==='unknown' || run.state==='completed' && !run.result_ref?'routine.runUnknown':`routine.${run.state}` as const
  return <article className="bot-routine stack stack--tight"><div className="split"><strong>{routine.name}</strong><span className="metadata">{t(routine.availability==='available'?(routine.schedule.kind==='webhook'?'webhook.waiting':'routine.active'):routine.availability==='paused'?'routine.paused':'routine.changed')}</span></div>
    <p>{routine.instruction}</p><p className="metadata">{routine.schedule.kind==='webhook' ? t('webhook.kind') : <>{routine.schedule.kind==='interval' ? `${t('routine.interval')} · ${routine.schedule.interval_minutes} ${t('routine.minutes')}` : `${routine.schedule.weekdays.map(day=>t(`routine.${(['monday','tuesday','wednesday','thursday','friday','saturday','sunday'] as const)[day]}`)).join(' · ')} ${String(routine.schedule.hour).padStart(2,'0')}:${String(routine.schedule.minute).padStart(2,'0')}`} · {routine.schedule.timezone}</>}</p>{routine.next_run_at && routine.availability==='available' && <p className="metadata">{t('routine.next')} · <ServerTime value={routine.next_run_at}/></p>}
    {['room_unavailable','agent_inactive'].includes(routine.availability) && <p role="status">{t('routine.unavailableRoom')}</p>}
    <div className="page-actions"><button type="button" className="text-link" disabled={disabled} onClick={onEdit}>{t('routine.edit')}</button><button type="button" className="text-link" disabled={disabled} onClick={()=>onEnable(!routine.enabled)}>{t(routine.enabled?'routine.pause':'routine.resume')}</button><Link className="text-link" to={`/chat/${routine.room_id}`} state={{routineEdit:{ownerId:scope.ownerId,epoch:scope.epoch,roomId:routine.room_id,agentId:routine.agent_id,name:routine.name}}}>{t('routine.natural')}</Link></div>
    {routine.schedule.kind==='webhook'&&<RoutineWebhook routine={routine} disabled={disabled} onRefresh={onRefresh}/>}
    <p className="metadata">{t('routine.testHelp')}</p><div><button type="button" className="button button--secondary button--compact" disabled={disabled} onClick={onTest}>{t('routine.test')}</button></div>
    <details open={open} onToggle={e=>setOpen(e.currentTarget.open)}><summary>{t('routine.runs')}</summary>{runs.isError && <p role="alert">{t('routine.runsFailed')} <button type="button" className="text-link" onClick={()=>{if(scope.canUse())void runs.refetch({cancelRefetch:false})}}>{t('routine.readAgain')}</button></p>}{runs.isSuccess && !runs.data.length && <p className="metadata">{t('routine.runsEmpty')}</p>}
      {runs.data?.map(run=>{const ref=run.result_ref||run.root_ref;return <div className="bot-routine__run" key={run.run_id}><span>{t(run.origin==='test'?'routine.testOrigin':run.origin==='webhook'?'webhook.runOrigin':'routine.scheduled')} · {t(state(run))} · <ServerTime value={run.completed_at||run.started_at||run.scheduled_at}/></span>{run.state==='failed' && <span className="metadata">{t(failure(run.failure_code))}</span>}{ref && <Link className="text-link" to={`/chat/${routine.room_id}?${new URLSearchParams({seq:String(ref.seq),event:ref.event_id})}`}>{t(run.state==='running'?'routine.openRunning':'routine.open')}</Link>}</div>})}</details>
  </article>
}
