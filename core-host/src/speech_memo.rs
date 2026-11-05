//! Speech-only Core authority. Engine supplies the once-approved review; Core
//! verifies its exact contents and owns the nonce, cancellation and scratch.
use crate::mcp_transport::{ExecutionBinding, ScopeEpoch};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::{HashMap, HashSet}, fs::{self, File, OpenOptions}, io::{Read, Seek, SeekFrom}, os::unix::fs::{DirBuilderExt, FileExt, OpenOptionsExt}, sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}, time::{Duration, Instant}};

const MAX_BYTES: u64 = 10 * 1024 * 1024;
#[derive(Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Binding {
    pub owner_id:i64, pub owner_epoch:String, pub core_epoch:String, pub room_id:i64, pub agent_id:i64,
    pub root_event_id:String, pub generation:u64, pub action_id:String, pub review_id:String, pub grant_id:i64,
    pub payload_digest:String, pub review_sha256:String, pub text_sha256:String, pub text_bytes:usize,
    pub voice_id:String, pub locale:String, pub rate_milli:u32,
    pub lease_expires_at:String,
    #[serde(default)] pub execution_binding:Option<ExecutionBinding>,
}
#[derive(Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    pub binding:Binding, pub arguments:Value, pub text:String, pub review:Value,
    pub review_canonical:String, pub action_payload_canonical:String, #[serde(default)] pub nonce:Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Cancel { pub owner_id:i64, pub owner_epoch:String, pub core_epoch:String, pub room_id:i64, pub agent_id:i64, pub root_event_id:String, pub generation:u64, pub action_id:String }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ReadRequest { pub owner_id:i64, pub owner_epoch:String, pub core_epoch:String, pub room_id:i64, pub agent_id:i64, pub root_event_id:String, pub action_id:String, pub artifact_id:String, pub offset:u64 }
fn digest(b:&[u8])->String { format!("{:x}",Sha256::digest(b)) }
fn hex(s:&str,n:usize)->bool { s.len()==n && s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) }
fn lease_valid(b:&Binding)->bool {
    chrono::DateTime::parse_from_rfc3339(&b.lease_expires_at)
        .is_ok_and(|expiry| {
            let now=chrono::Utc::now();
            expiry.offset().local_minus_utc()==0 && expiry>now && expiry<=now+chrono::Duration::minutes(5)
        })
}
fn wav_header_valid(h:&[u8;44], size:u64)->bool {
    let u16at=|i|u16::from_le_bytes([h[i],h[i+1]]);
    let u32at=|i|u32::from_le_bytes([h[i],h[i+1],h[i+2],h[i+3]]);
    &h[0..4]==b"RIFF" && u32at(4) as u64+8==size && &h[8..16]==b"WAVEfmt " &&
    u32at(16)==16 && u16at(20)==1 && u16at(22)==1 && u32at(24)==22050 &&
    u32at(28)==44100 && u16at(32)==2 && u16at(34)==16 && &h[36..40]==b"data" &&
    u32at(40) as u64+44==size && (size-44)%2==0
}
fn random(n:usize)->Result<String,&'static str> { let mut b=vec![0;n]; getrandom::fill(&mut b).map_err(|_|"speech_unavailable")?; Ok(b.iter().map(|v|format!("{v:02x}")).collect()) }
fn canonical(raw:&str, value:&Value, max:usize)->bool { raw.len()<=max && serde_json::from_str::<Value>(raw).ok().as_ref()==Some(value) }
impl Request {
    fn validate(&self)->Result<(),&'static str> {
        let b=&self.binding; let args=&self.arguments;
        if b.owner_id<=0 || b.room_id<=0 || b.agent_id<=0 || b.generation==0 || b.grant_id<=0 ||
           !hex(&b.owner_epoch,64)||!hex(&b.core_epoch,64)||!hex(&b.payload_digest,64)||!hex(&b.review_sha256,64)||!hex(&b.text_sha256,64)||
           [b.root_event_id.as_str(),b.action_id.as_str(),b.review_id.as_str()].iter().any(|s|s.is_empty()||s.len()>128) ||
           self.text.is_empty() || self.text.len()>4000 || self.text.contains('\0') || b.text_bytes!=self.text.len() || digest(self.text.as_bytes())!=b.text_sha256 ||
           b.voice_id.is_empty() || b.voice_id.len()>200 || b.voice_id.contains('\0') || !b.voice_id.starts_with("com.apple.") ||
           b.locale.is_empty() || b.locale.len()>64 || b.locale.contains('\0') || !(1..=1000).contains(&b.rate_milli) || !lease_valid(b) ||
           args.as_object().is_none_or(|o|o.len()!=4) || args.get("text")!=Some(&json!(self.text)) || args.get("voice_id")!=Some(&json!(b.voice_id)) ||
           args.get("locale")!=Some(&json!(b.locale)) || args.get("rate_milli")!=Some(&json!(b.rate_milli)) ||
           !canonical(&self.review_canonical,&self.review,65536) || digest(self.review_canonical.as_bytes())!=b.review_sha256 {
            return Err("speech_review_changed");
        }
        let action=self.review.get("action").ok_or("speech_review_changed")?;
        let authority=self.review.get("authority").ok_or("speech_review_changed")?;
        let proposer=authority.get("proposer").ok_or("speech_review_changed")?;
        let targets=self.review.get("targets").and_then(Value::as_array).ok_or("speech_review_changed")?;
        if targets.len()!=1 || targets[0].get("role")!=Some(&json!("voice_memo")) ||
           targets[0].get("status")!=Some(&json!("configured")) || targets[0].get("core_epoch")!=Some(&json!(b.core_epoch)) ||
           targets[0].get("room_id")!=Some(&json!(b.room_id)) || targets[0].get("agent_id")!=Some(&json!(b.agent_id)) ||
           targets[0].get("session_id")!=action.get("session_id") || targets[0].get("workflow_id")!=action.get("workflow_id") {
            return Err("speech_review_changed");
        }
        let scope=serde_json::to_value(&b.execution_binding).map_err(|_|"speech_review_changed")?;
        let payload=json!({"family":"synthesize_voice_memo","workflow_id":action.get("workflow_id").cloned().unwrap_or(Value::Null),"arguments":args});
        if !canonical(&self.action_payload_canonical,&payload,65536) || digest(self.action_payload_canonical.as_bytes())!=b.payload_digest ||
           action.get("payload_digest")!=Some(&json!(b.payload_digest)) || action.get("family")!=Some(&json!("synthesize_voice_memo")) ||
           action.get("target")!=Some(&json!("local:voice-memo")) || action.get("scope_target")!=Some(&json!("local:voice-memo")) ||
           action.get("action_id")!=Some(&json!(b.action_id)) || action.get("room_id")!=Some(&json!(b.room_id)) || action.get("agent_id")!=Some(&json!(b.agent_id)) ||
           action.get("trigger_event_id")!=Some(&json!(b.root_event_id)) || action.get("execution_binding").unwrap_or(&Value::Null)!=&scope ||
           authority.get("owner_id")!=Some(&json!(b.owner_id)) || authority.get("room_id")!=Some(&json!(b.room_id)) || authority.get("agent_id")!=Some(&json!(b.agent_id)) ||
           authority.get("os_user_status")!=Some(&json!("verified")) || authority.get("euid")!=Some(&json!(unsafe{libc::geteuid()})) ||
           authority.get("session_id")!=action.get("session_id") || proposer.get("room_id")!=Some(&json!(b.room_id)) || proposer.get("agent_id")!=Some(&json!(b.agent_id)) ||
           proposer.get("owner_id")!=Some(&json!(b.owner_id)) || proposer.get("owner_epoch")!=Some(&json!(b.owner_epoch)) {
            return Err("speech_review_changed");
        }
        if let Some(s)=&b.execution_binding { if !s.valid(b.owner_id,b.agent_id,&b.root_event_id,b.generation) || s.execution_room_id!=b.room_id { return Err("speech_scope_changed"); } }
        Ok(())
    }
}
struct Scratch { file:File }
impl Scratch {
    fn new()->Result<Self,&'static str> {
        let dir=std::env::temp_dir().join(format!("gamja-speech-{}",random(16)?));
        let mut d=fs::DirBuilder::new(); d.mode(0o700); d.create(&dir).map_err(|_|"speech_storage_unavailable")?;
        let path=dir.join("memo.wav");
        let file=match OpenOptions::new().read(true).write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&path) {
            Ok(file)=>file, Err(_)=>{let _=fs::remove_dir(&dir); return Err("speech_storage_unavailable")}
        };
        // Anonymous owned scratch cannot survive a Core crash. The open FD is
        // the only handle used for rendering, hashing and bounded reads.
        if fs::remove_file(&path).is_err() {let _=fs::remove_dir(&dir); return Err("speech_storage_unavailable")}
        if fs::remove_dir(&dir).is_err() {return Err("speech_storage_unavailable")}
        Ok(Self{file})
    }
}
struct Pending { request:Request, expires:Instant }
struct Running { binding:Binding, cancel:Arc<AtomicBool> }
struct Artifact { binding:Binding, scratch:Scratch, size:u64, offset:u64, expires:Instant }
#[derive(Default)]
struct Inner { pending:HashMap<String,Pending>, active:HashMap<String,Running>, artifacts:HashMap<String,Artifact>, used:HashSet<(i64,String)>, cancelled:HashSet<(i64,String)>, revoked:HashMap<(i64,String),u64>, closed:bool }
pub(crate) struct Registry(Mutex<Inner>);

#[cfg(test)]
mod revocation_capacity_tests {
    use super::*;
    #[test]
    fn repeated_scope_revocation_at_capacity_does_not_close_registry() {
        let registry=Registry::new();
        {let mut state=registry.0.lock().unwrap();
            for n in 0..16384 {state.revoked.insert((1,format!("{n:08x}-0000-4000-8000-000000000000")),1);}}
        let old=ScopeEpoch{scope_id:"00000000-0000-4000-8000-000000000000".into(),epoch:2};
        assert!(registry.revoke_scopes(1,&[old.clone(),old]).is_ok());
        let new=ScopeEpoch{scope_id:"ffffffff-0000-4000-8000-000000000000".into(),epoch:1};
        assert!(registry.revoke_scopes(1,&[new]).is_err());
    }
}
impl Registry {
    pub fn new()->Arc<Self> {
        let registry=Arc::new(Self(Mutex::new(Inner::default()))); let weak=Arc::downgrade(&registry);
        std::thread::spawn(move || loop { std::thread::sleep(Duration::from_secs(5)); let Some(registry)=weak.upgrade() else {break}; if let Ok(mut s)=registry.0.lock() { Self::prune(&mut s); }; }); registry
    }
    fn prune(s:&mut Inner) { let now=Instant::now(); let revoked=s.revoked.clone(); s.pending.retain(|_,p|p.expires>now); s.artifacts.retain(|_,a|a.expires>now && Self::scope_allowed(&revoked,&a.binding)); }
    fn scope_allowed(revoked:&HashMap<(i64,String),u64>,b:&Binding)->bool { lease_valid(b) && b.execution_binding.as_ref().is_none_or(|p|p.valid(b.owner_id,b.agent_id,&b.root_event_id,b.generation) && p.lineage.iter().all(|x|revoked.get(&(b.owner_id,x.scope_id.clone())).is_none_or(|e|x.epoch>*e))) }
    pub fn issue(&self, request:Request)->Result<Value,&'static str> {
        request.validate()?; if request.nonce.is_some() {return Err("speech_grant_changed")};
        let nonce=random(32)?; let mut s=self.0.lock().map_err(|_|"speech_unavailable")?; Self::prune(&mut s);
        let b=&request.binding; let key=(b.owner_id,b.action_id.clone());
        if s.closed || s.pending.len()>=32 || s.active.len()>=4 || s.artifacts.len()>=4 || s.used.len()>=16384 || s.used.contains(&key) || s.cancelled.contains(&key) || s.pending.contains_key(&nonce) || s.active.contains_key(&nonce) || !Self::scope_allowed(&s.revoked,b) { return Err("speech_grant_unavailable"); }
        s.used.insert(key); s.pending.insert(nonce.clone(),Pending{request,expires:Instant::now()+Duration::from_secs(60)}); Ok(json!({"state":"issued","nonce":nonce}))
    }
    pub fn start<F:Fn()->bool>(&self, mut request:Request, owner_valid:F)->Result<Value,&'static str> {
        request.validate()?; let nonce=request.nonce.take().ok_or("speech_grant_unavailable")?;
        let cancel=Arc::new(AtomicBool::new(false));
        { let mut s=self.0.lock().map_err(|_|"speech_unavailable")?; Self::prune(&mut s);
          let pending=s.pending.remove(&nonce).ok_or("speech_grant_unavailable")?;
          if pending.request!=request || !Self::scope_allowed(&s.revoked,&request.binding) || !owner_valid() {return Err("speech_grant_changed")}
          s.active.insert(nonce.clone(),Running{binding:request.binding.clone(),cancel:cancel.clone()}); }
        let result=(|| {
            let mut scratch=Scratch::new()?;
            let frames=render(&mut scratch.file,&request,&cancel,&owner_valid)?;
            if cancel.load(Ordering::Acquire)||!owner_valid() {return Err("speech_operation_uncertain")}
            let size=scratch.file.metadata().map_err(|_|"speech_storage_unavailable")?.len();
            if size!=44+frames*2 || size>MAX_BYTES || frames==0 || frames>22050*300 {return Err("speech_invalid_output")}
            scratch.file.seek(SeekFrom::Start(0)).map_err(|_|"speech_storage_unavailable")?;
            let mut header=[0u8;44]; scratch.file.read_exact(&mut header).map_err(|_|"speech_storage_unavailable")?;
            if !wav_header_valid(&header,size) {return Err("speech_invalid_output")}
            scratch.file.seek(SeekFrom::Start(0)).map_err(|_|"speech_storage_unavailable")?;
            let mut hasher=Sha256::new(); let mut buf=[0u8;32768]; loop {let n=scratch.file.read(&mut buf).map_err(|_|"speech_storage_unavailable")?; if n==0 {break} hasher.update(&buf[..n]);}
            let sha=format!("{:x}",hasher.finalize()); let artifact=random(16)?;
            let mut s=self.0.lock().map_err(|_|"speech_unavailable")?; Self::prune(&mut s);
            if s.closed || s.artifacts.len()>=4 || s.artifacts.contains_key(&artifact) || cancel.load(Ordering::Acquire) || !Self::scope_allowed(&s.revoked,&request.binding) || !owner_valid() {return Err("speech_operation_uncertain")}
            let b=&request.binding; let response=json!({"artifact_id":artifact,"size_bytes":size,"sha256":sha,"duration_ms":frames*1000/22050,"voice_id":b.voice_id,"locale":b.locale,"rate_milli":b.rate_milli,"text_sha256":b.text_sha256,"media_type":"audio/wav"});
            s.artifacts.insert(artifact,Artifact{binding:b.clone(),scratch,size,offset:0,expires:Instant::now()+Duration::from_secs(60)}); Ok(response)
        })();
        if let Ok(mut s)=self.0.lock() {s.active.remove(&nonce);} result
    }
    pub fn read<F:Fn()->bool>(&self,r:ReadRequest,owner_valid:F)->Result<Value,&'static str> {
        let (file,size)={
            let mut s=self.0.lock().map_err(|_|"speech_unavailable")?; Self::prune(&mut s);
            let a=s.artifacts.get(&r.artifact_id).ok_or("speech_artifact_unavailable")?; let b=&a.binding;
            if b.owner_id!=r.owner_id||b.owner_epoch!=r.owner_epoch||b.core_epoch!=r.core_epoch||b.room_id!=r.room_id||b.agent_id!=r.agent_id||b.root_event_id!=r.root_event_id||b.action_id!=r.action_id||r.offset!=a.offset {return Err("speech_artifact_changed")}
            (a.scratch.file.try_clone().map_err(|_|"speech_storage_unavailable")?,a.size)
        };
        // Never hold the authority lock across disk I/O: Stop can revoke while
        // a read is blocked. Recheck the artifact and lease before releasing data.
        let mut data=vec![0;(size-r.offset).min(32768) as usize];
        file.read_exact_at(&mut data,r.offset).map_err(|_|"speech_storage_unavailable")?;
        let mut s=self.0.lock().map_err(|_|"speech_unavailable")?; Self::prune(&mut s);
        if !owner_valid() {return Err("speech_owner_changed")}
        let a=s.artifacts.get_mut(&r.artifact_id).ok_or("speech_artifact_unavailable")?;
        if a.offset!=r.offset || a.size!=size {return Err("speech_artifact_changed")}
        a.offset+=data.len() as u64; let eof=a.offset==a.size; let result=json!({"offset":r.offset,"data_base64":STANDARD.encode(data),"eof":eof});
        if eof {s.artifacts.remove(&r.artifact_id);} Ok(result)
    }
    pub fn cancel(&self,r:Cancel)->Result<(),&'static str> {
        let mut s=self.0.lock().map_err(|_|"speech_unavailable")?; let key=(r.owner_id,r.action_id.clone());
        if s.cancelled.len()>=16384 {s.closed=true; return Err("speech_unavailable")}
        let matches=|b:&Binding| b.owner_id==r.owner_id&&b.owner_epoch==r.owner_epoch&&b.core_epoch==r.core_epoch&&b.room_id==r.room_id&&b.agent_id==r.agent_id&&b.root_event_id==r.root_event_id&&b.generation==r.generation&&b.action_id==r.action_id;
        s.cancelled.insert(key); s.pending.retain(|_,p|!matches(&p.request.binding)); s.artifacts.retain(|_,a|!matches(&a.binding)); for a in s.active.values(){if matches(&a.binding){a.cancel.store(true,Ordering::Release);}} Ok(())
    }
    pub fn revoke_scopes(&self,owner:i64,scopes:&[ScopeEpoch])->Result<(),&'static str> {
        let mut s=self.0.lock().map_err(|_|"speech_unavailable")?;
        let new_keys=scopes.iter().map(|scope|scope.scope_id.as_str())
            .filter(|id|!s.revoked.contains_key(&(owner,(*id).to_owned())))
            .collect::<std::collections::HashSet<_>>().len();
        if s.revoked.len().saturating_add(new_keys)>16384 {s.closed=true; return Err("speech_unavailable")}
        for x in scopes {s.revoked.entry((owner,x.scope_id.clone())).and_modify(|v|*v=(*v).max(x.epoch)).or_insert(x.epoch);}
        let revoked=s.revoked.clone(); s.pending.retain(|_,p|Self::scope_allowed(&revoked,&p.request.binding)); s.artifacts.retain(|_,a|Self::scope_allowed(&revoked,&a.binding)); for a in s.active.values(){if !Self::scope_allowed(&revoked,&a.binding){a.cancel.store(true,Ordering::Release);}} Ok(())
    }
    pub fn shutdown(&self) { if let Ok(mut s)=self.0.lock(){s.closed=true; s.pending.clear(); s.artifacts.clear(); for a in s.active.values(){a.cancel.store(true,Ordering::Release);}} }
}
#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn render<F:Fn()->bool>(file:&mut File,r:&Request,cancel:&AtomicBool,owner_valid:&F)->Result<u64,&'static str> {
    use std::{ffi::{CString,c_void},os::fd::AsRawFd};
    struct Check<'a,F>{cancel:&'a AtomicBool,owner:&'a F,binding:&'a Binding}
    unsafe extern "C" fn check<F:Fn()->bool>(ctx:*mut c_void)->i32 {let c=&*(ctx as *const Check<'_,F>); (c.cancel.load(Ordering::Acquire)||!(c.owner)()||!lease_valid(c.binding)||c.binding.execution_binding.as_ref().is_some_and(|s|!s.valid(c.binding.owner_id,c.binding.agent_id,&c.binding.root_event_id,c.binding.generation))) as i32}
    unsafe extern "C" {fn gamja_core_speech_memo(fd:i32,text:*const i8,voice:*const i8,locale:*const i8,rate:i32,cancel:unsafe extern "C" fn(*mut c_void)->i32,ctx:*mut c_void,frames:*mut u64)->i32;}
    let text=CString::new(r.text.as_str()).map_err(|_|"speech_invalid_input")?; let voice=CString::new(r.binding.voice_id.as_str()).map_err(|_|"speech_invalid_input")?; let locale=CString::new(r.binding.locale.as_str()).map_err(|_|"speech_invalid_input")?;
    let mut ctx=Check{cancel,owner:owner_valid,binding:&r.binding}; let mut frames=0;
    let status=unsafe{gamja_core_speech_memo(file.as_raw_fd(),text.as_ptr(),voice.as_ptr(),locale.as_ptr(),r.binding.rate_milli as i32,check::<F>,(&mut ctx as *mut Check<'_,F>).cast(),&mut frames)};
    match status {
        0 => Ok(frames),
        1 => Err("speech_voice_unavailable"),
        2 => Err("speech_format_unsupported"),
        3 => Err("speech_limit_exceeded"),
        4 => Err("speech_storage_unavailable"),
        _ => Err("speech_operation_uncertain"),
    }
}
#[cfg(not(all(target_os="macos",target_arch="aarch64")))]
fn render<F:Fn()->bool>(_:&mut File,_:&Request,_:&AtomicBool,_:&F)->Result<u64,&'static str>{Err("speech_unavailable")}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
pub(crate) fn available()->bool { unsafe extern "C" {fn gamja_core_speech_available()->i32;} unsafe{gamja_core_speech_available()!=0} }
#[cfg(not(all(target_os="macos",target_arch="aarch64")))]
pub(crate) fn available()->bool {false}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
pub(crate) fn voices()->Vec<Value> {
    use std::ffi::CStr;
    unsafe extern "C" {fn gamja_core_speech_voices()->*mut libc::c_char;}
    let ptr=unsafe{gamja_core_speech_voices()}; if ptr.is_null(){return vec![]}
    let bytes=unsafe{CStr::from_ptr(ptr)}.to_bytes();
    let result=if bytes.len()<=65536 {serde_json::from_slice::<Vec<Value>>(bytes).unwrap_or_default()} else {vec![]};
    unsafe{libc::free(ptr.cast())}; result
}
#[cfg(not(all(target_os="macos",target_arch="aarch64")))]
pub(crate) fn voices()->Vec<Value> {vec![]}
