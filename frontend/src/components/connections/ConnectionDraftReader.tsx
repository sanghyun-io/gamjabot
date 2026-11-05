import {Fragment,useMemo} from 'react'
import type {Capability,ProviderDescriptor} from '../../api/connections'
import type {ConnectionEditorDraft} from '../../lib/connectionEditorDraft'
import {readConnectionDraft} from '../../lib/connectionEditorDraft'
import {useClipboardText} from '../../lib/useClipboardText'
import {useI18n} from '../../i18n'
import ClipboardTextFallback from '../ClipboardTextFallback'
import type {MethodCopy} from './MethodFields'
import {providerDisplayName} from './ProviderCatalog'
export default function ConnectionDraftReader({draft,identity,provider,copy,canUse}:{draft:ConnectionEditorDraft;identity:string;provider?:ProviderDescriptor;copy:MethodCopy;canUse:()=>boolean}){
 const {t,formatDateTime}=useI18n(),text=useMemo(()=>JSON.stringify(draft.form,null,2),[draft])
 const context=useMemo(()=>({}),[draft,identity])
 const allowed=()=>canUse()&&readConnectionDraft(identity)===draft
 const clipboard=useClipboardText<{text:string}>(context,item=>allowed()&&item.text===text)
 const snapshot=()=>({text})
 const fieldName=(key:string)=>copy.fieldNames[key]||(key==='base_url'?copy.baseUrl:key==='allow_local_http'?copy.allowLocalHttp:`${copy.otherField} (${key})`)
 const fields=Object.entries(draft.form.fields),selected=(['max_tokens','temperature','tools','vision'] as Capability[]).filter(key=>draft.form.capabilities?.[key])
 const capability={max_tokens:'connections.cap.maxTokens',temperature:'connections.cap.temperature',tools:'connections.cap.tools',vision:'connections.cap.vision'} as const
 return <details className="compact-details"><summary>{t('connections.draft.inputs')}</summary><div className="stack stack--tight" style={{minWidth:0,overflowWrap:'anywhere'}}>
  <p className="field-hint">{t('connections.draft.reader.scope')}</p><p className="metadata">{t('connections.draft.reader.at',{time:formatDateTime(draft.changedAt,{dateStyle:'medium',timeStyle:'medium'})})}</p>
  <dl className="connection-confirm"><dt>{t('connections.name')}</dt><dd style={{whiteSpace:'pre-wrap'}}>{draft.form.label||t('connections.reviewUnset')}</dd><dt>{t('connections.provider')}</dt><dd>{provider?providerDisplayName(provider,t):draft.form.provider_id}</dd><dt>{t('connections.defaultModel')}</dt><dd style={{whiteSpace:'pre-wrap'}}>{draft.form.default_model_id||t('connections.reviewUnset')}</dd><dt>{copy.methodLegend}</dt><dd>{copy.methodNames[draft.form.auth_method_id]}</dd><dt>{copy.protocol}</dt><dd className="technical">{draft.form.protocol_id}</dd><dt>{t('connections.use')}</dt><dd>{t(draft.form.enabled===undefined?'connections.reviewUnset':draft.form.enabled?'connections.enabled':'connections.disabled')}</dd>
   {fields.map(([key,value])=><Fragment key={key}><dt>{fieldName(key)}</dt><dd style={{whiteSpace:'pre-wrap'}}>{typeof value==='boolean'?t(value?'connections.enabled':'connections.disabled'):value||t('connections.reviewUnset')}</dd></Fragment>)}<dt>{t('connections.draft.reader.requested')}</dt><dd>{selected.map(key=>t(capability[key])).join(', ')||t('connections.reviewRequestsNone')}</dd>
  </dl>
  <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={clipboard.busy} onClick={()=>void clipboard.copy(snapshot())}>{t(clipboard.busy?'common.loading':'connections.draft.reader.copy')}</button><button type="button" className="text-link" onClick={()=>clipboard.openManual(snapshot())}>{t('connections.draft.reader.select')}</button></div>
  {clipboard.result&&<p role={clipboard.result.status==='copied'?'status':'alert'} className="metadata">{t(clipboard.result.status==='copied'?'chat.code.copied':'chat.clipboard.failure')}</p>}
  {clipboard.manual&&<ClipboardTextFallback text={clipboard.manual.text} label={t('connections.draft.reader.manual')} caption={t('chat.clipboard.manualHelp')} canUse={clipboard.manualAllowed} onClose={clipboard.closeManualAndRestore} onRetry={()=>{if(clipboard.manual)void clipboard.copy(clipboard.manual)}} onInteract={clipboard.touchManual} busy={clipboard.busy}/>}
  <details className="compact-details"><summary>{copy.details}</summary><pre className="technical" style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{text}</pre></details>
 </div></details>
}
