import { useEffect, useRef, useSyncExternalStore } from 'react'
import { RotateCcw } from 'lucide-react'
import type { RequestRetryResult } from '../api/requestRetries'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useViewNavigationIntent } from '../lib/useViewNavigationIntent'
import { requestRetryPlan, requestTargetedRetry, retryBlockedKeys, retryOwnerCurrent, validTeammateRecovery } from '../lib/requestRetryActions'
import { readRequestRetries, requestRetryVersion, retryRoomBlocked, retryTarget, subscribeRequestRetries } from '../lib/requestRetryWorkspace'
import RequestRetryHistory from './RequestRetryHistory'
export default function TargetedRetry({roomId,failureEventId,ownerId,authEpoch,onCreated,onViewSource,enabled=true,showHistory=true,toolReview=false,teammateRecovery=false}:{roomId:number;failureEventId:string;ownerId:number;authEpoch:number;onCreated:(result:RequestRetryResult)=>void;onViewSource?:(seq:number,eventId:string)=>void;enabled?:boolean;showHistory?:boolean;toolReview?:boolean;teammateRecovery?:boolean}) {
  const {t,formatNumber}=useI18n(),identity=`${ownerId}:${authEpoch}`,generation=useRef(directoryViewGeneration()).current
  useSyncExternalStore(subscribeRequestRetries,requestRetryVersion)
  const workspace=readRequestRetries(identity),read=workspace.reads.get(retryTarget(roomId,failureEventId)),plan=read?.plan
  const root=useRef<HTMLElement>(null),submitted=useRef<string|null>(null),navigation=useViewNavigationIntent(),callback=useRef(onCreated);callback.current=onCreated
  const valid=()=>enabled&&retryOwnerCurrent(identity,generation)
  useEffect(()=>{submitted.current=null;navigation.cancel()},[identity,roomId,failureEventId,generation])
  const load=()=>{if(valid()){navigation.cancel();requestRetryPlan(identity,generation,roomId,failureEventId,teammateRecovery)}}
  const start=()=>{
    if(!valid()||!read || (teammateRecovery && (!plan || !validTeammateRecovery(plan))))return
    const original=requestTargetedRetry(identity,generation,roomId,failureEventId,read.id)
    if(original){submitted.current=original.id;navigation.begin(original.id,root.current)}
  }
  useEffect(()=>{
    const command=workspace.commands.find(value=>value.id===submitted.current)
    if(!command||!valid())return
    if(command.roomId!==roomId||command.failureId!==failureEventId||command.waitStopped||workspace.commands.filter(value=>value.roomId===roomId).at(-1)?.id!==command.id){submitted.current=null;navigation.cancel();return}
    if(command.phase==='pending')return
    submitted.current=null
    if(command.phase==='accepted'&&command.result&&navigation.current(command.id))callback.current(command.result)
    navigation.cancel()
  },[workspace.commands,identity,roomId,failureEventId])

  const source=plan?.source_request
  const canViewSource=!!onViewSource&&!!source&&plan?.room_id===roomId&&plan.failure_event_id===failureEventId
    &&Number.isSafeInteger(roomId)&&roomId>0&&Number.isSafeInteger(source.seq)&&source.seq>0
    &&Number.isSafeInteger(plan.failure_seq)&&source.seq<plan.failure_seq
    &&typeof source.event_id==='string'&&/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(source.event_id)
    &&typeof source.preview==='string'
  const viewSource=()=>{
    const latest=readRequestRetries(identity).reads.get(retryTarget(roomId,failureEventId))
    if(!canViewSource||!source||!onViewSource||!read||!valid()||latest?.id!==read.id||latest.plan!==plan
      ||!root.current?.isConnected||!root.current.getClientRects().length||root.current.closest('[hidden], [inert], [aria-hidden="true"]')
      ||document.visibilityState!=='visible'||!document.hasFocus())return
    // Bind the existing room route to the exact received event; never open an arbitrary server href.
    onViewSource(source.seq,source.event_id)
  }
  const name=plan?.agent?.current_name||plan?.agent?.name_snapshot||t('retry.agentUnknown')
  const blocked=retryRoomBlocked(identity,roomId),pending=workspace.pending.has(roomId)
  return <section ref={root} className="targeted-retry stack stack--tight" aria-label={t(toolReview?'retry.toolReview.open':'retry.title')}>
    {!read && <button type="button" className="button button--secondary button--compact" disabled={!enabled} onClick={load}><RotateCcw size={15} aria-hidden="true"/>{t(toolReview?'retry.toolReview.open':'retry.open')}</button>}
    {read?.phase==='pending'&&<p role="status">{t('retry.loading')}</p>}
    {read?.phase==='error'&&<p role="alert" className="notice notice--warning">{t('retry.loadFailure')}</p>}
    {read&&<button type="button" className="button button--secondary button--compact" disabled={!enabled||read.phase==='pending'||pending} onClick={load}>{t('retry.refresh')}</button>}
    {read?.phase!=='ready'&&plan&&<p className="metadata">{t('retry.recovery.lastPlan')}</p>}
    {plan&&<div className="stack stack--tight">
    <div><strong>{t(toolReview?'retry.toolReview.open':'retry.title')}</strong><p className="section-copy">{t(toolReview?'retry.toolReview.explanation':'retry.explanation')}</p></div>
    {plan.source_request && <div className="targeted-retry__preview"><div className="page-actions"><span className="metadata">{t('retry.source')}</span>
      {canViewSource&&<button type="button" className="text-link" onClick={viewSource}>{t('retry.viewSource')} · #{formatNumber(plan.source_request.seq)}</button>}</div>
      <p>{plan.source_request.preview}</p><p className="field-hint">{t('retry.sourcePreviewHelp')}</p></div>}
    {plan.agent && <p>{t('retry.agent', { name })}</p>}
    {teammateRecovery && plan.eligible && plan.teammate_recovery && <p className="notice notice--info">{t(plan.teammate_recovery.version===2?'retry.teammateRecovery.chainSummary':'retry.teammateRecovery.summary', { nested:formatNumber(plan.teammate_recovery.nested_count), chars:formatNumber(plan.teammate_recovery.total_chars), attempt:formatNumber(plan.teammate_recovery.version===2?plan.teammate_recovery.attempt_number:1) })}</p>}
    {plan.current_model && <p className="metadata">{t('retry.currentModel', { provider: plan.current_model.provider_kind, model: plan.current_model.model_id })}</p>}
    {plan.limits && <details><summary>{t('retry.details')}</summary><dl>
      <dt>{t('chat.policy.rounds')}</dt><dd>{formatNumber(plan.limits.max_rounds)}</dd>
      <dt>{t('chat.policy.tools')}</dt><dd>{formatNumber(plan.limits.max_tool_calls)}</dd>
      <dt>{t('chat.policy.executor')}</dt><dd>{formatNumber(plan.limits.max_executor_seconds)}</dd>
    </dl></details>}
    {!plan.eligible&&plan.blocked_reason&&<p role="status" className="notice notice--warning">{t(retryBlockedKeys[plan.blocked_reason])}</p>}
    {read?.consumed&&<p className="field-hint">{t('retry.recovery.consumed')}</p>}
    {blocked&&!pending&&<p role="status" className="notice notice--warning">{t('retry.recovery.originalFirst')}</p>}
    {plan.eligible&&plan.plan_hash&&(!teammateRecovery||validTeammateRecovery(plan))&&<button type="button" className="button button--primary button--compact" disabled={!enabled||blocked||read?.phase!=='ready'||read.consumed} onClick={start}>{pending?t('retry.starting'):t(toolReview?'retry.toolReview.send':'retry.start')}</button>}
    </div>}
    {showHistory&&<RequestRetryHistory identity={identity} roomId={roomId} failureId={failureEventId} onViewEvent={onViewSource} teammateRecovery={teammateRecovery}/>}
  </section>
}
