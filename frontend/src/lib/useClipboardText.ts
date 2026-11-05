import { useEffect, useMemo, useRef, useState } from 'react'

// A bounded UI snapshot, never a new canonical draft or persisted clipboard cache.
// Object identities let callers invalidate changed content without duplicating
// the full body into a string key. Such caller-owned objects must be memoized.
export function useClipboardText<T extends { text: string }>(contextKey: string | object, allowed: (item: T) => boolean) {
  const [revision,setRevision]=useState(0), context=useMemo(()=>({}),[contextKey,revision])
  const live=useRef({context,allowed});live.current={context,allowed}
  const mounted=useRef(false),lock=useRef(false),generation=useRef(0),manualIntent=useRef(0)
  type Snapshot={context:object;generation:number;item:T;returnFocus:HTMLElement|null}
  const [pending,setPending]=useState<Snapshot|null>(null),[outcome,setOutcome]=useState<(Snapshot&{status:'copied'|'failed'})|null>(null),[manual,setManual]=useState<Snapshot|null>(null)
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;generation.current++}},[])
  useEffect(()=>{setOutcome(null);setManual(null)},[context])
  const valid=(value:Snapshot)=>mounted.current&&live.current.context===value.context&&generation.current===value.generation&&live.current.allowed(value.item)
  const snapshot=(item:T):Snapshot=>{
    // Retrying the frozen manual item keeps its original launcher, not the
    // Retry button that will disappear with the fallback panel.
    const active=document.activeElement
    const returnFocus=manual&&manual.item===item&&valid(manual)?manual.returnFocus:
      active instanceof HTMLElement&&active.matches('button,a[href],input,textarea,select,[tabindex]')?active:null
    return {context,generation:generation.current,item:Object.freeze({...item}),returnFocus}
  }
  const copy=async(item:T)=>{
    const original=snapshot(item)
    if(!valid(original)||lock.current)return
    const beforeManual=manualIntent.current;lock.current=true;setPending(original);setOutcome(null)
    try{
      if(!navigator.clipboard?.writeText)throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(original.item.text)
      if(valid(original))setOutcome({...original,status:'copied'})
    }catch{
      if(valid(original)){
        setOutcome({...original,status:'failed'})
        if(beforeManual===manualIntent.current)setManual(original)
      }
    }finally{lock.current=false;if(mounted.current)setPending(null)}
  }
  const openManual=(item:T)=>{const value=snapshot(item);if(valid(value)){manualIntent.current++;setManual(value)}}
  const closeManual=()=>{manualIntent.current++;setManual(null)}
  const closeManualAndRestore=()=>{
    if(!manual||!valid(manual))return
    const target=manual.returnFocus
    if(document.visibilityState==='visible'&&document.hasFocus()&&target?.isConnected
      &&target.getClientRects().length>0&&!target.matches(':disabled')
      &&!target.closest('[hidden],[inert],[aria-hidden="true"]'))target.focus({preventScroll:true})
    closeManual()
  }
  const invalidate=()=>{generation.current++;manualIntent.current++;setOutcome(null);setManual(null);setRevision(value=>value+1)}
  return {copy,openManual,closeManual,closeManualAndRestore,invalidate,touchManual:()=>{manualIntent.current++},busy:pending!==null,
    pending:pending&&valid(pending)?pending.item:null,
    result:outcome&&valid(outcome)?{item:outcome.item,status:outcome.status}:null,
    manual:manual&&valid(manual)?manual.item:null,
    manualAllowed:()=>!!manual&&valid(manual)}
}
