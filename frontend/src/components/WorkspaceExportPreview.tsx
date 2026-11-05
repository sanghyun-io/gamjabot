import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useMemo,useRef,useState } from 'react'
import { useI18n } from '../i18n'
import type { TransferExport } from '../lib/transferWorkspace'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { transferOwnerCurrent } from '../lib/transferActions'
import { summarizeWorkspaceExport,workspaceExportText,type WorkspaceExportSummary } from '../lib/workspaceExportSnapshot'
export function WorkspaceExportSummaryView({identity,summary}:{identity:string;summary:WorkspaceExportSummary}){
 return <ExportNames key={`${identity}:${directoryViewGeneration()}:${summary.exportId}`} identity={identity} summary={summary}/>
}
function ExportNames({identity,summary}:{identity:string;summary:WorkspaceExportSummary}){
 const {t,formatDateTime}=useI18n(),generation=useRef(directoryViewGeneration()).current,[namesOpen,setNamesOpen]=useState(false),[query,setQuery]=useState('')
 const connectionNames=new Map(summary.connections.map(item=>[item.ref,item.name])),agentNames=new Map(summary.agents.map(item=>[item.ref,item.name]))
 const names=(refs:readonly string[])=>refs.map(ref=>agentNames.get(ref)||ref).join(', ')
 const matches=directoryMatcher(query)
 const connections=summary.connections.filter(item=>matches([item.name,item.provider,item.model||t('projects.modelUnspecified')]))
 const agents=summary.agents.filter(item=>matches([item.name,item.model||t('projects.modelUnspecified'),connectionNames.get(item.connectionRef)||item.connectionRef]))
 const projects=summary.projects.flatMap(item=>{
  const fields=[item.name,item.agentRefs.length?names(item.agentRefs):t('projects.exportSnapshot.empty')],projectMatch=matches(fields)
  const templates=projectMatch?item.templates:item.templates.filter(template=>matches([...fields,template.name,template.title,template.agentRefs.length?names(template.agentRefs):t('projects.exportSnapshot.empty')]))
  return projectMatch||templates.length>0?[{item,templates}]:[]
 })
 const shown=connections.length+agents.length+projects.length,total=summary.connections.length+summary.agents.length+summary.projects.length

 return <div className="stack stack--tight">
  <p className="metadata">{summary.receivedAt?t('projects.exportSnapshot.received',{time:formatDateTime(summary.receivedAt,{dateStyle:'medium',timeStyle:'medium'})}):t('projects.exportSnapshot.timeUnknown')}</p>
  <div className="workspace-transfer__counts"><span>{t('projects.transfer.connections',{count:summary.counts.connections})}</span><span>{t('projects.transfer.agents',{count:summary.counts.agents})}</span><span>{t('projects.transfer.projects',{count:summary.counts.projects})}</span><span>{t('projects.transfer.templates',{count:summary.counts.room_templates})}</span><span>{t('projects.transfer.assignments',{count:summary.counts.project_assignments})}</span><span>{t('projects.transfer.members',{count:summary.counts.template_members})}</span></div>
  <details className="compact-details" onToggle={event=>{if(transferOwnerCurrent(identity,generation))setNamesOpen(event.currentTarget.open)}}><summary>{t('projects.exportSnapshot.names')}</summary>{namesOpen&&<div className="stack stack--tight">
   <DirectorySearch value={query} onChange={value=>{if(transferOwnerCurrent(identity,generation))setQuery(value)}} label={t('projects.blueprintFind.title')} hint={t('projects.blueprintFind.placeholder')} shown={shown} total={total} known loading={false} preventSubmit/>
   <p className="field-hint">{t('projects.blueprintFind.scope')}</p>
   {query.trim()&&!shown&&<p role="status">{t('directory.noMatches')}</p>}
   {(!query.trim()||connections.length>0)&&<div><strong>{t('connections.title')}</strong>{connections.length?<ul>{connections.map(item=><li key={item.ref}>{item.name} · {item.provider} · {item.model||t('projects.modelUnspecified')} · {t(item.enabled?'connections.enabled':'connections.disabled')}</li>)}</ul>:<p>{t('projects.exportSnapshot.empty')}</p>}</div>}
   {(!query.trim()||agents.length>0)&&<div><strong>{t('agents.title')}</strong>{agents.length?<ul>{agents.map(item=><li key={item.ref}>{item.name} · {item.model||t('projects.modelUnspecified')} · {connectionNames.get(item.connectionRef)||item.connectionRef}</li>)}</ul>:<p>{t('projects.exportSnapshot.empty')}</p>}</div>}
   {(!query.trim()||projects.length>0)&&<div><strong>{t('projects.title')}</strong>{projects.length?<ul>{projects.map(({item,templates})=><li key={item.ref}><strong>{item.name}</strong><p className="metadata">{t('projects.participants')}: {item.agentRefs.length?names(item.agentRefs):t('projects.exportSnapshot.empty')}</p>{templates.length<item.templates.length&&<p className="metadata">{t('projects.blueprintFind.templateMatches',{shown:templates.length,total:item.templates.length})}</p>}{templates.length>0&&<ul>{templates.map(template=><li key={template.ref}>{template.name} · {template.title}<p className="metadata">{t('projects.participants')}: {template.agentRefs.length?names(template.agentRefs):t('projects.exportSnapshot.empty')}</p></li>)}</ul>}</li>)}</ul>:<p>{t('projects.exportSnapshot.empty')}</p>}</div>}
  </div>}</details>
  {summary.compactBytes>2*1024*1024&&<p className="notice notice--warning">{t('projects.exportSnapshot.sizeLimit')}</p>}
  <p className="field-hint">{t('projects.exportSnapshot.notVerification')}</p>
 </div>
}
function SnapshotJSON({identity,snapshot}:{identity:string;snapshot:TransferExport}){
 const {t}=useI18n(),generation=useRef(directoryViewGeneration()).current,area=useRef<HTMLTextAreaElement>(null),content=useMemo(()=>snapshot.result?workspaceExportText(snapshot.result):'',[snapshot.result])
 return <div className="stack stack--tight"><p className="field-hint">{t('projects.exportSnapshot.jsonHelp')}</p><label className="field">{t('projects.exportSnapshot.json')}<textarea ref={area} readOnly spellCheck={false} className="technical" rows={10} value={content}/></label><button type="button" className="button button--secondary button--compact" onClick={()=>{if(transferOwnerCurrent(identity,generation)){area.current?.focus();area.current?.select()}}}>{t('projects.exportSnapshot.selectAll')}</button></div>
}
export default function WorkspaceExportPreview({identity,snapshot,summary:provided}:{identity:string;snapshot:TransferExport;summary?:WorkspaceExportSummary|null}){
 const {t}=useI18n(),generation=useRef(directoryViewGeneration()).current,summary=useMemo(()=>provided!==undefined?provided:summarizeWorkspaceExport(snapshot),[snapshot,provided]),[jsonOpen,setJSONOpen]=useState(false)
 return <section className="stack stack--tight" aria-label={t('projects.exportSnapshot.title')}><strong>{t('projects.exportSnapshot.title')}</strong>{summary?<WorkspaceExportSummaryView identity={identity} summary={summary}/>:<p role="status">{t('projects.exportSnapshot.summaryUnavailable')}</p>}<p className="field-hint">{t('projects.exportSnapshot.privacy')}</p><details className="compact-details" onToggle={event=>{if(transferOwnerCurrent(identity,generation))setJSONOpen(event.currentTarget.open)}}><summary>{t('projects.exportSnapshot.json')}</summary>{jsonOpen&&<SnapshotJSON identity={identity} snapshot={snapshot}/>}</details></section>
}
