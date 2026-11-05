import { useEffect, useState } from 'react'
import type { SharedPrivateMemoryResult, SharedPrivateMemoryScope } from '../api/teamService'

export type SharedPrivateMemoryReader = (scope:SharedPrivateMemoryScope)=>Promise<SharedPrivateMemoryResult>
type Props = {scope:SharedPrivateMemoryScope;read:SharedPrivateMemoryReader;ko:boolean;startOpen?:boolean}

/** The memory list belongs only to the current shared account and publication. */
export function SharedPrivateMemory({scope,read,ko,startOpen=false}:Props) {
  const [open,setOpen]=useState(startOpen)
  const t=(k:string,e:string)=>ko?k:e
  const key=JSON.stringify([scope.room_id,scope.publication_id,scope.team_id,scope.account_id,scope.issuer,scope.session_generation])
  return <section className="surface stack stack--tight" aria-label={t('이 공유 Bot의 개인 기억','Private memory for this shared Bot')}>
    <div className="split"><h2 className="section-title">{t('이 공유 Bot의 개인 기억','Private memory for this shared Bot')}</h2><button type="button" className="button button--secondary button--compact" aria-expanded={open} onClick={()=>setOpen(value=>!value)}>{open?t('닫기','Close'):t('기억 보기','View memory')}</button></div>
    <p className="field-hint">{t('이 계정과 이 공유 Bot에만 적용됩니다. 공유자나 다른 계정에는 전달되지 않습니다. 선택한 모델이 지원하는 경우 대화에서 기억을 저장·수정하거나 잊어 달라고 요청할 수 있습니다. 잊은 내용은 이후 기억과 다음 요청의 과거 맥락에서 제외되며, 이미 표시된 대화는 남습니다.','This applies only to this account and shared Bot; the publisher and other accounts do not receive it. If the selected model supports it, ask in the conversation to save, edit, or forget a memory. Forgotten content is excluded from future memory and future request context; displayed conversation history remains.')}</p>
    {open&&<MemoryList key={key} scope={scope} read={read} ko={ko}/>}
  </section>
}
function MemoryList({scope,read,ko}:{scope:SharedPrivateMemoryScope;read:SharedPrivateMemoryReader;ko:boolean}) {
  const t=(k:string,e:string)=>ko?k:e
  const [attempt,setAttempt]=useState(0),[result,setResult]=useState<SharedPrivateMemoryResult|null>(null),[phase,setPhase]=useState<'loading'|'ready'|'error'>('loading')
  useEffect(()=>{
    let current=true
    setResult(null);setPhase('loading')
    read(scope).then(value=>{if(current){setResult(value);setPhase('ready')}}).catch(()=>{if(current){setResult(null);setPhase('error')}})
    return()=>{current=false}
  },[read,attempt])
  if(phase==='loading')return <p role="status">{t('개인 기억 확인 중…','Checking private memory…')}</p>
  if(phase==='error')return <div className="notice notice--warning stack stack--tight" role="alert"><p>{t('기억을 확인할 수 없습니다. 팀 연결과 공유 접근을 확인하세요.','Could not check memory. Check the team connection and shared access.')}</p><button type="button" className="button button--secondary button--compact" onClick={()=>setAttempt(value=>value+1)}>{t('다시 확인','Check again')}</button></div>
  return <div className="stack stack--tight">{result?.entries.length ? <ul>{result.entries.map(entry=><li key={entry.memory_id}><strong>{entry.category==='preference'?t('선호','Preference'):t('사실','Fact')}</strong> · {entry.content}</li>)}</ul> : <p>{t('저장된 개인 기억이 없습니다.','No private memories are saved.')}</p>}<button type="button" className="button button--secondary button--compact" onClick={()=>{setResult(null);setPhase('loading');setAttempt(value=>value+1)}}>{t('기억 새로고침','Refresh memory')}</button></div>
}
