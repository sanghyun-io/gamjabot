import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { eventsApi, type RoomEvent } from '../api/events'
import { validBotMessageRef } from '../api/botMessages'
import { routinesApi, validRoutine, validRun, matchesRoutine, type Routine } from '../api/routines'
import { canonicalEventId } from '../lib/peerReply'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'

type Meta={version:1;receipt_id:string;operation:'create'|'edit'|'pause'|'resume'|'test';state:'saved'|'queued';agent_id:number;root_request_event_id:string;routine_id:string;revision:number;routine:Routine}
const obj=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v)
const pos=(v:unknown):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>0
const equal=(a:unknown,b:unknown):boolean=>a===b || Array.isArray(a)&&Array.isArray(b)&&a.length===b.length&&a.every((v,i)=>equal(v,b[i])) || obj(a)&&obj(b)&&Object.keys(a).length===Object.keys(b).length&&Object.keys(a).every(k=>Object.hasOwn(b,k)&&equal(a[k],b[k]))
export function routinePersistenceMetadata(event:RoomEvent):Meta|null {
 const m=event.payload.routine_persistence
 if(!obj(m)||event.kind!=='activity'||event.actor_type!=='system'||event.actor_id!==null||event.payload.category!=='routine_persistence'||m.version!==1||typeof m.receipt_id!=='string'||!/^[a-f0-9]{64}$/.test(m.receipt_id)||!['create','edit','pause','resume','test'].includes(String(m.operation))||m.state!==(m.operation==='test'?'queued':'saved')||event.payload.status!==m.state||!pos(m.agent_id)||!canonicalEventId(m.root_request_event_id)||event.payload.trigger_event_id!==m.root_request_event_id||!canonicalEventId(m.routine_id)||!pos(m.revision)||!validRoutine(m.routine,m.agent_id)||m.routine.routine_id!==m.routine_id||m.routine.revision!==m.revision||m.operation==='create'&&m.revision!==1||m.operation==='pause'&&m.routine.enabled||m.operation==='resume'&&!m.routine.enabled)return null
 return m as Meta
}
export function matchingRoutineReceipt(event:RoomEvent,owner:number,data:unknown):boolean {
 const meta=routinePersistenceMetadata(event);if(!meta||!obj(data))return false
 if(!['version','receipt_id','operation','state','agent_id','root_request_event_id','routine_id','revision','routine'].every(k=>equal(data[k],meta[k as keyof Meta])))return false
 return validBotMessageRef(data.root_ref)&&data.root_ref.room_id===event.room_id&&data.root_ref.event_id===meta.root_request_event_id&&data.root_ref.seq<event.seq&&data.root_ref.actor_type==='user'&&data.root_ref.actor_id===owner&&data.root_ref.kind==='message'&&validBotMessageRef(data.result_ref)&&data.result_ref.room_id===event.room_id&&data.result_ref.event_id===event.event_id&&data.result_ref.seq===event.seq&&data.result_ref.actor_type==='system'&&data.result_ref.actor_id===null&&data.result_ref.kind==='activity'&&typeof data.replayed==='boolean'
}
export default function RoutinePersistenceEvent({event,allowed,visible,canUse,onOpen}:{event:RoomEvent;allowed:boolean;visible:boolean;canUse:()=>boolean;onOpen:(seq:number,id:string)=>void}) {
 const {t}=useI18n(),scope=useBotScope(),meta=routinePersistenceMetadata(event),node=useRef<HTMLElement>(null),[inView,setInView]=useState(false),[pageVisible,setPageVisible]=useState(()=>document.visibilityState==='visible')
 useEffect(()=>{const change=()=>setPageVisible(document.visibilityState==='visible');document.addEventListener('visibilitychange',change);return()=>document.removeEventListener('visibilitychange',change)},[])
 useEffect(()=>{const element=node.current;if(!element||!visible){setInView(false);return}if(typeof IntersectionObserver==='undefined'){setInView(true);return}const observer=new IntersectionObserver(entries=>setInView(entries.some(entry=>entry.isIntersecting)));observer.observe(element);return()=>observer.disconnect()},[visible,!!meta])
 const query=useQuery({queryKey:['routine-persistence',scope.identity,event.room_id,event.event_id,event.seq,meta],enabled:!!meta&&allowed&&scope.ownerId>0&&visible,retry:false,refetchIntervalInBackground:false,refetchInterval:q=>visible&&inView&&pageVisible&&meta?.operation==='test'&&(q.state.data?.testState==='pending'||q.state.data?.testState==='started'&&q.state.data?.runState==='running')?5000:false,queryFn:async()=>{
  if(!meta||!scope.canUse()||!canUse())throw Error('unavailable')
  const receipt=await routinesApi.persistence(meta.agent_id,meta.receipt_id)
  if(!scope.canUse()||!canUse()||!matchingRoutineReceipt(event,scope.ownerId,receipt)||!obj(receipt)||!validBotMessageRef(receipt.root_ref)||!validBotMessageRef(receipt.result_ref))throw Error('unavailable')
  const refs=[receipt.root_ref,receipt.result_ref],rows=await Promise.all(refs.map(async ref=>(await eventsApi.list(ref.room_id,ref.seq-1,1))[0]))
  if(!scope.canUse()||!canUse()||rows.some((row,i)=>!row||row.room_id!==refs[i].room_id||row.event_id!==refs[i].event_id||row.seq!==refs[i].seq||row.actor_type!==refs[i].actor_type||row.actor_id!==refs[i].actor_id||row.kind!==refs[i].kind)||!equal(rows[1].payload.routine_persistence,meta)||rows[1].payload.category!=='routine_persistence'||rows[1].payload.status!==meta.state||rows[1].payload.trigger_event_id!==meta.root_request_event_id)throw Error('unavailable')
  const current=await routinesApi.read(meta.agent_id,meta.routine_id)
  if(!scope.canUse()||!canUse()||current.revision<meta.revision||current.revision===meta.revision&&(!matchesRoutine(current,meta.routine)||current.room_id!==meta.routine.room_id))throw Error('unavailable')
  let testState:'pending'|'started'|'failed'|'cancelled'|null=null,runState:'running'|'completed'|'failed'|'cancelled'|'interrupted'|'unknown'|null=null,failureCode:string|null=null,runResult:import('../api/botMessages').BotMessageRef|null=null
  if(meta.operation==='test') {
   const test=receipt.test
   if(!obj(test)||!['pending','started','failed','cancelled'].includes(String(test.state))||!(test.failure_code===null||typeof test.failure_code==='string')) throw Error('unavailable')
   testState=test.state as 'pending'|'started'|'failed'|'cancelled'
   if(testState==='started') {
    if(!validRun(test.run,meta.routine.room_id,meta.agent_id)||!test.run.root_ref)throw Error('unavailable')
    const receiptRun=test.run
    const run=await routinesApi.run(current,receiptRun.run_id)
    if(!scope.canUse()||!canUse()||!run||!run.root_ref||!equal(run.root_ref,receiptRun.root_ref)||run.origin!=='test'||receiptRun.origin!=='test'||run.scheduled_at!==receiptRun.scheduled_at||run.started_at!==receiptRun.started_at||receiptRun.state!=='running'&&(run.state!==receiptRun.state||run.completed_at!==receiptRun.completed_at||run.failure_code!==receiptRun.failure_code||!equal(run.result_ref,receiptRun.result_ref))||run.state==='running'&&(run.completed_at!==null||run.result_ref!==null)||['completed','failed','cancelled','interrupted'].includes(run.state)&&!run.completed_at||run.state==='completed'&&!run.result_ref||['cancelled','interrupted'].includes(run.state)&&run.result_ref!==null)throw Error('unavailable')
    const ref=run.root_ref, actual=(await eventsApi.list(ref.room_id,ref.seq-1,1))[0], detail=actual?.payload.routine
    if(!scope.canUse()||!canUse()||!actual||actual.room_id!==ref.room_id||actual.event_id!==ref.event_id||actual.seq!==ref.seq||actual.actor_type!=='system'||actual.actor_id!==null||actual.kind!=='activity'||actual.payload.category!=='owner_routine'||actual.payload.status!=='started'||actual.payload.content!==meta.routine.instruction||!obj(detail)||detail.version!==1||detail.run_id!==run.run_id||detail.routine_id!==meta.routine_id||detail.config_revision!==meta.revision||detail.owner_id!==scope.ownerId||detail.agent_id!==meta.agent_id||detail.origin!=='test'||detail.scheduled_at!==run.scheduled_at)throw Error('unavailable')
    if(run.result_ref){const out=run.result_ref,actualResult=(await eventsApi.list(out.room_id,out.seq-1,1))[0],tag=actualResult?.payload.routine
     if(!scope.canUse()||!canUse()||!actualResult||actualResult.room_id!==out.room_id||actualResult.event_id!==out.event_id||actualResult.seq!==out.seq||actualResult.seq<=ref.seq||actualResult.actor_type!=='agent'||actualResult.actor_id!==meta.agent_id||actualResult.kind!==(run.state==='completed'?'message':'failure')||actualResult.payload.trigger_event_id!==ref.event_id||!obj(tag)||tag.version!==1||tag.run_id!==run.run_id||run.state==='failed'&&(actualResult.payload.code!=='routine_failed'||actualResult.payload.round!==1||!pos(actualResult.payload.generation)))throw Error('unavailable')
     runResult=out
    }
    runState=run.state;failureCode=run.failure_code
   } else if(test.run!==null) throw Error('unavailable')
  }
  return {root:receipt.root_ref,changed:current.revision>meta.revision,testState,runState,failureCode,runResult}
 }})
 if(!meta)return null
 return <section ref={node} className="bot-creation stack stack--tight" aria-label={t('routine.result')}>
  {query.isSuccess?<><div><strong>{t(meta.operation==='test'?'routine.queued':meta.operation==='create'?'routine.createdChat':meta.operation==='pause'?'routine.pausedChat':meta.operation==='resume'?'routine.resumedChat':'routine.updatedChat')}</strong> · {meta.routine.name}</div>{meta.operation==='test'&&<p className="metadata">{t(query.data.testState==='started'&&query.data.runState==='completed'?'routine.completed':query.data.testState==='started'&&query.data.runState==='failed'?'routine.failed':query.data.testState==='started'&&query.data.runState==='cancelled'?'routine.cancelled':query.data.testState==='started'&&query.data.runState==='interrupted'?'routine.interrupted':query.data.testState==='started'&&query.data.runState==='unknown'?'routine.runUnknown':query.data.testState==='started'?'routine.queuedStarted':query.data.testState==='failed'?'routine.queuedFailed':query.data.testState==='cancelled'?'routine.queuedCancelled':'routine.queuedHelp')}{query.data.runState==='failed'&&<> · {t(query.data.failureCode==='unsupported_option'?'chat.failure.unsupportedOption':'routine.failureOther')}</>}</p>}{query.data.changed&&<p className="metadata">{t('routine.changedAfter')}</p>}{query.data.runResult&&<Link className="text-link" to={`/chat/${query.data.runResult.room_id}?${new URLSearchParams({seq:String(query.data.runResult.seq),event:query.data.runResult.event_id})}`} onClick={e=>{if(!scope.canUse()||!canUse())e.preventDefault()}}>{t('routine.open')}</Link>}<div className="page-actions"><button className="text-link" type="button" onClick={()=>{if(scope.canUse()&&canUse())onOpen(query.data.root.seq,query.data.root.event_id)}}>{t('skillEvent.request')}</button><Link className="text-link" to="/agents" onClick={e=>{if(!scope.canUse()||!canUse())e.preventDefault()}}>{t('routine.openSettings')}</Link></div></>:<><span role="status">{t(query.isFetching?'routine.checkingResult':'routine.resultUnknown')}</span>{!query.isFetching&&allowed&&<button type="button" className="text-link" onClick={()=>{if(scope.canUse()&&canUse())void query.refetch({cancelRefetch:false})}}>{t('routine.readAgain')}</button>}</>}
 </section>
}
