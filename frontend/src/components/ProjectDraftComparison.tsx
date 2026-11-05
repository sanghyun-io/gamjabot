import ReadOnlyComparisonValue from './ReadOnlyComparisonValue'
import {useI18n} from '../i18n'
import type {ProjectDraft} from '../lib/projectEditorWorkspace'
// Read-only projection. No draft setter, receipt lookup, network or save action.
export default function ProjectDraftComparison({baseline,received,draft}:{baseline:ProjectDraft;received:ProjectDraft;draft:ProjectDraft}){
 const {t}=useI18n()
 const columns=[{id:'baseline',label:t('projects.compare.baseline'),value:baseline},{id:'received',label:t('projects.compare.received'),value:received},{id:'draft',label:t('projects.compare.draft'),value:draft}]
 return <section className="stack stack--tight" aria-label={t('projects.compare.title')} style={{minWidth:0,overflowWrap:'anywhere'}}><p className="field-hint">{t('projects.compare.scope')}</p><p className="field-hint">{t('editor.compare.readingHelp')}</p>
  {(['name','description'] as const).map(field=><div key={field} className="stack stack--tight"><h3 className="section-title">{t(field==='name'?'projects.name':'projects.description')}</h3><div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(min(100%, 210px),1fr))',gap:12,minWidth:0}}>{columns.map(column=><div key={column.id} className="notice stack stack--tight" style={{minWidth:0}}><strong>{column.label}</strong>{column.id!=='baseline'&&column.value[field]!==baseline[field]&&<span className="metadata">{t('editor.compare.differs')}</span>}<ReadOnlyComparisonValue text={column.value[field]||t('projects.compare.empty')}/></div>)}</div></div>)}
 </section>
}
