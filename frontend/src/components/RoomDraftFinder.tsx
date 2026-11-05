import {useId,useMemo,useState} from 'react'
import DirectorySearch,{directoryMatcher} from './DirectorySearch'
import type {RoomActionRecord} from '../lib/roomActionWorkspace'
import {useI18n} from '../i18n'
const excerpt=(text:string)=>{const points=Array.from(text.slice(0,1280).replace(/\s+/g,' ').trim());return points.slice(0,160).join('')+(points.length>160||text.length>1280?'…':'')}
export default function RoomDraftFinder({records,canUse,onResume}:{records:readonly RoomActionRecord[];canUse:()=>boolean;onResume:(roomId:number)=>void}){
 const {t}=useI18n(),id=useId(),[query,setQuery]=useState(''),[anchor,setAnchor]=useState<number|null>(null)
 const found=useMemo(()=>{const match=directoryMatcher(query);return records.filter(({room,draft})=>match([room.title,String(room.id),`#${room.id}`,draft.title,draft.cloneTitle,String(room.project_id),`#${room.project_id}`,String(draft.targetProject),`#${draft.targetProject}`]))},[records,query])
 const start=anchor===null?0:Math.max(0,found.findIndex(row=>row.room.id===anchor)),end=Math.min(start+10,found.length)
 const move=(index:number)=>{if(canUse())setAnchor(found[Math.max(0,Math.min(index,found.length-1))]?.room.id??null)}
 return <details className="compact-details"><summary>{t('projects.roomHistory.drafts')} · {records.length}</summary><div className="stack stack--tight">
  <p className="field-hint">{t('roomDraftFinder.help')}</p>
  <DirectorySearch value={query} onChange={value=>{if(canUse()){setQuery(value);setAnchor(null)}}} hint={t('roomDraftFinder.hint')} shown={found.length} total={records.length} known loading={false} controls={id} preventSubmit/>
  {found.length?<p role="status" className="metadata">{t('roomDraftFinder.range',{from:start+1,to:end,total:found.length})}</p>:<p role="status" className="metadata">{t('directory.noMatches')}</p>}
  <div className="page-actions"><button type="button" className="text-link" disabled={start===0} onClick={()=>move(0)}>{t('roomDraftFinder.first')}</button><button type="button" className="text-link" disabled={start===0} onClick={()=>move(start-10)}>{t('roomDraftFinder.previous')}</button><button type="button" className="text-link" disabled={end>=found.length} onClick={()=>move(start+10)}>{t('roomDraftFinder.next')}</button><button type="button" className="text-link" disabled={end>=found.length} onClick={()=>move(Math.max(0,found.length-10))}>{t('roomDraftFinder.last')}</button></div>
  <div id={id} className="stack stack--tight">{found.slice(start,end).map(({room,draft})=><div key={room.id} className="notice stack stack--tight" style={{overflowWrap:'anywhere'}}>
   <strong>#{room.id} · {excerpt(room.title)}</strong><p>{t('projects.renameRoom')} · {excerpt(draft.title)}</p>
   {draft.cloneTitle&&<p>{t('projects.newRoomFromSetup')} · {excerpt(draft.cloneTitle)}</p>}
   <p className="metadata">{t('roomDraftFinder.projects',{source:room.project_id,target:draft.targetProject})}</p>
   <button type="button" className="button button--secondary" onClick={()=>{if(canUse())onResume(room.id)}}>{t('roomDraftFinder.open')}</button>
  </div>)}</div>
 </div></details>
}
