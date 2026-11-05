import { useId,useMemo,useState,type ReactNode,type SelectHTMLAttributes } from 'react'
import { Link } from 'react-router-dom'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { evidenceExcerpt,type GoalEvidenceChoice } from '../lib/goalEvidence'
import { useI18n } from '../i18n'
export default function GoalEvidencePicker({roomId,choices,selected,disabled,historyComplete,fieldProps,help,onChange,canOpen,onNavigate}:{
 roomId:number;choices:GoalEvidenceChoice[];selected:number|null;disabled:boolean;historyComplete:boolean;fieldProps:SelectHTMLAttributes<HTMLSelectElement>;help:ReactNode;onChange:(choice:GoalEvidenceChoice|null)=>void;canOpen:(choice:GoalEvidenceChoice)=>boolean;onNavigate?:(seq:number,eventId:string)=>void
}){
 const {t}=useI18n(),generated=useId(),id=fieldProps.id||generated,[query,setQuery]=useState(''),[anchor,setAnchor]=useState<string|null>(null)
 const found=useMemo(()=>{if(!query.trim())return choices;const match=directoryMatcher(query);return choices.filter(row=>match([String(row.seq),`#${row.seq}`,row.actor,row.kind,row.text]))},[choices,query])
 const start=anchor?Math.max(0,found.findIndex(row=>row.eventId===anchor)):0,end=Math.min(start+50,found.length),page=found.slice(start,end)
 const chosen=selected?choices.find(row=>row.seq===selected):undefined,pinned=!!chosen&&!page.includes(chosen)
 const label=(row:GoalEvidenceChoice)=>`#${row.seq} · ${row.actor} · ${evidenceExcerpt(row.text)||t('roomPlans.evidenceFinder.metadataOnly',{kind:row.kind})}`
 const move=(index:number)=>{if(!disabled)setAnchor(found[Math.max(0,Math.min(index,found.length-1))]?.eventId||null)}
 return <div className="stack stack--tight" style={{minWidth:0}}>
  <DirectorySearch value={query} onChange={value=>{setQuery(value);setAnchor(null)}} label={t('roomPlans.evidenceFinder.find')} hint={t('roomPlans.evidenceFinder.hint')} shown={found.length} total={choices.length} known loading={false} controls={id} preventSubmit disabled={disabled}/>
  <p className="field-hint">{t('roomPlans.evidenceFinder.scope')}</p>
  {!historyComplete&&<p className="field-hint">{t('roomPlans.evidenceFinder.partial')}</p>}
  {found.length>0&&<p className="metadata">{t('roomPlans.evidenceFinder.range',{from:start+1,to:end,total:found.length})}</p>}
  <div className="page-actions"><button type="button" className="text-link" disabled={disabled||start===0} onClick={()=>move(0)}>{t('roomPlans.evidenceFinder.newest')}</button><button type="button" className="text-link" disabled={disabled||start===0} onClick={()=>move(start-50)}>{t('roomPlans.evidenceFinder.newer')}</button><button type="button" className="text-link" disabled={disabled||end>=found.length} onClick={()=>move(start+50)}>{t('roomPlans.evidenceFinder.older')}</button><button type="button" className="text-link" disabled={disabled||end>=found.length} onClick={()=>move(Math.max(0,found.length-50))}>{t('roomPlans.evidenceFinder.oldest')}</button></div>
  {query&&!found.length&&<p role="status" className="metadata">{t('directory.noMatches')}</p>}
  <label className="field" htmlFor={id}>{t('roomPlans.evidence')}<select {...fieldProps} id={id} value={selected||''} disabled={disabled} onChange={event=>{if(disabled)return;if(!event.target.value){onChange(null);return}const row=choices.find(choice=>choice.seq===Number(event.target.value));if(row)onChange(row)}}>
   <option value="">{t('roomPlans.chooseEvidence')}</option>
   {selected&&!chosen&&<option value={selected}>{t('roomPlans.evidenceFinder.missing',{seq:selected})}</option>}
   {pinned&&<option value={chosen.seq}>{label(chosen)}</option>}
   {page.map(row=><option key={row.eventId} value={row.seq}>{label(row)}</option>)}
  </select>{help}</label>
  {pinned&&<p className="field-hint">{t('roomPlans.evidenceFinder.pinned')}</p>}
  {selected&&!chosen&&<p className="field-hint">{t('roomPlans.evidenceFinder.retained')}</p>}
  {chosen&&<div className="notice stack stack--tight" style={{overflowWrap:'anywhere'}}>
   <p className="metadata">#{chosen.seq} · {chosen.actor} · {chosen.kind}</p><p style={{whiteSpace:'pre-wrap'}}>{evidenceExcerpt(chosen.text,240)||t('roomPlans.evidenceFinder.noPreview')}</p>
   <Link className="text-link" to={`/chat/${roomId}?seq=${chosen.seq}&event=${encodeURIComponent(chosen.eventId)}`} onClick={event=>{if(!canOpen(chosen)){event.preventDefault();return}if(onNavigate){event.preventDefault();onNavigate(chosen.seq,chosen.eventId)}}}>{t('roomPlans.openEvidence')}</Link>
  </div>}
 </div>
}
