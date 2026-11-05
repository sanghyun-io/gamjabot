import GoalEvidencePicker from './GoalEvidencePicker'
import { goalEvidenceChoices,type GoalEvidenceChoice } from '../lib/goalEvidence'
import type { RoomEvent } from '../api/events'
import WatchDraftShelf from './WatchDraftShelf'
import ZonedServerTime from './ZonedServerTime'
import WatchFailureNotice from './WatchFailureNotice'
import WatchEndDialog,{sameWatchEndTarget,watchEndTarget,type WatchEndTarget} from './WatchEndDialog'
import {planRejection,planRejectionMessages} from '../lib/roomPlanFeedback'
import {goalInputIssues,type GoalInputField,type GoalInputIssue} from '../lib/goalInputFeedback'
import {watchInputIssues,type WatchInputField} from '../lib/watchInputFeedback'
import {useEffect,useId,useLayoutEffect,useMemo,useRef,useState,useSyncExternalStore} from 'react'
import {useQuery,useQueryClient} from '@tanstack/react-query'
import api from '../lib/api'
import {useAuthStore} from '../stores/authStore'
import {useI18n} from '../i18n'
import {useImeFormGuard} from '../lib/useImeFormGuard'
import {editorSaveFailure} from './EditorSaveNotice'
import {directoryViewGeneration} from '../lib/directoryViewMemory'
import {readPlanEditor,subscribePlanEditors,updatePlanEditor,moveGoalCriterion,removeGoalCriterion,restoreGoalCriterion,selectWatchDraft,watchDraftKey,type Goal,type Watch,type WatchForm,type PlanData as Data,type PlanKind} from '../lib/roomPlanEditorState'

const uid=()=>crypto.randomUUID().replaceAll('-','')
const freshWatch=():WatchForm=>({command_id:uid(),name:'',url:'',interval_minutes:'30',timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC',mode:'text_change',keyword:'',stop_after_change:false,enabled:false})
const emptyEvents:readonly RoomEvent[]=[]
export default function RoomPlans({roomId,readOnly=false,events=emptyEvents,visibleMessageTexts,historyComplete=false,onNavigate}:{roomId:number;readOnly?:boolean;events?:readonly RoomEvent[];visibleMessageTexts?:ReadonlyMap<RoomEvent,string>;historyComplete?:boolean;onNavigate?:(seq:number,eventId:string)=>void}){
 const {t,formatDateTime}=useI18n(),cache=useQueryClient(),identity=useAuthStore(s=>`${s.user?.id||0}:${s.epoch}`),generation=useRef(0)
 const [open,setOpen]=useState(false)
 const [ending,setEnding]=useState<{scope:string;target:WatchEndTarget}|null>(null)
 const watchPrefix=useId(),watchFormRef=useRef<HTMLFormElement>(null)
 const goalPrefix=useId(),goalFormRef=useRef<HTMLFormElement>(null)
 const [goalFocus,setGoalFocus]=useState<{field:GoalInputField;value:Goal;revision:number;origin:Element|null;mode:'invalid'|'added'}|null>(null)
 const [watchFocus,setWatchFocus]=useState<{field:WatchInputField;value:WatchForm;revision:number;origin:Element|null}|null>(null)
 const mounted=useRef(true),scope=`${roomId}:${identity}`,scopeRef=useRef(scope),ownerGeneration=directoryViewGeneration()
 scopeRef.current=scope
 const editor=useSyncExternalStore(subscribePlanEditors,()=>readPlanEditor(identity,roomId))
 const watchSession=editor.watchSession||'legacy'
 const {draft,watch,notice}=editor,busy=editor.operation!==null
 const error=editor.error?t(editor.error==='unknown'?'roomPlans.unknownWrite':editor.error==='changed'?'roomPlans.changedHelp':editor.rejection?planRejectionMessages[editor.rejection.reason]:'roomPlans.error'):''
 const edit=(update:Parameters<typeof updatePlanEditor>[3])=>updatePlanEditor(identity,roomId,ownerGeneration,update)
 const isBusy=()=>readPlanEditor(identity,roomId).operation!==null
 const sameOwner=(sent=scope)=>{const state=useAuthStore.getState();return directoryViewGeneration()===ownerGeneration&&sent===`${roomId}:${state.user?.id||0}:${state.epoch}`}
 const current=(sent:string,run:number)=>mounted.current&&sent===scopeRef.current&&sameOwner(sent)&&run===generation.current
 useEffect(()=>{mounted.current=true;generation.current++;setEnding(null);return()=>{mounted.current=false;generation.current++}},[scope])
 const goalIme=useImeFormGuard(`${scope}:goal`),watchIme=useImeFormGuard(`${scope}:watch:${watchSession}`)
 const key=['room-plan',roomId,identity],base=`/conversations/${roomId}/plan`
 const plan=useQuery({queryKey:key,enabled:open,retry:false,refetchInterval:open&&!busy?10000:false,queryFn:async()=>{
  const sent=scope
  if(!sameOwner(sent))throw Error('Changed')
  const result=(await api.get<Data>(base)).data
  if(!sameOwner(sent))throw Error('Changed')
  return result
 }})
 const clearFeedback=()=>edit(previous=>previous.operation?previous:{...previous,error:previous.error==='unknown'?'unknown':'',rejection:null,notice:''})
 const setNotice=(value:''|'saving'|'saved')=>edit(previous=>previous.operation?previous:{...previous,notice:value})
 const setDraft=(value:Goal|null)=>{
  if(!current(scope,generation.current))return
  setGoalFocus(null)
  return edit(previous=>previous.operation?previous:{...previous,draft:value,removedGoalCriterion:value?previous.removedGoalCriterion:null,goalValidation:!!value&&previous.goalValidation&&goalInputIssues(value).length>0,goalRevision:previous.goalRevision+1,notice:''})
 }
 const setWatch=(value:WatchForm|null)=>{
  if(!current(scope,generation.current))return
  edit(previous=>previous.operation?previous:{...previous,watch:value,watchDrafts:value?previous.watchDrafts:(previous.watchDrafts||[]).filter(form=>!previous.watch||watchDraftKey(form)!==watchDraftKey(previous.watch)),watchSession:crypto.randomUUID(),watchValidation:false,watchRevision:previous.watchRevision+1,notice:''})
 }
 const changeWatch=(part:Partial<WatchForm>)=>{
  if(readOnly||!current(scope,generation.current))return
  setWatchFocus(null)
  edit(previous=>{
   if(previous.operation||!previous.watch||(previous.watchSession||'legacy')!==watchSession)return previous
   const changed={...previous.watch,...part}
   return {...previous,watch:changed,watchValidation:previous.watchValidation&&watchInputIssues(changed).length>0,watchRevision:previous.watchRevision+1,notice:''}
  })
 }
 const act=async(work:()=>Promise<Goal|Watch>,kind:PlanKind='control',allowReadOnly=false,subject='')=>{
  const sent=scope,snapshot=readPlanEditor(identity,roomId)
  if(snapshot.operation||!current(sent,generation.current)||(readOnly&&!allowReadOnly))return
  if(snapshot.error==='unknown'&&!window.confirm(t('roomPlans.retryUnknownConfirm')))return
  if(!current(sent,generation.current))return
  const operation=uid()
  if(!edit(previous=>previous!==snapshot?previous:{...previous,operation,goalValidation:kind==='goal'?false:previous.goalValidation,watchValidation:kind==='watch'?false:previous.watchValidation,error:'',rejection:null,notice:'saving',result:null}))return
  const label=subject||(kind==='goal'?(snapshot.draft||plan.data?.goal)?.title:snapshot.watch?.name)||''
  try{
   await cache.cancelQueries({queryKey:key})
   if(!sameOwner(sent)||readPlanEditor(identity,roomId).operation!==operation)return
   const value=await work()
   if(!sameOwner(sent)||readPlanEditor(identity,roomId).operation!==operation)return
   cache.setQueryData<Data>(key,previous=>{
    if(!previous)return previous
    if(kind==='goal')return {...previous,goal:value as Goal}
    const saved=value as Watch
    return {...previous,watches:previous.watches.some(row=>row.id===saved.id)?previous.watches.map(row=>row.id===saved.id?saved:row):[...previous.watches,saved]}
   })
   edit(previous=>previous.operation!==operation?previous:{...previous,operation:null,error:'',rejection:null,notice:'saved',
    draft:kind==='goal'&&previous.goalRevision===snapshot.goalRevision?null:previous.draft,
    removedGoalCriterion:kind==='goal'&&previous.goalRevision===snapshot.goalRevision?null:previous.removedGoalCriterion,
    watch:kind==='watch'&&previous.watchRevision===snapshot.watchRevision?null:previous.watch,
    watchDrafts:kind==='watch'&&previous.watchRevision===snapshot.watchRevision&&snapshot.watch?(previous.watchDrafts||[]).filter(form=>watchDraftKey(form)!==watchDraftKey(snapshot.watch!)):previous.watchDrafts,
    result:kind==='goal'?{kind:'goal',value:value as Goal}:{kind,value:value as Watch}})
   void cache.invalidateQueries({queryKey:key})
  }catch(failure){
   if(!sameOwner(sent))return
   const code=(failure as {response?:{data?:{detail?:{code?:string}}}}).response?.data?.detail?.code
   const unknown=editorSaveFailure(failure)==='unknown'
   const rejection=unknown?null:planRejection(failure)
   edit(previous=>previous.operation!==operation?previous:{...previous,operation:null,notice:'',
    error:unknown?'unknown':code==='room_plan_changed'||code==='room_plan_command_changed'?'changed':'failure',
    rejection:rejection?{reason:rejection,kind,subject:label}:null,
    uncertain:unknown?[...previous.uncertain,{id:operation,kind,subject:label}]:previous.uncertain})
   void cache.invalidateQueries({queryKey:key})
  }
 }
 const goal=draft||plan.data?.goal||(editor.result?.kind==='goal'?editor.result.value:undefined)
 const update=(part:Partial<Goal>)=>{if(goal&&!isBusy()&&!readOnly&&current(scope,generation.current)){setDraft({...goal,...part});setNotice('')}}
 const evidenceChoices=useMemo(()=>open?goalEvidenceChoices(roomId,events,plan.data?.evidence||[],visibleMessageTexts):[],[open,roomId,events,plan.data?.evidence,visibleMessageTexts])
 const evidenceBySeq=useMemo(()=>new Map(evidenceChoices.map(row=>[row.seq,row])),[evidenceChoices]),evidenceRef=useRef(evidenceBySeq);evidenceRef.current=evidenceBySeq
 const evidenceCurrent=(choice:GoalEvidenceChoice)=>current(scope,generation.current)&&evidenceRef.current.get(choice.seq)?.eventId===choice.eventId
 const changeEvidence=(criterionId:string,expected:Goal,choice:GoalEvidenceChoice|null)=>{
  if(readOnly||isBusy()||!current(scope,generation.current)||choice&&!evidenceCurrent(choice))return
  const live=readPlanEditor(identity,roomId),value=live.draft||cache.getQueryData<Data>(key)?.goal||(live.result?.kind==='goal'?live.result.value:undefined)
  if(!value||value!==expected||!value.criteria.some(row=>row.id===criterionId))return
  setDraft({...value,complete:false,criteria:value.criteria.map(row=>row.id===criterionId?{...row,evidence_seq:choice?.seq??null,done:false}:row)})
 }
 const setCriteriaOpen=(ids:readonly string[],expanded:boolean,expected=goal)=>{
  if(!expected||isBusy()||!current(scope,generation.current))return
  const snapshot=readPlanEditor(identity,roomId),value=snapshot.draft||cache.getQueryData<Data>(key)?.goal||(snapshot.result?.kind==='goal'?snapshot.result.value:undefined)
  if(value!==expected)return
  edit(previous=>{
   if(previous!==snapshot||previous.operation)return previous
   const active=new Set(value.criteria.map(row=>row.id)),wanted=new Set(ids.filter(id=>active.has(id)))
   if(![...wanted].some(id=>(previous.goalCriteriaOpen?.[id]??true)!==expanded))return previous
   const preferences=Object.fromEntries(Object.entries(previous.goalCriteriaOpen||{}).filter(([id])=>active.has(id)))
   for(const id of wanted)preferences[id]=expanded
   return {...previous,goalCriteriaOpen:Object.freeze(preferences)}
  })
 }
 const latestGoal=plan.data?.goal,goalChanged=!!(draft&&latestGoal&&draft.revision!==latestGoal.revision)
 // Keep the original command ID when a locally-created watch appears in a fresh read.
 const latestWatch=watch?plan.data?.watches.find(row=>row.id===(watch.id||watch.command_id)):undefined
 const watchChanged=!!(watch&&latestWatch&&(!watch.id||watch.revision!==latestWatch.revision))
 const parkedNewWatch=!!(watch&&!watch.id)||(editor.watchDrafts||[]).some(form=>!form.id)
 const watchUnavailable=!!(watch&&(watch.id&&plan.data&&!latestWatch||latestWatch?.state==='ended'))
 const chooseWatch=(value:WatchForm,fromShelf=false)=>{
  if(isBusy()||(readOnly&&!fromShelf)||!current(scope,generation.current))return
  setWatchFocus(null)
  if(edit(previous=>selectWatchDraft(previous,value)))clearFeedback()
 }
 const restoreParkedWatch=(key:string)=>{
  if(isBusy()||!current(scope,generation.current))return
  const value=(readPlanEditor(identity,roomId).watchDrafts||[]).find(form=>watchDraftKey(form)===key)
  if(value)chooseWatch(value,true)
 }
 const newWatch=()=>{
  if(isBusy()||readOnly||!current(scope,generation.current))return
  const live=readPlanEditor(identity,roomId)
  chooseWatch(live.watch&&!live.watch.id?live.watch:(live.watchDrafts||[]).find(form=>!form.id)||freshWatch())
 }
 const discard=(kind:'goal'|'watch')=>{
  if(isBusy()||!current(scope,generation.current)||kind==='watch'&&(readPlanEditor(identity,roomId).watchSession||'legacy')!==watchSession||!window.confirm(t('roomPlans.discardConfirm')))return
  if(!current(scope,generation.current)||kind==='watch'&&(readPlanEditor(identity,roomId).watchSession||'legacy')!==watchSession)return
  if(kind==='goal')setDraft(null);else setWatch(null)
  clearFeedback()
 }
 const rebase=(kind:'goal'|'watch')=>{
  if(isBusy()||plan.isFetching||readOnly||!current(scope,generation.current)||kind==='watch'&&(readPlanEditor(identity,roomId).watchSession||'legacy')!==watchSession)return
  if(kind==='goal'&&(!draft||!latestGoal)||kind==='watch'&&(!watch||!latestWatch||watchUnavailable))return
  if(!window.confirm(t('roomPlans.rebaseConfirm'))||!current(scope,generation.current)||kind==='watch'&&(readPlanEditor(identity,roomId).watchSession||'legacy')!==watchSession)return
  if(kind==='goal')setDraft({...draft!,revision:latestGoal!.revision})
  else setWatch({...watch!,id:latestWatch!.id,revision:latestWatch!.revision})
  clearFeedback()
 }
 const goalIssues=goal&&editor.goalValidation?goalInputIssues(goal):[]
 const goalId=(field:GoalInputField)=>`${goalPrefix}-${field}`
 const goalFieldProps=(field:GoalInputField)=>({id:goalId(field),'data-goal-field':field,'aria-invalid':goalIssues.some(issue=>issue.field===field)||undefined,'aria-describedby':`${goalId(field)}-help`})
 const goalRange=(kind:GoalInputIssue['kind'])=>t(kind==='title'?'roomPlans.goalInput.title':kind==='text'?'roomPlans.goalInput.text':kind==='criteria'?'roomPlans.goalInput.criteria':'roomPlans.goalInput.evidence')
 const goalLabel=(issue:GoalInputIssue)=>issue.kind==='title'?t('roomPlans.goalName'):issue.kind==='criteria'?t('roomPlans.goalInput.criteriaLabel'):`${t('roomPlans.condition')} ${(issue.index??0)+1}${issue.kind==='evidence'?` · ${t('roomPlans.evidence')}`:''}`
 const goalHint=(field:GoalInputField,kind:GoalInputIssue['kind'])=><span id={`${goalId(field)}-help`} className={`field-hint${goalIssues.some(issue=>issue.field===field)?' notice--danger':''}`}>{goalRange(kind)}</span>
 const requestGoalFocus=(field:GoalInputField,value:Goal,revision:number,mode:'invalid'|'added'='invalid')=>{
  if(field.startsWith('criterion:')||field.startsWith('evidence:'))setCriteriaOpen([field.slice(field.indexOf(':')+1)],true,value)
  setGoalFocus({field,value,revision,origin:document.activeElement,mode})
 }
 useLayoutEffect(()=>{
  if(!goalFocus||!open||busy||readOnly||!current(scope,generation.current))return
  const form=goalFormRef.current,element=document.activeElement,live=readPlanEditor(identity,roomId)
  const value=live.draft||plan.data?.goal||(live.result?.kind==='goal'?live.result.value:undefined)
  if(!form||!form.isConnected||!element||element!==goalFocus.origin||!form.contains(element)
    ||value!==goalFocus.value||live.goalRevision!==goalFocus.revision
    ||goalFocus.mode==='invalid'&&(!live.goalValidation||!value||!goalInputIssues(value).some(issue=>issue.field===goalFocus.field)))return
  const input=form.querySelector<HTMLElement>(`[data-goal-field="${goalFocus.field}"]`)
  if(!input||input.matches(':disabled')||!input.getClientRects().length||input.closest('[aria-modal="true"]')!==element.closest('[aria-modal="true"]'))return
  input.focus({preventScroll:true});input.scrollIntoView({block:'nearest'})
 },[goalFocus])
 const saveGoal=(event:React.FormEvent)=>{
  event.preventDefault()
  if(goalIme.isSubmitBlocked(event)||goalChanged||readOnly||isBusy()||!current(scope,generation.current)||!goal)return
  const snapshot=readPlanEditor(identity,roomId),value=snapshot.draft||plan.data?.goal||(snapshot.result?.kind==='goal'?snapshot.result.value:undefined)
  if(!value||value!==goal)return
  const issues=goalInputIssues(value)
  if(issues.length){
   if(edit(previous=>previous!==snapshot||previous.operation?previous:{...previous,goalValidation:true,notice:''}))requestGoalFocus(issues[0].field,value,snapshot.goalRevision)
   return
  }
  void act(async()=>(await api.put<Goal>(`${base}/goal`,value)).data,'goal')
 }
 const addCriterion=()=>{
  if(!goal||isBusy()||readOnly||goal.criteria.length>=12||!current(scope,generation.current))return
  const criterion={id:uid(),text:'',done:false,evidence_seq:null},value={...goal,complete:false,criteria:[...goal.criteria,criterion]}
  if(setDraft(value))requestGoalFocus(`criterion:${criterion.id}`,value,readPlanEditor(identity,roomId).goalRevision,'added')
 }
 const moveCriterion=(criterionId:string,direction:-1|1)=>{
  if(!goal||readOnly||!current(scope,generation.current))return
  const snapshot=readPlanEditor(identity,roomId),value=snapshot.draft||cache.getQueryData<Data>(key)?.goal||(snapshot.result?.kind==='goal'?snapshot.result.value:undefined)
  if(snapshot.operation||value!==goal)return
  if(edit(previous=>previous!==snapshot?previous:moveGoalCriterion(previous,goal,criterionId,direction))){
   const moved=readPlanEditor(identity,roomId)
   if(moved.draft)requestGoalFocus(`criterion:${criterionId}`,moved.draft,moved.goalRevision,'added')
  }
 }
 const removeCriterion=(criterionId:string)=>{
  if(!goal||readOnly||!current(scope,generation.current))return
  const snapshot=readPlanEditor(identity,roomId),value=snapshot.draft||cache.getQueryData<Data>(key)?.goal||(snapshot.result?.kind==='goal'?snapshot.result.value:undefined)
  if(snapshot.operation||value!==goal)return
  setGoalFocus(null)
  edit(previous=>previous!==snapshot?previous:removeGoalCriterion(previous,goal,criterionId))
 }
 const undoCriterion=()=>{
  if(readOnly||!current(scope,generation.current))return
  const snapshot=readPlanEditor(identity,roomId),removed=snapshot.removedGoalCriterion
  if(!removed||snapshot.operation)return
  if(edit(previous=>previous!==snapshot?previous:restoreGoalCriterion(previous))){
   const restored=readPlanEditor(identity,roomId)
   if(restored.draft)requestGoalFocus(`criterion:${removed.criterion.id}`,restored.draft,restored.goalRevision,'added')
  }
 }
 const watchFields=[
  {field:'name',label:'roomPlans.watchName',hint:'roomPlans.input.name'},
  {field:'url',label:'roomPlans.input.urlLabel',hint:'roomPlans.input.url'},
  {field:'interval_minutes',label:'roomPlans.interval',hint:'roomPlans.input.interval'},
  {field:'timezone',label:'roomPlans.timezone',hint:'roomPlans.input.timezone'},
  {field:'keyword',label:'roomPlans.keyword',hint:'roomPlans.input.keyword'},
 ] as const
 const watchIssues=watch&&editor.watchValidation?watchInputIssues(watch):[]
 const fieldId=(field:WatchInputField)=>`${watchPrefix}-${field}`
 const fieldProps=(field:WatchInputField)=>({id:fieldId(field),'data-watch-field':field,'aria-invalid':watchIssues.includes(field)||undefined,'aria-describedby':`${fieldId(field)}-help`})
 const watchHint=(field:WatchInputField)=>{
  const row=watchFields.find(row=>row.field===field)!
  return <span id={`${fieldId(field)}-help`} className={`field-hint${watchIssues.includes(field)?' notice--danger':''}`}>{t(row.hint)}</span>
 }
 const requestWatchFocus=(field:WatchInputField,value:WatchForm,revision:number)=>setWatchFocus({field,value,revision,origin:document.activeElement})
 useLayoutEffect(()=>{
  if(!watchFocus||!open||busy||readOnly||!current(scope,generation.current))return
  const form=watchFormRef.current,element=document.activeElement,live=readPlanEditor(identity,roomId)
  if(!form||!form.isConnected||!element||element!==watchFocus.origin||!form.contains(element)
   ||!live.watch||live.watch!==watchFocus.value||live.watchRevision!==watchFocus.revision||!live.watchValidation
   ||!watchInputIssues(live.watch).includes(watchFocus.field))return
  const input=form.querySelector<HTMLInputElement>(`[data-watch-field="${watchFocus.field}"]`)
  if(!input||input.disabled||!input.getClientRects().length||input.closest('[aria-modal="true"]')!==element.closest('[aria-modal="true"]'))return
  input.focus({preventScroll:true});input.scrollIntoView({block:'nearest'})
 },[watchFocus])
 const saveWatch=(event:React.FormEvent)=>{
  event.preventDefault()
  if(watchIme.isSubmitBlocked(event)||watchChanged||watchUnavailable||readOnly||isBusy()||!current(scope,generation.current))return
  const snapshot=readPlanEditor(identity,roomId),value=snapshot.watch
  if(!value||value!==watch||(snapshot.watchSession||'legacy')!==watchSession)return
  const issues=watchInputIssues(value)
  if(issues.length){
   if(edit(previous=>previous!==snapshot||previous.operation?previous:{...previous,watchValidation:true,notice:''}))requestWatchFocus(issues[0],value,snapshot.watchRevision)
   return
  }
  const {id,...form}=value,data={...form,interval_minutes:Number(value.interval_minutes)}
  void act(async()=>id?(await api.put<Watch>(`${base}/watches/${id}`,data)).data:(await api.post<Watch>(`${base}/watches`,data)).data,'watch')
 }
 const control=(row:Watch,action:'pause'|'resume'|'end')=>void act(async()=>(await api.post<Watch>(`${base}/watches/${row.id}/control`,{revision:row.revision,action})).data,'control',action!=='resume',row.name)
 const confirmEnd=()=>{
  if(!ending||ending.scope!==scope||!open||isBusy()||!current(scope,generation.current))return
  const latest=cache.getQueryData<Data>(key)?.watches.find(row=>row.id===ending.target.id)
  if(!latest||!sameWatchEndTarget(ending.target,latest))return
  control(latest,'end')
  // The existing admission is synchronous before its first await. A cancelled
  // unknown-outcome confirmation must leave this dialog and target intact.
  if(readPlanEditor(identity,roomId).operation!==null)setEnding(null)
 }
 return <><details className="room-options room-plans" onToggle={e=>{if(e.target===e.currentTarget){setOpen(e.currentTarget.open);if(!e.currentTarget.open)setEnding(null)}}}><summary>{t('roomPlans.title')}</summary>
  <div className="stack room-plans__body"><p className="section-copy">{t('roomPlans.help')}</p>
   <p className="field-hint">{t('roomPlans.sleepHelp')}</p>
   {error&&<div role="alert" className="notice notice--warning stack stack--tight"><p>{error}</p>
    {editor.rejection&&<p className="metadata">{t('roomPlans.reject.request')} · {t(editor.rejection.kind==='goal'?'roomPlans.goal':'roomPlans.watches')}{editor.rejection.subject&&` · ${editor.rejection.subject}`}</p>}
   </div>}
   {plan.isError&&<p role="alert" className="notice notice--warning">{t('roomPlans.read.failure')}</p>}
   {plan.data&&<div className="metadata stack stack--tight">
    {plan.isError&&<p role="status">{t('roomPlans.read.retained')}</p>}
    {plan.dataUpdatedAt>0&&<p>{t('roomPlans.read.received',{time:formatDateTime(new Date(plan.dataUpdatedAt).toISOString(),{dateStyle:'medium',timeStyle:'medium'})})}</p>}
    <p>{t('roomPlans.read.scope')}</p>
   </div>}
   <button type="button" className="button button--secondary button--compact" disabled={busy||plan.isFetching} onClick={()=>{if(!isBusy()&&current(scope,generation.current))void plan.refetch()}}>{t(plan.isFetching?'common.loading':'roomPlans.refreshKeepDraft')}</button>
   {(draft||watch||(editor.watchDrafts?.length||0)>0)&&<><p className="metadata">{t('roomPlans.draftHelp')}</p><p role="status" className="notice notice--info">{t('roomPlans.sessionDraft')}</p></>}
   <p className="field-hint">{t('roomPlans.sessionLimit')}</p>
   {notice&&<p role="status" className={`metadata ${notice==='saved'?'status-text--success':''}`}>{t(notice==='saving'?'common.saving':'roomPlans.writeSaved')}</p>}
   {plan.isPending&&<p role="status">{t('common.loading')}</p>}
   {editor.result&&notice==='saved'&&<details><summary>{t('roomPlans.lastResponse')}</summary><strong>{editor.result.kind==='goal'?editor.result.value.title:editor.result.value.name}</strong>{editor.result.kind!=='goal'&&<p>{editor.result.value.url}</p>}</details>}
   {editor.uncertain.length>0&&<details><summary>{t('roomPlans.uncertainHistory')}</summary><p>{t('roomPlans.unknownWrite')}</p><ul>{editor.uncertain.map(row=><li key={row.id}>{t(row.kind==='goal'?'roomPlans.goal':'roomPlans.watches')} · {row.subject}</li>)}</ul></details>}

   {goal&&<section className="stack"><h3>{t('roomPlans.goal')}</h3>
    {goalChanged&&<div className="notice notice--warning stack stack--tight"><p role="alert">{t('roomPlans.changedHelp')}</p>
     <details><summary>{t('roomPlans.read.receivedTitle')}</summary><strong>{latestGoal!.title}</strong>
      <ul>{latestGoal!.criteria.map(row=><li key={row.id}><label className="inline-actions"><input type="checkbox" disabled checked={row.done}/>{row.text}</label>{row.evidence_seq&&` · #${row.evidence_seq}`}</li>)}</ul>
      <label className="inline-actions"><input type="checkbox" disabled checked={latestGoal!.complete}/>{t('roomPlans.complete')}</label>
     </details><button type="button" className="button button--secondary button--compact" disabled={busy||plan.isFetching||readOnly} onClick={()=>rebase('goal')}>{t('roomPlans.keepDraftLatest')}</button></div>}
    {draft&&<button type="button" className="text-link" disabled={busy} onClick={()=>discard('goal')}>{t('roomPlans.discardGoal')}</button>}<form ref={goalFormRef} {...goalIme.handlers} className="form-grid" noValidate onSubmit={saveGoal}>
    <div className="stack stack--tight"><div className="page-actions">
     <button type="button" className="text-link" disabled={busy||!goal.criteria.some(row=>!(editor.goalCriteriaOpen?.[row.id]??true))} onClick={()=>setCriteriaOpen(goal.criteria.map(row=>row.id),true)}>{t('roomPlans.criteriaView.expand')}</button>
     <button type="button" className="text-link" disabled={busy||!goal.criteria.some(row=>editor.goalCriteriaOpen?.[row.id]??true)} onClick={()=>setCriteriaOpen(goal.criteria.map(row=>row.id),false)}>{t('roomPlans.criteriaView.collapse')}</button>
    </div><p className="field-hint">{t('roomPlans.criteriaView.help')}</p></div>
    <fieldset className="first-success__setup form-grid" disabled={readOnly||busy}>
     <label className="field">{t('roomPlans.goalName')}<input {...goalFieldProps('title')} required value={goal.title} onChange={e=>update({title:e.target.value,complete:false})}/>{goalHint('title','title')}</label>
     {goal.criteria.map((c,index)=><div className="surface stack stack--tight" key={c.id}>
      <details className="room-options" open={editor.goalCriteriaOpen?.[c.id]??true} onToggle={event=>{if(event.target===event.currentTarget)setCriteriaOpen([c.id],event.currentTarget.open)}}>
       <summary onClick={event=>{if(isBusy()||!current(scope,generation.current))event.preventDefault()}} style={{overflowWrap:'anywhere'}}>{t('roomPlans.condition')} {index+1} · {Array.from(c.text.replace(/\s+/g,' ').trim()).slice(0,90).join('')||t('roomPlans.criteriaView.unnamed')} · {t(c.done?'roomPlans.conditionDone':'roomPlans.criteriaView.pending')}{c.evidence_seq?` · #${c.evidence_seq}`:''}</summary>
       <div className="stack stack--tight" style={{marginTop:'0.75rem'}}>

      <label className="field">{t('roomPlans.condition')} {index+1}<input {...goalFieldProps(`criterion:${c.id}`)} required value={c.text} onChange={e=>update({complete:false,criteria:goal.criteria.map(x=>x.id===c.id?{...x,text:e.target.value,done:false,evidence_seq:null}:x)})}/>{goalHint(`criterion:${c.id}`,'text')}</label>
      <GoalEvidencePicker roomId={roomId} choices={evidenceChoices} selected={c.evidence_seq} disabled={readOnly||busy} historyComplete={historyComplete} fieldProps={goalFieldProps(`evidence:${c.id}`)} help={goalHint(`evidence:${c.id}`,'evidence')} onChange={choice=>changeEvidence(c.id,goal,choice)} canOpen={evidenceCurrent} onNavigate={onNavigate}/>
      <label className="inline-actions"><input type="checkbox" checked={c.done} disabled={!c.evidence_seq} onChange={e=>update({complete:false,criteria:goal.criteria.map(x=>x.id===c.id?{...x,done:e.target.checked}:x)})}/>{t('roomPlans.conditionDone')}</label>
       </div></details>
      <div className="page-actions">
       <button type="button" className="text-link" disabled={index===0} aria-label={t('roomPlans.criteriaView.upNamed',{position:index+1})} onClick={()=>moveCriterion(c.id,-1)}>{t('roomPlans.criteriaView.up')}</button>
       <button type="button" className="text-link" disabled={index===goal.criteria.length-1} aria-label={t('roomPlans.criteriaView.downNamed',{position:index+1})} onClick={()=>moveCriterion(c.id,1)}>{t('roomPlans.criteriaView.down')}</button>
       <button type="button" className="text-link danger-link" onClick={()=>removeCriterion(c.id)}>{t('common.delete')}</button>
      </div>
     </div>)}
     <div className="stack stack--tight">
      <button type="button" className="text-link" disabled={!editor.removedGoalCriterion||goal.criteria.length>=12||goal.criteria.some(row=>row.id===editor.removedGoalCriterion?.criterion.id)} onClick={undoCriterion}>{t('roomPlans.conditionUndo.action')}</button>
      {editor.removedGoalCriterion&&<p role="status" className="metadata" style={{overflowWrap:'anywhere'}}>{t('roomPlans.conditionUndo.removed',{text:Array.from(editor.removedGoalCriterion.criterion.text).slice(0,160).join('')+(Array.from(editor.removedGoalCriterion.criterion.text).length>160?'…':'')})}{goal.criteria.length>=12&&` · ${t('roomPlans.conditionUndo.full')}`}</p>}
      {editor.removedGoalCriterion&&<p className="field-hint">{t('roomPlans.conditionUndo.scope')}</p>}
     </div>
     <p className="field-hint">{t('roomPlans.goalInput.count',{count:goal.criteria.length})}</p>
     {!goal.criteria.length&&<p className="notice notice--info">{t('roomPlans.goalInput.empty')}</p>}
     <button {...goalFieldProps('criteria')} type="button" className="button button--secondary" disabled={goal.criteria.length>=12} onClick={addCriterion}>{t('roomPlans.addCondition')}</button>{goalHint('criteria','criteria')}
     <label className="inline-actions"><input type="checkbox" checked={goal.complete} disabled={!goal.criteria.length||goal.criteria.some(c=>!c.done||!c.evidence_seq)} onChange={e=>update({complete:e.target.checked})}/>{t('roomPlans.complete')}</label>
     {goalIssues.length>0&&<div role="alert" className="notice notice--warning stack stack--tight"><p>{t('roomPlans.goalInput.validation')}</p><ul>{goalIssues.map(issue=><li key={issue.field}><button type="button" className="text-link" onClick={()=>{
      const live=readPlanEditor(identity,roomId),value=live.draft||plan.data?.goal||(live.result?.kind==='goal'?live.result.value:undefined)
      if(value&&!isBusy()&&!readOnly&&current(scope,generation.current)&&live.goalValidation)requestGoalFocus(issue.field,value,live.goalRevision)
     }}>{goalLabel(issue)} · {goalRange(issue.kind)}</button></li>)}</ul></div>}
     <p className="field-hint">{t('roomPlans.evidenceHelp')}</p><button type="submit" className="button button--primary" disabled={!goal.criteria.length||goalChanged}>{t('common.save')}</button>
    </fieldset></form></section>}
   <section className="stack"><h3>{t('roomPlans.watches')}</h3>
    <WatchDraftShelf drafts={editor.watchDrafts||[]} busy={busy} canUse={()=>current(scope,generation.current)} onSelect={restoreParkedWatch}/>
<p className="field-hint">{t('roomPlans.watchHelp')}</p><p className="field-hint">{t('roomPlans.timing.scope')}</p>
    {!readOnly&&<button type="button" className="button button--secondary" disabled={busy||!plan.data||!!watch&&!watch.id||!parkedNewWatch&&(plan.data.watches.filter(w=>w.state!=='ended').length||0)>=3} onClick={newWatch}>{t(parkedNewWatch?'roomPlans.watchDrafts.continueNew':'roomPlans.addWatch')}</button>}
    {watch&&<><p className="metadata">{t('roomPlans.watchDrafts.active')}</p><div className="stack stack--tight">
     {watchUnavailable&&<p role="alert" className="notice notice--warning">{t('roomPlans.watchUnavailable')}</p>}
     {watchChanged&&<div className="notice notice--warning stack stack--tight"><p role="alert">{t('roomPlans.changedHelp')}</p>
      <details><summary>{t('roomPlans.read.receivedTitle')}</summary><strong>{latestWatch!.name}</strong><p>{latestWatch!.url}</p>
       <p>{t(`roomPlans.${latestWatch!.mode}`)} · {latestWatch!.keyword}</p><p>{latestWatch!.interval_minutes} {t('roomPlans.minutes')} · {latestWatch!.timezone}</p>
       <p>{t(`roomPlans.${latestWatch!.state}`)}</p><label className="inline-actions"><input type="checkbox" disabled checked={latestWatch!.stop_after_change}/>{t('roomPlans.stopAfter')}</label>
      </details><button type="button" className="button button--secondary button--compact" disabled={busy||plan.isFetching||readOnly||watchUnavailable} onClick={()=>rebase('watch')}>{t('roomPlans.keepDraftLatest')}</button></div>}
    </div><form key={watchSession} ref={watchFormRef} {...watchIme.handlers} className="form-grid" noValidate onSubmit={saveWatch}><fieldset className="first-success__setup form-grid" disabled={readOnly||busy}>
     <label className="field">{t('roomPlans.watchName')}<input {...fieldProps('name')} required value={watch.name} onChange={e=>changeWatch({name:e.target.value})}/>{watchHint('name')}</label>
     <label className="field">URL<input {...fieldProps('url')} required type="url" placeholder="https://example.com/page" value={watch.url} onChange={e=>changeWatch({url:e.target.value})}/>{watchHint('url')}</label>
     <label className="field">{t('roomPlans.interval')}<input {...fieldProps('interval_minutes')} type="number" required min={5} max={10080} step="1" value={watch.interval_minutes} onChange={e=>changeWatch({interval_minutes:e.target.value})}/>{watchHint('interval_minutes')}</label>
     <label className="field">{t('roomPlans.timezone')}<input {...fieldProps('timezone')} required value={watch.timezone} onChange={e=>changeWatch({timezone:e.target.value})}/>{watchHint('timezone')}</label>
     <label className="field">{t('roomPlans.rule')}<select value={watch.mode} onChange={e=>changeWatch({mode:e.target.value as Watch['mode']})}><option value="text_change">{t('roomPlans.text_change')}</option><option value="keyword_appears">{t('roomPlans.keyword_appears')}</option><option value="keyword_disappears">{t('roomPlans.keyword_disappears')}</option></select></label>
     {(watch.mode!=='text_change'||!!watch.keyword)&&<label className="field">{t('roomPlans.keyword')}<input {...fieldProps('keyword')} required={watch.mode!=='text_change'} value={watch.keyword} onChange={e=>changeWatch({keyword:e.target.value})}/>{watchHint('keyword')}{watch.mode==='text_change'&&<span className="field-hint">{t('roomPlans.input.keywordUnused')}</span>}</label>}
     <label className="inline-actions"><input type="checkbox" checked={watch.stop_after_change} onChange={e=>changeWatch({stop_after_change:e.target.checked})}/>{t('roomPlans.stopAfter')}</label>
     <label className="inline-actions"><input type="checkbox" checked={watch.enabled} onChange={e=>changeWatch({enabled:e.target.checked})}/>{t('roomPlans.enable')}</label><p className="field-hint">{t('roomPlans.editReset')}</p>
     {watchIssues.length>0&&<div role="alert" className="notice notice--warning stack stack--tight"><p>{t('roomPlans.input.validation')}</p><ul>{watchFields.filter(row=>watchIssues.includes(row.field)).map(row=><li key={row.field}><button type="button" className="text-link" onClick={()=>{
      const live=readPlanEditor(identity,roomId)
      if(!isBusy()&&!readOnly&&current(scope,generation.current)&&(live.watchSession||'legacy')===watchSession&&live.watch&&live.watchValidation)requestWatchFocus(row.field,live.watch,live.watchRevision)
     }}>{t(row.label)} · {t(row.hint)}</button></li>)}</ul></div>}
     <div className="page-actions"><button type="submit" className="button button--primary" disabled={watchChanged||watchUnavailable}>{t('common.save')}</button></div>
    </fieldset><button type="button" className="button button--secondary" disabled={busy} onClick={()=>discard('watch')}>{t('roomPlans.watchDrafts.discard')}</button></form></>}
    {plan.data?.watches.map(w=><article key={w.id} className="surface stack stack--tight"><div className="split"><strong>{w.name}</strong><span className="metadata">{t(`roomPlans.${w.state}`)}</span></div><p className="metadata">{w.url}</p><p>{t(`roomPlans.${w.mode}`)}{w.keyword&&` · ${w.keyword}`} · {w.interval_minutes} {t('roomPlans.minutes')}</p>
     {w.state==='active'&&w.observing&&<p role="status" className="notice notice--info">{t('roomPlans.timing.observing')}</p>}
     {w.next_at?<p className="metadata">{t('roomPlans.next')} · <ZonedServerTime value={w.next_at} timeZone={w.timezone}/></p>:w.state==='active'&&<p className="metadata">{t('roomPlans.timing.noNext')}</p>}
     {w.last_at?<p className="metadata">{t('roomPlans.last')} · <ZonedServerTime value={w.last_at} timeZone={w.timezone}/></p>:<p className="metadata">{t('roomPlans.timing.noLast')}</p>}
     <WatchFailureNotice watch={w} readOnly={readOnly}/>
     <div className="page-actions">{!readOnly&&w.state!=='ended'&&<><button type="button" className="button button--secondary button--compact" disabled={busy} onClick={()=>control(w,w.state==='active'?'pause':'resume')}>{t(w.state==='active'?'roomPlans.pause':'roomPlans.resume')}</button><button type="button" className="button button--secondary button--compact" disabled={busy} onClick={()=>chooseWatch({id:w.id,revision:w.revision,command_id:uid(),name:w.name,url:w.url,interval_minutes:String(w.interval_minutes),timezone:w.timezone,mode:w.mode,keyword:w.keyword,stop_after_change:w.stop_after_change,enabled:w.state==='active'})}>{t('common.edit')}</button></>}{w.state!=='ended'&&<button type="button" className="button button--secondary danger-link button--compact" disabled={busy} onClick={()=>{if(current(scope,generation.current)&&!isBusy())setEnding({scope,target:watchEndTarget(w)})}}>{t('roomPlans.end')}</button>}</div>
    </article>)}
   </section>
  </div></details>
  {ending&&ending.scope===scope&&open&&<WatchEndDialog target={ending.target} current={plan.data?.watches.find(row=>row.id===ending.target.id)} roomId={roomId} busy={busy}
   canUse={()=>current(scope,generation.current)} onClose={()=>setEnding(null)} onConfirm={confirmEnd}/>}
 </>
}
