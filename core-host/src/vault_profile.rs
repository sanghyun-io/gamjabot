//! Verified same-profile handoff data; no secret or renderer-controlled path.
use serde::{Deserialize,Serialize};
use serde_json::Value;
use sha2::{Digest,Sha256};
use std::{fs::{File,OpenOptions},io::Read,os::{fd::AsRawFd,unix::fs::{MetadataExt,OpenOptionsExt}},path::{Path,PathBuf}};
use zeroize::Zeroizing;

fn hex(s:&str,n:usize)->bool {s.len()==n && s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn uuid(s:&str)->bool {s.len()==36 && s.bytes().enumerate().all(|(i,b)|if [8,13,18,23].contains(&i){b==b'-'}else{b.is_ascii_digit()||(b'a'..=b'f').contains(&b)})}
fn safe_text(s:&str,max:usize)->bool {s.len()<=max && !s.chars().any(char::is_control)}

#[derive(Clone,Deserialize,Serialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Record {
    pub version:u8,pub handoff_id:String,pub owner_id:u64,pub owner_epoch:String,
    pub profile_id:String,pub profile_revision:u64,pub room_id:u64,pub agent_id:u64,
    pub action_id:String,pub review_id:String,pub root_request_event_id:String,
    pub execution_binding_sha256:String,pub item_id:String,pub item_revision:u64,pub secret_revision:u64,
    pub origin:String,pub username:String,pub profile_label:String,pub account_label:String,
    pub created_at:String,pub expires_at:String,pub state:String,pub prior_outcome_unknown:bool,
}
impl Record {
    pub(crate) fn valid(&self)->bool {self.valid_at(true)}
    fn valid_at(&self,require_live:bool)->bool {self.valid_base(require_live,false)}
    fn valid_base(&self,require_live:bool,recovery:bool)->bool {
        use chrono::{DateTime,Utc};
        let Ok(created)=DateTime::parse_from_rfc3339(&self.created_at) else{return false;};
        let Ok(expires)=DateTime::parse_from_rfc3339(&self.expires_at) else{return false;};
        self.version==1 && hex(&self.handoff_id,32) && self.owner_id>0 && hex(&self.owner_epoch,64)
            && hex(&self.profile_id,32) && self.room_id>0 && self.agent_id>0
            && uuid(&self.action_id) && uuid(&self.review_id) && uuid(&self.root_request_event_id)
            && hex(&self.execution_binding_sha256,64) && hex(&self.item_id,32)
            && self.item_revision>0 && self.secret_revision>0
            && crate::vault::login_origin(&self.origin).ok().as_deref()==Some(self.origin.as_str())
            && crate::vault::valid_username(&self.username) && safe_text(&self.profile_label,512) && safe_text(&self.account_label,512)
            && (self.state=="prepared" || (recovery && matches!(self.state.as_str(),"returned"|"recovered"))) && (recovery || !self.prior_outcome_unknown) && (!require_live || expires>Utc::now())
            && created<=Utc::now() && expires>created && (expires-created).num_seconds()<=1800
    }
}

/// Proxy secret is ephemeral and never serialized or made available to Renderer.
pub(crate) struct Grant {pub record:Record,pub metadata_sha256:String,pub record_sha256:String,pub proxy_port:u16,pub proxy_secret:Zeroizing<String>}
impl Grant {
    pub(crate) fn validate(&self,app_data:&Path,owner:u64,epoch:&str)->Result<PathBuf,&'static str> {
        if !self.record.valid() || self.record.owner_id!=owner || self.record.owner_epoch!=epoch || !hex(&self.metadata_sha256,64) || !hex(&self.record_sha256,64)
            || self.proxy_port==0 || !(32..=128).contains(&self.proxy_secret.len()) || !self.proxy_secret.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'-' || b==b'_') {return Err("vault_unavailable");}
        self.proof().validate(app_data,owner,epoch)
    }
    pub(crate) fn validate_for_close(&self,app_data:&Path,owner:u64,epoch:&str)->Result<PathBuf,&'static str> {self.proof().validate_mode(app_data,owner,epoch,false,false,None)}
    pub(crate) fn proof(&self)->ProfileProof {ProfileProof{record:self.record.clone(),metadata_sha256:self.metadata_sha256.clone(),record_sha256:self.record_sha256.clone()}}
}
#[derive(Clone)]
pub(crate) struct ProfileProof {pub record:Record,pub metadata_sha256:String,pub record_sha256:String}
impl ProfileProof {
    pub(crate) fn validate(&self,app_data:&Path,owner:u64,epoch:&str)->Result<PathBuf,&'static str> {self.validate_mode(app_data,owner,epoch,true,false,None)}
    /// A fresh task must be separately assigned in the frozen profile metadata.
    /// The Engine remains responsible for attesting the actual once review and
    /// DB-derived execution origin; this check does not create that authority.
    pub(crate) fn validate_recipient(&self,app_data:&Path,owner:u64,epoch:&str,room:u64,agent:u64,origin:&str,context_mode:&str)->Result<PathBuf,&'static str> {
        if room==0 || agent==0 || !matches!(context_mode,"user_room"|"delegated") || crate::vault::login_origin(origin).ok().as_deref()!=Some(origin) {return Err("vault_unavailable");}
        let data=self.validate(app_data,owner,epoch)?;
        let profile=data.parent().ok_or("vault_unavailable")?;
        let raw=read_private(&profile.join("metadata.json"),32768)?;
        if digest(&raw)!=self.metadata_sha256 {return Err("vault_unavailable");}
        let value:Value=serde_json::from_slice(&raw).map_err(|_|"vault_unavailable")?;
        if !value.get("origin_urls").and_then(Value::as_array).is_some_and(|urls|urls.iter().any(|v|v.as_str()==Some(origin))) {return Err("vault_unavailable");}
        if context_mode=="delegated" && value.get("allow_delegated").and_then(Value::as_bool)!=Some(true) {return Err("vault_unavailable");}
        let room_key=room.to_string();
        let assigned=if value.get("room_id").and_then(Value::as_u64)==Some(room) {value.get("agent_ids")} else {value.get("shared_rooms").and_then(|v|v.get(room_key.as_str()))};
        if !assigned.and_then(Value::as_array).is_some_and(|ids|ids.iter().any(|v|v.as_u64()==Some(agent))) {return Err("vault_unavailable");}
        Ok(data)
    }
    fn validate_mode(&self,app_data:&Path,owner:u64,epoch:&str,require_live:bool,recovery:bool,metadata_revision:Option<u64>)->Result<PathBuf,&'static str> {
        if !self.record.valid_base(require_live,recovery) || self.record.owner_id!=owner || self.record.owner_epoch!=epoch || !hex(&self.metadata_sha256,64) || !hex(&self.record_sha256,64) {return Err("vault_unavailable");}
        let root=app_data.join("browser-profiles");let profile=root.join(&self.record.profile_id);let data=profile.join("data");
        for path in [app_data,&root,&profile,&data] {checked_directory(path)?;}
        let raw=read_private(&profile.join("handoff.json"),32768)?;
        if digest(&raw)!=self.record_sha256 {return Err("vault_unavailable");}
        let stored:Record=serde_json::from_slice(&raw).map_err(|_|"vault_unavailable")?;
        if stored!=self.record {return Err("vault_unavailable");}
        let metadata=read_private(&profile.join("metadata.json"),16384)?;
        if digest(&metadata)!=self.metadata_sha256 {return Err("vault_unavailable");}
        let value:Value=serde_json::from_slice(&metadata).map_err(|_|"vault_unavailable")?;
        if value.get("id").and_then(Value::as_str)!=Some(self.record.profile_id.as_str()) || value.get("owner_id").and_then(Value::as_u64)!=Some(owner) || value.get("revision").and_then(Value::as_u64)!=Some(metadata_revision.unwrap_or(self.record.profile_revision))
            || value.get("name").and_then(Value::as_str)!=Some(self.record.profile_label.as_str())
            || value.get("account_label").and_then(Value::as_str)!=Some(self.record.account_label.as_str())
            || !value.get("origin_urls").and_then(Value::as_array).is_some_and(|a|a.iter().any(|v|v.as_str()==Some(self.record.origin.as_str()))) {return Err("vault_unavailable");}
        let room_key=self.record.room_id.to_string();
        let assigned=if value.get("room_id").and_then(Value::as_u64)==Some(self.record.room_id) {value.get("agent_ids")}
            else {value.get("shared_rooms").and_then(|v|v.get(room_key.as_str()))};
        if !assigned.and_then(Value::as_array).is_some_and(|a|a.iter().any(|v|v.as_u64()==Some(self.record.agent_id))) {return Err("vault_unavailable");}
        Ok(data)
    }
}
fn digest(data:&[u8])->String {format!("{:x}",Sha256::digest(data))}
fn checked_directory(path:&Path)->Result<(),&'static str> {
    let m=std::fs::symlink_metadata(path).map_err(|_|"vault_unavailable")?;
    if !m.is_dir() || m.file_type().is_symlink() || m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o077!=0 || path.canonicalize().ok().as_deref()!=Some(path) {return Err("vault_unavailable");}Ok(())
}
fn read_private(path:&Path,max:usize)->Result<Vec<u8>,&'static str> {
    let mut file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(path).map_err(|_|"vault_unavailable")?;
    let a=file.metadata().map_err(|_|"vault_unavailable")?;
    if !a.is_file() || a.uid()!=unsafe{libc::geteuid()} || a.mode()&0o777!=0o600 || a.nlink()!=1 || a.len()>max as u64 {return Err("vault_unavailable");}
    let mut bytes=Vec::new();file.by_ref().take(max as u64+1).read_to_end(&mut bytes).map_err(|_|"vault_unavailable")?;
    let b=std::fs::symlink_metadata(path).map_err(|_|"vault_unavailable")?;
    if bytes.len()>max || !b.is_file() || b.dev()!=a.dev() || b.ino()!=a.ino() || b.len()!=a.len() || b.mtime()!=a.mtime() || b.mtime_nsec()!=a.mtime_nsec() {return Err("vault_unavailable");}Ok(bytes)
}
// A profile's Chromium singleton is not a proof of cleanup by itself. A
// missing entry is acceptable only with the signed Engine no-ref attestation
// and the held profile flock. A stale link is accepted only for this host and
// a PID that the kernel says does not exist; all ambiguity is fail-closed.
fn no_live_singleton(data:&Path)->Result<(),&'static str> {
    use std::os::unix::ffi::OsStrExt;
    checked_directory(data)?;let path=data.join("SingletonLock");
    let before=match std::fs::symlink_metadata(&path) {
        Err(e) if e.kind()==std::io::ErrorKind::NotFound=>return Ok(()),
        Err(_)=>return Err("vault_unavailable"),Ok(m)=>m,
    };
    if !before.file_type().is_symlink() || before.uid()!=unsafe{libc::geteuid()} || before.len()==0 || before.len()>512 {return Err("vault_unavailable");}
    let target=std::fs::read_link(&path).map_err(|_|"vault_unavailable")?;
    let raw=target.as_os_str().as_bytes();
    if raw.is_empty() || raw.len()>512 || raw.contains(&b'/') || !raw.is_ascii() {return Err("vault_unavailable");}
    let value=std::str::from_utf8(raw).map_err(|_|"vault_unavailable")?;
    let (host,pid)=value.rsplit_once('-').ok_or("vault_unavailable")?;
    if host.is_empty() || pid.is_empty() || !pid.bytes().all(|b|b.is_ascii_digit()) || pid.starts_with('0') {return Err("vault_unavailable");}
    let pid:i32=pid.parse().map_err(|_|"vault_unavailable")?;if pid<=1{return Err("vault_unavailable");}
    let mut name=[0u8;256];
    if unsafe{libc::gethostname(name.as_mut_ptr().cast(),name.len())}!=0 {return Err("vault_unavailable");}
    let end=name.iter().position(|b|*b==0).ok_or("vault_unavailable")?;
    if name.get(..end)!=Some(host.as_bytes()) {return Err("vault_unavailable");}
    let after=std::fs::symlink_metadata(&path).map_err(|_|"vault_unavailable")?;
    if !after.file_type().is_symlink() || after.uid()!=before.uid() || after.dev()!=before.dev() || after.ino()!=before.ino() || std::fs::read_link(&path).ok().as_deref()!=Some(target.as_path()) {return Err("vault_unavailable");}
    if unsafe{libc::kill(pid,0)}==0 {return Err("vault_unavailable");}
    if std::io::Error::last_os_error().raw_os_error()!=Some(libc::ESRCH) {return Err("vault_unavailable");}Ok(())
}

/// Lock the same inode as Engine; a caller must retain it until confirmed child exit.
pub(crate) struct ProfileLease {file:Option<File>,path:PathBuf,dev:u64,ino:u64}
impl ProfileLease {
    pub(crate) fn acquire(profile:&Path)->Result<Self,&'static str> {
        checked_directory(profile)?;let path=profile.join("lease.lock");
        let file=OpenOptions::new().read(true).write(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&path).map_err(|_|"vault_unavailable")?;
        let m=file.metadata().map_err(|_|"vault_unavailable")?;
        if !m.is_file() || m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o777!=0o600 || m.nlink()!=1 || unsafe{libc::flock(file.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}!=0 {return Err("vault_unavailable");}
        let mut lease=Self{file:Some(file),path,dev:m.dev(),ino:m.ino()};
        if lease.check().is_err() {lease.file.take();return Err("vault_unavailable");}Ok(lease)
    }
    pub(crate) fn check(&self)->Result<(),&'static str> {
        checked_directory(self.path.parent().ok_or("vault_unavailable")?)?;
        let a=self.file.as_ref().ok_or("vault_unavailable")?.metadata().map_err(|_|"vault_unavailable")?;
        let b=std::fs::symlink_metadata(&self.path).map_err(|_|"vault_unavailable")?;
        if !b.is_file() || a.dev()!=self.dev || b.dev()!=self.dev || a.ino()!=self.ino || b.ino()!=self.ino || b.uid()!=unsafe{libc::geteuid()} || b.mode()&0o777!=0o600 || b.nlink()!=1 {return Err("vault_unavailable");}Ok(())
    }
    pub(crate) fn release_after_confirmed_exit(mut self) {self.file.take();}
    pub(crate) fn preserve_uncertain(mut self) {if let Some(file)=self.file.take(){std::mem::forget(file);}}
}

impl Drop for ProfileLease {fn drop(&mut self){if let Some(file)=self.file.take(){std::mem::forget(file);}}}

/// Custody is separate from the immutable v1 handoff record. A borrowed task is
/// never authority for another task, and an attempted action is never replayed.
pub(crate) struct Custody {
    pub id:String,
    pub phase:&'static str,
    pub borrow:Option<CustodyBorrow>,
    pub workflow:Option<CustodyWorkflow>,
    pub context_id:Option<String>,
    pub used_actions:std::collections::HashSet<String>,
    pub used_borrows:std::collections::HashSet<String>,
}
#[derive(Clone,PartialEq,Eq)]
pub(crate) struct CustodyWorkflow {pub room_id:u64,pub agent_id:u64,pub root_request_event_id:String,pub run_generation:u64,pub workflow_id:Option<String>}
#[derive(Clone,PartialEq,Eq)]
pub(crate) struct CustodyBorrow {
    pub id:String,pub review_context_id:String,pub workflow_id:Option<String>,pub room_id:u64,pub agent_id:u64,pub action_id:String,pub review_id:String,
    pub root_request_event_id:String,pub run_generation:u64,pub execution_binding_sha256:String,
    pub execution_binding:Option<crate::mcp_transport::ExecutionBinding>,
    pub origin:String,pub scope:String,pub context_mode:String,pub operation:String,pub selector:Option<Zeroizing<String>>,
    pub url:Zeroizing<String>,pub url_sha256:String,pub payload_digest:String,pub call_id:Option<String>,pub expires_at:String,
}
impl CustodyBorrow {
    pub(crate) fn workflow(&self)->CustodyWorkflow {CustodyWorkflow{room_id:self.room_id,agent_id:self.agent_id,root_request_event_id:self.root_request_event_id.clone(),run_generation:self.run_generation,workflow_id:self.workflow_id.clone()}}
    pub(crate) fn valid(&self,record:&Record)->bool {
        use chrono::{DateTime,Utc};
        let Ok(expires)=DateTime::parse_from_rfc3339(&self.expires_at) else{return false;};
        let Ok(handoff_expires)=DateTime::parse_from_rfc3339(&record.expires_at) else{return false;};
        let now=Utc::now();
        hex(&self.id,32) && hex(&self.review_context_id,32) && self.workflow_id.as_ref().is_none_or(|v|!v.is_empty() && v.len()<=200) && self.room_id>0 && self.agent_id>0 && uuid(&self.action_id) && uuid(&self.review_id)
            && uuid(&self.root_request_event_id) && hex(&self.execution_binding_sha256,64)
            && matches!(self.context_mode.as_str(),"user_room"|"delegated") && self.scope=="once"
            && (match self.operation.as_str() {
                "navigate"|"inspect"|"screenshot" => self.selector.is_none(),
                "click" => self.selector.as_ref().is_some_and(|s|!s.is_empty() && s.len()<=4096 && !s.chars().any(char::is_control)),
                _ => false,
            }) && self.url.len()<=4096
            && crate::vault::login_origin(&self.url).ok().as_deref()==Some(self.origin.as_str())
            && crate::vault::login_origin(&self.origin).ok().as_deref()==Some(self.origin.as_str())
            && hex(&self.url_sha256,64) && digest(self.url.as_bytes())==self.url_sha256 && hex(&self.payload_digest,64)
            && self.call_id.as_ref().is_none_or(|s|!s.is_empty() && s.len()<=200)
            && expires>now && expires<=handoff_expires && (expires.with_timezone(&Utc)-now).num_seconds()<=60
    }
}

pub(crate) struct CustodyTask {
    pub handoff_id:String,pub custody_id:String,pub borrow_id:String,pub action_id:String,
    pub status:&'static str,pub text:Zeroizing<String>,pub started:std::time::Instant,
    pub capture_id:Option<String>,pub capture:Option<Zeroizing<Vec<u8>>>,
}

/// Publish only a zero-target proof; the file alone never proves live custody.
/// A hard link gives no-replace publication; a crash before unlinking the
/// temporary name leaves nlink>1 and is rejected by every consumer.
pub(crate) fn persist_parked(proof:&ProfileProof,app_data:&Path,owner:u64,epoch:&str,custody_id:&str,lease:&ProfileLease)->Result<(),&'static str> {
    use std::io::Write;
    if !hex(custody_id,32) {return Err("vault_unavailable");}
    let data=proof.validate(app_data,owner,epoch)?;lease.check()?;
    let dir=data.parent().ok_or("vault_unavailable")?;checked_directory(dir)?;
    let name=format!("custody-{}.json",proof.record.handoff_id);
    let path=dir.join(name);let temp=dir.join(format!(".custody-{}.tmp",custody_id));
    let raw=serde_json::to_vec(&serde_json::json!({"version":1,"status":"parked","handoff_id":proof.record.handoff_id,"profile_id":proof.record.profile_id,"profile_revision":proof.record.profile_revision,"custody_id":custody_id})).map_err(|_|"vault_unavailable")?;
    let mut file=OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&temp).map_err(|_|"vault_unavailable")?;
    let m=file.metadata().map_err(|_|"vault_unavailable")?;
    let result=(|| -> Result<(),&'static str> {
        if !m.is_file() || m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o777!=0o600 || m.nlink()!=1 {return Err("vault_unavailable");}
        file.write_all(&raw).and_then(|_|file.sync_all()).map_err(|_|"vault_unavailable")?;
        proof.validate(app_data,owner,epoch)?;lease.check()?;checked_directory(dir)?;
        let current=std::fs::symlink_metadata(&temp).map_err(|_|"vault_unavailable")?;
        if !current.is_file() || current.dev()!=m.dev() || current.ino()!=m.ino() || current.uid()!=m.uid() || current.nlink()!=1 {return Err("vault_unavailable");}
        std::fs::hard_link(&temp,&path).map_err(|_|"vault_unavailable")?;
        let linked=std::fs::symlink_metadata(&path).map_err(|_|"vault_unavailable")?;
        if !linked.is_file() || linked.dev()!=m.dev() || linked.ino()!=m.ino() || linked.uid()!=m.uid() || linked.nlink()!=2 {return Err("vault_unavailable");}
        std::fs::remove_file(&temp).map_err(|_|"vault_unavailable")?;
        File::open(dir).and_then(|f|f.sync_all()).map_err(|_|"vault_unavailable")?;
        let written=read_private(&path,1024)?;if written!=raw{return Err("vault_unavailable");}
        proof.validate(app_data,owner,epoch)?;lease.check()?;Ok(())
    })();
    if result.is_err() {if let Ok(found)=std::fs::symlink_metadata(&temp) {if found.is_file() && found.dev()==m.dev() && found.ino()==m.ino() {let _=std::fs::remove_file(&temp);}}}
    result
}

pub(crate) fn verify_parked_receipt(proof:&ProfileProof,app_data:&Path,owner:u64,epoch:&str,custody_id:&str,lease:&ProfileLease)->Result<(),&'static str> {
    if !hex(custody_id,32) {return Err("vault_unavailable");}
    let data=proof.validate(app_data,owner,epoch)?;lease.check()?;
    let path=data.parent().ok_or("vault_unavailable")?.join(format!("custody-{}.json",proof.record.handoff_id));
    let expected=serde_json::to_vec(&serde_json::json!({"version":1,"status":"parked","handoff_id":proof.record.handoff_id,"profile_id":proof.record.profile_id,"profile_revision":proof.record.profile_revision,"custody_id":custody_id})).map_err(|_|"vault_unavailable")?;
    if read_private(&path,1024)?!=expected {return Err("vault_unavailable");}Ok(())
}

/// In-memory handoff authority. Active browser ownership will be attached only
/// after the durable attempted marker and the matching profile lease exist.
pub(crate) struct Handoff {pub grant:Grant,pub realm:String,pub state:&'static str,pub fill_status:&'static str,pub browser:Option<crate::vault_browser::Browser>,pub attempted:bool,pub custody:Option<Custody>,pub cancel:std::sync::Arc<std::sync::atomic::AtomicBool>}
impl Handoff {
    pub(crate) fn public(&self)->Value {serde_json::json!({"version":1,"status":self.state,"handoff_id":self.grant.record.handoff_id,"profile_id":self.grant.record.profile_id,"profile_revision":self.grant.record.profile_revision,"fill_status":self.fill_status})}
}
pub(crate) fn unavailable(handoff_id:&str)->Value {serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":null,"profile_revision":null,"fill_status":"unknown"})}

/// The marker is created and synced before a browser is spawned. It never
/// proves successful cleanup; on restart it only makes the prior effect unknown.
pub(crate) fn mark_attempted(profile:&Path,handoff_id:&str)->Result<(),&'static str> {
    use std::io::Write;
    if !hex(handoff_id,32){return Err("vault_unavailable");}checked_directory(profile)?;
    let path=profile.join(format!("vault-attempt-{handoff_id}.json"));
    let mut file=OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&path).map_err(|_|"vault_unavailable")?;
    let meta=file.metadata().map_err(|_|"vault_unavailable")?;
    if !meta.is_file() || meta.uid()!=unsafe{libc::geteuid()} || meta.mode()&0o777!=0o600 || meta.nlink()!=1 {return Err("vault_unavailable");}
    file.write_all(b"{\"version\":1,\"state\":\"attempted\"}").and_then(|_|file.sync_all()).map_err(|_|"vault_unavailable")?;
    File::open(profile).and_then(|f|f.sync_all()).map_err(|_|"vault_unavailable")?;Ok(())
}

impl Handoff {
    /// Establish custody before starting Chromium. The caller must recheck the
    /// current Owner, generation, peer, and grant before exposing the browser.
    pub(crate) fn start_browser(&mut self,config:&crate::vault::VaultConfig,manifest:&str,command:&str,peer:&std::os::unix::net::UnixStream,stop:std::sync::Arc<std::sync::atomic::AtomicBool>)->std::result::Result<crate::vault_browser::Browser,crate::vault_browser::StartFailure> {
        use std::sync::atomic::Ordering;
        if self.state!="prepared" || self.attempted || self.cancel.load(Ordering::SeqCst) {return Err(crate::vault_browser::StartFailure::Unconfirmed);}
        let app_data=config.directory.parent().ok_or(crate::vault_browser::StartFailure::Unconfirmed)?;
        let data=self.grant.validate(app_data,self.grant.record.owner_id,&self.grant.record.owner_epoch).map_err(|_|crate::vault_browser::StartFailure::Unconfirmed)?;
        let profile=data.parent().ok_or(crate::vault_browser::StartFailure::Unconfirmed)?;
        let lease=ProfileLease::acquire(profile).map_err(|_|crate::vault_browser::StartFailure::Unconfirmed)?;
        if self.grant.validate(app_data,self.grant.record.owner_id,&self.grant.record.owner_epoch).is_err() || lease.check().is_err() || self.cancel.load(Ordering::SeqCst) {
            lease.release_after_confirmed_exit();return Err(crate::vault_browser::StartFailure::Unconfirmed);
        }
        // Set the in-memory state to unknown before touching disk: a partial
        // marker write must never look like a never-activated grant.
        self.attempted=true;self.state="unknown";self.fill_status="unknown";
        if mark_attempted(profile,&self.grant.record.handoff_id).is_err() {lease.release_after_confirmed_exit();return Err(crate::vault_browser::StartFailure::Unconfirmed);}
        match crate::vault_browser::Browser::start_profile(config,manifest,&self.realm,&self.grant,lease,command,peer,stop,self.cancel.clone()) {
            Ok(browser)=>{self.state="active";self.fill_status="not_started";Ok(browser)},
            Err(failure)=>Err(failure),
        }
    }
}

#[derive(Deserialize)] #[serde(deny_unknown_fields)]
struct Transition {version:u8,handoff_id:String,phase:String,old_metadata_sha256:String,new_metadata_sha256:String,old_record_sha256:String,new_record_sha256:String}
/// Called only after the signed Engine's explicit Owner recovery and no-ref
/// checks. The held flock fences the fixed profile while this proof is made.
pub(crate) fn recover_proof(app_data:&Path,owner:u64,profile_id:&str,handoff_id:&str,metadata_hash:&str,record_hash:&str,journal_hash:Option<&str>)->Result<(ProfileProof,ProfileLease),&'static str> {
    if owner==0 || !hex(profile_id,32) || !hex(handoff_id,32) || !hex(metadata_hash,64) || !hex(record_hash,64) || journal_hash.is_some_and(|v|!hex(v,64)) {return Err("vault_unavailable");}
    let root=app_data.join("browser-profiles");let profile=root.join(profile_id);let data=profile.join("data");
    for path in [app_data,&root,&profile,&data] {checked_directory(path)?;}
    let lease=ProfileLease::acquire(&profile)?;
    let checked=(|| -> Result<ProfileProof,&'static str> {
        let record_raw=read_private(&profile.join("handoff.json"),32768)?;
        if digest(&record_raw)!=record_hash {return Err("vault_unavailable");}
        let record:Record=serde_json::from_slice(&record_raw).map_err(|_|"vault_unavailable")?;
        if record.owner_id!=owner || record.profile_id!=profile_id || record.handoff_id!=handoff_id || !record.valid_base(false,true) {return Err("vault_unavailable");}
        let metadata=read_private(&profile.join("metadata.json"),16384)?;
        if digest(&metadata)!=metadata_hash {return Err("vault_unavailable");}
        let meta:Value=serde_json::from_slice(&metadata).map_err(|_|"vault_unavailable")?;
        let revision=meta.get("revision").and_then(Value::as_u64).ok_or("vault_unavailable")?;
        let journal_path=profile.join("handoff-transition.json");
        match journal_hash {
            None=>{
                match std::fs::symlink_metadata(&journal_path) {Err(e) if e.kind()==std::io::ErrorKind::NotFound=>{},_=>return Err("vault_unavailable")}
                if record.state!="prepared" || revision!=record.profile_revision {return Err("vault_unavailable");}
            },
            Some(expected)=>{
                let bytes=read_private(&journal_path,32768)?;if digest(&bytes)!=expected {return Err("vault_unavailable");}
                let j:Transition=serde_json::from_slice(&bytes).map_err(|_|"vault_unavailable")?;
                if j.version!=1 || j.handoff_id!=handoff_id || !matches!(j.phase.as_str(),"close"|"release") || [&j.old_metadata_sha256,&j.new_metadata_sha256,&j.old_record_sha256,&j.new_record_sha256].iter().any(|s|!hex(s,64)) {return Err("vault_unavailable");}
                let old_old=metadata_hash==j.old_metadata_sha256 && record_hash==j.old_record_sha256;
                let new_old=metadata_hash==j.new_metadata_sha256 && record_hash==j.old_record_sha256;
                let new_new=metadata_hash==j.new_metadata_sha256 && record_hash==j.new_record_sha256;
                if !(old_old||new_old||new_new) {return Err("vault_unavailable");}
                if j.phase=="close" {
                    if record.state!="prepared" || record.profile_revision.checked_add(if new_old {1} else {0})!=Some(revision) {return Err("vault_unavailable");}
                } else if record.profile_revision.checked_add(if new_old||new_new {1} else {0})!=Some(revision) || (new_new && !matches!(record.state.as_str(),"returned"|"recovered")) || (!new_new && record.state!="prepared") {return Err("vault_unavailable");}
            },
        }
        let marker=profile.join(format!("vault-attempt-{handoff_id}.json"));
        match std::fs::symlink_metadata(&marker) {
            Ok(_)=>{let raw=read_private(&marker,1024)?;let value:Value=serde_json::from_slice(&raw).map_err(|_|"vault_unavailable")?;if value!=serde_json::json!({"version":1,"state":"attempted"}) {return Err("vault_unavailable");}},
            Err(e) if e.kind()==std::io::ErrorKind::NotFound=>{},Err(_)=>return Err("vault_unavailable"),
        }
        let proof=ProfileProof{record,metadata_sha256:metadata_hash.into(),record_sha256:record_hash.into()};
        proof.validate_mode(app_data,owner,&proof.record.owner_epoch,false,true,Some(revision))?;lease.check()?;no_live_singleton(&data)?;Ok(proof)
    })();
    match checked {Ok(proof)=>Ok((proof,lease)),Err(e)=>{lease.release_after_confirmed_exit();Err(e)}}
}
