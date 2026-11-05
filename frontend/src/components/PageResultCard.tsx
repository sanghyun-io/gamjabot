import { useEffect, useRef, useState } from 'react'
import { roomResultsApi, type ResultDetail } from '../api/roomResults'
import type { RoomEvent } from '../api/events'
import { canonicalEventId } from '../lib/peerReply'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import ServerTime from './ServerTime'

type Meta={version:1;room_id:number;result_id:string;page_version:number;base_version:number|null;title:string;document_sha256:string;agent_id:number}
export function pageResultMetadata(event:RoomEvent):Meta|null {
 const x=event.payload.page_result as Record<string,unknown>|undefined
 if(event.kind!=='tool_result'||event.actor_type!=='tool'||event.payload.family!=='write_page'||event.payload.status!=='completed'||!canonicalEventId(event.event_id)||!canonicalEventId(event.payload.trigger_event_id)||!x||x.version!==1||x.room_id!==event.room_id||!canonicalEventId(x.result_id)||!Number.isSafeInteger(x.page_version)||Number(x.page_version)<1||!(x.base_version===null||Number.isSafeInteger(x.base_version)&&Number(x.base_version)>0&&Number(x.base_version)+1===x.page_version)||x.base_version===null&&x.page_version!==1||typeof x.title!=='string'||!x.title.trim()||typeof x.document_sha256!=='string'||!/^[a-f0-9]{64}$/.test(x.document_sha256)||!Number.isSafeInteger(x.agent_id)||Number(x.agent_id)<1)return null
 return x as unknown as Meta
}
export default function PageResultCard({event,ownerId,authEpoch,signal,agentName,updatedBelow,canUse,onOpen,onSource}:{event:RoomEvent;ownerId:number;authEpoch:number;signal:number;agentName:string;updatedBelow:boolean;canUse:()=>boolean;onOpen:(id:string,version:number)=>void;onSource:(seq:number,id:string)=>void}) {
 const {t}=useI18n(),meta=pageResultMetadata(event),alive=useRef(false),serial=useRef(0),gate=useRef(canUse);gate.current=canUse
 const [value,setValue]=useState<ResultDetail|null>(null),[failure,setFailure]=useState(false),[busy,setBusy]=useState(false)
 const current=(n:number)=>{const a=useAuthStore.getState();return alive.current&&serial.current===n&&a.user?.id===ownerId&&a.epoch===authEpoch&&gate.current()}
 const read=async(open=false)=>{if(!meta||!gate.current())return;const n=++serial.current;setBusy(true);setFailure(false);try{const result=await roomResultsApi.pageCurrent(event.room_id,meta.result_id);if(!current(n))return;if(result.room_id!==event.room_id||result.result_id!==meta.result_id||result.kind!=='page'||result.author_agent_id!==meta.agent_id||!Number.isSafeInteger(result.version)||result.version<meta.page_version||result.current_version!==result.version||typeof result.content!=='string')throw Error('Page identity changed');setValue(result);if(open&&current(n))onOpen(result.result_id,result.version)}catch{if(current(n)){setValue(null);setFailure(true)}}finally{if(current(n))setBusy(false)}}
 useEffect(()=>{alive.current=true;void read();return()=>{alive.current=false;serial.current++}},[event.event_id,ownerId,authEpoch,signal])
 if(!meta)return null
 return <section className="notice notice--info stack stack--tight"><div className="conversation-event__header"><span className="conversation-event__name">{agentName}</span><ServerTime className="metadata" value={event.created_at} compact/></div><strong>{value?.title||meta.title}</strong><p className="metadata">{t('pageView.cardVersion',{version:meta.page_version})}</p>{updatedBelow&&<p className="metadata">{t('pageView.updatedBelow')}</p>}{busy&&<p role="status">{t('common.loading')}</p>}{failure&&<p role="alert">{t('pageView.unavailable')} <button type="button" className="text-link" onClick={()=>void read()}>{t('common.retry')}</button></p>}
  {value&&<button type="button" className="button button--secondary button--compact" onClick={()=>{if(gate.current())void read(true)}}>{t('pageView.open')}</button>}
  <button type="button" className="text-link" onClick={()=>{if(gate.current())onSource(event.seq,event.event_id)}}>{t('pageView.source')}</button>
 </section>
}
