import { useEffect, useRef } from 'react'
import { useI18n } from '../i18n'

export default function ClipboardTextFallback({ text,label,caption,canUse,onClose,onRetry,onInteract,busy,rows=8 }: {
 text:string;label:string;caption:string;canUse:()=>boolean;onClose:()=>void;onRetry:()=>void;onInteract:()=>void;busy:boolean;rows?:number
}) {
 const {t}=useI18n(),area=useRef<HTMLTextAreaElement>(null),root=useRef<HTMLElement>(null)
 const live=useRef({canUse,onClose});live.current={canUse,onClose}
 useEffect(()=>{
  const panel=root.current;if(!panel)return
  let composing=false,until=0
  const start=()=>{composing=true;until=0}
  const end=()=>{composing=false;until=performance.now()+120}
  const reset=()=>{composing=false;until=0}
  const visibility=()=>{if(document.visibilityState!=='visible')reset()}
  const key=(event:KeyboardEvent)=>{
   if(event.key!=='Escape'||event.defaultPrevented||!live.current.canUse()||!panel.isConnected||!panel.getClientRects().length
    ||panel.closest('[hidden], [inert], [aria-hidden="true"]')||document.visibilityState!=='visible'||!document.hasFocus())return
   const modals=Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node=>node.getClientRects().length>0
    &&!node.closest('[hidden], [inert], [aria-hidden="true"]')&&getComputedStyle(node).visibility!=='hidden')
   const top=modals.at(-1);if(top&&!top.contains(panel))return
   // Handle before a containing dialog's native listener, preserving native IME cancellation.
   event.stopPropagation()
   const ime=composing||event.isComposing||event.keyCode===229
   if(ime){until=performance.now()+120;return}
   event.preventDefault()
   if(performance.now()<until||event.repeat)return
   live.current.onClose()
  }
  panel.addEventListener('compositionstart',start,true);panel.addEventListener('compositionend',end,true)
  panel.addEventListener('focusout',reset,true);panel.addEventListener('keydown',key)
  window.addEventListener('blur',reset);document.addEventListener('visibilitychange',visibility)
  return()=>{panel.removeEventListener('compositionstart',start,true);panel.removeEventListener('compositionend',end,true)
   panel.removeEventListener('focusout',reset,true);panel.removeEventListener('keydown',key)
   window.removeEventListener('blur',reset);document.removeEventListener('visibilitychange',visibility)}
 },[])
 return <section ref={root} className="notice notice--info stack stack--tight" style={{gridColumn:'1 / -1',minWidth:0}} aria-label={label} onPointerDownCapture={onInteract} onKeyDownCapture={onInteract}>
  <label className="field">{label}<textarea ref={area} readOnly rows={rows} value={text} spellCheck={false}/></label>
  <p className="field-hint">{caption}</p>
  <p className="field-hint">{t('clipboard.manual.escapeHelp')}</p>
  <div className="page-actions">
   <button type="button" className="button button--secondary button--compact" onClick={()=>{if(canUse()){area.current?.focus({preventScroll:true});area.current?.select()}}}>{t('projects.exportSnapshot.selectAll')}</button>
   <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={()=>{if(canUse())onRetry()}}>{t(busy?'common.loading':'chat.clipboard.retry')}</button>
   <button type="button" className="text-link" onClick={()=>{if(canUse())onClose()}}>{t('common.close')}</button>
  </div>
 </section>
}
