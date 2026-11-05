//! Core-owned browser plumbing for the Owner's explicit Vault sign-in.
//! No model-supplied endpoint and no browser protocol payload is logged.
use crate::{broker_runtime::SourcePin, macos_spawn::OwnedChild, Result};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest,Sha256};
use std::{collections::HashMap,fs::{File,OpenOptions},io::{Read,Write},os::{fd::AsRawFd,unix::{fs::{MetadataExt,OpenOptionsExt},net::UnixStream}},path::{Path,PathBuf},sync::{Arc,atomic::{AtomicBool,Ordering}},time::{Duration,Instant}};
use zeroize::{Zeroize,Zeroizing};

// Fixed diagnostic labels only; never include site, item, payload, or errors.
#[derive(Clone,Copy)]
pub(crate) enum FailureStage {BrowserPrepare,RuntimePin,ProfileSetup,Spawn,CdpPrepare,CardRead,Dispatch,CdpTargets,CdpTargetsCommand,CdpTargetsMalformed,CdpTargetsMultiple,CdpTargetsEmpty,CdpTargetsId,CdpTargetsFreshMissing,CdpTargetsSettleTimeout,CdpAttach,CdpPageEnable,CdpRuntimeEnable,CdpNavigate,CdpFrameTree,CdpWorld,CdpContext,CdpInvalidProbe,CdpValidProbe,CdpRead,ParkDetach,ParkSnapshot,ParkNonPage,ParkClose,ParkSettle,ParkGuard,ParkPrepare,ParkState,ParkToken,ParkRestore,ParkReply}
pub(crate) fn record_failure(directory:&Path,stage:FailureStage) {
    use std::os::fd::FromRawFd;
    let _=(|| -> std::io::Result<()> {
        if crate::platform::verify_directory(directory).is_err() || directory.canonicalize().ok().as_deref()!=Some(directory) {return Ok(());}
        let dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(directory)?;
        let dm=dir.metadata()?;
        if !dm.is_dir() || dm.uid()!=unsafe{libc::geteuid()} || dm.mode()&0o777!=0o700 {return Ok(());}
        let target=b"vault-browser-last-failure.json\0";let temp=b".vault-browser-last-failure.tmp\0";
        let d=dir.as_raw_fd();
        let existing=unsafe{libc::openat(d,target.as_ptr().cast(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
        let mut previous=None;
        if existing>=0 {
            let mut f=unsafe{File::from_raw_fd(existing)};let m=f.metadata()?;
            if !m.is_file() || m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o777!=0o600 || m.nlink()!=1 || m.len()>1024 {return Ok(());}
            let mut data=Vec::new();std::io::Read::by_ref(&mut f).take(1025).read_to_end(&mut data)?;
            let parsed:Value=match serde_json::from_slice(&data){Ok(v)=>v,Err(_)=>return Ok(())};
            let valid=parsed.as_object().is_some_and(|o|o.len()==2 && o.get("version").and_then(Value::as_u64)==Some(1) && o.get("stage").and_then(Value::as_str).is_some_and(|v|matches!(v,"browser_prepare"|"runtime_pin"|"profile_setup"|"spawn"|"cdp_prepare"|"card_read"|"dispatch"|"cdp_targets"|"cdp_targets_command"|"cdp_targets_malformed"|"cdp_targets_multiple"|"cdp_targets_empty"|"cdp_targets_id"|"cdp_targets_fresh_missing"|"cdp_targets_settle_timeout"|"cdp_attach"|"cdp_page_enable"|"cdp_runtime_enable"|"cdp_navigate"|"cdp_frame_tree"|"cdp_world"|"cdp_context"|"cdp_invalid_probe"|"cdp_valid_probe"|"cdp_read"|"park_detach"|"park_snapshot"|"park_non_page"|"park_close"|"park_settle"|"park_guard"|"park_prepare"|"park_state"|"park_token"|"park_restore"|"park_reply")));
            if !valid {return Ok(());}
            previous=Some((m.dev(),m.ino()));
        } else if std::io::Error::last_os_error().kind()!=std::io::ErrorKind::NotFound {return Ok(());}
        let fd=unsafe{libc::openat(d,temp.as_ptr().cast(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_NOFOLLOW|libc::O_CLOEXEC,0o600)};
        if fd<0 {return Ok(());}let mut f=unsafe{File::from_raw_fd(fd)};let initial=f.metadata()?;
        let code=match stage {FailureStage::BrowserPrepare=>"browser_prepare",FailureStage::RuntimePin=>"runtime_pin",FailureStage::ProfileSetup=>"profile_setup",FailureStage::Spawn=>"spawn",FailureStage::CdpPrepare=>"cdp_prepare",FailureStage::CardRead=>"card_read",FailureStage::Dispatch=>"dispatch",FailureStage::CdpTargets=>"cdp_targets",FailureStage::CdpTargetsCommand=>"cdp_targets_command",FailureStage::CdpTargetsMalformed=>"cdp_targets_malformed",FailureStage::CdpTargetsMultiple=>"cdp_targets_multiple",FailureStage::CdpTargetsEmpty=>"cdp_targets_empty",FailureStage::CdpTargetsId=>"cdp_targets_id",FailureStage::CdpTargetsFreshMissing=>"cdp_targets_fresh_missing",FailureStage::CdpTargetsSettleTimeout=>"cdp_targets_settle_timeout",FailureStage::CdpAttach=>"cdp_attach",FailureStage::CdpPageEnable=>"cdp_page_enable",FailureStage::CdpRuntimeEnable=>"cdp_runtime_enable",FailureStage::CdpNavigate=>"cdp_navigate",FailureStage::CdpFrameTree=>"cdp_frame_tree",FailureStage::CdpWorld=>"cdp_world",FailureStage::CdpContext=>"cdp_context",FailureStage::CdpInvalidProbe=>"cdp_invalid_probe",FailureStage::CdpValidProbe=>"cdp_valid_probe",FailureStage::CdpRead=>"cdp_read",FailureStage::ParkDetach=>"park_detach",FailureStage::ParkSnapshot=>"park_snapshot",FailureStage::ParkNonPage=>"park_non_page",FailureStage::ParkClose=>"park_close",FailureStage::ParkSettle=>"park_settle",FailureStage::ParkGuard=>"park_guard",FailureStage::ParkPrepare=>"park_prepare",FailureStage::ParkState=>"park_state",FailureStage::ParkToken=>"park_token",FailureStage::ParkRestore=>"park_restore",FailureStage::ParkReply=>"park_reply"};
        let result=(|| -> std::io::Result<()> {
            write!(f,"{{\"version\":1,\"stage\":\"{}\"}}",code)?;f.sync_all()?;
            let current=std::fs::symlink_metadata(directory)?;
            if !current.is_dir() || current.dev()!=dm.dev() || current.ino()!=dm.ino() || current.uid()!=dm.uid() || current.mode()&0o777!=0o700 {return Err(std::io::ErrorKind::Other.into());}
            let mut st=std::mem::MaybeUninit::<libc::stat>::uninit();
            let exists=unsafe{libc::fstatat(d,target.as_ptr().cast(),st.as_mut_ptr(),libc::AT_SYMLINK_NOFOLLOW)};
            if exists==0 {let st=unsafe{st.assume_init()};if previous!=Some((st.st_dev as u64,st.st_ino)) || st.st_mode & libc::S_IFMT!=libc::S_IFREG || st.st_uid!=unsafe{libc::geteuid()} || st.st_mode&0o777!=0o600 || st.st_nlink!=1 || st.st_size>1024 {return Err(std::io::ErrorKind::Other.into());}}
            else if previous.is_some() || std::io::Error::last_os_error().kind()!=std::io::ErrorKind::NotFound {return Err(std::io::ErrorKind::Other.into());}
            if unsafe{libc::renameat(d,temp.as_ptr().cast(),d,target.as_ptr().cast())}!=0{return Err(std::io::Error::last_os_error());}Ok(())
        })();
        if result.is_err() {let mut st=std::mem::MaybeUninit::<libc::stat>::uninit();if unsafe{libc::fstatat(d,temp.as_ptr().cast(),st.as_mut_ptr(),libc::AT_SYMLINK_NOFOLLOW)}==0 {let st=unsafe{st.assume_init()};if st.st_dev as u64==initial.dev() && st.st_ino==initial.ino() {unsafe{libc::unlinkat(d,temp.as_ptr().cast(),0);}}}}
        result
    })();
}

const BROWSER_PREFIX:&str="libexec/gamja/playwright-browsers/";
const BROWSER_SUFFIX:&str="/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing";
const MAX_PACKET:usize=262_144;
fn wipe(value:Value) {
    match value {
        Value::String(mut s)=>s.zeroize(),
        Value::Array(values)=>for value in values {wipe(value)},
        Value::Object(values)=>for (mut key,value) in values {key.zeroize();wipe(value)},
        _=>{},
    }
}

const FUNCTION:&str=r#"function(expected,username,password) {
 let secretStarted=false;
 try {
  const d=document;
  const visible=x=>{if(!x.isConnected||x.disabled||x.readOnly)return false;const r=x.getBoundingClientRect(),s=getComputedStyle(x),px=Math.min(innerWidth-1,Math.max(0,r.left+r.width/2)),py=Math.min(innerHeight-1,Math.max(0,r.top+r.height/2));return typeof x.checkVisibility==='function'&&x.checkVisibility({checkOpacity:true,checkVisibilityCSS:true,contentVisibilityAuto:true})&&r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth&&d.elementFromPoint(px,py)===x&&s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>0};
  const validDocument=()=>window.top===window&&document===d&&location.origin===expected&&d.visibilityState==='visible';
  const passwords=()=>[...d.querySelectorAll('input[type="password"]')].filter(visible);
  if(!validDocument())return 'not_filled';
  const initial=passwords();if(initial.length!==1)return 'not_filled';
  const target=initial[0], form=target.form;
  const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;
  if(!setter)return 'not_filled';
  let usernameFilled=false;
  if(form) {
   const candidates=[...form.querySelectorAll('input[autocomplete~="username" i],input[type="email" i],input[name="username" i],input[id="username" i]')].filter(x=>x!==target&&visible(x));
   if(candidates.length===1) {setter.call(candidates[0],username);candidates[0].dispatchEvent(new Event('input',{bubbles:true}));candidates[0].dispatchEvent(new Event('change',{bubbles:true}));usernameFilled=true;}
  }
  const again=passwords();
  if(!validDocument()||!target.isConnected||again.length!==1||again[0]!==target)return 'not_filled';
  secretStarted=true;setter.call(target,password);target.dispatchEvent(new Event('input',{bubbles:true}));target.dispatchEvent(new Event('change',{bubbles:true}));
  return usernameFilled?'filled':'filled_password_only';
 } catch (_) {return secretStarted?'unknown':'not_filled';}
}"#;

// Only recognized top-document card fields are accepted; no caller selector or submit.
const CARD_FUNCTION:&str=r#"function(expected,pan,cvv,month,year,cardholder) {
 let started=false;
 try {
  const d=document, valid=()=>window.top===window&&document===d&&location.origin===expected&&d.visibilityState==='visible';
  const visible=x=>{const r=x.getBoundingClientRect(),s=getComputedStyle(x),px=Math.min(innerWidth-1,Math.max(0,r.left+r.width/2)),py=Math.min(innerHeight-1,Math.max(0,r.top+r.height/2));return x.isConnected&&!x.disabled&&!x.matches(':disabled')&&!x.readOnly&&typeof x.checkVisibility==='function'&&x.checkVisibility({checkOpacity:true,checkVisibilityCSS:true,contentVisibilityAuto:true})&&r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth&&d.elementFromPoint(px,py)===x&&s.display!=='none'&&s.visibility==='visible'&&Number(s.opacity)>0};
  const find=name=>[...d.querySelectorAll((name==='cc-exp-month'||name==='cc-exp-year'?'input,select':'input').split(',').map(tag=>tag+'[autocomplete~="'+name+'" i]').join(','))];
  const names=()=>[...new Set([...find('cc-name'),...find('name')])];
  const snapshot=()=>[find('cc-number'),find('cc-exp'),find('cc-exp-month'),find('cc-exp-year'),find('cc-csc'),names()];
  if(!valid())return 'not_filled';
  const groups=snapshot(),[numbers,combined,months,years,codes,holders]=groups;
  const split=combined.length===0&&months.length===1&&years.length===1;
  const single=combined.length===1&&months.length===0&&years.length===0;
  if(numbers.length!==1||(!split&&!single)||(cvv&&codes.length!==1)||(!cvv&&codes.length>1)||holders.length>1||groups.some(g=>g.some(x=>!visible(x))))return 'not_filled';
  const selectOption=(node,kind,wanted)=>{
   if(!(node instanceof HTMLSelectElement))return null;
   const matches=[...node.options].filter(o=>o.hasAttribute('value') && (kind==='month'?/^(?:[1-9]|0[1-9]|1[0-2])$/.test(o.value) && Number(o.value)===Number(wanted):/^[0-9]{4}$/.test(o.value) && o.value===wanted));
   if(matches.length!==1 || matches[0].disabled || matches[0].closest('optgroup')?.disabled)return null;
   return matches[0];
  };
  let expiryNodes,expiryValues,optionProof=[];
  if(split){
   expiryNodes=[months[0],years[0]];expiryValues=[month,year];
   for(let i=0;i<2;i++)if(expiryNodes[i] instanceof HTMLSelectElement){
    const kind=i===0?'month':'year',wanted=expiryValues[i],option=selectOption(expiryNodes[i],kind,wanted);
    if(!option)return 'not_filled';optionProof.push({node:expiryNodes[i],kind,wanted,option,value:option.value});expiryValues[i]=option.value;
   }
  }
  else {
   const x=combined[0],type=x.type.toLowerCase();let value;
   if(type==='month') value=year+'-'+month;
   else if(type==='text'&&x.maxLength===5) value=month+'/'+year.slice(-2);
   else if(type==='text'&&(x.maxLength===-1||x.maxLength>=7)) value=month+'/'+year;
   else return 'not_filled';
   expiryNodes=[x];expiryValues=[value];
  }
  const nodes=[numbers[0],...expiryNodes,...(cvv?[codes[0]]:[]),...(holders.length?[holders[0]]:[])];
  const form=nodes[0].form;
  if(new Set(nodes).size!==nodes.length||!nodes.every(x=>x.form===form))return 'not_filled';
  const inputSetter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;
  const selectSetter=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value')?.set;
  if(!inputSetter || (optionProof.length&&!selectSetter))return 'not_filled';
  const proofValid=()=>optionProof.every(p=>selectOption(p.node,p.kind,p.wanted)===p.option && p.option.value===p.value);
  const values=[pan,...expiryValues,...(cvv?[cvv]:[]),...(holders.length?[cardholder]:[])];
  for(let i=0;i<nodes.length;i++) {
   const current=snapshot();
   if(!valid()||!nodes.every(visible)||!nodes.every(x=>x.form===form)||!proofValid()||current.some((g,j)=>g.length!==groups[j].length||g.some((x,k)=>x!==groups[j][k])))return started?'unknown':'not_filled';
   started=true;(nodes[i] instanceof HTMLSelectElement?selectSetter:inputSetter).call(nodes[i],values[i]);
   if(nodes[i].value!==values[i])return 'unknown';
   nodes[i].dispatchEvent(new Event('input',{bubbles:true}));nodes[i].dispatchEvent(new Event('change',{bubbles:true}));
   const after=snapshot();
   if(!valid()||!nodes.every(visible)||!nodes.every(x=>x.form===form)||!proofValid()||after.some((g,j)=>g.length!==groups[j].length||g.some((x,k)=>x!==groups[j][k]))||nodes.slice(0,i+1).some((x,j)=>x.value!==values[j]))return 'unknown';
  }
  return 'filled';
 } catch (_) {return started?'unknown':'not_filled';}
}"#;
const CUSTODY_READ:&str=r#"function(expected){if(location.origin!==expected || !document.body)return null;const controls=Array.from(document.querySelectorAll('a,button,input,textarea,select')).slice(0,80).map(e=>({tag:e.tagName.toLowerCase(),text:(e.matches('input,textarea,select,[contenteditable]:not([contenteditable="false"])')||e.querySelector('input,textarea,select,[contenteditable]:not([contenteditable="false"])'))?'':(e.innerText||'').slice(0,80),id:(e.id||'').slice(0,80),name:(e.getAttribute('name')||'').slice(0,80),placeholder:(e.getAttribute('placeholder')||'').slice(0,80)}));const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let n,parts=[],size=0,seen=0;while((n=w.nextNode())&&seen++<10000&&size<10000){const p=n.parentElement;if(!p||p.closest('input,textarea,select,option,[contenteditable]:not([contenteditable="false"])')||!p.getClientRects().length)continue;const style=getComputedStyle(p);if(style.visibility==='hidden'||style.display==='none')continue;const t=(n.nodeValue||'').slice(0,10000-size);parts.push(t);size+=t.length}return 'Title: '+(document.title||'').slice(0,500)+'\nText: '+parts.join(' ')+'\nControls: '+JSON.stringify(controls).slice(0,3000)}"#;
const CUSTODY_INSPECT:&str=r#"function(expected,expectedUrl){if(location.origin!==expected || location.href!==expectedUrl || !document.body)return null;const controls=Array.from(document.querySelectorAll('a,button,input,textarea,select')).slice(0,80).map(e=>({tag:e.tagName.toLowerCase(),text:(e.matches('input,textarea,select,[contenteditable]:not([contenteditable="false"])')||e.querySelector('input,textarea,select,[contenteditable]:not([contenteditable="false"])'))?'':(e.innerText||'').slice(0,80),id:(e.id||'').slice(0,80),name:(e.getAttribute('name')||'').slice(0,80),placeholder:(e.getAttribute('placeholder')||'').slice(0,80)}));const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let n,parts=[],size=0,seen=0;while((n=w.nextNode())&&seen++<10000&&size<10000){const p=n.parentElement;if(!p||p.closest('input,textarea,select,option,[contenteditable]:not([contenteditable="false"])')||!p.getClientRects().length)continue;const style=getComputedStyle(p);if(style.visibility==='hidden'||style.display==='none')continue;const t=(n.nodeValue||'').slice(0,10000-size);parts.push(t);size+=t.length}return 'Title: '+(document.title||'').slice(0,500)+'\nText: '+parts.join(' ')+'\nControls: '+JSON.stringify(controls).slice(0,3000)}"#;
// Only the reviewed CSS selector is data; this function is fixed by Core.
// HTMLElement.click is synthetic and does not promise a trusted pointer event.
const CUSTODY_CLICK:&str=r#"function(expected,expectedUrl,selector){let e;try {if(location.origin!==expected||location.href!==expectedUrl)return false;const found=document.querySelectorAll(selector);if(found.length!==1)return false;e=found[0];if(!(e instanceof HTMLElement)||!e.isConnected||e.ownerDocument!==document||e.matches(':disabled')||!e.getClientRects().length)return false;const style=getComputedStyle(e);if(style.display==='none'||style.visibility==='hidden')return false;if(location.origin!==expected||location.href!==expectedUrl||!e.isConnected||e.ownerDocument!==document)return false;const again=document.querySelectorAll(selector);if(again.length!==1||again[0]!==e)return false}catch(_){return false}HTMLElement.prototype.click.call(e);if(location.origin!==expected)return null;return true}"#;
#[derive(Serialize)] struct CustodyClick<'a>{#[serde(rename="functionDeclaration")] function:&'static str,#[serde(rename="uniqueContextId")] context:&'a str,arguments:[Arg<'a>;3],#[serde(rename="returnByValue")] return_by_value:bool,silent:bool}
#[derive(Serialize)] struct CustodyInspect<'a>{#[serde(rename="functionDeclaration")] function:&'static str,#[serde(rename="uniqueContextId")] context:&'a str,arguments:[Arg<'a>;2],#[serde(rename="returnByValue")] return_by_value:bool,silent:bool}
#[derive(Serialize)] struct CustodyRead<'a>{#[serde(rename="functionDeclaration")] function:&'static str,#[serde(rename="uniqueContextId")] context:&'a str,arguments:[Arg<'a>;1],#[serde(rename="returnByValue")] return_by_value:bool,silent:bool}
#[derive(Serialize)] struct CardFill<'a>{#[serde(rename="functionDeclaration")] function:&'static str,#[serde(rename="uniqueContextId")] context:&'a str,arguments:[Arg<'a>;6],#[serde(rename="returnByValue")] return_by_value:bool,silent:bool}

#[derive(Serialize)] struct Command<'a,T:Serialize> {id:u64,method:&'a str,params:T,#[serde(skip_serializing_if="Option::is_none",rename="sessionId")] session:Option<&'a str>}
#[derive(Serialize)] struct Empty {}
#[derive(Serialize)] struct TargetId<'a> {#[serde(rename="targetId")]target:&'a str,flatten:bool}
#[derive(Serialize)] struct Navigate<'a>{url:&'a str}
#[derive(Serialize)] struct CloseTarget<'a>{#[serde(rename="targetId")] target:&'a str}
#[derive(Serialize)] struct DetachSession<'a>{#[serde(rename="sessionId")] session:&'a str}
#[derive(Serialize)] struct Isolated<'a>{#[serde(rename="frameId")] frame:&'a str,#[serde(rename="worldName")] world:&'a str}
#[derive(Serialize)] struct Arg<'a>{value:&'a str}
#[derive(Serialize)] struct Probe<'a>{#[serde(rename="functionDeclaration")] function:&'static str,#[serde(rename="uniqueContextId")] context:&'a str,#[serde(rename="returnByValue")] return_by_value:bool,silent:bool}
#[derive(Serialize)] struct AtomicFill<'a>{#[serde(rename="functionDeclaration")] function:&'a str,#[serde(rename="uniqueContextId")] context:&'a str,arguments:[Arg<'a>;3],#[serde(rename="returnByValue")] return_by_value:bool,silent:bool}

fn checked_runtime(config:&crate::vault::VaultConfig, manifest_hash:&str)->Result<PathBuf> {
    let root=config.core.parent().and_then(Path::parent).ok_or("vault_unavailable")?;
    let manifest=root.join("core-manifest.json");
    let pinned=SourcePin::open(&manifest,Some(manifest_hash)).map_err(|_| "vault_unavailable")?;
    let f=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&manifest).map_err(|_| "vault_unavailable")?;
    let mut raw=Vec::new(); f.take(2*1024*1024+1).read_to_end(&mut raw).map_err(|_| "vault_unavailable")?;
    if raw.len()>2*1024*1024 || format!("{:x}",Sha256::digest(&raw))!=manifest_hash {return Err("vault_unavailable");}
    let m:Value=serde_json::from_slice(&raw).map_err(|_| "vault_unavailable")?;
    let revision=m.pointer("/browsers/chromium").and_then(Value::as_str).ok_or("vault_unavailable")?;
    if revision.is_empty() || revision.len()>12 || !revision.bytes().all(|b|b.is_ascii_digit()) {return Err("vault_unavailable");}
    let relative=format!("{BROWSER_PREFIX}chromium-{revision}{BROWSER_SUFFIX}");
    let e=m.get("files").and_then(|v|v.get(relative.as_str())).ok_or("vault_unavailable")?;
    if e.get("executable").and_then(Value::as_bool)!=Some(true) {return Err("vault_unavailable");}
    let hash=e.get("sha256").and_then(Value::as_str).ok_or("vault_unavailable")?;
    let path=root.join(relative); let binary=SourcePin::open(&path,Some(hash)).map_err(|_| "vault_unavailable")?;
    let meta=std::fs::symlink_metadata(&path).map_err(|_| "vault_unavailable")?;
    if meta.mode()&0o111==0 || e.get("bytes").and_then(Value::as_u64)!=Some(meta.len()) {return Err("vault_unavailable");}
    if binary.path().canonicalize().ok().as_deref()!=Some(binary.path()) {return Err("vault_unavailable");}
    binary.revalidate().map_err(|_| "vault_unavailable")?; pinned.revalidate().map_err(|_| "vault_unavailable")?;Ok(path)
}

fn disable_password_saving(profile:&Path)->Result<()> {
    let dir=profile.join("Default");crate::platform::secure_directory(&dir).map_err(|_| "vault_unavailable")?;
    if dir.canonicalize().ok().as_deref()!=Some(dir.as_path()) {return Err("vault_unavailable");}
    let path=dir.join("Preferences");
    let existing=match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&path) {
        Ok(file)=>{
            let m=file.metadata().map_err(|_| "vault_unavailable")?;
            if !m.is_file() || m.uid()!=unsafe{libc::geteuid()} || m.nlink()!=1 || m.len()>2*1024*1024 {return Err("vault_unavailable");}
            let mut bytes=Vec::new();file.take(2*1024*1024+1).read_to_end(&mut bytes).map_err(|_| "vault_unavailable")?;
            let value:Value=serde_json::from_slice(&bytes).map_err(|_| "vault_unavailable")?;
            Some((value,m.dev(),m.ino(),m.len(),m.mtime(),m.mtime_nsec()))
        },
        Err(e) if e.kind()==std::io::ErrorKind::NotFound=>None,
        Err(_)=>return Err("vault_unavailable"),
    };
    let mut value=existing.as_ref().map(|v|v.0.clone()).unwrap_or_else(||serde_json::json!({}));
    let root=value.as_object_mut().ok_or("vault_unavailable")?;
    root.insert("credentials_enable_service".into(),Value::Bool(false));
    let profile_value=root.entry("profile").or_insert_with(||serde_json::json!({}));
    profile_value.as_object_mut().ok_or("vault_unavailable")?.insert("password_manager_enabled".into(),Value::Bool(false));
    let data=serde_json::to_vec(&value).map_err(|_| "vault_unavailable")?;
    let temp=dir.join(".gamja-vault-preferences.tmp");
    let mut file=OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&temp).map_err(|_| "vault_unavailable")?;
    let temp_identity=file.metadata().map_err(|_| "vault_unavailable")?;
    let result=(|| {
        file.write_all(&data).and_then(|_|file.sync_all()).map_err(|_| "vault_unavailable")?;
        match (&existing,std::fs::symlink_metadata(&path)) {
            (None,Err(e)) if e.kind()==std::io::ErrorKind::NotFound=>{},
            (Some((_,dev,ino,len,sec,nsec)),Ok(m)) if m.is_file() && m.dev()==*dev && m.ino()==*ino && m.len()==*len && m.mtime()==*sec && m.mtime_nsec()==*nsec=>{},
            _=>return Err("vault_unavailable"),
        }
        std::fs::rename(&temp,&path).map_err(|_| "vault_unavailable")?;
        Ok(())
    })();
    if result.is_err() {
        if let Ok(m)=std::fs::symlink_metadata(&temp) {
            if m.is_file() && m.dev()==temp_identity.dev() && m.ino()==temp_identity.ino() && m.uid()==unsafe{libc::geteuid()} {let _=std::fs::remove_file(&temp);}
        }
    }
    result
}

// A closed proof is produced only after explicit process-group shutdown and
// manifest revalidation. It carries the exact original window identity.
pub(crate) struct ClosedStartProof {pub browser:Browser,pub manifest:String,pub item:String,pub command:String}
pub(crate) enum StartFailure {Unconfirmed,ConfirmedClosed(ClosedStartProof)}
impl From<&'static str> for StartFailure {fn from(_: &'static str)->Self {Self::Unconfirmed}}
pub(crate) struct Browser { child:OwnedChild, closed:bool, pub needs_close:bool, pub card:bool, cdp_stage:FailureStage, diagnostic_dir:PathBuf, input:Option<UnixStream>, output:Option<UnixStream>, proxy_pump:Option<crate::vault_proxy_pump::ProxyPump>, profile_lease:Option<Arc<std::sync::Mutex<crate::vault_profile::ProfileLease>>>, profile_lock:Option<File>, pub item:String,pub command:String,pub window_item:String,pub window_command:String,pub revision:u64, next:u64, session:String, frame:String, context:String, world:String, contexts:HashMap<i64,String>, documents:Vec<crate::vault_proxy_pump::DocumentResponse>, loaded:Vec<crate::vault_proxy_pump::DocumentLoaded>, started:Vec<crate::vault_proxy_pump::FrameStarted>, origin:String, manifest:String, manifest_path:PathBuf, receipts:PathBuf, cancellation:Option<UnixStream>, stop:Arc<AtomicBool> }
impl Browser {
    pub(crate) fn start(config:&crate::vault::VaultConfig,manifest:&str,realm:&str,item:&str,command:&str,revision:u64,website:&str,cancel:&UnixStream,stop:Arc<AtomicBool>)->std::result::Result<Self,StartFailure> {
        if realm.len()!=64 || !realm.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err(StartFailure::Unconfirmed);}
        let origin=crate::vault::login_origin(website)?;let executable=checked_runtime(config,manifest).map_err(|e|{record_failure(&config.directory,FailureStage::RuntimePin);e})?;
        let profile_setup=|| -> Result<_> {
        let root=config.directory.parent().ok_or("vault_unavailable")?.join("vault-browser-profiles");
        crate::platform::secure_directory(&root).map_err(|_| "vault_unavailable")?;
        if root.canonicalize().ok().as_deref()!=Some(root.as_path()) {return Err("vault_unavailable");}
        let profile=root.join(realm);crate::platform::secure_directory(&profile).map_err(|_| "vault_unavailable")?;
        if profile.canonicalize().ok().as_deref()!=Some(profile.as_path()) {return Err("vault_unavailable");}
        let lock=crate::platform::acquire_lock(&root.join(format!("{realm}.lock"))).map_err(|_| "vault_unavailable")?;
        disable_password_saving(&profile)?;
        if command.len()!=32 || !command.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err("vault_unavailable");}
        let receipts=root.join(format!("receipts-{realm}"));crate::platform::secure_directory(&receipts).map_err(|_| "vault_unavailable")?;
        if receipts.canonicalize().ok().as_deref()!=Some(receipts.as_path()) {return Err("vault_unavailable");}

        Ok((profile,lock,receipts))
        };
        let (profile,lock,receipts)=profile_setup().map_err(|e|{record_failure(&config.directory,FailureStage::ProfileSetup);e})?;
        let manifest_path=config.core.parent().and_then(Path::parent).ok_or("vault_unavailable")?.join("core-manifest.json");
        let cancellation=cancel.try_clone().map_err(|_| {record_failure(&config.directory,FailureStage::Spawn);"vault_unavailable"})?;
        let (input,child_input)=UnixStream::pair().map_err(|_| {record_failure(&config.directory,FailureStage::Spawn);"vault_unavailable"})?;
        let (output,child_output)=UnixStream::pair().map_err(|_| {record_failure(&config.directory,FailureStage::Spawn);"vault_unavailable"})?;
        let null=File::open("/dev/null").map_err(|_| {record_failure(&config.directory,FailureStage::Spawn);"vault_unavailable"})?;
        let sink=OpenOptions::new().write(true).open("/dev/null").map_err(|_| {record_failure(&config.directory,FailureStage::Spawn);"vault_unavailable"})?;
        // OwnedChild requires unique source FDs, even when both sinks are /dev/null.
        let errors=sink.try_clone().map_err(|_| {record_failure(&config.directory,FailureStage::Spawn);"vault_unavailable"})?;
        let user_data=format!("--user-data-dir={}",profile.to_str().ok_or("vault_unavailable")?);
        let args=[user_data.as_str(),"--remote-debugging-pipe","--no-first-run","--no-default-browser-check","--disable-background-networking","--new-window","about:blank"];
        let env=std::env::var("HOME").map(|v|vec![("HOME".into(),v)]).unwrap_or_default();
        let child=OwnedChild::spawn(&executable,&args,env,&[(null.as_raw_fd(),0),(sink.as_raw_fd(),1),(errors.as_raw_fd(),2),(child_input.as_raw_fd(),3),(child_output.as_raw_fd(),4)],Duration::from_secs(5)).map_err(|_| {record_failure(&config.directory,FailureStage::Spawn);"vault_unavailable"})?;
        drop((child_input,child_output,null,sink,errors));
        let mut browser=Self{child,closed:false,needs_close:false,card:false,cdp_stage:FailureStage::CdpPrepare,diagnostic_dir:config.directory.clone(),input:Some(input),output:Some(output),proxy_pump:None,profile_lease:None,profile_lock:Some(lock),item:item.into(),command:command.into(),window_item:item.into(),window_command:command.into(),revision,next:1,session:String::new(),frame:String::new(),context:String::new(),world:String::new(),contexts:HashMap::new(),documents:Vec::new(),loaded:Vec::new(),started:Vec::new(),origin,manifest:manifest.to_owned(),manifest_path,receipts,cancellation:Some(cancellation),stop};
        match browser.prepare(website) {
            Ok(())|Err("vault_origin_changed")=>Ok(browser),
            Err(_)=>{
                browser.record_cdp_failure();
                if browser.shutdown_confirmed().is_ok() {
                    if let Ok(verified)=browser.checked_manifest() {
                        let item=browser.window_item.clone();let command=browser.window_command.clone();
                        return Err(StartFailure::ConfirmedClosed(ClosedStartProof{browser,manifest:verified,item,command}));
                    }
                }
                Err(StartFailure::Unconfirmed)
            }
        }
    }
    /// Start the exact Engine-attested persistent Bot profile under the same
    /// cross-process lease, with the authenticated OriginProxy as sole route.
    pub(crate) fn start_profile(config:&crate::vault::VaultConfig,manifest:&str,realm:&str,grant:&crate::vault_profile::Grant,lease:crate::vault_profile::ProfileLease,command:&str,cancel:&UnixStream,stop:Arc<AtomicBool>,revoked:Arc<AtomicBool>)->std::result::Result<Self,StartFailure> {
        let setup=(|| -> Result<_> {
            let app_data=config.directory.parent().ok_or("vault_unavailable")?;
            let profile=grant.validate(app_data,grant.record.owner_id,&grant.record.owner_epoch)?;
            lease.check()?;
            let executable=checked_runtime(config,manifest)?;
            disable_password_saving(&profile)?;
            if command.len()!=32 || !command.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err("vault_unavailable");}
            let receipts_root=app_data.join("vault-browser-profiles");crate::platform::secure_directory(&receipts_root).map_err(|_|"vault_unavailable")?;
            let receipts=receipts_root.join(format!("receipts-{realm}"));crate::platform::secure_directory(&receipts).map_err(|_|"vault_unavailable")?;
            if receipts.canonicalize().ok().as_deref()!=Some(receipts.as_path()) {return Err("vault_unavailable");}
            let manifest_path=config.core.parent().and_then(Path::parent).ok_or("vault_unavailable")?.join("core-manifest.json");
            let cancellation=cancel.try_clone().map_err(|_|"vault_unavailable")?;
            let (input,child_input)=UnixStream::pair().map_err(|_|"vault_unavailable")?;
            let (output,child_output)=UnixStream::pair().map_err(|_|"vault_unavailable")?;
            let null=File::open("/dev/null").map_err(|_|"vault_unavailable")?;
            let sink=OpenOptions::new().write(true).open("/dev/null").map_err(|_|"vault_unavailable")?;
            let errors=sink.try_clone().map_err(|_|"vault_unavailable")?;
            let user_data=format!("--user-data-dir={}",profile.to_str().ok_or("vault_unavailable")?);
            let proxy=format!("--proxy-server=http://127.0.0.1:{}",grant.proxy_port);
            let args=[user_data.as_str(),proxy.as_str(),"--proxy-bypass-list=<-loopback>","--disable-http2","--disable-quic","--remote-debugging-pipe","--no-first-run","--no-default-browser-check","--disable-background-networking","--new-window","about:blank"];
            let env=std::env::var("HOME").map(|v|vec![("HOME".into(),v)]).unwrap_or_default();
            let child=OwnedChild::spawn(&executable,&args,env,&[(null.as_raw_fd(),0),(sink.as_raw_fd(),1),(errors.as_raw_fd(),2),(child_input.as_raw_fd(),3),(child_output.as_raw_fd(),4)],Duration::from_secs(5)).map_err(|_|"vault_unavailable")?;
            drop((child_input,child_output,null,sink,errors));
            Ok((child,input,output,manifest_path,cancellation,receipts))
        })();
        let (mut child,input,output,manifest_path,cancellation,receipts)=match setup {Ok(value)=>value,Err(_)=>{lease.release_after_confirmed_exit();return Err(StartFailure::Unconfirmed);}};
        let pump=match crate::vault_proxy_pump::ProxyPump::start(input,output,grant.proxy_port,Zeroizing::new(grant.proxy_secret.as_str().to_owned()),stop.clone(),revoked) {
            Ok(pump)=>pump,
            Err(_)=>{if child.shutdown(Duration::from_secs(2)).is_ok(){lease.release_after_confirmed_exit();}else{lease.preserve_uncertain();}return Err(StartFailure::Unconfirmed);}
        };
        let mut browser=Self{child,closed:false,needs_close:false,card:false,cdp_stage:FailureStage::CdpPrepare,diagnostic_dir:config.directory.clone(),input:None,output:None,proxy_pump:Some(pump),profile_lease:Some(Arc::new(std::sync::Mutex::new(lease))),profile_lock:None,item:grant.record.item_id.clone(),command:command.into(),window_item:grant.record.item_id.clone(),window_command:command.into(),revision:grant.record.secret_revision,next:1,session:String::new(),frame:String::new(),context:String::new(),world:String::new(),contexts:HashMap::new(),documents:Vec::new(),loaded:Vec::new(),started:Vec::new(),origin:grant.record.origin.clone(),manifest:manifest.to_owned(),manifest_path,receipts,cancellation:Some(cancellation),stop};
        match browser.prepare(&grant.record.origin) {
            Ok(())|Err("vault_origin_changed")=>Ok(browser),
            Err(_)=>{browser.record_cdp_failure();if browser.shutdown_confirmed().is_ok(){if let Ok(verified)=browser.checked_manifest(){let item=browser.window_item.clone();let command=browser.window_command.clone();return Err(StartFailure::ConfirmedClosed(ClosedStartProof{browser,manifest:verified,item,command}));}}Err(StartFailure::Unconfirmed)}
        }
    }
    fn command<T:Serialize>(&mut self,method:&str,params:T,session:Option<&str>,deadline:Instant)->Result<Value> {
        if self.stop.load(Ordering::SeqCst) || self.cancellation.as_ref().is_some_and(|s|!crate::vault_control::active(s)) {return Err("vault_unavailable");}
        let id=self.next;self.next=self.next.checked_add(1).ok_or("vault_unavailable")?;
        let data=Zeroizing::new(serde_json::to_vec(&Command{id,method,params,session}).map_err(|_| "vault_unavailable")?);
        if data.len()>MAX_PACKET {return Err("vault_unavailable");}
        if let Some(pump)=&self.proxy_pump {
            let reply=pump.command(id,data,deadline)?;
            if self.documents.len().saturating_add(reply.documents.len())>256 || self.loaded.len().saturating_add(reply.loaded.len())>256 || self.started.len().saturating_add(reply.started.len())>256 {return Err("vault_unavailable");}
            self.documents.extend(reply.documents);self.loaded.extend(reply.loaded);self.started.extend(reply.started);
            for event in reply.contexts {
                if event.session==self.session && event.name==self.world && event.frame==self.frame {self.contexts.insert(event.id,event.unique_id);}
            }
            return Ok(reply.response.into_value());
        }
        let input=self.input.as_mut().ok_or("vault_unavailable")?;
        input.set_write_timeout(Some(deadline.saturating_duration_since(Instant::now()))).map_err(|_| "vault_unavailable")?;
        input.write_all(&data).and_then(|_|input.write_all(&[0])).map_err(|_| "vault_unavailable")?;
        loop {let event=self.read(deadline)?;
            if event.get("method").and_then(Value::as_str)==Some("Runtime.executionContextCreated") && event.get("sessionId").and_then(Value::as_str)==Some(self.session.as_str()) {
                if let Some(c)=event.pointer("/params/context") {
                    if c.get("name").and_then(Value::as_str)==Some(self.world.as_str()) && c.pointer("/auxData/frameId").and_then(Value::as_str)==Some(self.frame.as_str()) {
                        if let (Some(id),Some(unique))=(c.get("id").and_then(Value::as_i64),c.get("uniqueId").and_then(Value::as_str)) {
                            if !unique.is_empty() && unique.len()<=256 {self.contexts.insert(id,unique.to_owned());}
                        }
                    }
                }
            }
            if event.get("id").and_then(Value::as_u64)==Some(id) {if event.get("error").is_some(){wipe(event);return Err("vault_unavailable");}return Ok(event);}
            wipe(event);
        }

    }
    fn read(&mut self,deadline:Instant)->Result<Value> {
        let mut data=Zeroizing::new(Vec::new());let mut byte=[0u8;1];
        loop {let remaining=deadline.checked_duration_since(Instant::now()).ok_or("vault_unavailable")?;
            if self.stop.load(Ordering::SeqCst) || self.cancellation.as_ref().is_some_and(|s|!crate::vault_control::active(s)) {return Err("vault_unavailable");}
            let output=self.output.as_mut().ok_or("vault_unavailable")?;
            let mut p=libc::pollfd{fd:output.as_raw_fd(),events:libc::POLLIN,revents:0};
            let wait=remaining.as_millis().min(100).max(1) as i32;
            let n=unsafe{libc::poll(&mut p,1,wait)};if n<0 {return Err("vault_unavailable");}if n==0 {continue;}
            if p.revents & (libc::POLLHUP|libc::POLLERR|libc::POLLNVAL)!=0 {return Err("vault_unavailable");}
            output.read_exact(&mut byte).map_err(|_| "vault_unavailable")?;
            if byte[0]==0 {break;}if data.len()>=MAX_PACKET {return Err("vault_unavailable");}data.push(byte[0]);}
        serde_json::from_slice(&data).map_err(|_| "vault_unavailable")
    }
    fn prepare(&mut self,website:&str)->Result<()> {
        let end=Instant::now()+Duration::from_secs(20);
        self.cdp_stage=FailureStage::CdpTargets;
        // For the dedicated, leased Bot profile, create the desired blank
        // target first so closing restored pages cannot terminate Chromium.
        // Only target IDs are used; prior page URL and content stay unread.
        let target=if self.proxy_pump.is_some() {
            self.cdp_stage=FailureStage::CdpTargetsCommand;
            let created=self.command("Target.createTarget",Navigate{url:"about:blank"},None,end)?;
            self.cdp_stage=FailureStage::CdpTargetsId;
            let fresh=created.pointer("/result/targetId").and_then(Value::as_str).filter(|id|!id.is_empty() && id.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;wipe(created);
            // A close acknowledgement precedes target destruction. Poll the
            // same bounded snapshot; never repeat a close for an acknowledged ID.
            let mut closed=std::collections::HashSet::<String>::new();
            loop {
                if Instant::now()>=end {self.cdp_stage=FailureStage::CdpTargetsSettleTimeout;return Err("vault_unavailable");}
                self.cdp_stage=FailureStage::CdpTargetsCommand;
                let targets=self.command("Target.getTargets",Empty{},None,end)?;
                self.cdp_stage=FailureStage::CdpTargetsMalformed;
                let items=targets.pointer("/result/targetInfos").and_then(Value::as_array).ok_or("vault_unavailable")?;
                if items.len()>32 {wipe(targets);return Err("vault_unavailable");}
                let ids=items.iter().filter(|v|v.get("type").and_then(Value::as_str)==Some("page")).map(|v|v.get("targetId").and_then(Value::as_str).filter(|id|!id.is_empty() && id.len()<=256).map(str::to_owned).ok_or("vault_unavailable")).collect::<Result<Vec<_>>>()?;
                wipe(targets);
                let mut seen=std::collections::HashSet::new();
                if ids.iter().any(|id|!seen.insert(id.as_str())) {return Err("vault_unavailable");}
                if !ids.iter().any(|id|id==&fresh) {self.cdp_stage=FailureStage::CdpTargetsFreshMissing;return Err("vault_unavailable");}
                if ids.len()==1 {break;}
                for id in &ids {
                    if id==&fresh || closed.contains(id) {continue;}
                    if closed.len()>=31 {self.cdp_stage=FailureStage::CdpTargetsMalformed;return Err("vault_unavailable");}
                    self.cdp_stage=FailureStage::CdpTargetsCommand;
                    let result=self.command("Target.closeTarget",CloseTarget{target:id},None,end)?;
                    let accepted=result.pointer("/result/success").and_then(Value::as_bool)==Some(true);wipe(result);
                    if !accepted {return Err("vault_unavailable");}
                    closed.insert(id.clone());
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            fresh
        } else {loop {
            if Instant::now()>=end {self.cdp_stage=FailureStage::CdpTargetsEmpty;return Err("vault_unavailable");}
            self.cdp_stage=FailureStage::CdpTargetsCommand;
            let targets=self.command("Target.getTargets",Empty{},None,end)?;
            self.cdp_stage=FailureStage::CdpTargetsMalformed;
            let pages=targets.pointer("/result/targetInfos").and_then(Value::as_array).ok_or("vault_unavailable")?.iter().filter(|v|v.get("type").and_then(Value::as_str)==Some("page")).collect::<Vec<_>>();
            if pages.len()>1 {self.cdp_stage=FailureStage::CdpTargetsMultiple;return Err("vault_unavailable");}
            if let Some(page)=pages.first() {self.cdp_stage=FailureStage::CdpTargetsId;break page.get("targetId").and_then(Value::as_str).ok_or("vault_unavailable")?.to_owned();}
            if Instant::now()>=end {self.cdp_stage=FailureStage::CdpTargetsEmpty;return Err("vault_unavailable");}
            std::thread::sleep(Duration::from_millis(100));
        }};
        self.cdp_stage=FailureStage::CdpAttach;
        let attached=self.command("Target.attachToTarget",TargetId{target:&target,flatten:true},None,end)?;
        self.session=attached.pointer("/result/sessionId").and_then(Value::as_str).ok_or("vault_unavailable")?.to_owned();
        let session=self.session.clone();
        if let Some(pump)=&self.proxy_pump {pump.bind_session(session.clone(),end)?;}
        self.cdp_stage=FailureStage::CdpPageEnable;self.command("Page.enable",Empty{},Some(&session),end)?;
        self.cdp_stage=FailureStage::CdpRuntimeEnable;self.command("Runtime.enable",Empty{},Some(&session),end)?;
        if self.proxy_pump.is_some() {self.command("Fetch.enable",serde_json::json!({"handleAuthRequests":true}),Some(&session),end)?;}
        self.cdp_stage=FailureStage::CdpNavigate;let navigation=self.command("Page.navigate",Navigate{url:website},Some(&session),end)?;
        if navigation.pointer("/result/errorText").is_some() {return Err("vault_unavailable");}
        let expected_frame=navigation.pointer("/result/frameId").and_then(Value::as_str).ok_or("vault_unavailable")?.to_owned();
        let expected_loader=navigation.pointer("/result/loaderId").and_then(Value::as_str).ok_or("vault_unavailable")?.to_owned();
        self.cdp_stage=FailureStage::CdpFrameTree;
        loop {
            let tree=self.command("Page.getFrameTree",Empty{},Some(&session),end)?;
            let frame=tree.pointer("/result/frameTree/frame").ok_or("vault_unavailable")?;
            let frame_id=frame.get("id").and_then(Value::as_str).ok_or("vault_unavailable")?;
            let loader=frame.get("loaderId").and_then(Value::as_str).unwrap_or("");
            let frame_url=frame.get("url").and_then(Value::as_str).unwrap_or("");
            if frame_id==expected_frame && loader==expected_loader {
                if crate::vault::login_origin(frame_url).ok().as_deref()!=Some(self.origin.as_str()) {return Err("vault_origin_changed");}
                self.frame=frame_id.to_owned();break;
            }
            if Instant::now()>=end {return Err("vault_unavailable");}
            std::thread::sleep(Duration::from_millis(100));
        }
        self.bind_world(end)
    }
    pub(crate) fn rearm(&mut self,item:&str,command:&str,revision:u64,website:&str,cancel:&UnixStream,stop:Arc<AtomicBool>)->Result<()> {
        self.cdp_stage=FailureStage::CdpPrepare;
        if command.len()!=32 || !command.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err("vault_unavailable");}
        self.item=item.to_owned();self.command=command.to_owned();self.revision=revision;
        self.origin=crate::vault::login_origin(website)?;self.cancellation=Some(cancel.try_clone().map_err(|_| "vault_unavailable")?);self.stop=stop;
        self.context.clear();self.contexts.clear();
        let session=self.session.clone();let end=Instant::now()+Duration::from_secs(15);
        self.cdp_stage=FailureStage::CdpPageEnable;self.command("Page.enable",Empty{},Some(&session),end)?;
        self.cdp_stage=FailureStage::CdpRuntimeEnable;self.command("Runtime.enable",Empty{},Some(&session),end)?;
        self.cdp_stage=FailureStage::CdpFrameTree;let tree=self.command("Page.getFrameTree",Empty{},Some(&session),end)?;
        let frame=tree.pointer("/result/frameTree/frame").ok_or("vault_unavailable")?;
        let frame_url=frame.get("url").and_then(Value::as_str).ok_or("vault_unavailable")?;
        if crate::vault::login_origin(frame_url).ok().as_deref()!=Some(self.origin.as_str()) {return Err("vault_origin_changed");}
        self.frame=frame.get("id").and_then(Value::as_str).ok_or("vault_unavailable")?.to_owned();
        self.bind_world(end)
    }
    fn bind_world(&mut self,end:Instant)->Result<()> {
        let session=self.session.clone();let frame=self.frame.clone();
        self.world=format!("gamja-vault-{}",self.command);let world=self.world.clone();
        // Re-enable reporting to capture an existing named world as well as a
        // newly-created one; never trust an old numeric context ID.
        self.cdp_stage=FailureStage::CdpRuntimeEnable;
        self.command("Runtime.disable",Empty{},Some(&session),end)?;
        self.context.clear();self.contexts.clear();
        self.command("Runtime.enable",Empty{},Some(&session),end)?;
        self.cdp_stage=FailureStage::CdpWorld;
        let isolated=self.command("Page.createIsolatedWorld",Isolated{frame:&frame,world:&world},Some(&session),end)?;
        let id=isolated.pointer("/result/executionContextId").and_then(Value::as_i64).ok_or("vault_unavailable")?;
        self.cdp_stage=FailureStage::CdpContext;self.context=self.contexts.remove(&id).ok_or("vault_unavailable")?;
        self.cdp_stage=FailureStage::CdpInvalidProbe;let invalid=Probe{function:"function(){return true}",context:"gamja-invalid-context",return_by_value:true,silent:true};
        if self.command("Runtime.callFunctionOn",invalid,Some(&session),end).is_ok() {return Err("vault_unavailable");}
        self.cdp_stage=FailureStage::CdpValidProbe;let context=self.context.clone();
        let probe=Probe{function:"function(){return true}",context:&context,return_by_value:true,silent:true};
        let response=self.command("Runtime.callFunctionOn",probe,Some(&session),end)?;
        if response.pointer("/result/result/value").and_then(Value::as_bool)!=Some(true) || response.pointer("/result/exceptionDetails").is_some() {return Err("vault_unavailable");}
        Ok(())
    }
    pub(crate) fn authorize_peer(&self,peer:&UnixStream)->Result<()> {
        let pin=SourcePin::open(&self.manifest_path,Some(&self.manifest)).map_err(|_| "vault_unavailable")?;
        crate::vault_control::verify_peer(peer,&self.manifest)?;
        pin.revalidate().map_err(|_| "vault_unavailable")
    }
    pub(crate) fn checked_manifest(&self)->Result<String> {
        let pin=SourcePin::open(&self.manifest_path,Some(&self.manifest)).map_err(|_| "vault_unavailable")?;
        pin.revalidate().map_err(|_| "vault_unavailable")?;Ok(self.manifest.clone())
    }
    pub(crate) fn record_cdp_failure(&self) {record_failure(&self.diagnostic_dir,self.cdp_stage);}
    pub(crate) fn record_card_read_failure(&self) {record_failure(&self.diagnostic_dir,FailureStage::CardRead);}
    pub(crate) fn mark_unknown(&mut self) {self.needs_close=true;self.cancellation.take();}
    pub(crate) fn has_context(&self)->bool {!self.context.is_empty()}
    pub(crate) fn disarm(&mut self) {self.cancellation.take();}
    pub(crate) fn quiet(&mut self)->Result<()> {
        let session=self.session.clone();let end=Instant::now()+Duration::from_secs(5);
        self.command("Runtime.disable",Empty{},Some(&session),end)?;
        self.command("Page.disable",Empty{},Some(&session),end)?;
        Ok(())
    }
    /// Send only a caller-selected fixed operation through the sole writer;
    /// the guard runs directly before its frame reaches Chromium.
    fn custody_guarded<T:Serialize>(&mut self,method:&str,params:T,session:&str,deadline:Instant,guard:crate::vault_proxy_pump::WriteGuard)->Result<Value> {
        if self.stop.load(Ordering::SeqCst) || self.cancellation.as_ref().is_some_and(|s|!crate::vault_control::active(s)) {return Err("vault_unavailable");}
        let id=self.next;self.next=self.next.checked_add(1).ok_or("vault_unavailable")?;
        let frame=Zeroizing::new(serde_json::to_vec(&Command{id,method,params,session:Some(session)}).map_err(|_|"vault_unavailable")?);
        if frame.len()>MAX_PACKET {return Err("vault_unavailable");}
        let reply=self.proxy_pump.as_ref().ok_or("vault_unavailable")?.command_guarded(id,frame,deadline,Some(guard))?;
        if self.documents.len().saturating_add(reply.documents.len())>256 || self.loaded.len().saturating_add(reply.loaded.len())>256 || self.started.len().saturating_add(reply.started.len())>256 {return Err("vault_unavailable");}self.documents.extend(reply.documents);self.loaded.extend(reply.loaded);self.started.extend(reply.started);
        for event in reply.contexts {if event.session==self.session && event.name==self.world && event.frame==self.frame {self.contexts.insert(event.id,event.unique_id);}}
        Ok(reply.response.into_value())
    }
    /// Capture only the reviewed current top document. Pixels remain in Core
    /// until a separately authorized bounded private chunk request.
    pub(crate) fn custody_screenshot(&mut self,url:&str,origin:&str,guard:crate::vault_proxy_pump::WriteGuard)->Result<Option<Zeroizing<Vec<u8>>>> {
        use base64::Engine as _;
        self.cdp_stage=FailureStage::CdpFrameTree;
        if crate::vault::login_origin(url).ok().as_deref()!=Some(origin) || self.session.is_empty() || self.frame.is_empty() {return Ok(None);}
        let normalized=url::Url::parse(url).map_err(|_|"vault_unavailable")?.to_string();let session=self.session.clone();let end=Instant::now()+Duration::from_secs(5);
        let tree=self.command("Page.getFrameTree",Empty{},Some(&session),end)?;
        let frame=tree.pointer("/result/frameTree/frame").ok_or("vault_unavailable")?;
        let valid=frame.get("id").and_then(Value::as_str)==Some(self.frame.as_str()) && frame.get("url").and_then(Value::as_str).and_then(|u|url::Url::parse(u).ok()).is_some_and(|u|u.as_str()==normalized);
        let loader=frame.get("loaderId").and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;wipe(tree);
        if !valid{return Ok(None);}
        let id=self.next;self.next=self.next.checked_add(1).ok_or("vault_unavailable")?;
        let frame=Zeroizing::new(serde_json::to_vec(&Command{id,method:"Page.captureScreenshot",params:serde_json::json!({"format":"png","fromSurface":true,"captureBeyondViewport":false}),session:Some(session.as_str())}).map_err(|_|"vault_unavailable")?);
        self.cdp_stage=FailureStage::CdpRead;
        let reply=self.proxy_pump.as_ref().ok_or("vault_unavailable")?.capture_guarded(id,frame,end,guard)?;
        if self.documents.len().saturating_add(reply.documents.len())>256 || self.loaded.len().saturating_add(reply.loaded.len())>256 || self.started.len().saturating_add(reply.started.len())>256 {wipe(reply.response.into_value());return Err("vault_unavailable");}
        self.documents.extend(reply.documents);self.loaded.extend(reply.loaded);self.started.extend(reply.started);
        for event in reply.contexts {if event.session==self.session && event.name==self.world && event.frame==self.frame {self.contexts.insert(event.id,event.unique_id);}}
        let response=reply.response.into_value();
        let decoded=(|| -> Result<Option<Zeroizing<Vec<u8>>>> {
            let data=response.pointer("/result/data").and_then(Value::as_str).ok_or("vault_unavailable")?;
            if data.len()>13_333_336 {return Ok(None);}
            Ok(Some(Zeroizing::new(base64::engine::general_purpose::STANDARD.decode(data).map_err(|_|"vault_unavailable")?)))
        })();wipe(response);
        let Some(image)=decoded? else{return Ok(None);};
        if image.len()<24 || image.len()>10_000_000 || &image[..16]!=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" {return Ok(None);}
        let width=u32::from_be_bytes(image[16..20].try_into().map_err(|_|"vault_unavailable")?);let height=u32::from_be_bytes(image[20..24].try_into().map_err(|_|"vault_unavailable")?);
        if width==0 || height==0 || u64::from(width)*u64::from(height)>25_000_000 {return Ok(None);}
        self.cdp_stage=FailureStage::CdpFrameTree;
        let after=self.command("Page.getFrameTree",Empty{},Some(&session),end)?;
        let same=after.pointer("/result/frameTree/frame").is_some_and(|v|v.get("id").and_then(Value::as_str)==Some(self.frame.as_str()) && v.get("loaderId").and_then(Value::as_str)==Some(loader.as_str()) && v.get("url").and_then(Value::as_str).and_then(|u|url::Url::parse(u).ok()).is_some_and(|u|u.as_str()==normalized));wipe(after);
        if !same {return Err("vault_unavailable");}Ok(Some(image))
    }
    /// Navigate the one borrowed blank page and return bounded displayed text.
    /// The caller revalidates Owner/approval/lease both before each write and
    /// before releasing this result. All raw CDP values stay in Core.
    pub(crate) fn custody_navigate(&mut self,url:&str,origin:&str,navigate_guard:crate::vault_proxy_pump::WriteGuard,read_guard:crate::vault_proxy_pump::WriteGuard)->Result<Option<String>> {
        self.cdp_stage=FailureStage::CdpNavigate;
        if crate::vault::login_origin(url).ok().as_deref()!=Some(origin) || self.session.is_empty(){return Err("vault_unavailable");}
        let end=Instant::now()+Duration::from_secs(15);let session=self.session.clone();self.documents.clear();self.loaded.clear();
        self.cdp_stage=FailureStage::CdpNavigate;
        let navigated=self.custody_guarded("Page.navigate",Navigate{url},&session,end,navigate_guard)?;
        if navigated.pointer("/result/errorText").is_some() {wipe(navigated);return Ok(None);}
        let frame=navigated.pointer("/result/frameId").and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;
        let loader=navigated.pointer("/result/loaderId").and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;wipe(navigated);
        loop {
            self.cdp_stage=FailureStage::CdpFrameTree;
            let tree=self.command("Page.getFrameTree",Empty{},Some(&session),end)?;
            let matches=tree.pointer("/result/frameTree/frame").is_some_and(|v|v.get("id").and_then(Value::as_str)==Some(frame.as_str()) && v.get("loaderId").and_then(Value::as_str)==Some(loader.as_str()) && v.get("url").and_then(Value::as_str).and_then(|v|crate::vault::login_origin(v).ok()).as_deref()==Some(origin));wipe(tree);
            if let Some(doc)=self.documents.iter().find(|v|v.session==session && v.frame==frame && v.loader==loader) {
                if doc.origin!=origin || !(200..300).contains(&doc.status) {return Ok(None);}
                if matches && self.loaded.iter().any(|v|v.session==session && v.frame==frame && v.loader==loader) {break;}
            }
            if Instant::now()>=end {return Err("vault_unavailable");}std::thread::sleep(Duration::from_millis(50));
        }
        self.origin=origin.to_owned();self.frame=frame;self.context.clear();self.contexts.clear();self.bind_world(end)?;
        let context=self.context.clone();let params=CustodyRead{function:CUSTODY_READ,context:&context,arguments:[Arg{value:origin}],return_by_value:true,silent:true};
        self.cdp_stage=FailureStage::CdpRead;
        let response=self.custody_guarded("Runtime.callFunctionOn",params,&session,end,read_guard)?;
        let text=if response.pointer("/result/exceptionDetails").is_none(){response.pointer("/result/result/value").and_then(Value::as_str).map(str::to_owned)}else{None};wipe(response);
        let mut text=text.ok_or("vault_unavailable")?;
        if text.len()>12000 {let mut n=12000;while !text.is_char_boundary(n){n-=1;}text.truncate(n);}
        Ok(Some(text))
    }
    /// Inspect only the existing same-workflow document; never navigate here.
    pub(crate) fn custody_inspect(&mut self,url:&str,origin:&str,guard:crate::vault_proxy_pump::WriteGuard)->Result<Option<String>> {
        self.cdp_stage=FailureStage::CdpFrameTree;
        if crate::vault::login_origin(url).ok().as_deref()!=Some(origin) || self.session.is_empty() || self.frame.is_empty() {return Ok(None);}
        let normalized=url::Url::parse(url).map_err(|_|"vault_unavailable")?.to_string();
        let end=Instant::now()+Duration::from_secs(10);let session=self.session.clone();
        self.cdp_stage=FailureStage::CdpFrameTree;
        let tree=self.command("Page.getFrameTree",Empty{},Some(&session),end)?;
        let matches=tree.pointer("/result/frameTree/frame").is_some_and(|v|v.get("id").and_then(Value::as_str)==Some(self.frame.as_str()) && v.get("url").and_then(Value::as_str).and_then(|u|url::Url::parse(u).ok()).is_some_and(|u|u.as_str()==normalized));wipe(tree);
        if !matches {return Ok(None);}
        self.origin=origin.to_owned();self.context.clear();self.contexts.clear();self.bind_world(end)?;
        let context=self.context.clone();let params=CustodyInspect{function:CUSTODY_INSPECT,context:&context,arguments:[Arg{value:origin},Arg{value:&normalized}],return_by_value:true,silent:true};
        self.cdp_stage=FailureStage::CdpRead;
        let response=self.custody_guarded("Runtime.callFunctionOn",params,&session,end,guard)?;
        let text=if response.pointer("/result/exceptionDetails").is_none(){response.pointer("/result/result/value").and_then(Value::as_str).map(str::to_owned)}else{None};wipe(response);
        let Some(mut text)=text else{return Ok(None);};
        if text.len()>12000 {let mut n=12000;while !text.is_char_boundary(n){n-=1;}text.truncate(n);}
        Ok(Some(text))
    }
    /// One approved synthetic CSS click in the existing same-workflow document.
    pub(crate) fn custody_click(&mut self,url:&str,origin:&str,selector:&str,guard:crate::vault_proxy_pump::WriteGuard)->Result<Option<String>> {
        self.cdp_stage=FailureStage::CdpFrameTree;
        if crate::vault::login_origin(url).ok().as_deref()!=Some(origin) || self.session.is_empty() || self.frame.is_empty() || selector.is_empty() || selector.len()>4096 || selector.chars().any(char::is_control) {return Ok(None);}
        let normalized=url::Url::parse(url).map_err(|_|"vault_unavailable")?.to_string();
        let end=Instant::now()+Duration::from_secs(10);let session=self.session.clone();
        let tree=self.command("Page.getFrameTree",Empty{},Some(&session),end)?;
        let frame=tree.pointer("/result/frameTree/frame").ok_or("vault_unavailable")?;
        let matches=frame.get("id").and_then(Value::as_str)==Some(self.frame.as_str()) && frame.get("url").and_then(Value::as_str).and_then(|u|url::Url::parse(u).ok()).is_some_and(|u|u.as_str()==normalized);
        let original_loader=frame.get("loaderId").and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;wipe(tree);
        if !matches {return Ok(None);}
        self.origin=origin.to_owned();self.context.clear();self.contexts.clear();self.bind_world(end)?;
        self.documents.clear();self.loaded.clear();self.started.clear();
        let context=self.context.clone();let params=CustodyClick{function:CUSTODY_CLICK,context:&context,arguments:[Arg{value:origin},Arg{value:&normalized},Arg{value:selector}],return_by_value:true,silent:true};
        self.cdp_stage=FailureStage::CdpRead;
        let response=self.custody_guarded("Runtime.callFunctionOn",params,&session,end,guard)?;
        let clicked=if response.pointer("/result/exceptionDetails").is_none() {response.pointer("/result/result/value").and_then(Value::as_bool)} else {None};wipe(response);
        match clicked {Some(false)=>return Ok(None),Some(true)=>{},None=>return Err("vault_unavailable")}
        // Do not replay the click. Observe the same main frame for a bounded
        // interval; an observed navigation must finish before authority ends.
        let observe_until=(Instant::now()+Duration::from_secs(1)).min(end);
        let mut navigating=false;
        loop {
            self.cdp_stage=FailureStage::CdpFrameTree;
            if Instant::now()>=end {return Err("vault_unavailable");}
            let tree=self.command("Page.getFrameTree",Empty{},Some(&session),end)?;
            let frame=tree.pointer("/result/frameTree/frame").ok_or("vault_unavailable")?;
            let frame_id=frame.get("id").and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;
            let loader=frame.get("loaderId").and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;
            let current_origin=frame.get("url").and_then(Value::as_str).and_then(|u|crate::vault::login_origin(u).ok());wipe(tree);
            if current_origin.as_deref()!=Some(origin) {return Err("vault_unavailable");}
            navigating|=frame_id!=self.frame || loader!=original_loader || self.started.iter().any(|v|v.session==session && (v.frame==frame_id || v.frame==self.frame));
            if navigating && (frame_id!=self.frame || loader!=original_loader) {
                if let Some(doc)=self.documents.iter().find(|v|v.session==session && v.frame==frame_id && v.loader==loader) {
                    if doc.origin!=origin || !(200..300).contains(&doc.status) {return Err("vault_unavailable");}
                    if self.loaded.iter().any(|v|v.session==session && v.frame==frame_id && v.loader==loader) {
                        self.frame=frame_id;self.context.clear();self.contexts.clear();self.bind_world(end)?;
                        return Ok(Some("Browser synthetic click dispatched; same-origin navigation completed.".to_owned()));
                    }
                }
            }
            if !navigating && Instant::now()>=observe_until {return Ok(Some("Browser synthetic click dispatched; no navigation observed within one second.".to_owned()));}
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    /// Caller has already sealed and drained the authenticated Engine proxy.
    /// No target data leaves Core. An unfamiliar non-page target is an error,
    /// not proof that its worker stopped.
    pub(crate) fn record_park_guard_failure(&self) {record_failure(&self.diagnostic_dir,FailureStage::ParkGuard);}
    pub(crate) fn park_targets(&mut self)->Result<()> {
        let result=self.park_targets_inner();
        if result.is_err(){self.record_cdp_failure();}result
    }
    fn park_targets_inner(&mut self)->Result<()> {
        self.cdp_stage=FailureStage::ParkDetach;
        if self.proxy_pump.is_none() || self.session.is_empty() {return Err("vault_unavailable");}
        let end=Instant::now()+Duration::from_secs(3);
        let session=self.session.clone();
        self.command("Fetch.disable",Empty{},Some(&session),end)?;
        self.command("Target.detachFromTarget",DetachSession{session:&session},None,end)?;
        self.proxy_pump.as_ref().ok_or("vault_unavailable")?.unbind_session(session,end)?;
        self.session.clear();self.context.clear();self.contexts.clear();self.documents.clear();self.loaded.clear();self.started.clear();self.frame.clear();self.world.clear();
        self.cdp_stage=FailureStage::ParkSnapshot;
        let targets=self.command("Target.getTargets",Empty{},None,end)?;
        let items=targets.pointer("/result/targetInfos").and_then(Value::as_array).ok_or("vault_unavailable")?;
        if items.len()>32 {wipe(targets);return Err("vault_unavailable");}
        if items.iter().any(|v|v.get("type").and_then(Value::as_str)!=Some("page")) {self.cdp_stage=FailureStage::ParkNonPage;wipe(targets);return Err("vault_unavailable");}
        let ids=items.iter().map(|v|v.get("targetId").and_then(Value::as_str).filter(|id|!id.is_empty() && id.len()<=256).map(str::to_owned).ok_or("vault_unavailable")).collect::<Result<Vec<_>>>()?;
        wipe(targets);
        let allowed=ids.iter().cloned().collect::<std::collections::HashSet<_>>();
        if allowed.len()!=ids.len() {return Err("vault_unavailable");}
        self.cdp_stage=FailureStage::ParkClose;
        for id in &ids {
            let result=self.command("Target.closeTarget",CloseTarget{target:id},None,end)?;
            let accepted=result.pointer("/result/success").and_then(Value::as_bool)==Some(true);wipe(result);
            if !accepted{return Err("vault_unavailable");}
        }
        // An accepted close does not prove destruction. Never resend it.
        loop {
            self.cdp_stage=FailureStage::ParkSettle;
            if Instant::now()>=end{return Err("vault_unavailable");}
            let result=self.command("Target.getTargets",Empty{},None,end)?;
            self.cdp_stage=FailureStage::ParkSnapshot;
            let items=result.pointer("/result/targetInfos").and_then(Value::as_array).ok_or("vault_unavailable")?;
            if items.len()>32 {wipe(result);return Err("vault_unavailable");}
            if items.iter().any(|v|v.get("type").and_then(Value::as_str)!=Some("page")) {self.cdp_stage=FailureStage::ParkNonPage;wipe(result);return Err("vault_unavailable");}
            let remaining=items.iter().map(|v|v.get("targetId").and_then(Value::as_str).filter(|id|!id.is_empty() && id.len()<=256).map(str::to_owned).ok_or("vault_unavailable")).collect::<Result<Vec<_>>>()?;
            wipe(result);
            let mut seen=std::collections::HashSet::new();
            if remaining.iter().any(|id|!allowed.contains(id) || !seen.insert(id)) {return Err("vault_unavailable");}
            if remaining.is_empty(){return Ok(());}
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    /// Rebind the same sole pump only after the parked target set was empty.
    /// Network remains sealed by Engine until the fresh grant is revalidated.
    pub(crate) fn borrow_blank(&mut self)->Result<()> {
        if self.proxy_pump.is_none() || !self.session.is_empty() {return Err("vault_unavailable");}
        let end=Instant::now()+Duration::from_secs(3);
        let before=self.command("Target.getTargets",Empty{},None,end)?;
        let empty=before.pointer("/result/targetInfos").and_then(Value::as_array).is_some_and(|x|x.is_empty());wipe(before);
        if !empty {return Err("vault_unavailable");}
        let created=self.command("Target.createTarget",Navigate{url:"about:blank"},None,end)?;
        let target=created.pointer("/result/targetId").and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;wipe(created);
        let attached=self.command("Target.attachToTarget",TargetId{target:&target,flatten:true},None,end)?;
        let session=attached.pointer("/result/sessionId").and_then(Value::as_str).filter(|s|!s.is_empty() && s.len()<=256).map(str::to_owned).ok_or("vault_unavailable")?;wipe(attached);
        self.proxy_pump.as_ref().ok_or("vault_unavailable")?.bind_session(session.clone(),end)?;
        self.session=session.clone();
        self.command("Page.enable",Empty{},Some(&session),end)?;
        self.command("Page.setLifecycleEventsEnabled",serde_json::json!({"enabled":true}),Some(&session),end)?;
        self.command("Runtime.enable",Empty{},Some(&session),end)?;
        self.command("Fetch.enable",serde_json::json!({"handleAuthRequests":true}),Some(&session),end)?;
        self.command("Network.enable",Empty{},Some(&session),end)?;
        let after=self.command("Target.getTargets",Empty{},None,end)?;
        let only=after.pointer("/result/targetInfos").and_then(Value::as_array).is_some_and(|x|x.len()==1 && x[0].get("targetId").and_then(Value::as_str)==Some(target.as_str()) && x[0].get("type").and_then(Value::as_str)==Some("page"));wipe(after);
        if !only {return Err("vault_unavailable");}Ok(())
    }
    pub(crate) fn shutdown_confirmed(&mut self)->Result<()> {
        if self.closed {return Ok(());}
        // A best-effort graceful request lets Chromium persist its clean-exit
        // marker. It is never evidence of shutdown or a replacement for the
        // existing bounded owned-group cleanup below.
        let grace=Instant::now()+Duration::from_secs(1);
        if self.command("Browser.close",Empty{},None,grace).is_ok() {
            while Instant::now()<grace && !self.stop.load(Ordering::SeqCst)
                && !self.cancellation.as_ref().is_some_and(|s|!crate::vault_control::active(s)) {
                match self.child.try_wait() {Ok(None)=>std::thread::sleep(Duration::from_millis(20)),_=>break}
            }
        }
        self.child.shutdown(Duration::from_secs(2)).map_err(|_| "vault_unavailable")?;
        self.proxy_pump.take();
        if let Some(lease)=self.profile_lease.take(){
            let lease=Arc::try_unwrap(lease).map_err(|_|"vault_unavailable")?.into_inner().map_err(|_|"vault_unavailable")?;
            lease.release_after_confirmed_exit();
        }
        self.closed=true;Ok(())
    }
    pub(crate) fn custody_live(&mut self)->bool {
        if self.closed || self.proxy_pump.is_none() || self.checked_manifest().is_err() {return false;}
        if !matches!(self.child.try_wait(),Ok(None)) {return false;}
        self.profile_lease.as_ref().is_some_and(|lease|lease.lock().is_ok_and(|v|v.check().is_ok()))
    }
    pub(crate) fn profile_lease(&self)->Option<Arc<std::sync::Mutex<crate::vault_profile::ProfileLease>>> {self.profile_lease.clone()}
    /// Queue the secret-bearing effect on the sole pump. The caller-provided
    /// guard revalidates Owner/peer/lease immediately around the actual write.
    pub(crate) fn fill_profile(&mut self,username:&str,password:&Zeroizing<String>,guard:crate::vault_proxy_pump::WriteGuard)->&'static str {
        let mut receipt=match OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(self.receipts.join(&self.command)) {Ok(f)=>f,Err(e) if e.kind()==std::io::ErrorKind::AlreadyExists=>return "unknown",Err(_)=>return "not_filled"};
        if receipt.write_all(b"attempted\n").and_then(|_|receipt.sync_all()).is_err() || File::open(&self.receipts).and_then(|d|d.sync_all()).is_err() {return "not_filled";}
        if self.stop.load(Ordering::SeqCst) || self.cancellation.as_ref().is_some_and(|s|!crate::vault_control::active(s)) {return "unknown";}
        let id=self.next;self.next=match self.next.checked_add(1){Some(v)=>v,None=>return "unknown"};
        let session=self.session.clone();let context=self.context.clone();let origin=self.origin.clone();
        let args=AtomicFill{function:FUNCTION,context:&context,arguments:[Arg{value:&origin},Arg{value:username},Arg{value:password.as_str()}],return_by_value:true,silent:true};
        let frame=match serde_json::to_vec(&Command{id,method:"Runtime.callFunctionOn",params:args,session:Some(&session)}){Ok(v)=>Zeroizing::new(v),Err(_)=>return "unknown"};
        let Some(pump)=self.proxy_pump.as_ref() else{return "unknown";};
        let state=match pump.command_guarded(id,frame,Instant::now()+Duration::from_secs(5),Some(guard)) {
            Ok(reply)=>{let value=reply.response.into_value();let result=if value.pointer("/result/exceptionDetails").is_none(){match value.pointer("/result/result/value").and_then(Value::as_str){Some("filled")=>"filled",Some("filled_password_only")=>"filled_password_only",Some("not_filled")=>"not_filled",_=>"unknown"}}else{"unknown"};wipe(value);result},
            Err(_)=>"unknown",
        };
        if matches!(state,"filled"|"filled_password_only"|"not_filled") {if self.quiet().is_err(){return "unknown";}self.cancellation.take();}state
    }
    pub(crate) fn fill_card(&mut self,card:&crate::vault_card::CardRead)->&'static str {
        let mut receipt=match OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(self.receipts.join(&self.command)) {Ok(f)=>f,Err(e) if e.kind()==std::io::ErrorKind::AlreadyExists=>return "unknown",Err(_)=>return "not_filled"};
        if receipt.write_all(b"attempted\n").and_then(|_|receipt.sync_all()).is_err() || File::open(&self.receipts).and_then(|d|d.sync_all()).is_err(){return "not_filled";}
        let session=self.session.clone();let context=self.context.clone();let origin=self.origin.clone();let month=format!("{:02}",card.metadata.expiry_month);let year=card.metadata.expiry_year.to_string();
        let args=CardFill{function:CARD_FUNCTION,context:&context,arguments:[Arg{value:&origin},Arg{value:card.pan.as_str()},Arg{value:card.cvv.as_str()},Arg{value:&month},Arg{value:&year},Arg{value:&card.metadata.cardholder}],return_by_value:true,silent:true};
        let state=match self.command("Runtime.callFunctionOn",args,Some(&session),Instant::now()+Duration::from_secs(5)) {Ok(value)=>{let result=if value.pointer("/result/exceptionDetails").is_none(){match value.pointer("/result/result/value").and_then(Value::as_str){Some("filled")=>"filled",Some("not_filled")=>"not_filled",_=>"unknown"}}else{"unknown"};wipe(value);result},Err(_)=>"unknown"};
        if state=="unknown" {record_failure(&self.diagnostic_dir,FailureStage::Dispatch);}
        if matches!(state,"filled"|"not_filled") {if self.quiet().is_err(){record_failure(&self.diagnostic_dir,FailureStage::Dispatch);return "unknown";}self.cancellation.take();}state
    }
    pub(crate) fn fill(&mut self,username:&str,password:&Zeroizing<String>)->&'static str {
        // Create the exact command receipt before dispatch; never replay an
        // existing attempt even after Core restarts.
        let mut receipt=match OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(self.receipts.join(&self.command)) {
            Ok(f)=>f,Err(e) if e.kind()==std::io::ErrorKind::AlreadyExists=>return "unknown",Err(_)=>return "not_filled"
        };
        if receipt.write_all(b"attempted\n").and_then(|_|receipt.sync_all()).is_err() {return "not_filled";}
        if File::open(&self.receipts).and_then(|dir|dir.sync_all()).is_err() {return "not_filled";}
        let session=self.session.clone();let context=self.context.clone();let origin=self.origin.clone();
        let args=AtomicFill{function:FUNCTION,context:&context,arguments:[Arg{value:&origin},Arg{value:username},Arg{value:password.as_str()}],return_by_value:true,silent:true};
        // Once protocol write starts, any transport/parse failure is unknown.
        let result=self.command("Runtime.callFunctionOn",args,Some(&session),Instant::now()+Duration::from_secs(5));
        let state=match result {
            Ok(value)=>{
                let result=if value.pointer("/result/exceptionDetails").is_none() {match value.pointer("/result/result/value").and_then(Value::as_str) {
                    Some("filled")=>"filled",Some("filled_password_only")=>"filled_password_only",Some("not_filled")=>"not_filled",_=>"unknown"
                }} else {"unknown"};
                wipe(value);result
            },
            _=>"unknown",
        };
        if state=="filled" || state=="filled_password_only" || state=="not_filled" {
            if self.quiet().is_err() {return "unknown";}
            self.cancellation.take();
        }
        state
    }
}
impl Drop for Browser {fn drop(&mut self){if !self.closed && self.child.shutdown(Duration::from_secs(2)).is_err(){if let Some(lock)=self.profile_lock.take(){std::mem::forget(lock);}}}}
