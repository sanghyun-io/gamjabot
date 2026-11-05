import { useI18n,type Translate } from '../i18n'
import type { TranscriptExportScope } from '../lib/transcriptFileSave'
export function describeTranscriptExport(scope:TranscriptExportScope,t:Translate){
 const description=t('chat.exportScope.description',{range:t(scope.range==='matches'?'chat.exportScope.matches':'chat.exportScope.all'),count:scope.messageCount,body:t(scope.body==='original'?'chat.exportScope.original':'chat.exportScope.visible')})
 return scope.range==='matches'?description+'\n'+t('chat.exportScope.filter',{actor:t(scope.actor==='people'?'chat.transcript.people':scope.actor==='agents'?'chat.transcript.agents':'chat.transcript.all'),query:scope.query||t('chat.exportScope.noQuery')}):description
}
export function TranscriptExportScopeSummary({scope}:{scope:TranscriptExportScope}){
 const {t}=useI18n()
 return <p className="metadata" style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{describeTranscriptExport(scope,t)}</p>
}
export default function TranscriptExportOptions({scope,onChange,disabled}:{scope:TranscriptExportScope;onChange:(range:TranscriptExportScope['range'],body:TranscriptExportScope['body'])=>void;disabled:boolean}){
 const {t}=useI18n()
 return <fieldset disabled={disabled} className="stack stack--tight" style={{gridColumn:'1 / -1',minWidth:0,border:0,padding:0}}>
  <legend className="section-title">{t('chat.exportScope.title')}</legend>
  <div className="page-actions"><label className="field">{t('chat.exportScope.range')}<select value={scope.range} onChange={event=>onChange(event.target.value as TranscriptExportScope['range'],scope.body)}>
   <option value="all">{t('chat.exportScope.all')}</option><option value="matches">{t('chat.exportScope.matches')}</option>
  </select></label><label className="field">{t('chat.exportScope.body')}<select value={scope.body} onChange={event=>onChange(scope.range,event.target.value as TranscriptExportScope['body'])}>
   <option value="visible">{t('chat.exportScope.visible')}</option><option value="original">{t('chat.exportScope.original')}</option>
  </select></label></div>
  <TranscriptExportScopeSummary scope={scope}/>
  <p className="field-hint">{t('chat.exportScope.help')}</p>
  {scope.body==='original'&&<p className="notice notice--warning">{t('chat.exportScope.originalWarning')}</p>}
  {scope.messageCount===0&&<p role="status" className="notice notice--info">{t('chat.exportScope.empty')}</p>}
 </fieldset>
}
