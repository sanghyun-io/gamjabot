import FeatureTable from './FeatureTable'
import FeatureCheckHistory from './FeatureCheckHistory'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useSyncExternalStore } from 'react'
import { useI18n } from '../../i18n'
import { useAuthStore } from '../../stores/authStore'
import { connectionsApi } from '../../api/connections'
import { directoryViewGeneration } from '../../lib/directoryViewMemory'
import { normalizeLocalUrl } from '../../lib/localSetupWorkspace'
import { featureChecksVersion, subscribeFeatureChecks, readFeatureChecks, featureCheckBusy, startFeatureCheck, updateFeatureCheck } from '../../lib/localFeatureChecks'

export { default as FeatureTable } from './FeatureTable'
export default function ModelFeatures({connectionId,modelId,identity,revision='',enabled=true}:{connectionId:number;modelId:string;identity:string;revision?:string;enabled?:boolean}) {
 const {t}=useI18n(),cache=useQueryClient(),key=['model-features',identity,connectionId,modelId,revision]
 const generation=useRef(directoryViewGeneration()).current,mounted=useRef(false)
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
 useSyncExternalStore(subscribeFeatureChecks,featureChecksVersion)
 const checks=readFeatureChecks(identity,connectionId,modelId,revision),latest=checks[checks.length-1],busy=featureCheckBusy(identity)
 const valid=()=>{const s=useAuthStore.getState();return !!s.token&&`${s.user?.id||0}:${s.epoch}`===identity&&generation===directoryViewGeneration()}
 const query=useQuery({queryKey:key,retry:false,queryFn:async()=>{if(!valid())throw Error('Owner changed');const r=await connectionsApi.features(connectionId,modelId);if(!valid()||r.model_id!==modelId)throw Error('Feature target changed');return r}})
 const read=()=>{if(valid()&&!query.isFetching)void query.refetch({cancelRefetch:false})}
 const check=async()=>{
  if(!valid()||!enabled||query.isFetching||query.isError||busy||!query.data?.local)return
  const baseUrl=normalizeLocalUrl(query.data.base_url||'');if(!baseUrl)return
  if(latest?.status==='unknown'&&!window.confirm(t('localModels.session.newCheckConfirm')))return
  if(!valid())return
  const record=startFeatureCheck(identity,generation,{connectionId,modelId,revision,baseUrl});if(!record)return
  let sent=false
  try {
   const revisions=await connectionsApi.revisions(connectionId)
   if(!valid())return
   if(!mounted.current){updateFeatureCheck(identity,generation,record,'not_sent',undefined,'localModels.featureSession.deferred');return}
   if(`${revisions.config_revision}:${revisions.auth_grant_revision}`!==revision){updateFeatureCheck(identity,generation,record,'not_sent',undefined,'localModels.session.configurationChanged');return}
   updateFeatureCheck(identity,generation,record,'checking');sent=true
   const report=await connectionsApi.checkFeatures(connectionId,modelId)
   if(!valid())return
   if(!report.local||report.model_id!==modelId||normalizeLocalUrl(report.base_url||'')!==baseUrl){updateFeatureCheck(identity,generation,record,'unknown',undefined,'localModels.session.checkUnknown');return}
   updateFeatureCheck(identity,generation,record,'done',report)
   // Set only this original query target; no navigation, form change or automatic generation.
   cache.setQueryData(key,report)
  }catch{if(valid())updateFeatureCheck(identity,generation,record,sent?'unknown':'not_sent',undefined,sent?'localModels.session.checkUnknown':'localModels.session.preflightFailed')}
 }
 return <section className="stack stack--tight model-feature-panel">
  <h4>{t('localModels.supportedFeatures')}</h4>
  
  
  {query.data&&<>{(query.isFetching||query.isError)&&<p className="field-hint">{t('localModels.featureSession.previousRead')}</p>}<FeatureTable report={query.data}/></>}
  {query.isFetching&&<p role="status">{t('common.loading')}</p>}
  {query.isError&&<p role="alert" className="notice notice--warning">{t('localModels.reportLoadFailed')}</p>}
  <button type="button" className="button button--secondary button--compact" disabled={query.isFetching} onClick={read}>{t('localModels.session.readReport')}</button>
  {query.data?.local&&<><p className="field-hint">{t('localModels.probeBudget')}</p><button type="button" className="button button--secondary" disabled={query.isFetching||query.isError||busy||!enabled} onClick={()=>void check()}>{t(busy?'localModels.checking':'localModels.check')}</button></>}
  {busy&&<p role="status">{t('localModels.featureSession.pending')}</p>}
  {latest?.notice&&<p role="alert" className="notice notice--warning">{t(latest.notice)}</p>}
  {latest?.status==='unknown'&&<p className="field-hint">{t('localModels.session.readNotReceipt')}</p>}
  {checks.length>0&&<FeatureCheckHistory key={JSON.stringify([identity,connectionId,modelId,revision])} checks={checks}/>}
 </section>
}
