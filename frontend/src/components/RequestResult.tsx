import { useEffect,useLayoutEffect,useRef,useState,useSyncExternalStore } from 'react'
import { Sparkles } from 'lucide-react'
import type { FinalizeResult } from '../api/requestFinalize'
import { starterApi } from '../api/starter'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useViewNavigationIntent } from '../lib/useViewNavigationIntent'
import { finalizeBlockedKeys,finalizeOwnerCurrent,requestFinalization,requestFinalizePlan } from '../lib/finalizeActions'
import { finalizeRoomBlocked,finalizeSource,finalizeTarget,finalizeVersion,initializeFinalizer,patchFinalizer,readFinalizers,subscribeFinalizers,type FinalizeAgent } from '../lib/finalizeWorkspace'
import FinalizeHistory from './FinalizeHistory'
const excludedReasonKey:Record<string,MessageKey>={failed:'finalize.excludedReason.failed',passed:'finalize.excludedReason.passed'}
export default function RequestResult({roomId,sourceRequestId,ownerId,authEpoch,selectedAgents,onCreated,onViewSource,onNavigate}:{roomId:number;sourceRequestId:string;ownerId:number;authEpoch:number;selectedAgents:FinalizeAgent[];onCreated:(result:FinalizeResult,canNavigate?:()=>boolean)=>void;onViewSource?:(seq:number,eventId:string)=>void;onNavigate?:(href:string)=>void}){
 const {t,formatNumber}=useI18n(),identity=`${ownerId}:${authEpoch}`,generation=useRef(directoryViewGeneration()).current
 useSyncExternalStore(subscribeFinalizers,finalizeVersion)
 const workspace=readFinalizers(identity),draft=workspace.drafts.get(finalizeSource(roomId,sourceRequestId))||{agentId:selectedAgents[0]?.agent_id||0,version:0,open:false,hintAttempted:false},agentId=draft.agentId,open=draft.open
 const read=workspace.reads.get(finalizeTarget(roomId,sourceRequestId,agentId)),plan=read?.plan,selected=selectedAgents.find(agent=>agent.agent_id===agentId)
 const root=useRef<HTMLElement>(null),stage=useRef<HTMLDivElement>(null),alive=useRef(false),navigation=useViewNavigationIntent(),submitted=useRef<string|null>(null),focusRead=useRef<string|null>(null),callback=useRef(onCreated);callback.current=onCreated
 const viewId=`${identity}:${roomId}:${sourceRequestId}`,view=useRef(viewId);view.current=viewId
 const hintSerial=useRef(0),[selecting,setSelecting]=useState(false)
 const valid=()=>finalizeOwnerCurrent(identity,generation)
 useLayoutEffect(()=>{if(valid())initializeFinalizer(identity,generation,roomId,sourceRequestId,selectedAgents)},[identity,roomId,sourceRequestId,generation])
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;hintSerial.current++;submitted.current=null}},[])
 useEffect(()=>{submitted.current=null;focusRead.current=null;hintSerial.current++;setSelecting(false);navigation.cancel()},[viewId])
 const reveal=()=>{
  if(!valid())return
  const current=readFinalizers(identity).drafts.get(finalizeSource(roomId,sourceRequestId));if(!current)return
  patchFinalizer(identity,generation,roomId,sourceRequestId,{open:true})
  const intent=crypto.randomUUID();navigation.begin(intent,root.current)
  requestAnimationFrame(()=>{if(valid()&&navigation.current(intent))root.current?.focus({preventScroll:true})})
  if(current.hintAttempted)return
  patchFinalizer(identity,generation,roomId,sourceRequestId,{hintAttempted:true});const serial=++hintSerial.current;setSelecting(true)
  void(async()=>{try{
   if(!valid())return
   const last=await starterApi.lastResult(),latest=readFinalizers(identity).drafts.get(finalizeSource(roomId,sourceRequestId))
   if(valid()&&alive.current&&view.current===viewId&&hintSerial.current===serial&&navigation.current(intent)&&latest?.open&&latest.version===current.version&&last?.room_id===roomId&&selectedAgents.some(agent=>agent.agent_id===last.agents.proposer))patchFinalizer(identity,generation,roomId,sourceRequestId,{agentId:last.agents.proposer})
  }catch{/* Optional first-open hint only; original selection and commands stay. */}finally{if(valid()&&alive.current&&view.current===viewId&&hintSerial.current===serial)setSelecting(false)}})()
 }
 const close=()=>{if(valid()){navigation.cancel();submitted.current=null;focusRead.current=null;hintSerial.current++;setSelecting(false);patchFinalizer(identity,generation,roomId,sourceRequestId,{open:false})}}
 const choose=(next:number)=>{if(valid()){navigation.cancel();submitted.current=null;focusRead.current=null;patchFinalizer(identity,generation,roomId,sourceRequestId,{agentId:next})}}
 const preview=()=>{
  if(!valid()||!selected)return
  const original=requestFinalizePlan(identity,generation,roomId,sourceRequestId,agentId,draft.version)
  if(original){focusRead.current=original.id;navigation.begin(original.id,root.current)}
 }
 useEffect(()=>{
  if(!read||focusRead.current!==read.id||read.phase==='pending')return
  focusRead.current=null
  if(valid()&&open&&read.draftVersion===draft.version&&navigation.current(read.id)){stage.current?.focus({preventScroll:true});navigation.cancel()}
 },[read,open,draft.version])
 const execute=()=>{
  if(!valid()||!read||!selected)return
  const original=requestFinalization(identity,generation,roomId,sourceRequestId,agentId,read.id)
  if(original){submitted.current=original.id;focusRead.current=null;navigation.begin(original.id,root.current)}
 }
 useEffect(()=>{
  const command=workspace.commands.find(value=>value.id===submitted.current)
  if(!command||!valid())return
  if(command.roomId!==roomId||command.sourceId!==sourceRequestId||command.waitStopped||workspace.commands.filter(value=>value.roomId===roomId).at(-1)?.id!==command.id){submitted.current=null;navigation.cancel();return}
  if(command.phase==='pending')return
  submitted.current=null
  const canNavigate=()=>{const latest=readFinalizers(identity),current=latest.drafts.get(finalizeSource(roomId,sourceRequestId));return alive.current&&valid()&&view.current===viewId&&current?.open===true&&current.agentId===command.agentId&&current.version===command.draftVersion&&latest.commands.filter(value=>value.roomId===roomId).at(-1)?.id===command.id&&navigation.current(command.id)===true}
  if(command.phase==='accepted'&&command.result&&canNavigate())callback.current(command.result,canNavigate)
  else navigation.cancel()
 },[workspace.commands,viewId])
 const eventValid=(seq:number,eventId:string)=>Number.isSafeInteger(seq)&&seq>0&&typeof eventId==='string'&&/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(eventId)
 const canRead=()=>{
  const latest=readFinalizers(identity).reads.get(finalizeTarget(roomId,sourceRequestId,agentId))
  return valid()&&alive.current&&view.current===viewId&&open&&!!read&&latest?.id===read.id&&latest.plan===plan&&plan?.room_id===roomId
   &&plan.source_request?.event_id===sourceRequestId&&!!root.current?.isConnected&&!!root.current.getClientRects().length
   &&!root.current.closest('[hidden], [inert], [aria-hidden="true"]')&&document.visibilityState==='visible'&&document.hasFocus()
 }
 const viewEvent=(seq:number,eventId:string)=>{if(canRead()&&eventValid(seq,eventId))onViewSource?.(seq,eventId)}
 const viewSettings=(href:'/connections'|'/agents')=>{if(canRead())onNavigate?.(href)}
 const pending=workspace.pending.has(roomId),blocked=finalizeRoomBlocked(identity,roomId),currentPlan=read?.phase==='ready'&&read.draftVersion===draft.version&&!read.consumed
 return <section ref={root} tabIndex={-1} className="request-result stack stack--tight" aria-label={t('finalize.title')}>
  {!open?<button type="button" className="button button--secondary button--compact" onClick={reveal}><Sparkles size={15} aria-hidden="true"/>{t('finalize.open')}</button>:<div className="stack stack--tight">
   <div><strong>{t('finalize.title')}</strong><p className="section-copy">{t('finalize.description')}</p></div>
   <button type="button" className="button button--secondary button--compact" onClick={close}>{t('common.close')}</button>
   <label className="field">{t('finalize.agent')}<select value={agentId} onChange={event=>choose(Number(event.target.value))}>{!selected&&<option value={agentId}>#{agentId} · {t('finalize.recovery.missingAgent')}</option>}{selectedAgents.map(agent=><option key={agent.agent_id} value={agent.agent_id}>{agent.name_snapshot||t('inspector.agentFallback',{id:agent.agent_id})}</option>)}</select></label>
   {selecting&&<p className="metadata">{t('finalize.recovery.defaultHint')}</p>}
   <p className="metadata">{t('memory.finalizer')}</p>
   <button type="button" className="button button--secondary button--compact" disabled={!selected||!agentId||pending||read?.phase==='pending'} onClick={preview}>{t('finalize.preview')}</button>
   {read?.phase==='pending'&&<p role="status" className="metadata">{t('finalize.loading')}</p>}
   {read?.phase==='error'&&<p role="alert" className="notice notice--warning">{t('finalize.loadFailure')}</p>}
   {plan&&<div ref={stage} tabIndex={-1} className="request-result__preview stack stack--tight">
    {!currentPlan&&<p className="metadata">{t('finalize.recovery.lastPlan')}</p>}
      {plan.eligible && <p className="status-text">{t(plan.source_completion?.outcome_status === 'partial' ? 'finalize.partial' : 'finalize.completed')}</p>}
      {plan.source_request && <div><p>{plan.source_request.preview}</p><button type="button" className="text-link" disabled={!onViewSource||!eventValid(plan.source_request.seq,plan.source_request.event_id)} onClick={()=>viewEvent(plan.source_request!.seq,plan.source_request!.event_id)}>{t('finalize.goSource')}</button></div>}
      <p>{t('finalize.chosen', { name: plan.agent?.current_name || plan.agent?.name_snapshot || selected?.name_snapshot || t('inspector.agentFallback', { id: agentId }) })}</p>
      {plan.current_model && <p className="metadata">{plan.current_model.provider_kind} · {plan.current_model.model_id}</p>}
      {plan.limits && <p className="metadata">{t('finalize.rounds', { count: plan.limits.max_rounds })} · {t('finalize.tools', { count: plan.limits.max_tool_calls })}</p>}
      {plan.eligible && <p className="notice notice--warning">{t('finalize.cost')}</p>}
      {plan.eligible && <details><summary>{t('finalize.transportDetails')}</summary><p className="metadata">{t('finalize.transportRetry')}</p></details>}
      {plan.excluded_agents.length > 0 && <div><strong>{t('finalize.excluded')}</strong><ul>{plan.excluded_agents.map((agent) => <li key={agent.agent_id}>{agent.name_snapshot || t('inspector.agentFallback', { id: agent.agent_id })} · {t(excludedReasonKey[agent.reason] || 'finalize.excludedReason.other')}</li>)}</ul></div>}
      <details><summary>{t('finalize.sources')}</summary><div className="stack stack--tight">
        <p>{t('finalize.outcomes', { count: plan.outcome_count })}</p>
        <ul>{plan.outcomes.map((outcome) => <li key={outcome.event_id}><button type="button" className="text-link" disabled={!onViewSource||!eventValid(outcome.seq,outcome.event_id)} onClick={()=>viewEvent(outcome.seq,outcome.event_id)}>{outcome.agent_name_snapshot || t('inspector.agentFallback', { id: outcome.agent_id })} · #{formatNumber(outcome.seq)}</button></li>)}</ul>
        {plan.outcomes_truncated && <p className="notice notice--warning">{t('finalize.outcomesTruncated')}</p>}
        {plan.source_coverage && <><p>{t('finalize.coverage', { included: plan.source_coverage.included_message_event_ids.length, total: plan.source_coverage.source_outcome_event_ids.length })}</p>
          {plan.source_coverage.omitted_event_ids.length > 0 && <p>{t('finalize.omitted', { count: plan.source_coverage.omitted_event_ids.length })}</p>}
          {plan.source_coverage.truncations.map((item) => <p key={item.event_id}>{t('finalize.truncation', { original: formatNumber(item.original_chars), included: formatNumber(item.included_chars), omitted: formatNumber(item.omitted_chars) })}</p>)}</>}
      </div></details>
      {!plan.eligible && plan.blocked_reason && <><p role="status" className="notice notice--warning">{t(finalizeBlockedKeys[plan.blocked_reason])}</p>
        {plan.blocked_reason === 'model_unavailable' && <button type="button" className="button button--secondary button--compact" disabled={!onNavigate} onClick={()=>viewSettings('/connections')}>{t('finalize.openConnections')}</button>}
        {plan.blocked_reason === 'agent_unavailable' && <button type="button" className="button button--secondary button--compact" disabled={!onNavigate} onClick={()=>viewSettings('/agents')}>{t('finalize.openAgents')}</button>}</>}
      {read?.consumed&&<p>{t('finalize.recovery.consumed')}</p>}
      {blocked&&!pending&&<p role="status" className="notice notice--warning">{t('finalize.recovery.originalFirst')}</p>}
      {plan.eligible&&plan.plan_hash&&<button type="button" className="button button--primary button--compact" disabled={blocked||!currentPlan||!selected} onClick={execute}>{pending?t('finalize.starting'):t('finalize.start')}</button>}
   </div>}
  </div>}
  <FinalizeHistory identity={identity} roomId={roomId} sourceId={sourceRequestId} onViewEvent={onViewSource}/>
 </section>
}
