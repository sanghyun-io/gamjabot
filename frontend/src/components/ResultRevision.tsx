import { useEffect, useRef, useState } from 'react'
import { roomResultsApi, type ResultDetail, type RevisionRef, type RevisionStatus } from '../api/roomResults'
import { eventsApi } from '../api/events'
import { canonicalEventId } from '../lib/peerReply'
import { resultVersionHref } from '../lib/resultLink'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'

type Operation={key:string;room:number;result:string;version:number;agent:number;instruction:string;phase:'sending'|'pending'|'unknown'|'missing'|'failed'|'cancelled'|'conflict'|'unavailable'|'tooLong'|'completed';root?:RevisionRef;resultRef?:NonNullable<RevisionStatus['result_ref']>;finalizeAttempted:boolean}
const operations=new Map<string,Operation>()
useAuthStore.subscribe((a,b)=>{if(!a.token||a.epoch!==b.epoch||a.user?.id!==b.user?.id)operations.clear()})
const validRef=(r:RevisionRef|undefined,room:number,actor:'user'|'agent',id:number)=>!!r&&r.room_id===room&&canonicalEventId(r.event_id)&&Number.isSafeInteger(r.seq)&&r.seq>0&&r.actor_type===actor&&r.actor_id===id&&r.kind==='message'&&r.href===`/chat/${room}?seq=${r.seq}&event=${r.event_id}`
export default function ResultRevision({value,ownerId,identity,canUse,canStart,active,onNavigate,onOpenVersion,onSaved}:{value:ResultDetail;ownerId:number;identity:string;canUse:()=>boolean;canStart:boolean;active:boolean;onNavigate:(seq:number,id:string)=>void;onOpenVersion:(id:string,version:number)=>void;onSaved:()=>void}){
 const {t}=useI18n(),id=`${identity}:${value.room_id}:${value.result_id}`,[open,setOpen]=useState(false),[instruction,setInstruction]=useState(''),[op,setState]=useState<Operation|null>(()=>operations.get(id)||null),[checked,setChecked]=useState(()=>operations.has(id)),[checkFailed,setCheckFailed]=useState(false),lock=useRef(false),alive=useRef(false),checkSerial=useRef(0)
 const current=()=>alive.current&&canUse(),setOp=(x:Operation|null)=>{if(x)operations.set(id,x);else operations.delete(id);if(current())setState(x)}
 useEffect(()=>{alive.current=true;setState(operations.get(id)||null);return()=>{alive.current=false;checkSerial.current++;const p=operations.get(id);if(p?.phase==='sending')operations.set(id,{...p,phase:'unknown'})}},[id])
 const verify=async(request:Operation,receipt:RevisionStatus)=>{
  if(!receipt||receipt.room_id!==request.room||receipt.result_id!==request.result||receipt.expected_current_version!==request.version||receipt.author_agent_id!==request.agent||receipt.instruction!==request.instruction||!validRef(receipt.root_ref,request.room,'user',ownerId)||request.root&&request.root.event_id!==receipt.root_ref.event_id||!['pending','ready','completed','failed','cancelled','conflict','unknown'].includes(receipt.state))throw Error('Revision mismatch')
  const root=(await eventsApi.list(request.room,receipt.root_ref.seq-1,1))[0],meta=root?.payload?.page_revision as Record<string,unknown>|undefined
  if(!current()||!root||root.room_id!==request.room||root.seq!==receipt.root_ref.seq||root.event_id!==receipt.root_ref.event_id||root.actor_type!=='user'||root.actor_id!==ownerId||root.kind!=='message'||meta?.version!==1||meta.result_id!==request.result||meta.base_version!==request.version||meta.agent_id!==request.agent||meta.instruction!==request.instruction)throw Error('Revision root mismatch')
  const source=receipt.state==='ready'?receipt.source_ref:receipt.state==='completed'?receipt.result_ref?.source_ref:undefined
  if(receipt.state==='ready'||receipt.state==='completed'){
   const sourceId=receipt.state==='ready'?receipt.source_event_id:receipt.result_ref?.source_event_id
   if(!validRef(source,request.room,'agent',request.agent)||source!.event_id!==sourceId)throw Error('Revision answer mismatch')
   const answer=(await eventsApi.list(request.room,source!.seq-1,1))[0]
   if(!current()||!answer||answer.room_id!==request.room||answer.event_id!==sourceId||answer.seq!==source!.seq||answer.actor_type!=='agent'||answer.actor_id!==request.agent||answer.kind!=='message'||answer.payload.trigger_event_id!==receipt.root_ref.event_id)throw Error('Revision answer missing')
  }
  if(receipt.state==='completed'){
   const result=receipt.result_ref
   if(!result||result.room_id!==request.room||result.result_id!==request.result||result.version!==request.version+1||result.href!==resultVersionHref(request.room,request.result,result.version))throw Error('Revision result mismatch')
   const detail=await roomResultsApi.detail(request.room,request.result,result.version)
   if(!current()||detail.room_id!==request.room||detail.result_id!==request.result||detail.version!==result.version||detail.author_agent_id!==request.agent||detail.kind!=='text'||detail.source.event_id!==result.source_event_id||detail.source.seq!==result.source_ref.seq)throw Error('Revision detail mismatch')
  }
 }
 const apply=async(request:Operation,receipt:RevisionStatus)=>{
  await verify(request,receipt);if(!current())return
  const next={...request,root:receipt.root_ref}
  if(receipt.state==='ready'&&!request.finalizeAttempted){
   const final={...next,phase:'sending' as const,finalizeAttempted:true};setOp(final)
   try{const response=await roomResultsApi.revisionFinalize(final.room,final.result,final.root!.event_id);await verify(final,response);if(!current())return;setOp({...final,phase:response.state==='completed'?'completed':response.state==='failed'?'failed':response.state==='cancelled'?'cancelled':response.state==='conflict'?'conflict':'unknown',resultRef:response.result_ref||undefined});if(response.state==='completed')onSaved()}catch(e){if(current()){const r=(e as {response?:{status?:number;data?:{detail?:{code?:string}}}}).response;setOp({...final,phase:r?.status===409&&r.data?.detail?.code==='result_version_changed'?'conflict':'unknown'})}}
   return
  }
  setOp({...next,phase:receipt.state==='ready'?'unknown':receipt.state,resultRef:receipt.result_ref||undefined});if(receipt.state==='completed')onSaved()
 }
 const read=async()=>{const request=operations.get(id);if(!current()||!request||lock.current||request.phase==='sending')return;lock.current=true
  try{const data=request.root?await roomResultsApi.revisionStatus(request.room,request.result,request.root.event_id):await roomResultsApi.revisionReceipt(request.room,request.result,request.key);await apply(request.phase==='unknown'?{...request,finalizeAttempted:false}:request,data)}catch(e){if(current()){const status=(e as {response?:{status?:number}}).response?.status;setOp({...request,phase:!request.root&&request.key&&status===404?'missing':'unknown'})}}finally{lock.current=false}}
 const checkActive=async()=>{if(!current()||!active)return;const serial=++checkSerial.current;setCheckFailed(false)
  try{const response=await roomResultsApi.revisionActive(value.room_id,value.result_id);if(!current()||serial!==checkSerial.current)return
   if(response.room_id!==value.room_id||response.result_id!==value.result_id||!Number.isSafeInteger(response.base_version)||response.base_version<1)throw Error('Active revision mismatch')
   const r=response.revision
   if(!r){setChecked(true);return}
   if(r.state==='unknown'&&(!r.instruction||!r.author_agent_id)){setChecked(false);setCheckFailed(true);return}
   if(r.expected_current_version!==response.base_version||!r.instruction||!Number.isSafeInteger(r.author_agent_id)||r.author_agent_id<1||!validRef(r.root_ref,value.room_id,'user',ownerId))throw Error('Active revision unavailable')
   const recovered:Operation={key:'',room:value.room_id,result:value.result_id,version:response.base_version,agent:r.author_agent_id,instruction:r.instruction,root:r.root_ref,phase:'unknown',finalizeAttempted:false}
   await apply(recovered,r);if(current()&&serial===checkSerial.current)setChecked(true)
  }catch{if(current()&&serial===checkSerial.current){setChecked(false);setCheckFailed(true)}}
 }
 useEffect(()=>{if(active&&!checked&&!checkFailed&&!operations.has(id))void checkActive()},[id,active,checked,checkFailed])
 useEffect(()=>{if(!op||op.phase!=='pending'||!active||!canUse())return;const timer=window.setInterval(()=>void read(),5000);return()=>window.clearInterval(timer)},[op?.phase,id,active])
 const start=async()=>{if(!current()||lock.current||operations.has(id)||!checked||!canStart||!instruction.trim()||instruction.length>4000||value.kind!=='text'||value.version!==value.current_version)return
  const request:Operation={key:crypto.randomUUID(),room:value.room_id,result:value.result_id,version:value.version,agent:value.author_agent_id,instruction,phase:'sending',finalizeAttempted:false};lock.current=true;setOp(request)
  try{await apply(request,await roomResultsApi.revisionStart(request.room,request.result,request.version,request.instruction,request.key))}catch(e){if(current()){const response=(e as {response?:{status?:number;data?:{detail?:{code?:string}}}}).response,status=response?.status,code=response?.data?.detail?.code;if(status===409&&code==='result_revision_unsettled'){setOp(null);setChecked(false);setCheckFailed(false)}else setOp({...request,phase:code==='result_revision_source_too_large'?'tooLong':code==='result_revision_author_unavailable'?'unavailable':status===409?'conflict':status&&[400,403,422].includes(status)?'failed':'unknown'})}}finally{lock.current=false}}
 const recover=async()=>{const request=operations.get(id);if(!current()||!request||request.phase!=='missing'||!request.key||request.root||lock.current)return;lock.current=true;setOp({...request,phase:'sending'})
  try{await apply(request,await roomResultsApi.revisionStart(request.room,request.result,request.version,request.instruction,request.key))}catch{if(current())setOp({...request,phase:'unknown'})}finally{lock.current=false}}
 const reset=()=>{if(!current()||!op||!['failed','cancelled','conflict','unavailable','tooLong','completed'].includes(op.phase))return;setOp(null);setInstruction('');setOpen(false)}
 return <section className="stack stack--tight">
  {!op&&!checked&&<p role={checkFailed?'alert':'status'}>{t(checkFailed?'pageRevision.activeUnknown':'common.loading')} {checkFailed&&<button type="button" className="text-link" onClick={()=>void checkActive()}>{t('pageRevision.read')}</button>}</p>}
  {!op&&checked&&!open&&<button type="button" className="text-link" disabled={!canUse()||!canStart} onClick={()=>setOpen(true)}>{t('pageRevision.open')}</button>}
  {!op&&checked&&open&&<><label className="field">{t('pageRevision.instruction')}<textarea maxLength={4000} value={instruction} onChange={e=>setInstruction(e.target.value)}/></label><p className="metadata">{t('pageRevision.help')}</p><div className="page-actions"><button type="button" className="button button--primary button--compact" disabled={!instruction.trim()||instruction.length>4000||!canUse()||!canStart} onClick={()=>void start()}>{t('pageRevision.send')}</button><button type="button" className="button button--ghost button--compact" onClick={()=>setOpen(false)}>{t('common.cancel')}</button></div></>}
  {op&&<><p role={['unknown','missing','failed','conflict','unavailable','tooLong'].includes(op.phase)?'alert':'status'}>{t(`pageRevision.${op.phase}`)}</p>{op.root&&<button type="button" className="text-link" onClick={()=>{if(current())onNavigate(op.root!.seq,op.root!.event_id)}}>{t('pageRevision.source')}</button>}{['pending','unknown','missing'].includes(op.phase)&&<button type="button" className="text-link" onClick={()=>void read()}>{t('pageRevision.read')}</button>}{op.phase==='missing'&&<button type="button" className="button button--secondary button--compact" onClick={()=>void recover()}>{t('pageRevision.recover')}</button>}{op.phase==='completed'&&op.resultRef&&<button type="button" className="text-link" onClick={()=>{if(current())onOpenVersion(op.result,op.resultRef!.version)}}>{t('pageRevision.openVersion',{version:op.resultRef.version})}</button>}{['failed','cancelled','conflict','unavailable','tooLong','completed'].includes(op.phase)&&<button type="button" className="text-link" onClick={reset}>{t('common.close')}</button>}</>}
 </section>
}
