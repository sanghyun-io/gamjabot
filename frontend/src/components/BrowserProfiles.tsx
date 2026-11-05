import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import BrowserProfileSharingDialog from './BrowserProfileSharingDialog'
import VaultProfileHandoff from './VaultProfileHandoff'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../lib/api'
import { conversationsApi, type RoomMember } from '../api/conversations'
import { agentsApi, type Agent } from '../api/agents'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import BrowserProfileFailureNotice, { profileFailure, type ProfileFailure, type ProfileOperationTarget } from './BrowserProfileFailureNotice'
import BrowserDemonstration from './BrowserDemonstration'
import BrowserProfilePreview from './BrowserProfilePreview'
import { useBrowserProfileWorkspace } from './useBrowserProfileWorkspace'
import BrowserProfileInputFields, { profileInputState } from './BrowserProfileInputFields'
import BrowserProfileAgentPicker, { profileAgentChoices } from './BrowserProfileAgentPicker'
import BrowserProfileEditConflict, { sameProfileEditBase } from './BrowserProfileEditConflict'
import BrowserProfileActionDialog, { profileActionTarget, sameProfileActionTarget, type BrowserProfile as Profile, type ProfileAction, type ProfileActionTarget } from './BrowserProfileActionDialog'

type ChangeKind = 'save' | 'control' | 'delete'
type ActionDialog = { action: ProfileAction; target: ProfileActionTarget; identity: string; roomId: number; run: number }
export default function BrowserProfiles({roomId,readOnly=false}:{roomId:number;readOnly?:boolean}) {
  const {t,formatDateTime}=useI18n(),cache=useQueryClient()
  const ownerId=useAuthStore(s=>s.user?.id||0)
  const identity=useAuthStore(s=>`${s.user?.id||0}:${s.epoch}`),generation=useRef(0),mounted=useRef(false),mutationLock=useRef(false),formSession=useRef(0),liveReadOnly=useRef(readOnly)
  const [sharing,setSharingState]=useState<{target:Profile;identity:string;run:number}|null>(null),sharingRef=useRef<{target:Profile;identity:string;run:number}|null>(null)
  const setSharing=(value:{target:Profile;identity:string;run:number}|null)=>{const wasOpen=!!sharingRef.current;sharingRef.current=value;setSharingState(value);if(value)mutationLock.current=true;else if(wasOpen)mutationLock.current=false}
  const [custodyLabels,setCustodyLabels]=useState<Record<string,{identity:string;roomId:number;revision:number;status:'parked'|'borrowed'|'unknown'}>>({})
  const [teachingOpen,setTeachingOpen]=useState<Record<string,boolean>>({})
  const [open,setOpen]=useState(false),[editing,setEditing]=useState<Profile|null>(null),[form,setForm]=useState(false)
  const [name,setName]=useState(''),[account,setAccount]=useState(''),[origins,setOrigins]=useState(''),[selected,setSelected]=useState<number[]>([]),[error,setError]=useState<ProfileFailure|null>(null)
  const [danger,setDangerState]=useState<ActionDialog|null>(null),dangerRef=useRef<ActionDialog|null>(null)
  const setDanger=(value:ActionDialog|null)=>{dangerRef.current=value;setDangerState(value)}
  liveReadOnly.current=readOnly
  const ime=useImeFormGuard(`${identity}:${roomId}:${form}:${editing?.id??'new'}`)
  const base=`/tools/rooms/${roomId}/browser-profiles`,key=['browser-profiles',roomId,identity]
  const current=(sent:string,run:number)=>{const s=useAuthStore.getState();return mounted.current&&!!s.token&&sent===`${s.user?.id||0}:${s.epoch}`&&run===generation.current}
  useEffect(()=>{
    mounted.current=true;generation.current++;formSession.current++;setSharing(null);setDanger(null);setTeachingOpen({});setCustodyLabels({});setForm(false);setEditing(null);setError(null);setName('');setAccount('');setOrigins('');setSelected([])
    return()=>{mounted.current=false;generation.current++}
  },[roomId,identity])
  const profiles=useQuery({queryKey:key,enabled:open,queryFn:async()=>{const sent=identity,run=generation.current;const r=(await api.get<{items:Profile[]}>(base)).data;if(!current(sent,run))throw Error('Changed');return r.items},refetchInterval:open?3000:false,retry:false})
  const membersKey=['members',roomId,identity,'browser-profile'],agentsKey=['agents',identity,'browser-profile']
  const members=useQuery({queryKey:membersKey,enabled:open&&!readOnly,queryFn:()=>conversationsApi.listMembers(roomId)})
  const agents=useQuery({queryKey:agentsKey,enabled:open,queryFn:agentsApi.list})
  const reset=()=>{formSession.current++;setForm(false);setEditing(null);setName('');setAccount('');setOrigins('');setSelected([])}
  const change=useMutation({retry:false,
    mutationFn:async({work}:{work:()=>Promise<unknown>;identity:string;run:number;kind:ChangeKind;formSession:number;target:ProfileOperationTarget})=>work(),
    onSuccess:(_result,sent)=>{
      if(!current(sent.identity,sent.run))return
      // A browser control result is not a profile-form save result.
      // Also avoid closing a different editor opened after the original save.
      if(sent.kind==='save'&&sent.formSession===formSession.current)reset()
      setError(null);void cache.invalidateQueries({queryKey:key})
    },
    onError:(failure,sent)=>{if(!current(sent.identity,sent.run))return;setError(profileFailure(failure,sent.target));void cache.invalidateQueries({queryKey:key})},
    onSettled:()=>{mutationLock.current=false},
  })
  const act=(kind:ChangeKind,work:()=>Promise<unknown>,target:ProfileOperationTarget)=>{
    const sent=identity,run=generation.current
    if(!current(sent,run)||mutationLock.current||kind==='save'&&liveReadOnly.current)return false
    mutationLock.current=true
    change.mutate({identity:sent,run,kind,formSession:formSession.current,target:Object.freeze({...target}),work:async()=>{
      if(!current(sent,run)||kind==='save'&&liveReadOnly.current)throw Error('Changed')
      return work()
    }})
    return true
  }
  const control=(p:Profile,action:'takeover'|'return'|'close'|'reset')=>{if(action==='takeover'&&liveReadOnly.current)return;workspace.abort(p.id);act('control',()=>api.post(`${base}/${p.id}/control`,{action,revision:p.revision}),{action,name:p.name})}
  const latestProfile=(id:string)=>{
    const state=cache.getQueryState<Profile[]>(key)
    return state?.status==='success'&&!state.isInvalidated?state.data?.find(profile=>profile.id===id):undefined
  }
  const inputState=useMemo(()=>profileInputState(name,account,origins),[name,account,origins])
  const assignmentReady=members.isSuccess&&!members.isError&&agents.isSuccess&&!agents.isError&&profiles.isSuccess&&!profiles.isError
    &&!cache.getQueryState(membersKey)?.isInvalidated&&!cache.getQueryState(agentsKey)?.isInvalidated&&!cache.getQueryState(key)?.isInvalidated
  const assignmentChoices=useMemo(()=>!form?[]:profileAgentChoices({members:members.data||[],agents:agents.data||[],profiles:profiles.data||[],profileId:editing?.id,selected,ownerId,ready:assignmentReady}),[form,members.data,agents.data,profiles.data,editing?.id,selected,ownerId,assignmentReady])
  const assignmentBlocked=selected.some(id=>!assignmentChoices.some(choice=>choice.id===id&&choice.state==='available'))
  const currentChoices=(ids:readonly number[])=>{
    const m=cache.getQueryState<RoomMember[]>(membersKey),a=cache.getQueryState<Agent[]>(agentsKey),p=cache.getQueryState<Profile[]>(key)
    const ready=[m,a,p].every(state=>state?.status==='success'&&!state.isInvalidated)
    return profileAgentChoices({members:m?.data||[],agents:a?.data||[],profiles:p?.data||[],profileId:editing?.id,selected:ids,ownerId,ready})
  }
  const receivedEdit=editing?profiles.data?.find(profile=>profile.id===editing.id):undefined
  const editReadReady=profiles.isSuccess&&!profiles.isError&&!cache.getQueryState(key)?.isInvalidated
  const editBlocked=!!editing&&(!editReadReady||!sameProfileEditBase(editing,receivedEdit)||receivedEdit?.controller!=='disconnected'||!!receivedEdit?.connected)
  const adoptEditBase=(shown:Profile,session:number)=>{
    if(!editing||!form||!open||session!==formSession.current||!current(identity,generation.current)||mutationLock.current||liveReadOnly.current)return
    const latest=latestProfile(editing.id)
    if(!latest||latest.controller!=='disconnected'||latest.connected||!sameProfileActionTarget(profileActionTarget(shown),latest))return
    // Adopt only the received base metadata. Keep every current input field untouched.
    setEditing(latest)
  }
  const openDanger=(action:ProfileAction,profile:Profile)=>{
    if(!open||!current(identity,generation.current)||mutationLock.current||!sameProfileActionTarget(profileActionTarget(profile),latestProfile(profile.id)))return
    setDanger({action,target:profileActionTarget(profile),identity,roomId,run:generation.current})
  }
  const canUseDialog=(value:ActionDialog)=>dangerRef.current===value&&open&&value.roomId===roomId&&current(value.identity,value.run)
  const confirmDanger=(value:ActionDialog)=>{
    if(!canUseDialog(value)||mutationLock.current||!sameProfileActionTarget(value.target,latestProfile(value.target.id)))return
    const {target,action}=value
    workspace.abort(target.id)
    const accepted=act(action==='reset'?'control':'delete',()=>{
      if(!sameProfileActionTarget(target,latestProfile(target.id)))throw Error('Changed')
      return action==='reset'?api.post(`${base}/${target.id}/control`,{action:'reset',revision:target.revision}):api.delete(`${base}/${target.id}`,{params:{revision:target.revision}})
    },{action,name:target.name})
    if(accepted)setDanger(null)
  }
  const workspace=useBrowserProfileWorkspace(roomId,identity,open,current,generation,latestProfile)
  const renderedFormSession=formSession.current
  return <details className="room-options browser-profiles" onToggle={e=>{if(e.target===e.currentTarget){setOpen(e.currentTarget.open);if(!e.currentTarget.open){setDanger(null);setSharing(null)}}}}><summary>{t('browserProfiles.title')}</summary>
    <div className="stack browser-profiles__body"><p className="section-copy">{t('browserProfiles.help')}</p><p className="field-hint">{t('browserProfiles.privacy')}</p>
      {profiles.isPending&&<p role="status">{t('common.loading')}</p>}
      {profiles.isError&&<p role="alert" className="notice notice--warning">{t('browserProfiles.unavailable')}</p>}
      {profiles.isError&&profiles.data&&<p className="field-hint">{t('browserProfiles.failure.retainedList')}</p>}
      {error&&<BrowserProfileFailureNotice failure={error}/>}
      <button type="button" className="button button--secondary button--compact" onClick={()=>void profiles.refetch({cancelRefetch:false})}>{t('browserProfiles.refresh')}</button>
      {!readOnly&&!form&&<button type="button" className="button button--secondary" disabled={change.isPending} onClick={()=>{if(!current(identity,generation.current)||mutationLock.current||liveReadOnly.current)return;reset();setForm(true)}}>{t('browserProfiles.add')}</button>}
      {form&&<form {...ime.handlers} className="form-grid" onSubmit={e=>{e.preventDefault();if(ime.isSubmitBlocked(e)||renderedFormSession!==formSession.current||liveReadOnly.current||mutationLock.current||!current(identity,generation.current))return;const input=profileInputState(name,account,origins);if(!input.valid||selected.length>16)return;if(selected.length&&currentChoices(selected).some(choice=>selected.includes(choice.id)&&choice.state!=='available'))return;if(editing){const latest=latestProfile(editing.id);if(!latest||!sameProfileEditBase(editing,latest)||latest.controller!=='disconnected'||latest.connected)return}const data={name,account_label:account,origins:input.lines.map(row=>row.value),agent_ids:selected,...(editing?{revision:editing.revision}:{})};if(editing)workspace.abort(editing.id);act('save',()=>editing?api.patch(`${base}/${editing.id}`,data):api.post(base,data),{action:'save',name})}}>
        <p className="field-hint">{t('browserProfiles.draftHelp')}</p>
        {editing&&editBlocked&&<BrowserProfileEditConflict original={editing} current={receivedEdit} draft={{name,account,origins,agentIds:selected}} ready={editReadReady} busy={change.isPending} readOnly={readOnly}
          agentName={id=>agents.data?.find(agent=>agent.id===id)?.name||`Agent #${id}`} onAdopt={()=>{if(receivedEdit)adoptEditBase(receivedEdit,renderedFormSession)}}/>}
        {readOnly&&<p role="status" className="notice notice--warning">{t('browserProfiles.draftReadOnly')}</p>}
        <fieldset className="first-success__setup form-grid" disabled={change.isPending||readOnly}>
          <BrowserProfileInputFields name={name} account={account} origins={origins} state={inputState} onChange={(field,value)=>{
            if(renderedFormSession!==formSession.current||!current(identity,generation.current)||mutationLock.current||liveReadOnly.current)return
            if(field==='name')setName(value);else if(field==='account')setAccount(value);else setOrigins(value)
          }}/>
          <BrowserProfileAgentPicker key={renderedFormSession} choices={assignmentChoices} selected={selected} ready={assignmentReady} refreshing={members.isFetching||agents.isFetching||profiles.isFetching} disabled={change.isPending||readOnly}
            onChange={(id,checked)=>{if(renderedFormSession!==formSession.current||!current(identity,generation.current)||mutationLock.current||liveReadOnly.current)return;if(checked&&!selected.includes(id)&&selected.length>=16)return;if(checked&&!currentChoices([...selected,id]).some(choice=>choice.id===id&&choice.state==='available'))return;setSelected(previous=>checked?previous.includes(id)||previous.length>=16?previous:[...previous,id]:previous.filter(value=>value!==id))}}
            onRefresh={()=>{if(renderedFormSession===formSession.current&&current(identity,generation.current)&&!mutationLock.current&&!liveReadOnly.current)void Promise.allSettled([members.refetch({cancelRefetch:false}),agents.refetch({cancelRefetch:false}),profiles.refetch({cancelRefetch:false})])}}/>
          <div className="page-actions"><button type="submit" className="button button--primary" disabled={editBlocked||assignmentBlocked||!inputState.valid||selected.length>16}>{t('common.save')}</button></div>
        </fieldset>
        <button type="button" className="button button--secondary" disabled={change.isPending} onClick={()=>{if(current(identity,generation.current)&&!mutationLock.current)reset()}}>{t('common.cancel')}</button>
      </form>}
      {workspace.slots.length>0&&<section className="stack stack--tight" aria-label={t('browserProfiles.workspace.title')}>
        <p className="field-hint">{t('browserProfiles.workspace.help')}</p>
        <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(min(100%, 340px),1fr))',gap:16}}>
          {workspace.slots.map(slot=>{const profile=profiles.data?.find(p=>p.id===slot.id);const valid=!!profile&&profile.connected&&profile.revision===slot.revision
            return <article key={slot.id} className="surface stack stack--tight"><strong>{profile?.name||slot.name}</strong>
              <p className="metadata">{profile?t(`browserProfiles.${profile.controller}`):t('browserProfiles.disconnected')}{profile?.controller==='agent'&&` · ${agents.data?.find(a=>a.id===profile.agent_id)?.name||'Agent'}`}</p>
              {profile&&<p className="metadata">{profile.agent_ids.map(id=>agents.data?.find(a=>a.id===id)?.name||`Agent #${id}`).join(' · ')||t('browserProfiles.manualOnly')}</p>}
              {slot.failed&&<p role="alert" className="notice notice--warning">{t('browserProfiles.previewFailure')}</p>}
              {slot.preview?<BrowserProfilePreview key={slot.preview.url} preview={slot.preview} stale={!valid||profiles.isError||slot.failed||slot.invalidated||Date.now()-Date.parse(slot.preview.received)>30000}
                pending={slot.pending} canCapture={!!profile&&profile.connected&&!slot.pending&&!change.isPending} onCapture={()=>profile&&void workspace.capture(profile)} onClose={()=>workspace.close(slot.id)}/>:<><p role="status">{slot.pending?t('browserProfiles.previewPanel.pending'):t('browserProfiles.previewFailure')}</p><div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={!profile?.connected||slot.pending} onClick={()=>profile&&void workspace.capture(profile)}>{t('browserProfiles.previewPanel.capture')}</button><button type="button" className="button button--secondary button--compact" onClick={()=>workspace.close(slot.id)}>{t('browserProfiles.previewPanel.close')}</button></div></>}
            </article>})}
        </div>
      </section>}
      {profiles.data?.map(p=>{const entry=custodyLabels[p.id];const custodyLabel=entry&&entry.identity===identity&&entry.roomId===roomId&&entry.revision===p.revision?entry.status:null;return <article key={p.id} className="surface stack stack--tight"><div className="split"><strong>{p.name}</strong><span className="metadata">{custodyLabel?`${t('browserProfiles.custodyChecked')} · ${t(`browserProfiles.custody${custodyLabel==='parked'?'Parked':custodyLabel==='borrowed'?'Borrowed':'Unknown'}`)}`:t(`browserProfiles.${p.controller}`)}{!custodyLabel&&p.controller==='agent'&&` · ${agents.data?.find(a=>a.id===p.agent_id)?.name||'Agent'}`}</span></div>
        {(p.home_room_id??roomId)!==roomId&&<p className="field-hint">{t('profileSharing.sharedFrom')} · <Link to={`/chat/${p.home_room_id}`}>{t('profileSharing.openHome')}</Link></p>}
        {p.account_label&&<p className="section-copy">{p.account_label} · {t('browserProfiles.notVerified')}</p>}
        <p className="metadata">{p.origin_urls.join(' · ')}</p>
        <p className="metadata">{p.agent_ids.map(id=>agents.data?.find(a=>a.id===id)?.name||`Agent #${id}`).join(' · ')||t('browserProfiles.manualOnly')}</p>
        {p.last_used_at&&<p className="metadata">{t('browserProfiles.lastUsed')} {formatDateTime(p.last_used_at,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}</p>}
        <div className="page-actions">
          {!readOnly&&p.controller!=='user'&&<button type="button" className="button button--primary button--compact" disabled={change.isPending||p.controller==='settling'} onClick={()=>control(p,'takeover')}>{t('browserProfiles.takeover')}</button>}
          {p.controller==='user'&&<button type="button" className="button button--primary button--compact" disabled={change.isPending} onClick={()=>control(p,'return')}>{t('browserProfiles.return')}</button>}
          {p.connected&&<button type="button" className="button button--secondary button--compact" disabled={change.isPending||workspace.slots.some(s=>s.id===p.id&&s.pending)||workspace.full&&!workspace.slots.some(s=>s.id===p.id)} onClick={()=>void workspace.capture(p)}>{t('browserProfiles.preview')}</button>}
          {(p.connected||p.controller==='parked')&&<button type="button" className="button button--secondary button--compact" disabled={change.isPending} onClick={()=>control(p,'close')}>{t('common.close')}</button>}
        </div>
        {p.controller==='user'&&<p role="status" className="notice notice--info">{t('browserProfiles.userHelp')}</p>}
        {p.controller==='parked'&&<p role="status" className="field-hint">{t('browserProfiles.parkedHelp')}</p>}
        <VaultProfileHandoff key={`${identity}:${roomId}:${p.id}:${p.revision}`} roomId={roomId} profileId={p.id} readOnly={readOnly} onChanged={()=>void cache.invalidateQueries({queryKey:key})} onCustodyStatus={value=>{if(!current(identity,generation.current))return;setCustodyLabels(old=>{const prior=old[p.id];if(!value){if(!prior||prior.identity!==identity||prior.roomId!==roomId||prior.revision!==p.revision)return old;const next={...old};delete next[p.id];return next}if(value.revision!==p.revision)return old;return {...old,[p.id]:{identity,roomId,revision:p.revision,status:value.status}}})}}/>
        {!readOnly&&<details className="compact-details" onToggle={event=>{if(event.target===event.currentTarget&&event.currentTarget.open)setTeachingOpen(previous=>({...previous,[p.id]:true}))}}><summary>{t('demonstration.title')}</summary>{teachingOpen[p.id]&&<BrowserDemonstration key={`${identity}:${roomId}:${p.id}:${p.revision}`} roomId={roomId} profile={p} agents={agents.data||[]} readOnly={readOnly}/>}</details>}
        <details className="compact-details"><summary>{t('common.details')}</summary>{(p.home_room_id??roomId)===roomId&&(p.connected||p.controller!=='disconnected')&&<p className="field-hint">{t('profileSharing.closeFirst')}</p>}<div className="page-actions">
          {(p.home_room_id??roomId)===roomId&&!readOnly&&<button type="button" className="button button--secondary button--compact" disabled={change.isPending||!!form||p.connected||p.controller!=='disconnected'} onClick={()=>{if(!open||!current(identity,generation.current)||mutationLock.current||!sameProfileActionTarget(profileActionTarget(p),latestProfile(p.id)))return;setDanger(null);setSharing({target:p,identity,run:generation.current})}}>{t('profileSharing.title')}</button>}
          {(p.home_room_id??roomId)===roomId&&!readOnly&&<button type="button" className="button button--secondary button--compact" disabled={change.isPending||p.controller!=='disconnected'||form} onClick={()=>{if(!current(identity,generation.current)||mutationLock.current||liveReadOnly.current||form)return;formSession.current++;setEditing(p);setName(p.name);setAccount(p.account_label);setOrigins(p.origin_urls.join('\n'));setSelected(p.agent_ids);setForm(true)}}>{t('common.edit')}</button>}
          {(p.home_room_id??roomId)===roomId&&<button type="button" className="button button--secondary danger-link button--compact" disabled={change.isPending} onClick={()=>openDanger('reset',p)}>{t('browserProfiles.clear')}</button>}
          {(p.home_room_id??roomId)===roomId&&<button type="button" className="button button--secondary danger-link button--compact" disabled={change.isPending} onClick={()=>openDanger('delete',p)}>{t('common.delete')}</button>}
        </div></details>
      </article>})}
      {sharing&&open&&sharing.identity===identity&&sharing.run===generation.current&&<BrowserProfileSharingDialog roomId={roomId} target={sharing.target} current={profiles.data?.find(p=>p.id===sharing.target.id)} canUse={()=>sharingRef.current===sharing&&open&&current(sharing.identity,sharing.run)} onClose={()=>setSharing(null)} onSaved={()=>{if(sharingRef.current!==sharing)return;workspace.abort(sharing.target.id);setSharing(null);void cache.invalidateQueries({queryKey:key})}}/>}
      {danger&&open&&danger.identity===identity&&danger.roomId===roomId&&danger.run===generation.current&&<BrowserProfileActionDialog
        action={danger.action} target={danger.target} current={profiles.data?.find(profile=>profile.id===danger.target.id)} roomId={roomId}
        ready={profiles.isSuccess&&!profiles.isError&&!cache.getQueryState(key)?.isInvalidated} busy={change.isPending}
        agentName={id=>agents.data?.find(agent=>agent.id===id)?.name||`Agent #${id}`} canUse={()=>canUseDialog(danger)} onClose={()=>setDanger(null)} onConfirm={()=>confirmDanger(danger)}/>}
    </div>
  </details>
}
