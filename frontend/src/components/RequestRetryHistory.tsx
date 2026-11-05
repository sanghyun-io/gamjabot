import { useRef, useSyncExternalStore } from 'react'
import HistoryEventLink, { type HistoryEventNavigation } from './HistoryEventLink'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { requestTargetedRetry, retryBlockedKeys, retryOwnerCurrent, validTeammateRecovery } from '../lib/requestRetryActions'
import { readRequestRetries, requestRetryVersion, retryKeyRejected, retryReceipt, stopRetryWait, subscribeRequestRetries, type RetryCommand } from '../lib/requestRetryWorkspace'
function OriginalRetry({ identity, command, onViewEvent, teammateRecovery=false }: { identity: string; command: RetryCommand; onViewEvent?: HistoryEventNavigation; teammateRecovery?:boolean }) {
  const { t,formatDateTime }=useI18n(),generation=useRef(directoryViewGeneration()).current,valid=()=>retryOwnerCurrent(identity,generation)
  const family=retryReceipt(identity,command.key),keyRejected=family?undefined:retryKeyRejected(identity,command.key),result=command.result || family?.result,name=command.plan.agent?.current_name || command.plan.agent?.name_snapshot || t('retry.agentUnknown')
  const canRead=()=>{const current=readRequestRetries(identity).commands.find(value=>value.id===command.id);return valid()&&current===command&&(current.result||retryReceipt(identity,command.key)?.result)===result}
  const replay=()=>{
    if(!valid() || (teammateRecovery && !validTeammateRecovery(command.plan)) || !window.confirm(t('retry.recovery.replayConfirm')))return
    if(valid())requestTargetedRetry(identity,generation,command.roomId,command.failureId,'',command.id)
  }
  return <article className={`notice stack stack--tight ${command.phase==='accepted'?'notice--success':command.phase==='pending'?'notice--info':'notice--warning'}`}>
    <strong>{t('retry.agent',{name})}</strong><p className="metadata">{t('projects.workspace.started',{time:formatDateTime(command.startedAt,{dateStyle:'medium',timeStyle:'short'})})}</p>
    <p role={command.phase==='unknown'||command.phase==='rejected'?'alert':'status'}>{t(command.phase==='pending'?'retry.recovery.pending':command.phase==='accepted'?'retry.recovery.accepted':command.phase==='unknown'?'retry.recovery.unknown':'editor.rejected')}</p>
    {command.receivedAt && <p className="metadata">{t('projects.workspace.received',{time:formatDateTime(command.receivedAt,{dateStyle:'medium',timeStyle:'short'})})}</p>}
    {command.phase==='unknown' && family && <p>{t('retry.recovery.familyReceipt')}</p>}
    {command.result?.replayed && <p>{t('retry.recovery.replayed')}</p>}
    {result && <HistoryEventLink roomId={command.roomId} seq={result.retry_seq} eventId={result.retry_event_id} canUse={canRead} onNavigate={onViewEvent}>{t('retry.recovery.openReceipt')}</HistoryEventLink>}
    <HistoryEventLink roomId={command.roomId} seq={command.plan.failure_seq} eventId={command.failureId} canUse={canRead} onNavigate={onViewEvent}>{t('retry.recovery.openFailure')}</HistoryEventLink>
    {command.phase==='pending' && <button type="button" className="button button--secondary button--compact" onClick={()=>{if(valid()&&window.confirm(t('projects.workspace.stopConfirm'))&&valid())stopRetryWait(identity,generation,command.id)}}>{t('projects.workspace.stopWaiting')}</button>}
    {command.phase==='unknown'&&!family&&!keyRejected && (!teammateRecovery||validTeammateRecovery(command.plan)) && <><p>{t('retry.recovery.replayHelp')}</p><button type="button" className="button button--secondary button--compact" disabled={readRequestRetries(identity).pending.has(command.roomId)} onClick={replay}>{t('retry.recovery.replay')}</button></>}
    {command.code==='request_retry_plan_stale' && <p>{t('retry.stale')}</p>}
    {command.code==='request_retry_blocked' && command.reason && <p>{t(retryBlockedKeys[command.reason])}</p>}
    {keyRejected && <p>{t('retry.recovery.keyRejected')}</p>}
    <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p>{t('retry.recovery.contract')}</p>{command.retryOf && <p>{t('retry.recovery.originalAttempt')}</p>}<pre className="technical">{JSON.stringify({room_id:command.roomId,failure_event_id:command.failureId,body:{plan_hash:command.plan.plan_hash},received_plan:command.plan,received_receipt:command.result},null,2)}</pre>{command.code && <p className="technical">{command.code}</p>}</details>
  </article>
}
export default function RequestRetryHistory({ identity,roomId,failureId,onViewEvent,teammateRecovery=false }: { identity:string;roomId:number;failureId?:string;onViewEvent?:HistoryEventNavigation;teammateRecovery?:boolean }) {
  const { t }=useI18n();useSyncExternalStore(subscribeRequestRetries,requestRetryVersion)
  const commands=readRequestRetries(identity).commands.filter(command=>command.roomId===roomId&&(!failureId||command.failureId===failureId)),latest=commands[commands.length-1]
  if(!latest)return null
  const important=new Set(commands.filter(command=>command.phase==='pending'||command.phase==='unknown'&&!retryReceipt(identity,command.key)&&!retryKeyRejected(identity,command.key)).map(command=>command.id));important.add(latest.id)
  return <section className="stack stack--tight" aria-label={t('retry.recovery.history')}>{commands.some(command=>command.phase!=='accepted') && <p className="field-hint">{t('retry.recovery.retained')}</p>}{commands.filter(command=>important.has(command.id)).map(command=>command.phase==='accepted'?<details key={command.id} className="compact-details"><summary>{t('retry.recovery.accepted')}</summary><p className="field-hint">{t('retry.recovery.retained')}</p><OriginalRetry identity={identity} command={command} onViewEvent={onViewEvent} teammateRecovery={teammateRecovery}/></details>:<OriginalRetry key={command.id} identity={identity} command={command} onViewEvent={onViewEvent} teammateRecovery={teammateRecovery}/>)}{commands.some(command=>!important.has(command.id)) && <details className="compact-details"><summary>{t('projects.workspace.previous')}</summary>{commands.filter(command=>!important.has(command.id)).map(command=><OriginalRetry key={command.id} identity={identity} command={command} onViewEvent={onViewEvent} teammateRecovery={teammateRecovery}/>)}</details>}</section>
}
