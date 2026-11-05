import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { agentsApi } from '../api/agents'
import { botLibraryApi, librarySourceHref } from '../api/botLibrary'
import { resultVersionHref } from '../lib/resultLink'
import { useBotScope } from '../lib/useBotScope'
import LibraryUploads from '../components/LibraryUploads'
import LibraryPinManager, { useLibraryPinActions } from '../components/LibraryPinManager'
import type { PinSource } from '../api/libraryPins'
import PageRename from '../components/PageRename'
import { useI18n } from '../i18n'

export default function BotLibrary(){
 const {agentId}=useParams(),id=agentId&&/^[1-9]\d*$/.test(agentId)?Number(agentId):0
 const scope=useBotScope(),valid=Number.isSafeInteger(id)&&id>0
 return <BotLibraryScope key={`${scope.identity}:${id}`} id={id} valid={valid} scope={scope}/>
}

function BotLibraryScope({id,valid,scope}:{id:number;valid:boolean;scope:ReturnType<typeof useBotScope>}){
 const {t,formatDateTime}=useI18n(),cache=useQueryClient(),[input,setInput]=useState(''),[query,setQuery]=useState(''),[searchRevision,setSearchRevision]=useState(0),[invalid,setInvalid]=useState(false)
 const [focusUpload,setFocusUpload]=useState<{id:string;sha:string;nonce:number}|null>(null)
 const openUpload=useCallback((id:string,sha:string)=>setFocusUpload({id,sha,nonce:Date.now()}),[])
 const submit=(e:FormEvent)=>{e.preventDefault();if(!scope.canUse())return;const next=input.trim();if(Array.from(next).length>100||Array.from(next).some(c=>c!==' '&&/[\p{C}\p{Z}]/u.test(c))){setInvalid(true);return}setInvalid(false);setQuery(next);setSearchRevision(n=>n+1)}
 const reset=()=>{if(scope.canUse()){setInput('');setQuery('');setInvalid(false);setSearchRevision(n=>n+1)}}
 const agent=useQuery({queryKey:['library-agent',scope.identity,id],enabled:valid,retry:false,queryFn:async()=>{const v=await agentsApi.get(id);if(!scope.canUse()||v.id!==id||v.user_id!==scope.ownerId)throw Error('Bot changed');return v}})
 const list=useInfiniteQuery({queryKey:['bot-library',scope.identity,id,query,searchRevision],enabled:valid,retry:false,initialPageParam:undefined as string|undefined,queryFn:async({pageParam})=>{const page=await botLibraryApi.list(id,pageParam,query);if(!scope.canUse())throw Error('Owner changed');return page},getNextPageParam:p=>p.next_cursor||undefined})
 const unavailable=(list.error as {response?:{status?:number}}|null)?.response?.status===404||(agent.error as {response?:{status?:number}}|null)?.response?.status===404
 const failed=!valid||agent.isError||list.isError,rows=list.data?.pages.flatMap(page=>page.items)||[]
 return <main className="page-frame page-frame--medium page-content stack"><header className="page-heading"><div><Link className="text-link" to="/agents">{t('library.back')}</Link><h1 className="page-title">{agent.data&&!failed?t('library.titleNamed',{name:agent.data.name}):t('library.title')}</h1><p className="section-copy">{t('library.scope')}</p></div></header>
 <LibraryPinManager key={`${scope.identity}:${id}`} botId={id} onUpload={openUpload}>
 <form className="page-actions" onSubmit={submit}><label className="field">{t('library.searchLabel')}<input value={input} onChange={e=>{setInput(e.target.value);setInvalid(false)}} maxLength={200} placeholder={t('library.searchPlaceholder')} /></label><button type="submit" className="button button--secondary button--compact">{t('library.searchSubmit')}</button>{(input||query)&&<button type="button" className="button button--ghost button--compact" onClick={reset}>{t('library.searchClear')}</button>}</form><p className="field-hint">{t('library.searchHint')}</p>{invalid&&<p role="alert">{t('library.searchInvalid')}</p>}
 {(agent.isPending||list.isPending)&&!failed&&<p role="status">{t('common.loading')}</p>}
 {failed&&<p role="alert">{t(unavailable?'library.unavailable':'library.error')} {valid&&!unavailable&&<button type="button" className="text-link" onClick={()=>{if(scope.canUse()){void agent.refetch();void cache.resetQueries({queryKey:['bot-library',scope.identity,id,query,searchRevision],exact:true})}}}>{t('common.retry')}</button>}</p>}
 {!failed&&agent.isSuccess&&list.isSuccess&&<>{rows.length===0?<p>{t(query?'library.searchEmpty':'library.empty')}</p>:<ul className="stack stack--tight" style={{listStyle:'none',padding:0}}>{rows.map(row=><li className="skill-row stack stack--tight" key={row.result_id}><strong>{row.title}</strong><p className="metadata">{t(`results.kind.${row.kind}`)} · {t(`results.state.${row.state}`)} · v{row.version} · {formatDateTime(row.updated_at)}</p><div className="page-actions"><PinResult source={{kind:'result',room_id:row.room_id,result_id:row.result_id,version:row.version}}/><Link className="text-link" onClick={e=>{if(!scope.canUse())e.preventDefault()}} to={resultVersionHref(row.room_id,row.result_id,row.version)!}>{t('library.open')}</Link><Link className="text-link" onClick={e=>{if(!scope.canUse())e.preventDefault()}} to={librarySourceHref(row)!}>{t('library.source')}</Link></div>{row.kind==='page'&&<PageRename key={`${scope.identity}:${row.result_id}`} ownerId={scope.ownerId} authEpoch={scope.epoch} roomId={row.room_id} resultId={row.result_id} version={row.version} title={row.title} enabled={!list.isFetching} canUse={scope.canUse} onSaved={()=>{if(scope.canUse())void cache.invalidateQueries({queryKey:['bot-library',scope.identity,id]})}}/>}</li>)}</ul>}{list.hasNextPage&&<button type="button" className="button button--secondary button--compact" disabled={list.isFetching} onClick={()=>{if(scope.canUse())void list.fetchNextPage()}}>{t('library.more')}</button>}</>}
 {agent.isSuccess&&valid&&<LibraryUploads key={`${scope.identity}:${id}`} botId={id} focusUpload={focusUpload}/>}
 </LibraryPinManager>
 </main>
}

function PinResult({source}:{source:PinSource}) {
 const actions=useLibraryPinActions(),{locale}=useI18n()
 const status=actions.status(source)
 return <button type="button" className="text-link" disabled={actions.busy||status===true} onClick={()=>actions.pin(source)}>{status===true?(locale==='ko'?'고정됨':'Pinned'):status===false?(locale==='ko'?'고정':'Pin'):(locale==='ko'?'고정 상태 확인 후 추가':'Check and pin')}</button>
}
