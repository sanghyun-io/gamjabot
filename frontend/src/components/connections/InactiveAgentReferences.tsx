import InactiveReferenceHistory from './InactiveReferenceHistory'
import {directoryViewGeneration} from '../../lib/directoryViewMemory'
import {inactiveReferenceOwnerCurrent,sendInactiveReference} from '../../lib/inactiveReferenceActions'
import {beginInactiveReference,inactiveReferenceBusy,inactiveReferencesRead,inactiveReferenceVersion,readInactiveReferences,sameInactiveReference as same,subscribeInactiveReferences} from '../../lib/inactiveReferenceWorkspace'
import {useEffect,useId,useMemo,useRef,useState,useSyncExternalStore} from 'react'
import {useInfiniteQuery,useQueryClient,type InfiniteData} from '@tanstack/react-query'
import {inactiveAgentReferencesApi,type InactiveAgentReference,type InactiveAgentReferencePage} from '../../api/agents'
import DirectorySearch,{directoryMatcher} from '../DirectorySearch'
import {useDialogFocus} from '../../lib/useDialogFocus'
import {useI18n} from '../../i18n'
export default function InactiveAgentReferences({connection,identity,canUse,onClose}:{connection:{id:number;label:string};identity:string;canUse:()=>boolean;onClose:()=>void}){
 const {t}=useI18n(),cache=useQueryClient(),root=useRef<HTMLDivElement>(null),title=useId(),help=useId(),mounted=useRef(true),generation=useRef(directoryViewGeneration()).current
 useSyncExternalStore(subscribeInactiveReferences,inactiveReferenceVersion)
 const workspace=readInactiveReferences(identity),busy=inactiveReferenceBusy(identity),blocked=new Set(workspace.needsRead)
 const [query,setQuery]=useState(''),[review,setReview]=useState<InactiveAgentReference|null>(null),[ack,setAck]=useState(false)
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
 const current=()=>mounted.current&&canUse()&&inactiveReferenceOwnerCurrent(identity,generation),close=()=>{if(current())onClose()}
 useDialogFocus(root,close)
 const key=['inactive-agent-references',identity,connection.id] as const
 const list=useInfiniteQuery({queryKey:key,enabled:canUse(),retry:false,refetchOnWindowFocus:false,refetchOnReconnect:false,initialPageParam:0,
  queryFn:async({pageParam})=>{if(!current())throw Error('Owner changed');const value=await inactiveAgentReferencesApi.list(connection.id,pageParam);if(!current())throw Error('Owner changed');return value},
  getNextPageParam:page=>page.next_after_id??undefined})
 const rows=useMemo(()=>[...new Map((list.data?.pages.flatMap(page=>page.items)||[]).filter(row=>row.is_active===false&&row.connection_id===connection.id).map(row=>[row.id,row])).values()],[list.data,connection.id])
 const shown=useMemo(()=>{const match=directoryMatcher(query);return rows.filter(row=>match([String(row.id),`#${row.id}`,row.name,row.model_id,row.model],row.id))},[rows,query])
 const ready=list.isSuccess&&!list.isFetching&&!list.isError&&!cache.getQueryState(key)?.isInvalidated
 const prepare=(row:InactiveAgentReference)=>{if(!current()||inactiveReferenceBusy(identity)||!ready||blocked.has(row.id))return;setReview(Object.freeze({...row}));setAck(false)}
 const readCurrent=async()=>{
  if(!current()||inactiveReferenceBusy(identity)||list.isFetching)return
  const result=await list.refetch()
  if(current()&&!result.isError){setReview(null);setAck(false);inactiveReferencesRead(identity,generation,connection.id)}
 }
 const detach=()=>{
  if(!current()||busy||!review||!ack||!ready||blocked.has(review.id))return
  const snapshot=review,state=cache.getQueryState<InfiniteData<InactiveAgentReferencePage>>(key)
  const observed=state?.data?.pages.flatMap(page=>page.items).find(row=>row.id===snapshot.id)
  if(state?.status!=='success'||state.fetchStatus!=='idle'||state.isInvalidated||!same(snapshot,observed)||observed?.is_active!==false||observed.connection_id!==connection.id)return
  const original=beginInactiveReference(identity,generation,connection,snapshot)
  if(!original)return
  setAck(false);setReview(null)
  void sendInactiveReference(identity,generation,original)
 }
 return <div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack" style={{overflowWrap:'anywhere'}}>
  <h2 id={title} className="page-title">{t('inactiveRefs.title')} · {connection.label} · #{connection.id}</h2>
  <p id={help}>{t('inactiveRefs.help')}</p><p className="field-hint">{t('inactiveHistory.limit')}</p>
  <div className="page-actions"><button type="button" data-dialog-initial className="button button--secondary" onClick={close}>{t('common.close')}</button><button type="button" className="button button--secondary" disabled={busy||list.isFetching} onClick={()=>void readCurrent()}>{t('inactiveRefs.read')}</button></div>
  {list.isPending&&<p role="status">{t('common.loading')}</p>}{list.isError&&<p role="alert" className="notice notice--warning">{t('inactiveRefs.readFailure')}</p>}
  {list.data&&(list.isError||list.isFetching)&&<p className="metadata">{t('editor.previousObservation')}</p>}
  <DirectorySearch exactId value={query} onChange={setQuery} hint={t('inactiveRefs.search')} shown={shown.length} total={rows.length} known={!!list.data} loading={list.isFetching} preventSubmit disabled={busy}/>
  <p className="field-hint">{t('inactiveRefs.receivedOnly')}</p>
  {!list.isPending&&!shown.length&&<p role="status">{t('inactiveRefs.noMatch')}</p>}
  <div className="stack stack--tight">{shown.map(row=><div key={row.id} className="notice stack stack--tight"><strong>#{row.id} · {row.name}</strong><p>{row.model_id||row.model}</p><button type="button" className="text-link" disabled={busy||!ready||blocked.has(row.id)} onClick={()=>prepare(row)}>{t('inactiveRefs.prepare')}</button></div>)}</div>
  {list.hasNextPage&&<button type="button" className="button button--secondary" disabled={busy||list.isFetching} onClick={()=>{if(current()&&!inactiveReferenceBusy(identity))void list.fetchNextPage()}}>{t('inactiveRefs.more')}</button>}
  {review&&<section className="notice notice--warning stack stack--tight"><strong>#{review.id} · {review.name}</strong><p>{connection.label} · #{connection.id} · {review.model_id||review.model}</p><p>{t('inactiveRefs.review')}</p>
   {!same(review,rows.find(row=>row.id===review.id))&&<p role="status">{t('inactiveRefs.changed')}</p>}
   <label className="check-row"><input type="checkbox" checked={ack} disabled={busy||!ready||blocked.has(review.id)||!same(review,rows.find(row=>row.id===review.id))} onChange={event=>{if(current()&&!inactiveReferenceBusy(identity))setAck(event.target.checked)}}/>{t('inactiveRefs.ack')}</label>
   <button type="button" className="button button--danger" disabled={busy||!ack||!ready||blocked.has(review.id)||!same(review,rows.find(row=>row.id===review.id))} onClick={()=>void detach()}>{t(busy?'common.saving':'inactiveRefs.apply')}</button>
  </section>}
  <InactiveReferenceHistory identity={identity} connectionId={connection.id}/>
 </div></div>
}
