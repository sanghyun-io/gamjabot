import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { libraryPinsApi, pinKey, validPinSource, type PinOperation, type PinSource, type PinStatus } from '../api/libraryPins'
import { libraryUploadsApi } from '../api/libraryUploads'
import { roomResultsApi } from '../api/roomResults'
import { resultVersionHref } from '../lib/resultLink'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'
import LibraryPins, { type LibraryPinView } from './LibraryPins'

type Actions = {pin:(source:PinSource)=>void;busy:boolean;status:(source:PinSource)=>boolean|undefined}
const Context=createContext<Actions>({pin:()=>{},busy:true,status:()=>undefined})
export const useLibraryPinActions=()=>useContext(Context)
const fallbackFor=(p:PinStatus):LibraryPinView=>{const s=p.source;return {key:pinKey(s),basis:`${p.revision}:${p.availability}`,title:s.kind==='result'?`#${s.result_id.slice(-8)}`:`#${s.upload_id.slice(-8)}`,detail:s.kind==='result'?`v${s.version}`:s.sha256.slice(0,12),availability:p.availability}}
const validOperation=(v:unknown):v is PinOperation=>{
 if(!v||typeof v!=='object'||Array.isArray(v))return false
 const x=v as PinOperation
 return typeof x.operation_id==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(x.operation_id)&&validPinSource(x.source)&&Number.isSafeInteger(x.expected_revision)&&x.expected_revision>=0&&typeof x.pinned==='boolean'&&typeof x.expected_owner_epoch==='string'&&/^[a-f0-9]{64}$/.test(x.expected_owner_epoch)
}
export default function LibraryPinManager({botId,children,onUpload}:{botId:number;children:ReactNode;onUpload:(id:string,sha:string)=>void}){
 const scope=useBotScope(),{locale}=useI18n(),ko=locale==='ko',alive=useRef(false),generation=useRef(0),locked=useRef(false)
 const storageKey=`gamja.library-pin.${scope.ownerId}.${botId}`
 const [items,setItems]=useState<PinStatus[]>([]),[views,setViews]=useState<LibraryPinView[]>([]),[cursor,setCursor]=useState<string|null>(null)
 const [storageReady,setStorageReady]=useState(false)
 const [loading,setLoading]=useState(false),[error,setError]=useState(false),[busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[pending,setPending]=useState<PinOperation|null>(null)
 const current=()=>alive.current&&scope.canUse()
 useEffect(()=>{alive.current=true;try{const s=sessionStorage.getItem(storageKey);if(s){const value=JSON.parse(s);if(validOperation(value))setPending(value);else setNotice('storage')}}catch{setNotice('storage')}setStorageReady(true);return()=>{alive.current=false;generation.current++}},[storageKey])
 const load=useCallback(async(after?:string)=>{
  if(!current())return
  const gen=++generation.current;setLoading(true);setError(false)
  try{
   const page=await libraryPinsApi.list(botId,after)
   if(!current()||generation.current!==gen)return
   setItems(old=>after?[...old,...page.items.filter(p=>!old.some(x=>pinKey(x.source)===pinKey(p.source)))]:page.items);setCursor(page.next_cursor)
  }catch{if(current()&&generation.current===gen)setError(true)}finally{if(current()&&generation.current===gen)setLoading(false)}
 },[botId,scope.identity])
 useEffect(()=>{void load()},[load])
 useEffect(()=>{
  let active=true
  setViews(items.map(fallbackFor))
  for(const p of items)void (async()=>{
   const s=p.source,fallback=fallbackFor(p)
   if(p.availability!=='available'&&!(p.availability==='removed'&&s.kind==='upload')&&!(p.availability==='unknown'&&s.kind==='result'))return fallback
   try{
    if(s.kind==='result'){
     const row=await roomResultsApi.detail(s.room_id,s.result_id,s.version)
     if(row.room_id!==s.room_id||row.result_id!==s.result_id||row.version!==s.version||row.author_agent_id!==botId||typeof row.title!=='string')throw Error('Original changed')
     const href=resultVersionHref(s.room_id,s.result_id,s.version);if(!href)throw Error('Invalid result')
     return {...fallback,title:row.title,detail:`${row.kind} · v${s.version}`,...(p.availability==='unknown'?{unknownMessage:ko?'파일 원본 상태를 확인하지 못했습니다. 결과는 열 수 있지만 파일을 사용할 수 있는지는 별도 확인이 필요합니다.':'The file status could not be checked. You can open the result, but the file may be unavailable.'}:{}),open:<Link className="text-link" to={href} onClick={e=>{if(!current())e.preventDefault()}}>{ko?'열기':'Open'}</Link>}
    }
    const row=await libraryUploadsApi.read(botId,s.upload_id)
    if(row.upload_id!==s.upload_id||row.bot_id!==botId||row.sha256!==s.sha256)throw Error('Original changed')
    if(row.removed)return {...fallback,title:row.filename,availability:'removed' as const}
    if(p.availability!=='available')return fallback
    return {...fallback,title:row.filename,detail:`${ko?'업로드':'Upload'} · ${s.sha256.slice(0,12)}`,open:<button type="button" className="text-link" onClick={()=>{if(current())onUpload(s.upload_id,s.sha256)}}>{ko?'파일 위치로 이동':'Go to file'}</button>}
   }catch{return {...fallback,availability:'unknown' as const}}
  })().then(row=>{if(active&&current())setViews(old=>old.map(item=>item.key===row.key?row:item))})
  return()=>{active=false}
 },[items,botId,ko,onUpload,scope.identity])
 const finish=async(op:PinOperation,resolve:boolean)=>{
  const response=resolve?await libraryPinsApi.resolve(botId,op):await libraryPinsApi.operate(botId,op)
  if(!current())return
  if(response.state==='applied'&&(response.pinned!==op.pinned||response.revision!==op.expected_revision&&response.revision!==op.expected_revision+1))throw Error('Receipt mismatch')
  sessionStorage.removeItem(storageKey);setPending(null)
  setNotice(response.state==='applied'?'saved':response.state==='fenced'?'fenced':response.code==='pin_source_unavailable'?'unavailable':'conflict');await load()
 }
 const mutate=async(source:PinSource,pinned:boolean)=>{
  if(!current()||locked.current||pending||!storageReady||notice==='storage')return
  locked.current=true;setBusy(true);setNotice('');let prepared=false
  try{
   const authority=await libraryPinsApi.context(botId,scope.ownerId);if(!current())return
   const status=await libraryPinsApi.status(botId,source);if(!current())return
   if(status.pinned===pinned){await load();return}
   if(pinned&&status.availability!=='available'&&!(source.kind==='result'&&status.availability==='unknown')){setNotice('unavailable');return}
   const op:PinOperation={operation_id:crypto.randomUUID(),source,expected_revision:status.revision,pinned,expected_owner_epoch:authority.owner_epoch}
   sessionStorage.setItem(storageKey,JSON.stringify(op));setPending(op);prepared=true
   await finish(op,false)
  }catch{if(current())setNotice(prepared?'unknown':'failed')}finally{locked.current=false;if(current())setBusy(false)}
 }
 const resolve=async()=>{if(!pending||!current()||locked.current)return;locked.current=true;setBusy(true);try{await finish(pending,true)}catch{if(current())setNotice('unknown')}finally{locked.current=false;if(current())setBusy(false)}}
 const labels:Record<string,string>={saved:ko?'고정 상태를 저장했습니다.':'Pin state saved.',fenced:ko?'요청이 적용되지 않았습니다. 다시 선택해 주세요.':'The request was not applied. Choose again.',conflict:ko?'다른 변경이 있어 저장하지 않았습니다. 최신 상태를 확인해 주세요.':'The state changed elsewhere. Review the latest state.',unknown:ko?'요청 결과를 확인하지 못했습니다. 먼저 확인해 주세요.':'The request result is unknown. Check it first.',failed:ko?'고정 상태를 확인하지 못해 요청하지 않았습니다. 다시 시도해 주세요.':'The pin state could not be checked, so no request was sent. Try again.',storage:ko?'이전 요청을 확인할 수 없어 고정 상태를 변경할 수 없습니다.':'The earlier request cannot be checked, so changes are disabled.',unavailable:ko?'원본을 확인할 수 없어 고정하지 않았습니다.':'The original is unavailable, so it was not pinned.'}
 return <Context.Provider value={{pin:s=>void mutate(s,true),busy:busy||!!pending||!storageReady||notice==='storage',status:s=>items.some(item=>pinKey(item.source)===pinKey(s))?true:loading||error||cursor!==null?undefined:false}}>
  <LibraryPins ko={ko} items={items.map(item=>views.find(v=>v.key===pinKey(item.source)&&v.basis===`${item.revision}:${item.availability}`)||fallbackFor(item))} loading={loading} error={error} busy={busy||!!pending||!storageReady||notice==='storage'} onRefresh={()=>void load()} onRemove={key=>{const item=items.find(x=>pinKey(x.source)===key);if(item)void mutate(item.source,false)}}/>
  {cursor&&!error&&<button type="button" disabled={loading||busy} onClick={()=>void load(cursor)}>{ko?'고정 항목 더 보기':'More pinned items'}</button>}
  {notice&&<p role={notice==='unknown'||notice==='storage'?'alert':'status'}>{labels[notice]}</p>}
  {pending&&<div className="notice notice--warning"><p>{ko?'이전 고정 요청 결과를 먼저 확인하세요.':'Check the previous pin request first.'}</p><button type="button" disabled={busy} onClick={()=>void resolve()}>{ko?'요청 확인':'Check request'}</button></div>}
  {children}
 </Context.Provider>
}
