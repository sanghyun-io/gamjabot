import {useSyncExternalStore} from 'react'
import {useI18n} from '../../i18n'
import {connectionRemovalVersion,readConnectionRemovals,subscribeConnectionRemovals,type ConnectionRemovalRequest} from '../../lib/connectionRemovalWorkspace'
import CommandHistoryArchive from '../CommandHistoryArchive'
export default function ConnectionRemovalHistory({identity}:{identity:string}){
 const {t,formatDateTime}=useI18n();useSyncExternalStore(subscribeConnectionRemovals,connectionRemovalVersion)
 const requests=readConnectionRemovals(identity),latest=requests.at(-1)
 if(!latest)return null
 const pinned=new Set(requests.filter(row=>row.phase==='sending'||row.phase==='unknown').map(row=>row.id));pinned.add(latest.id)
 const phase=(row:ConnectionRemovalRequest)=>row.phase==='sending'?'pending' as const:row.phase==='succeeded'?'succeeded' as const:row.phase==='unknown'?'unknown' as const:'rejected' as const
 const label=(row:ConnectionRemovalRequest)=>row.phase==='sending'?t('connections.deleting'):row.phase==='succeeded'?t('connectionRemovalHistory.succeeded'):t(`connections.removeDialog.${row.phase}`)
 const content=(row:ConnectionRemovalRequest)=><div className={`notice stack stack--tight ${row.phase==='succeeded'?'notice--success':row.phase==='sending'?'notice--info':'notice--warning'}`} style={{overflowWrap:'anywhere'}}><strong>#{row.target.id} · {row.target.label}</strong><p>{row.target.provider_id||row.target.provider_kind} · {row.target.default_model_id||t('connections.removeDialog.noModel')}</p><p className="metadata">{formatDateTime(row.startedAt,{dateStyle:'medium',timeStyle:'medium'})}</p><p role={row.phase==='unknown'?'alert':'status'}>{label(row)}</p>{row.finishedAt&&<p className="metadata">{t('inactiveHistory.finished',{time:formatDateTime(row.finishedAt,{dateStyle:'medium',timeStyle:'medium'})})}</p>}<details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p className="technical">{row.id}</p><p>{t('connectionRemovalHistory.original',{config:row.target.config_revision??'',auth:row.target.auth_grant_revision??''})}</p></details></div>
 const older=requests.filter(row=>!pinned.has(row.id)).reverse()
 return <section className="stack stack--tight" aria-label={t('connectionRemovalHistory.title')}><h3 className="section-title">{t('connectionRemovalHistory.title')}</h3><p className="field-hint">{t('connectionRemovalHistory.scope')}</p>{requests.filter(row=>pinned.has(row.id)).map(row=><div key={row.id}>{content(row)}</div>)}{older.length>0&&<CommandHistoryArchive key={identity} title={t('projects.workspace.previous')} items={older.map(row=>({id:row.id,phase:phase(row),search:[row.target.label,String(row.target.id),`#${row.target.id}`,row.target.provider_id||row.target.provider_kind,row.target.default_model_id||'',row.id,row.startedAt,formatDateTime(row.startedAt,{dateStyle:'medium',timeStyle:'short'})],content:content(row)}))}/>}</section>
}
