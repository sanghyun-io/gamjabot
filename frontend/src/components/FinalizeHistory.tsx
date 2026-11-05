import { useRef,useSyncExternalStore } from 'react'
import HistoryEventLink, { type HistoryEventNavigation } from './HistoryEventLink'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { finalizeBlockedKeys,finalizeOwnerCurrent,requestFinalization } from '../lib/finalizeActions'
import { finalizeKeyRejected,finalizeReceipt,finalizeVersion,readFinalizers,stopFinalizeWait,subscribeFinalizers,type FinalizeCommand } from '../lib/finalizeWorkspace'
function OriginalFinalize({identity,command,onViewEvent}:{identity:string;command:FinalizeCommand;onViewEvent?:HistoryEventNavigation}){
 const {t,formatDateTime}=useI18n(),generation=useRef(directoryViewGeneration()).current,valid=()=>finalizeOwnerCurrent(identity,generation)
 const family=finalizeReceipt(identity,command.key),keyRejected=family?undefined:finalizeKeyRejected(identity,command.key),result=command.result||family?.result
 const canRead=()=>{const current=readFinalizers(identity).commands.find(value=>value.id===command.id);return valid()&&current===command&&(current.result||finalizeReceipt(identity,command.key)?.result)===result}
 const name=command.plan.agent?.current_name||command.plan.agent?.name_snapshot||t('inspector.agentFallback',{id:command.agentId})
 const resend=()=>{if(valid()&&window.confirm(t('finalize.recovery.replayConfirm'))&&valid())requestFinalization(identity,generation,command.roomId,command.sourceId,command.agentId,'',command.id)}
 return <article className={`notice stack stack--tight ${command.phase==='accepted'?'notice--success':command.phase==='pending'?'notice--info':'notice--warning'}`}>
  <strong>{t('finalize.chosen',{name})}</strong><p className="metadata">{t('projects.workspace.started',{time:formatDateTime(command.startedAt,{dateStyle:'medium',timeStyle:'short'})})}</p>
  <p role={command.phase==='unknown'||command.phase==='rejected'?'alert':'status'}>{t(command.phase==='pending'?'finalize.recovery.pending':command.phase==='accepted'?'finalize.recovery.accepted':command.phase==='unknown'?'retry.recovery.unknown':'editor.rejected')}</p>
  {command.receivedAt&&<p className="metadata">{t('projects.workspace.received',{time:formatDateTime(command.receivedAt,{dateStyle:'medium',timeStyle:'short'})})}</p>}
  {command.phase==='unknown'&&family&&<p>{t('retry.recovery.familyReceipt')}</p>}{command.result?.replayed&&<p>{t('finalize.recovery.replayed')}</p>}
  {result&&<HistoryEventLink roomId={command.roomId} seq={result.finalize_seq} eventId={result.finalize_event_id} canUse={canRead} onNavigate={onViewEvent}>{t('finalize.recovery.openReceipt')}</HistoryEventLink>}
  <HistoryEventLink roomId={command.roomId} seq={command.plan.source_request?.seq} eventId={command.sourceId} canUse={canRead} onNavigate={onViewEvent}>{t('finalize.goSource')}</HistoryEventLink>
  <p className="metadata">{t('memory.finalizer')}</p>
  {command.phase==='pending'&&<button type="button" className="button button--secondary button--compact" onClick={()=>{if(valid()&&window.confirm(t('projects.workspace.stopConfirm'))&&valid())stopFinalizeWait(identity,generation,command.id)}}>{t('projects.workspace.stopWaiting')}</button>}
  {command.phase==='unknown'&&!family&&!keyRejected&&<><p>{t('finalize.recovery.replayHelp')}</p><button type="button" className="button button--secondary button--compact" disabled={readFinalizers(identity).pending.has(command.roomId)} onClick={resend}>{t('finalize.recovery.replay')}</button></>}
  {command.code==='finalize_plan_stale'&&<p>{t('finalize.stale')}</p>}{command.code==='finalize_blocked'&&command.reason&&<p>{t(finalizeBlockedKeys[command.reason])}</p>}{keyRejected&&<p>{t('retry.recovery.keyRejected')}</p>}
  <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p>{t('finalize.recovery.contract')}</p>{command.retryOf&&<p>{t('retry.recovery.originalAttempt')}</p>}<pre className="technical">{JSON.stringify({room_id:command.roomId,source_request_event_id:command.sourceId,body:command.body,received_plan:command.plan,received_receipt:command.result},null,2)}</pre>{command.code&&<p className="technical">{command.code}</p>}</details>
 </article>
}
export default function FinalizeHistory({identity,roomId,sourceId,onViewEvent}:{identity:string;roomId:number;sourceId?:string;onViewEvent?:HistoryEventNavigation}){
 const {t}=useI18n();useSyncExternalStore(subscribeFinalizers,finalizeVersion)
 const commands=readFinalizers(identity).commands.filter(command=>command.roomId===roomId&&(!sourceId||command.sourceId===sourceId)),latest=commands.at(-1)
 if(!latest)return null
 const important=new Set(commands.filter(command=>command.phase==='pending'||command.phase==='unknown'&&!finalizeReceipt(identity,command.key)&&!finalizeKeyRejected(identity,command.key)).map(command=>command.id));important.add(latest.id)
 return <section className="stack stack--tight" aria-label={t('finalize.recovery.history')}><p className="field-hint">{t('finalize.recovery.retained')}</p>{commands.filter(command=>important.has(command.id)).map(command=><OriginalFinalize key={command.id} identity={identity} command={command} onViewEvent={onViewEvent}/>)}{commands.some(command=>!important.has(command.id))&&<details className="compact-details"><summary>{t('projects.workspace.previous')}</summary>{commands.filter(command=>!important.has(command.id)).map(command=><OriginalFinalize key={command.id} identity={identity} command={command} onViewEvent={onViewEvent}/>)}</details>}</section>
}
