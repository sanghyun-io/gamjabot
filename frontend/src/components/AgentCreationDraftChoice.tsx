import {useEffect,useRef,useState} from 'react'
import {useQueryClient} from '@tanstack/react-query'
import type {Connection} from '../api/connections'
import {useAuthStore} from '../stores/authStore'
import {useI18n} from '../i18n'
import {directoryViewGeneration} from '../lib/directoryViewMemory'
import {validEditorModelId} from '../lib/editorValidation'
import {agentCommandBlocked,agentDraftKey,keepAgentDraft,readAgentWorkspace,type AgentDraft,type AgentDraftRecord} from '../lib/agentEditorWorkspace'
type Selection={connectionId:number;modelId:string}
const connectionTarget=(row:Connection)=>({id:row.id,owner_id:row.owner_id,label:row.label,enabled:row.enabled,provider_id:row.provider_id,protocol_id:row.protocol_id,auth_method_id:row.auth_method_id,default_model_id:row.default_model_id,config_revision:row.config_revision,auth_grant_revision:row.auth_grant_revision})
export default function AgentCreationDraftChoice({identity,generation,selection,record,initial,connections,ready,reading,blocked,canUse,onRead,onOpen}:{identity:string;generation:number;selection:Selection;record:AgentDraftRecord;initial:AgentDraft;connections:Connection[];ready:boolean;reading:boolean;blocked:boolean;canUse:()=>boolean;onRead:()=>void;onOpen:()=>void}){
 const {t}=useI18n(),cache=useQueryClient(),alive=useRef(true),choosing=useRef(false)
 const [review,setReview]=useState<{original:AgentDraftRecord;target:ReturnType<typeof connectionTarget>;modelId:string}|null>(null),[ack,setAck]=useState(false)
 useEffect(()=>{alive.current=true;return()=>{alive.current=false}},[])
 useEffect(()=>{setReview(null);setAck(false)},[identity,record,selection.connectionId,selection.modelId])
 const incoming=connections.find(row=>row.id===selection.connectionId),existing=connections.find(row=>row.id===record.value.connectionId)
 const sourceKey=JSON.stringify(incoming?connectionTarget(incoming):null)
 useEffect(()=>setAck(false),[sourceKey])
 const current=()=>alive.current&&canUse()&&directoryViewGeneration()===generation
 const received=()=>{
  const state=cache.getQueryState<Connection[]>(['connections',identity]),row=state?.data?.find(value=>value.id===selection.connectionId)
  return state?.status==='success'&&state.fetchStatus==='idle'&&!state.isInvalidated&&row?.enabled&&row.owner_id===useAuthStore.getState().user?.id?row:null
 }
 const same=record.value.connectionId===selection.connectionId&&record.value.modelId===selection.modelId
 const sourceSame=!!review&&!!incoming&&Object.entries(review.target).every(([key,value])=>incoming[key as keyof ReturnType<typeof connectionTarget>]===value)
 const prepare=()=>{
  if(!current()||blocked||agentCommandBlocked(identity)||choosing.current||!validEditorModelId(selection.modelId))return
  const actual=readAgentWorkspace(identity).drafts.get(agentDraftKey()),target=received()
  if(actual!==record||!target)return
  setReview({original:record,target:Object.freeze(connectionTarget(target)),modelId:selection.modelId});setAck(false)
 }
 const apply=()=>{
  if(!current()||blocked||agentCommandBlocked(identity)||choosing.current||!review||!ack||!ready||!sourceSame)return
  const actual=readAgentWorkspace(identity).drafts.get(agentDraftKey()),target=received()
  if(actual!==review.original||actual!==record||!target||review.modelId!==selection.modelId||!Object.entries(review.target).every(([key,value])=>target[key as keyof ReturnType<typeof connectionTarget>]===value))return
  choosing.current=true
  try{keepAgentDraft(identity,generation,undefined,{...actual.value,connectionId:target.id,modelId:review.modelId},actual.baseline,initial);setReview(null);setAck(false);if(current())onOpen()}
  finally{choosing.current=false}
 }
 const existingConnection=record.value.connectionId>0?`${existing?.label||t('agents.savedConnectionNumber',{id:String(record.value.connectionId)})} · #${record.value.connectionId}`:t('projects.connectionMissing')
 return <section className="stack stack--tight" aria-label={t('agents.draftChoice.title')} style={{overflowWrap:'anywhere'}}><strong>{t('agents.draftChoice.title')} · {record.value.name||t('agents.draftChoice.untitled')}</strong><p>{t('agents.connection',{value:existingConnection})}</p><p style={{whiteSpace:'pre-wrap'}}>{t('agents.model',{value:record.value.modelId||t('projects.modelUnspecified')})}</p><p className="field-hint">{t('agents.draftChoice.help')}</p>
  {same?<p role="status">{t('agents.draftChoice.same')}</p>:<button type="button" className="button button--secondary" disabled={blocked||!ready||!incoming?.enabled} onClick={prepare}>{t('agents.draftChoice.prepare')}</button>}
  {!ready&&<button type="button" className="text-link" disabled={reading||blocked} onClick={()=>{if(current())onRead()}}>{t(reading?'common.loading':'agents.draftChoice.read')}</button>}
  {review&&<div className="notice notice--warning stack stack--tight"><p>{t('agents.connection',{value:`${review.target.label} · #${review.target.id}`})}</p><p style={{whiteSpace:'pre-wrap'}}>{t('agents.model',{value:review.modelId})}</p>{!sourceSame&&<p role="status">{t('agents.draftChoice.changed')}</p>}<label className="check-row"><input type="checkbox" checked={ack} disabled={blocked||!ready||!sourceSame} onChange={event=>{if(current())setAck(event.target.checked)}}/>{t('agents.draftChoice.ack')}</label><button type="button" className="button button--primary" disabled={blocked||!ready||!sourceSame||!ack} onClick={apply}>{t('agents.draftChoice.apply')}</button></div>}
 </section>
}
