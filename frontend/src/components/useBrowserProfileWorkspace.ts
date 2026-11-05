import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'
import api from '../lib/api'
import type { BrowserProfile } from './BrowserProfileActionDialog'
import type { ProfilePreview } from './BrowserProfilePreview'

type Slot = { scope:string; id:string; name:string; revision:number; preview:ProfilePreview|null; context:string|null; pending:boolean; failed:boolean; invalidated:boolean }

export function useBrowserProfileWorkspace(roomId:number, identity:string, open:boolean, current:(identity:string,run:number)=>boolean, generation:MutableRefObject<number>, latest:(id:string)=>BrowserProfile|undefined) {
  const [slots,setSlots]=useState<Slot[]>([]), slotsRef=useRef<Slot[]>([]), requests=useRef(new Map<string,AbortController>()), serial=useRef(new Map<string,number>())
  const scope=`${roomId}:${identity}`, visibleScope=useRef(scope)
  const latestRef=useRef(latest);latestRef.current=latest
  const update=useCallback((fn:(items:Slot[])=>Slot[])=>{const next=fn(slotsRef.current);slotsRef.current=next;setSlots(next)},[])
  const abort=useCallback((id:string)=>{serial.current.set(id,(serial.current.get(id)||0)+1);requests.current.get(id)?.abort();requests.current.delete(id);update(items=>items.map(s=>s.id===id?{...s,pending:false,invalidated:true,context:null}:s))},[update])
  const close=useCallback((id:string)=>{abort(id);const slot=slotsRef.current.find(s=>s.id===id);if(slot?.preview)URL.revokeObjectURL(slot.preview.url);update(items=>items.filter(s=>s.id!==id))},[abort,update])
  useEffect(()=>{
    visibleScope.current=scope
    setSlots([])
    return()=>{for(const controller of requests.current.values())controller.abort();requests.current.clear();for(const s of slotsRef.current)if(s.preview)URL.revokeObjectURL(s.preview.url);slotsRef.current=[];serial.current.clear()}
  },[roomId,identity])
  useEffect(()=>{if(!open)for(const id of requests.current.keys())abort(id)},[open,abort])
  useEffect(()=>{const hidden=()=>{if(document.visibilityState==='hidden')for(const id of requests.current.keys())abort(id)};document.addEventListener('visibilitychange',hidden);return()=>document.removeEventListener('visibilitychange',hidden)},[abort])
  const capture=useCallback(async(p:BrowserProfile)=>{
    if(visibleScope.current!==scope||!open||document.visibilityState==='hidden'||requests.current.has(p.id)||!p.connected||!current(identity,generation.current))return
    const existing=slotsRef.current.find(s=>s.id===p.id)
    if(!existing&&slotsRef.current.length>=2)return
    if(!existing)update(items=>[...items,{scope,id:p.id,name:p.name,revision:p.revision,preview:null,context:null,pending:true,failed:false,invalidated:false}])
    else update(items=>items.map(s=>s.id===p.id?{...s,pending:true,failed:false}:s))
    const controller=new AbortController(), run=generation.current, sequence=(serial.current.get(p.id)||0)+1
    serial.current.set(p.id,sequence);requests.current.set(p.id,controller)
    try {
      const result=await api.get<Blob>(`/tools/rooms/${roomId}/browser-profiles/${p.id}/preview`,{responseType:'blob',params:{revision:p.revision},signal:controller.signal})
      const h=result.headers, id=h['x-browser-profile-id'],revision=Number(h['x-browser-profile-revision']),context=h['x-browser-context-id'],captured=h['x-captured-at']
      const now=latestRef.current(p.id), slot=slotsRef.current.find(s=>s.id===p.id)
      if(controller.signal.aborted||!current(identity,run)||serial.current.get(p.id)!==sequence||!slot||!now||!now.connected||now.revision!==p.revision||id!==p.id||revision!==p.revision||typeof context!=='string'||!context||slot.context&&slot.context!==context&&slot.revision===p.revision&&!slot.invalidated||typeof captured!=='string'||!Number.isFinite(Date.parse(captured))||!(result.data instanceof Blob)||result.data.type!=='image/jpeg')throw Error('Preview changed')
      const url=URL.createObjectURL(result.data)
      if(slot.preview)URL.revokeObjectURL(slot.preview.url)
      update(items=>items.map(s=>s.id===p.id?{...s,name:p.name,revision:p.revision,context,preview:{id:p.id,url,received:captured},pending:false,failed:false,invalidated:false}:s))
    } catch {
      if(!controller.signal.aborted&&current(identity,run)&&serial.current.get(p.id)===sequence)update(items=>items.map(s=>s.id===p.id?{...s,pending:false,failed:true}:s))
    } finally {if(requests.current.get(p.id)===controller)requests.current.delete(p.id)}
  },[open,roomId,identity,scope,current,generation,update])
  const visible=slots.filter(slot=>slot.scope===scope)
  return {slots:visible,capture,close,abort,full:visible.length>=2}
}
