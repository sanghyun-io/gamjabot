import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { isTauri } from '@tauri-apps/api/core'
import { saveVerifiedBinaryFile } from '../lib/binaryFileSave'
import type { GeneratedSaveOutcome } from '../lib/generatedFileSave'
import { useAuthStore } from '../stores/authStore'
import { loadVoiceMemo, sha256Bytes, validMemoWav, validVoiceMemo, type VoiceMemo, type VoiceMemoLoader, type VoiceMemoSource } from '../api/voiceMemos'

type Phase = 'verifying' | 'ready' | 'loading' | 'loaded' | 'missing' | 'changed' | 'unsupported' | 'unknown' | 'failed' | 'player_error'
export default function VoiceMemoArtifact({ memo, source, roomId, ownerId, epoch, canUse, availability, load = loadVoiceMemo }: { memo:VoiceMemo; source:VoiceMemoSource; roomId:number; ownerId:number; epoch:number; canUse:()=>boolean; availability?:'unchecked'|'removed'|'unavailable'|null; load?:VoiceMemoLoader }) {
  const { locale, t } = useI18n(), ko = locale === 'ko', currentOwner = useAuthStore(s=>s.user?.id), currentEpoch = useAuthStore(s=>s.epoch)
  const [phase,setPhase] = useState<Phase>('verifying'), [url,setUrl] = useState(''), [textValid,setTextValid] = useState(false), [verifiedKey,setVerifiedKey] = useState('')
  const fingerprint = JSON.stringify([memo,source,roomId,ownerId,epoch])
  // Contains only scoped identifiers, never transcript or audio bytes.
  const saveKey = `gamja.voice-memo-save.${ownerId}.${epoch}.${roomId}.${memo.action_id}.${memo.review_id}.${memo.grant_id}.${memo.sha256}.${source.kind === 'result' ? `${source.resultId}.${source.version}` : 'event'}`
  const [saveState,setSaveState] = useState<{key:string;outcome:GeneratedSaveOutcome}|null>(null), [saving,setSaving] = useState(false)
  const saveOutcome = saveState?.key===saveKey ? saveState.outcome : null
  const saveLock=useRef(false), saveBytes=useRef<ArrayBuffer|null>(null), liveFingerprint=useRef(fingerprint);liveFingerprint.current=fingerprint
  const native=isTauri()
  useEffect(()=>{ try {setSaveState(sessionStorage.getItem(saveKey)?{key:saveKey,outcome:{phase:'unknown'}}:null)} catch {setSaveState({key:saveKey,outcome:{phase:'unknown'}})} },[saveKey])
  const live = useRef({canUse,load}); live.current = {canUse,load}
  const mounted = useRef(false), request = useRef<AbortController|null>(null), objectUrl = useRef<string|null>(null), player = useRef<HTMLAudioElement|null>(null), revision = useRef(0)
  const current = () => { const auth=useAuthStore.getState(); return mounted.current && !!auth.token && auth.user?.id === ownerId && auth.epoch === epoch && live.current.canUse() }
  const release = () => { saveBytes.current=null; request.current?.abort(); request.current=null; const old=objectUrl.current; objectUrl.current=null; if(player.current){player.current.pause(); player.current.removeAttribute('src'); player.current.load()} if(old) URL.revokeObjectURL(old) }
  useLayoutEffect(()=>{revision.current++;release();setUrl('');setTextValid(false);setVerifiedKey('');setPhase('verifying')},[fingerprint])
  useEffect(()=>{ mounted.current=true; const serial=++revision.current
    if (!validVoiceMemo(memo,ownerId,roomId)) { setPhase('changed'); return () => { mounted.current=false; revision.current++; release() } }
    void sha256Bytes(new TextEncoder().encode(memo.transcript)).then(digest=>{if(current()&&revision.current===serial){if(digest===memo.text_sha256){setVerifiedKey(fingerprint);setTextValid(true);setPhase(memo.deleted||availability==='removed'?'missing':availability==='unavailable'?'failed':'ready')}else setPhase('changed')}}).catch(()=>{if(mounted.current&&revision.current===serial)setPhase('unsupported')})
    return()=>{mounted.current=false;revision.current++;release()}
  },[fingerprint,availability])
  useLayoutEffect(()=>{if(currentOwner!==ownerId||currentEpoch!==epoch||!canUse()){revision.current++;release();setUrl('');setTextValid(false);setPhase('unknown')}},[currentOwner,currentEpoch,canUse,ownerId,epoch])
  useEffect(()=>{ if(phase!=='unknown'||textValid||!current()||!validVoiceMemo(memo,ownerId,roomId))return;const serial=++revision.current;setPhase('verifying');void sha256Bytes(new TextEncoder().encode(memo.transcript)).then(digest=>{if(current()&&revision.current===serial){if(digest===memo.text_sha256){setVerifiedKey(fingerprint);setTextValid(true);setPhase(memo.deleted||availability==='removed'?'missing':availability==='unavailable'?'failed':'ready')}else setPhase('changed')}}).catch(()=>{if(current()&&revision.current===serial)setPhase('unsupported')}) },[phase,textValid,canUse,ownerId,epoch,roomId,memo])
  useLayoutEffect(()=>{if(availability==='removed'||availability==='unavailable'){revision.current++;release();setUrl('');setPhase(availability==='removed'?'missing':'failed')}},[availability])
  const open = async () => {
    if (!current() || phase !== 'ready' || !textValid || verifiedKey!==fingerprint || request.current) return
    if (!('createObjectURL' in URL) || !crypto.subtle) {setPhase('unsupported');return}
    const controller=new AbortController(),serial=revision.current;request.current=controller;setPhase('loading')
    try { const result=await live.current.load(roomId,memo,source,controller.signal)
      if(!current()||controller.signal.aborted||serial!==revision.current)return
      const valid=result.mediaType==='audio/wav' && validMemoWav(result.data,memo) && await sha256Bytes(result.data)===memo.sha256
      if(!current()||controller.signal.aborted||serial!==revision.current)return
      if(!valid) {setPhase('changed');return}
      release();const next=URL.createObjectURL(new Blob([result.data],{type:'audio/wav'}));objectUrl.current=next;setUrl(next);setPhase('loaded')
    } catch(e) { if(current()&&!controller.signal.aborted&&serial===revision.current){const status=(e as {response?:{status?:number}})?.response?.status;setPhase(status===410?'missing':status===404?'failed':status===409?'changed':status===415?'unsupported':'unknown')} }
    finally {if(request.current===controller)request.current=null}
  }
  const save = async () => {
    if (!native || !current() || saveLock.current || request.current || !textValid || verifiedKey!==fingerprint || !['ready','loaded'].includes(phase) || saveOutcome?.phase==='unknown' || memo.deleted || availability==='removed' || availability==='unavailable') return
    const key=saveKey, original=fingerprint, serial=revision.current, controller=new AbortController()
    saveLock.current=true;request.current=controller;setSaving(true);setSaveState(null)
    let marked=false, invoked=false
    const stillCurrent=()=>current() && liveFingerprint.current===original && revision.current===serial && !controller.signal.aborted && !!saveBytes.current
    try {
      if(sessionStorage.getItem(key)) {setSaveState({key,outcome:{phase:'unknown'}});return}
      sessionStorage.setItem(key,'pending');marked=true
      const result=await live.current.load(roomId,memo,source,controller.signal)
      if(!current() || liveFingerprint.current!==original || revision.current!==serial || controller.signal.aborted){sessionStorage.removeItem(key);return}
      if(result.mediaType!=='audio/wav' || !validMemoWav(result.data,memo) || await sha256Bytes(result.data)!==memo.sha256) {
        if(current() && liveFingerprint.current===original && revision.current===serial) { release();setUrl('');setPhase('changed') }
        sessionStorage.removeItem(key);return
      }
      if(!current() || liveFingerprint.current!==original || revision.current!==serial || controller.signal.aborted){sessionStorage.removeItem(key);return}
      saveBytes.current=result.data;invoked=true
      const outcome=await saveVerifiedBinaryFile(result.data,{filename:`voice-memo-${memo.action_id}.wav`,size_bytes:memo.size_bytes,sha256:memo.sha256},ko?'음성 메모 저장':'Save voice memo',stillCurrent)
      if(outcome.phase!=='unknown')sessionStorage.removeItem(key)
      if(current() && liveFingerprint.current===original) setSaveState({key,outcome})
    } catch(error) {
      let uncertain=invoked || !marked
      if(!uncertain)try{sessionStorage.removeItem(key)}catch{uncertain=true}
      if(current() && liveFingerprint.current===original && !controller.signal.aborted) {
        const status=(error as {response?:{status?:number}})?.response?.status
        if(!uncertain && (status===410||status===404||status===409||status===415)) {release();setUrl('');setPhase(status===410?'missing':status===404?'failed':status===409?'changed':'unsupported')}
        else setSaveState({key,outcome:{phase:uncertain?'unknown':'failed'}})
      }
    } finally {if(request.current===controller)request.current=null;saveBytes.current=null;saveLock.current=false;if(mounted.current)setSaving(false)}
  }
  const messages:Record<Phase,string> = ko ? {verifying:'내용 확인 중',ready:'텍스트가 확인되었습니다. 음성을 불러올 수 있습니다.',loading:'음성 파일 확인 중',loaded:'재생 버튼을 눌러 들으세요.',missing:'음성 파일이 삭제되었습니다.',changed:'내용이나 음성 파일이 변경되어 열 수 없습니다.',unsupported:'이 환경에서 음성 메모를 열 수 없습니다.',unknown:'음성 파일을 확인하지 못했습니다.',failed:'음성 파일을 찾을 수 없습니다.',player_error:'음성 재생에 실패했습니다.'} : {verifying:'Checking transcript',ready:'Transcript verified. Load audio to listen.',loading:'Checking audio',loaded:'Press play to listen.',missing:'The audio was removed.',changed:'The transcript or audio changed and cannot be opened.',unsupported:'Voice memos are unavailable in this environment.',unknown:'The audio could not be confirmed.',failed:'The audio file is unavailable.',player_error:'Audio playback failed.'}
  if (currentOwner!==ownerId||currentEpoch!==epoch) return null
  return <section className="notice stack stack--tight" aria-label={ko?'음성 메모':'Voice memo'}><strong>{ko?'음성 메모':'Voice memo'}</strong>
    {textValid && verifiedKey===fingerprint && <details open><summary>{ko?'텍스트 보기':'Transcript'}</summary><p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{memo.transcript}</p></details>}
    <p role={['changed','failed','unsupported','unknown','player_error'].includes(phase)?'alert':'status'} className="field-hint">{messages[phase]}</p>
    {phase==='ready' && <button type="button" className="button button--secondary button--compact" disabled={saving} onClick={()=>void open()}>{ko?'음성 불러오기':'Load audio'}</button>}
    {phase==='loading' && <button type="button" className="button button--secondary button--compact" onClick={()=>{revision.current++;release();setPhase('ready')}}>{ko?'취소':'Cancel'}</button>}
    {phase==='loaded' && url && <audio ref={player} controls preload="metadata" src={url} onError={()=>{if(objectUrl.current){release();setUrl('');setPhase('player_error')}}}>{ko?'이 환경에서 재생할 수 없습니다.':'Audio playback is unavailable.'}</audio>}
    {textValid && verifiedKey===fingerprint && ['ready','loaded'].includes(phase) && (native ? <button type="button" className="button button--secondary button--compact" disabled={saving || saveOutcome?.phase==='unknown'} onClick={()=>void save()}>{ko?'기기에 저장':'Save to device'}</button> : <p className="field-hint">{ko?'기기에 저장하려면 Mac 앱을 사용하세요.':'Use the Mac app to save to your device.'}</p>)}
    {saving && <p role="status">{ko?'저장 준비 중':'Preparing to save'}</p>}
    {saveOutcome && <p role={saveOutcome.phase==='failed'||saveOutcome.phase==='unknown'?'alert':'status'}>{t(`generatedFile.save.${saveOutcome.phase}`)}{saveOutcome.phase==='saved' && <> {saveOutcome.filename}{saveOutcome.receipt.durability_unconfirmed && <> {t('generatedFile.saveDurability')}</>}</>}</p>}
    {saveOutcome?.phase==='unknown' && <button type="button" className="button button--secondary button--compact" onClick={()=>{if(!current())return;try{sessionStorage.removeItem(saveKey);setSaveState(null)}catch{/* Keep the unresolved marker and do not enable a retry. */}}}>{t('generatedFile.saveChecked')}</button>}
    {(phase==='unknown'||phase==='failed'||phase==='player_error') && textValid && verifiedKey===fingerprint && <button type="button" className="button button--secondary button--compact" onClick={()=>setPhase('ready')}>{ko?'다시 확인':'Check again'}</button>}
  </section>
}
