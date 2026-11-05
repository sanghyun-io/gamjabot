import { useId, useState } from 'react'
import { useI18n } from '../../i18n'
import type { MessageKey } from '../../i18n/locales/en'
import type { ModelFeatureReport } from '../../api/connections'
import FeatureReportCopy from './FeatureReportCopy'
import { reviewTimestamp, reviewTimeOptions } from '../../lib/reviewTime'

export default function FeatureTable({report}:{report:ModelFeatureReport}){
 const {t,formatDateTime}=useI18n()
 const titleId=useId(), helpId=useId()
 const available=Object.entries(report.features).filter(([,item])=>item.available===true).map(([feature])=>t(`localModels.feature.${feature}` as MessageKey))
 // Feature metadata is UTC; saved SQLite execution history may omit its zone.
 // Invalid or absent values remain unrecorded rather than reaching Intl as an invalid Date.
 const observedAt=reviewTimestamp(report.observed_at),testedAt=reviewTimestamp(report.tested_at)
 const time=(value:string|null)=>value?<time dateTime={value}>{formatDateTime(value,reviewTimeOptions)}</time>:t('results.unrecorded')
 return <div className="stack stack--tight">
 <p><strong>{available.length?t('localModels.summary.available',{features:available.join(' · ')}):t('localModels.summary.none')}</strong></p>
 <details className="compact-details"><summary>{t('localModels.ui.featureDetails')}</summary><div className="model-features__scroll" role="region" tabIndex={0} aria-labelledby={titleId} aria-describedby={helpId}><table className="model-features"><caption id={titleId} className="sr-only">{t('localModels.tableRegion',{model:report.model_id})}</caption><thead><tr><th scope="col">{t('localModels.feature')}</th><th scope="col">{t('localModels.reported')}</th><th scope="col">{t('localModels.tested')}</th><th scope="col">{t('localModels.available')}</th></tr></thead><tbody>{Object.entries(report.features).map(([feature,item])=><tr key={feature}><th scope="row">{t(`localModels.feature.${feature}` as MessageKey)}</th><td>{t(item.reported===true?'localModels.yes':item.reported===false?'localModels.no':'localModels.unknown')}</td><td>{t(item.tested===true?'localModels.pass':item.tested===false?'localModels.fail':'localModels.notTested')}</td><td>{t(`localModels.reason.${item.reason}`)}</td></tr>)}</tbody></table></div>
 <p id={helpId} className="field-hint">{t('localModels.tableKeyboard')}</p>
 <p className="field-hint">{t('localModels.distinction')}</p>
 <p className="metadata">{t('localModels.observedAt')}: {time(observedAt)} · {t('localModels.testedAt')}: {time(testedAt)}</p>
 <p className="field-hint">{t('localModels.featureTimeHelp')}</p>
 {testedAt&&typeof report.actual_generation_requests==='number'&&Number.isSafeInteger(report.actual_generation_requests)&&report.actual_generation_requests>=0&&<p className="metadata">{report.actual_generation_requests} {t('localModels.requests')}</p>}
 <FeatureReportCopy report={report}/></details></div>
}

export function FeatureReportDetails({ report, summary, help }: { report: ModelFeatureReport; summary?: string; help?: string }) {
 const { t } = useI18n(), [expanded, setExpanded] = useState(false)
 return <details className="compact-details" onToggle={event => { if (event.target === event.currentTarget) setExpanded(event.currentTarget.open) }}>
  <summary>{summary || t('localModels.session.reportOriginal')}</summary>
  {expanded && <>{help && <p className="field-hint">{help}</p>}<FeatureTable report={report}/></>}
 </details>
}
