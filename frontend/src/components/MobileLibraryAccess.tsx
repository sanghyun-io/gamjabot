import { useEffect, useRef, useState } from 'react'
import { mobileAccessApi, type MobileLibraryCommand, type MobileLibraryReceipt } from '../api/remote'
import { useAuthStore } from '../stores/authStore'

type Device={id:string;name:string;library_read?:boolean;library_revision?:number}
function valid(value:unknown):value is MobileLibraryCommand {
 if(!value||typeof value!=='object'||Array.isArray(value))return false
 const x=value as MobileLibraryCommand
 return typeof x.operation_id==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(x.operation_id)&&typeof x.enabled==='boolean'&&Number.isSafeInteger(x.expected_revision)&&x.expected_revision>=0&&typeof x.expected_owner_epoch==='string'&&/^[0-9a-f]{64}$/.test(x.expected_owner_epoch)
}
export default function MobileLibraryAccess({device,owner,epoch,ownerEpoch,disabled,onRefresh}:{device:Device;owner:number;epoch:number;ownerEpoch?:string;disabled:boolean;onRefresh:()=>Promise<unknown>}){
 const storageKey=`gamja.mobile-library.${owner}.${device.id}`,alive=useRef(false),lock=useRef(false)
 const [pending,setPending]=useState<MobileLibraryCommand|null>(null),[ready,setReady]=useState(false),[broken,setBroken]=useState(false),[busy,setBusy]=useState(false),[confirm,setConfirm]=useState<boolean|null>(null),[notice,setNotice]=useState('')
 const current=()=>{const a=useAuthStore.getState();return alive.current&&a.user?.id===owner&&a.epoch===epoch}
 useEffect(()=>{alive.current=true;try{const raw=sessionStorage.getItem(storageKey);if(raw){const value=JSON.parse(raw);if(valid(value))setPending(value);else setBroken(true)}}catch{setBroken(true)}setReady(true);return()=>{alive.current=false}},[storageKey,owner,epoch])
 const finish=async(command:MobileLibraryCommand,resolve:boolean)=>{
  const receipt:MobileLibraryReceipt=resolve?await mobileAccessApi.libraryResolve(device.id,command):await mobileAccessApi.libraryChange(device.id,command)
  if(!current())return
  if(receipt.state==='applied'&&(receipt.library_read!==command.enabled||receipt.library_revision<command.expected_revision))throw Error('Receipt mismatch')
  sessionStorage.removeItem(storageKey);setPending(null);setConfirm(null)
  setNotice(receipt.state==='applied'?'설정을 저장했습니다.':receipt.state==='fenced'?'이전 요청은 적용되지 않았습니다. 최신 상태를 확인하고 다시 선택하세요.':'다른 변경이 있어 저장하지 않았습니다. 최신 상태를 확인하세요.')
  try{await onRefresh()}catch{if(current())setNotice('요청은 확인됐지만 최신 설정을 불러오지 못했습니다. 새로고침해 주세요.')}
 }
 const start=async()=>{
  if(!current()||disabled||lock.current||pending||broken||!ready||confirm===null||typeof ownerEpoch!=='string'||!/^[a-f0-9]{64}$/.test(ownerEpoch)||!Number.isSafeInteger(device.library_revision)||device.library_revision!<0)return
  lock.current=true;setBusy(true);setNotice('');let prepared=false
  try{const command={operation_id:crypto.randomUUID(),enabled:confirm,expected_revision:device.library_revision!,expected_owner_epoch:ownerEpoch};sessionStorage.setItem(storageKey,JSON.stringify(command));setPending(command);prepared=true;await finish(command,false)}catch{if(current())setNotice(prepared?'요청 결과를 확인하지 못했습니다. 이전 요청을 먼저 확인하세요.':'요청을 시작하지 못했습니다. 다시 시도하세요.')}finally{lock.current=false;if(current())setBusy(false)}
 }
 const resolve=async()=>{if(!current()||!pending||lock.current)return;lock.current=true;setBusy(true);try{await finish(pending,true)}catch{if(current())setNotice('아직 요청 결과를 확인하지 못했습니다. 자동으로 다시 보내지 않습니다.')}finally{lock.current=false;if(current())setBusy(false)}}
 const known=typeof device.library_read==='boolean'&&Number.isSafeInteger(device.library_revision)&&device.library_revision!>=0
 return <div className="stack stack--tight"><p className="metadata">이 기기의 보관함 읽기: {known?(device.library_read?'허용됨':'허용 안 함'):'상태 확인 필요'}</p>
 <p className="field-hint">허용하면 이 휴대폰에서 이 계정의 개인 Bot 보관함에서 개인 작업공간의 일반 그룹 대화를 포함한 결과·고정 목록과 업로드를 읽고 지원 파일을 다운로드할 수 있습니다. 공유 Bot과 공유 방은 제외됩니다. Mac과 감자봇이 켜져 있어야 합니다. 대화 권한과 별개이며 읽기 전용입니다.</p>
 {notice&&<p role="status">{notice}</p>}{broken&&<p role="alert">이전 요청 기록을 읽을 수 없어 설정을 변경할 수 없습니다.</p>}
 {pending?<div className="notice notice--warning"><p>이전 설정 요청의 결과를 먼저 확인하세요.</p><button type="button" disabled={busy} onClick={()=>void resolve()}>요청 확인</button></div>:<button type="button" className="button button--secondary button--compact" disabled={disabled||busy||!ready||broken||!known||!ownerEpoch} onClick={()=>setConfirm(!device.library_read)}>{device.library_read?'보관함 읽기 끄기':'보관함 읽기 허용'}</button>}
 {confirm!==null&&!pending&&<div className="notice notice--warning"><p>{confirm?`${device.name}에서 이 계정의 개인 Bot 보관함을 읽도록 허용할까요? 개인 작업공간의 일반 그룹 대화는 포함하며 공유 Bot과 공유 방은 제외됩니다.`:`${device.name}의 보관함 읽기를 끌까요? 이미 전송된 내용은 회수할 수 없습니다.`}</p><button type="button" disabled={busy} onClick={()=>void start()}>{confirm?'허용 확인':'끄기 확인'}</button> <button type="button" disabled={busy} onClick={()=>setConfirm(null)}>취소</button></div>}
 </div>
}
