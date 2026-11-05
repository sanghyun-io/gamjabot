import {useRef,useSyncExternalStore} from 'react'
import {connectionsApi} from '../../api/connections'
import {useI18n} from '../../i18n'
import {useAuthStore} from '../../stores/authStore'
import {directoryViewGeneration} from '../../lib/directoryViewMemory'
import {connectionSavesVersion,readConnectionSaves,subscribeConnectionSaves,type ConnectionSaveRecord} from '../../lib/connectionSaveWorkspace'
import ConnectionSaveNotice,{type ConnectionSaveFeedback} from '../ConnectionSaveNotice'
import CommandHistoryArchive from '../CommandHistoryArchive'
export default function ConnectionSaveHistory({identity,onShowSaved,findingSaved}:{identity:string;onShowSaved:(feedback:ConnectionSaveFeedback)=>void;findingSaved:boolean}){
 const {t,formatDateTime}=useI18n(),generation=useRef(directoryViewGeneration()).current
 useSyncExternalStore(subscribeConnectionSaves,connectionSavesVersion)
 const workspace=readConnectionSaves(identity),rows=workspace.requests.filter(row=>row.original.attempt!==workspace.active?.attempt),latest=rows.at(-1)
 if(!workspace.requests.length)return null
 const current=()=>{const owner=useAuthStore.getState();return !!owner.token&&`${owner.user?.id||0}:${owner.epoch}`===identity&&directoryViewGeneration()===generation}
 const inspect=async(row:ConnectionSaveRecord)=>{
  if(!current())throw Error('Owner changed')
  const result=await connectionsApi.list()
  if(!current())throw Error('Owner changed')
  const target=row.original.target
  return result.filter(value=>target.id!==null?value.id===target.id:value.label===target.label&&(value.provider_id||value.provider_kind)===target.providerId&&value.default_model_id===target.modelId).map(value=>({id:value.id,name:value.label,observation:{provider_id:value.provider_id,provider_kind:value.provider_kind,default_model_id:value.default_model_id,enabled:value.enabled,has_secret:value.has_secret}}))
 }
 const phase=(row:ConnectionSaveRecord)=>row.feedback.phase==='saving'?'pending' as const:row.feedback.phase==='success'?'succeeded' as const:row.feedback.phase
 const pinned=new Set(rows.filter(row=>row.feedback.phase==='saving'||row.feedback.phase==='unknown').map(row=>row.id));if(latest)pinned.add(latest.id)
 const content=(row:ConnectionSaveRecord)=><div className="notice stack stack--tight" style={{overflowWrap:'anywhere'}}><strong>{row.original.target.id!==null&&`#${row.original.target.id} · `}{row.original.target.label}</strong><p className="metadata">{formatDateTime(row.startedAt,{dateStyle:'medium',timeStyle:'medium'})}</p><ConnectionSaveNotice history feedback={row.feedback} findingSaved={findingSaved} onShowSaved={()=>onShowSaved(row.feedback)} onInspect={()=>inspect(row)} onDismiss={()=>{}}/>{row.finishedAt&&<p className="metadata">{t('inactiveHistory.finished',{time:formatDateTime(row.finishedAt,{dateStyle:'medium',timeStyle:'medium'})})}</p>}<details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p className="technical">{row.id}</p><p>{row.original.target.providerId} · {row.original.target.modelId}</p></details></div>
 const older=rows.filter(row=>!pinned.has(row.id)).reverse()
 return <section className="stack stack--tight" aria-label={t('connectionSaveHistory.title')}><h3 className="section-title">{t('connectionSaveHistory.title')}</h3><p className="field-hint">{t('connectionSaveHistory.scope')}</p>{rows.filter(row=>pinned.has(row.id)).map(row=><div key={row.id}>{content(row)}</div>)}{older.length>0&&<CommandHistoryArchive key={identity} title={t('projects.workspace.previous')} items={older.map(row=>({id:row.id,phase:phase(row),search:[row.original.target.label,String(row.original.target.id??''),row.original.target.providerId,row.original.target.modelId,row.id,row.startedAt,formatDateTime(row.startedAt,{dateStyle:'medium',timeStyle:'short'})],content:content(row)}))}/>}</section>
}
