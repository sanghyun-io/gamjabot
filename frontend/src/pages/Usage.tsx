import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AppShell, PageHeader } from '../components/ui'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { en, type MessageKey } from '../i18n/locales/en'
import { usageApi, type UsageReport } from '../api/usage'

export default function Usage() {
  const identity = useAuthStore(s => `${s.user?.id ?? 0}:${s.epoch}`)
  return <UsageView key={identity} identity={identity}/>
}
function UsageView({identity}:{identity:string}) {
  const {t} = useI18n(), cache = useQueryClient()
  const [days,setDays] = useState(30), [pricesBusy,setPricesBusy] = useState(false), [priceError,setPriceError] = useState(false)
  const [visible,setVisible] = useState(document.visibilityState === 'visible')
  const alive = useRef(true), refreshLock = useRef(false)
  useEffect(() => {alive.current=true;const change=()=>setVisible(document.visibilityState==='visible');document.addEventListener('visibilitychange',change);return()=>{alive.current=false;document.removeEventListener('visibilitychange',change)}}, [])
  const current=()=>alive.current && !!useAuthStore.getState().token && `${useAuthStore.getState().user?.id ?? 0}:${useAuthStore.getState().epoch}` === identity
  const key=['model-usage',identity,days]
  const query=useQuery({queryKey:key,queryFn:async()=>{if(!current())throw Error('owner_changed');const data=await usageApi.read(days);if(!current())throw Error('owner_changed');return data},refetchInterval:visible?30000:false,refetchIntervalInBackground:false,retry:false})
  const data: UsageReport | undefined=query.data
  const number=(v:number|null|undefined)=>v==null?t('usage.unknown'):v.toLocaleString()
  const usd=(v:number|null|undefined)=>v==null?t('usage.unknown'):`USD ${v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:8})}`
  const providerName=(value:string)=>{const key=`connections.providers.${value}`;return Object.prototype.hasOwnProperty.call(en,key)?t(key as MessageKey):value}
  const stamp=(v:string|null|undefined)=>v?new Date(v).toLocaleString():t('usage.unknown')
  const groups=data?.groups??[], priced=groups.reduce((n,r)=>n+r.priced,0), attempts=groups.reduce((n,r)=>n+r.attempts,0)
  const refreshPrices=async()=>{if(!current()||refreshLock.current)return;refreshLock.current=true;setPricesBusy(true);setPriceError(false);try{const result=await usageApi.prices(days);if(current())cache.setQueryData(key,result)}catch{if(current())setPriceError(true)}finally{refreshLock.current=false;if(current())setPricesBusy(false)}}
  return <AppShell><PageHeader title={t('usage.title')} backTo="/"/>
    <main className="page-frame page-frame--wide page-content stack usage-page">
      <div className="usage-toolbar page-tools"><label>{t('usage.period')} <select value={days} onChange={e=>setDays(Number(e.target.value))}>{[1,7,30,90,365].map(n=><option key={n} value={n}>{t('usage.days',{days:n})}</option>)}</select></label><button className="button button--secondary" disabled={query.isFetching} onClick={()=>void query.refetch()}>{t('usage.reload')}</button></div>
      {query.isPending&&<p role="status">{t('common.loading')}</p>}
      {query.isError&&<p role="alert" className="notice notice--danger inline-feedback">{t('usage.failed')}</p>}
      {data&&<section className="usage-overview">
        <div><h2 className="metadata">{t('usage.subtotal')}</h2><p className="usage-total">{usd(priced?groups.reduce((n,r)=>n+(r.estimate_usd??0),0):null)}</p></div>
        <div className="stack stack--tight"><p className="metadata">{t('usage.ui.reference')}</p><p className="metadata">{t('usage.ui.coverage',{priced,attempts})}</p></div>
      </section>}
      {data&&data.dropped_records>0&&<p role="alert" className="notice notice--warning">{t('usage.dropped',{count:data.dropped_records})}</p>}
      {(priceError||data?.last_error)&&<p role="alert" className="notice notice--warning">{t('usage.priceFailed')}</p>}
      {data&&<section className="stack usage-model-list"><h2 className="section-title">{t('usage.byModel')}</h2>{groups.length===0&&<p>{t('usage.empty')}</p>}{groups.map(r=><article key={`${r.connection_id}:${r.provider}:${r.model}:${r.auth}:${r.billing}`} className="surface-card stack stack--tight usage-model-card" style={{overflowWrap:'anywhere'}}>
        <div className="usage-model-card__heading"><h3 className="section-title">{providerName(r.provider)} / {r.model}</h3><p className="usage-model-card__cost"><strong>{r.billing==='subscription'?t('usage.subscription'):r.billing==='local'?t('usage.local'):usd(r.estimate_usd)}</strong></p></div>
        <p className="metadata">{t('usage.attempts')}: {number(r.attempts)}</p>
        <div className="page-actions usage-model-card__tokens"><span>{t('usage.input')}: {number(r.input_tokens)}</span><span>{t('usage.output')}: {number(r.output_tokens)}</span></div>
        <details className="compact-details"><summary>{t('usage.details')}</summary><div className="stack stack--tight"><p className="metadata">{t('usage.connection')} #{r.connection_id??'—'} · {r.auth??'—'}</p><p>{t('usage.reportedAttempts', {input:r.input_reported,output:r.output_reported,total:r.attempts})}</p><p>{t('usage.priced',{count:r.priced})}</p><p>{t('usage.cacheRead')}: {number(r.cache_read)} · {t('usage.cacheWrite')}: {number(r.cache_write)} · {t('usage.reasoning')}: {number(r.reasoning)}</p><p className="field-hint">{t('usage.cacheHelp')}</p><p>{t('usage.priceVersion')}: {stamp(r.price_at)}</p></div></details>
      </article>)}</section>}
      <details className="usage-accounting-details directory-supporting-details"><summary>{t('usage.ui.details')}</summary><div className="stack">
        <p className="field-hint">{t('usage.subtitle')}</p><p className="field-hint">{t('usage.auto')}</p><p className="field-hint">{t('usage.coverage',{priced,attempts})}</p><p className="field-hint">{t('usage.snapshot')}</p>
        {data&&<p className="metadata">{t('usage.started')}: {stamp(data.capture_started)} · {t('usage.received')}: {stamp(query.dataUpdatedAt?new Date(query.dataUpdatedAt).toISOString():null)}</p>}
        <section className="stack stack--tight"><h2 className="section-title">{t('usage.prices')}</h2><p>{t('usage.priceHelp')}</p><a className="text-link" href="https://models.dev/" target="_blank" rel="noopener noreferrer">Models.dev</a><div className="page-actions"><button className="button button--secondary" disabled={pricesBusy} onClick={()=>void refreshPrices()}>{t(pricesBusy?'common.loading':'usage.refreshPrices')}</button><span className="metadata">{t('usage.lastCheck')}: {stamp(data?.last_success)}</span></div>{data?.prices.map(p=><details key={`${p.provider}:${p.model}`}><summary style={{overflowWrap:'anywhere'}}>{p.provider} / {p.model} — {p.cost?t('usage.perMillion'):t('usage.noPrice')}</summary>{p.cost&&<><p>{t('usage.input')}: {typeof p.cost.input==='number'?usd(p.cost.input):t('usage.unknown')} · {t('usage.output')}: {typeof p.cost.output==='number'?usd(p.cost.output):t('usage.unknown')}</p><p>{t('usage.cacheRead')}: {typeof p.cost.cache_read==='number'?usd(p.cost.cache_read):t('usage.unknown')} · {t('usage.cacheWrite')}: {typeof p.cost.cache_write==='number'?usd(p.cost.cache_write):t('usage.unknown')}</p><p className="metadata">{t('usage.priceVersion')}: {stamp(p.observed_at)}</p><p className="field-hint">{t('usage.tiers')}</p></>}</details>)}</section>
        <section className="stack stack--tight"><h2 className="section-title">{t('usage.limitsTitle')}</h2><p>{t('usage.limits')}</p><p>{t('usage.scope')}</p><p>{t('usage.privacy')}</p></section>
        <div><Link className="text-link" to="/connections">{t('connections.title')}</Link></div>
      </div></details>
    </main></AppShell>
}
