import {useId,useLayoutEffect,useMemo,useRef,useState,type ReactNode} from 'react'
import DirectorySearch,{directoryMatcher} from './DirectorySearch'
import {useI18n} from '../i18n'
type Phase='pending'|'succeeded'|'rejected'|'unknown'|'failed'|'not_sent'
export type ArchiveCommand={id:string;phase:Phase;search:readonly string[];content:ReactNode}
export default function CommandHistoryArchive({title,items,phases}:{title:string;items:readonly ArchiveCommand[];phases?:readonly Phase[]}){
 const {t}=useI18n(),id=useId(),[query,setQuery]=useState(''),[phase,setPhase]=useState<'all'|Phase>('all'),[anchor,setAnchor]=useState<string|null>(null)
 const visiblePhases:readonly Phase[]=phases||['succeeded','rejected','unknown']
 const root=useRef<HTMLDetailsElement>(null),readingRoot=useRef<HTMLDivElement>(null)
 const pageContext=useMemo(()=>({}),[items,query,phase,t])
 const pageIntent=useRef<{context:object;id:string;origin:HTMLButtonElement}|null>(null)
 const found=useMemo(()=>{const match=directoryMatcher(query);return items.filter(row=>(phase==='all'||row.phase===phase)&&match([...row.search,row.phase,t(`commandArchive.${row.phase}`)]))},[items,query,phase,t])
 const start=anchor?Math.max(0,found.findIndex(row=>row.id===anchor)):0,end=Math.min(start+5,found.length),shown=new Set(found.slice(start,end).map(row=>row.id))
 const move=(index:number,origin:HTMLButtonElement)=>{
  if(!root.current?.open||!found.length||!Number.isSafeInteger(index)||!origin.isConnected||origin.disabled||!root.current.contains(origin))return
  const target=found[Math.max(0,Math.min(index,found.length-1))]
  if(!target||target.id===found[start]?.id)return
  origin.focus({preventScroll:true});pageIntent.current={context:pageContext,id:target.id,origin};setAnchor(target.id)
 }
 useLayoutEffect(()=>{
  const intent=pageIntent.current;pageIntent.current=null
  if(!intent||intent.context!==pageContext||intent.id!==found[start]?.id||!root.current?.open
    ||document.visibilityState!=='visible'||!document.hasFocus()||document.activeElement!==intent.origin&&document.activeElement!==document.body)return
  const target=readingRoot.current
  if(!target?.isConnected||!target.getClientRects().length||target.closest('[hidden], [inert], [aria-hidden="true"]'))return
  const top=Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node=>node.getClientRects().length>0
    &&!node.closest('[hidden], [inert], [aria-hidden="true"]')&&getComputedStyle(node).visibility!=='hidden').at(-1)
  if(top&&!top.contains(target))return
  target.focus({preventScroll:true});target.scrollIntoView({block:'start',inline:'nearest'})
 },[anchor,pageContext,found[start]?.id])
 return <details ref={root} className="compact-details" onToggle={event=>{if(event.target===event.currentTarget&&!event.currentTarget.open)pageIntent.current=null}}><summary>{title}</summary><div className="stack stack--tight">
  <p className="field-hint">{t('commandArchive.help')}</p>
  <DirectorySearch value={query} onChange={value=>{pageIntent.current=null;setQuery(value);setAnchor(null)}} hint={t('commandArchive.hint')} shown={found.length} total={items.length} known loading={false} controls={id} preventSubmit/>
  <label className="field">{t('commandArchive.state')}<select value={phase} onChange={event=>{pageIntent.current=null;setPhase(event.target.value as typeof phase);setAnchor(null)}}>{(['all',...visiblePhases] as const).map(value=><option key={value} value={value}>{t(`commandArchive.${value}`)}</option>)}</select></label>
  {found.length>0?<p role="status" className="metadata">{t('commandArchive.range',{from:start+1,to:end,total:found.length})}</p>:<p role="status" className="metadata">{t('directory.noMatches')}</p>}
  <div className="page-actions"><button type="button" className="text-link" disabled={start===0} onClick={event=>move(0,event.currentTarget)}>{t('commandArchive.newest')}</button><button type="button" className="text-link" disabled={start===0} onClick={event=>move(start-5,event.currentTarget)}>{t('commandArchive.newer')}</button><button type="button" className="text-link" disabled={end>=found.length} onClick={event=>move(start+5,event.currentTarget)}>{t('commandArchive.older')}</button><button type="button" className="text-link" disabled={end>=found.length} onClick={event=>move(Math.max(0,found.length-5),event.currentTarget)}>{t('commandArchive.oldest')}</button>{(query||phase!=='all')&&<button type="button" className="text-link" onClick={()=>{pageIntent.current=null;setQuery('');setPhase('all');setAnchor(null)}}>{t('commandArchive.reset')}</button>}</div>
  <p className="field-hint">{t('commandArchive.pageReadHelp')}</p>
  <div ref={readingRoot} id={id} role="region" tabIndex={found.length?-1:undefined} className="save-history-reading-page" aria-label={found.length?t('commandArchive.range',{from:start+1,to:end,total:found.length}):t('directory.noMatches')}>{/* Keep existing row-local read observations mounted across filtering/pages. */}
   {items.map(row=><div key={row.id} hidden={!shown.has(row.id)} style={{display:shown.has(row.id)?undefined:'none',marginBottom:'0.75rem'}}>{row.content}</div>)}
  </div>
 </div></details>
}
