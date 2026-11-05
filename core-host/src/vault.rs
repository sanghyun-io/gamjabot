//! Private Vault primitives. Not exposed to Engine or the public control socket.
//! The owner mutex must be held across authorization and the broker operation.
use crate::auth_broker::{AuthBroker, SyntheticAuthTransport};
use crate::credential::CredentialBackend;
use serde::{Deserialize, Serialize};
use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

fn lower_hex(s: &str, n: usize) -> bool {
    s.len() == n && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// A caller must retain this until the authenticated broker process has exited.
/// The lease directory is dedicated to Vault and must already exist as 0700.
pub(crate) struct RealmLease { file: File, path: PathBuf, directory: PathBuf, dir_dev: u64, dir_ino: u64, realm: String, dev: u64, ino: u64 }
impl RealmLease {
    pub(crate) fn acquire(directory: &Path, realm: &str) -> Result<Self, &'static str> {
        if !lower_hex(realm, 64) || !directory.is_absolute() || directory.canonicalize().ok().as_deref() != Some(directory) {
            return Err("vault_unavailable");
        }
        let meta = std::fs::symlink_metadata(directory).map_err(|_| "vault_unavailable")?;
        if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o777 != 0o700 { return Err("vault_unavailable"); }
        let path = directory.join(format!("{realm}.lock"));
        let file = OpenOptions::new().read(true).write(true).create(true).mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(&path).map_err(|_| "vault_unavailable")?;
        let m = file.metadata().map_err(|_| "vault_unavailable")?;
        if !m.is_file() || m.uid() != unsafe { libc::geteuid() } || m.mode() & 0o777 != 0o600 || m.nlink() != 1 { return Err("vault_unavailable"); }
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 { return Err("vault_unavailable"); }
        let lease = Self { file, path, directory: directory.to_owned(), dir_dev: meta.dev(), dir_ino: meta.ino(), realm: realm.to_owned(), dev: m.dev(), ino: m.ino() };
        lease.check()?;
        Ok(lease)
    }
    fn check(&self) -> Result<(), &'static str> {
        let d = std::fs::symlink_metadata(&self.directory).map_err(|_| "vault_unavailable")?;
        if !d.is_dir() || d.uid()!=unsafe {libc::geteuid()} || d.mode() & 0o777 != 0o700
            || d.dev()!=self.dir_dev || d.ino()!=self.dir_ino
            || self.directory.canonicalize().ok().as_deref()!=Some(self.directory.as_path()) { return Err("vault_unavailable"); }
        let a = self.file.metadata().map_err(|_| "vault_unavailable")?;
        let b = std::fs::symlink_metadata(&self.path).map_err(|_| "vault_unavailable")?;
        if !b.is_file() || a.dev()!=self.dev || b.dev()!=self.dev || a.ino()!=self.ino || b.ino()!=self.ino
            || a.uid()!=unsafe {libc::geteuid()} || b.uid()!=a.uid() || a.nlink()!=1 || b.nlink()!=1
            || a.mode() & 0o777 != 0o600 || b.mode() & 0o777 != 0o600 { return Err("vault_unavailable"); }
        Ok(())
    }
}

/// Normalize a user-provided website without allowing userinfo or non-HTTPS schemes.
pub(crate) fn login_origin(website:&str)->Result<String,&'static str> {
    if website.is_empty() || website.len()>4096 || website.chars().any(char::is_control) || website.trim()!=website {return Err("vault_unavailable");}
    let (_,tail)=website.split_once("://").ok_or("vault_unavailable")?;
    let authority=tail.split(['/', '?', '#']).next().ok_or("vault_unavailable")?;
    if authority.is_empty() || authority.contains('@') || website.contains('\\') {return Err("vault_unavailable");}
    let url=url::Url::parse(website).map_err(|_| "vault_unavailable")?;
    if url.scheme()!="https" || url.host_str().is_none() || !url.username().is_empty() || url.password().is_some() {return Err("vault_unavailable");}
    let origin=url.origin().ascii_serialization();
    if origin=="null" || origin.len()>2048 {return Err("vault_unavailable");}
    Ok(origin)
}
pub(crate) fn valid_username(username:&str)->bool {
    username.len()<=512 && !username.chars().any(char::is_control)
}
#[derive(Serialize)]
struct LoginWrite<'a> { version:u8, kind:&'static str, origin:&'a str, username:&'a str, password:&'a str }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LoginRead { version:u8, kind:String, origin:String, username:String, password:Zeroizing<String> }

#[derive(Serialize)]
struct Request<'a> {
    version: u8, operation: &'static str, namespace: &'static str, reference: String,
    owner_epoch: &'a str, expected_revision: Option<u64>, command_id: Option<&'a str>, value: Option<&'a str>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Response { status: String, revision: Option<u64>, command_id: Option<String>, value: Option<Zeroizing<String>> }
/// Publicly consumable status contains no secret. Values may only go to a future Core-private fill path.
pub(crate) struct Status { pub status: String, pub revision: Option<u64>, pub command_id: Option<String> }
#[derive(Serialize)]
pub(crate) struct MetadataStatus { pub version:u8, pub status:&'static str, pub revision:Option<u64>, pub command_id:Option<String> }
impl MetadataStatus {
    pub(crate) fn unavailable()->Self {Self{version:1,status:"unavailable",revision:None,command_id:None}}
}


pub(crate) struct VaultStore { stream: UnixStream, lease: RealmLease }
impl VaultStore {
    pub(crate) fn new(stream: UnixStream, lease: RealmLease) -> Self { Self { stream, lease } }
    fn request<B: CredentialBackend, T: SyntheticAuthTransport>(&mut self, auth: &AuthBroker<B,T>, owner: u64, epoch: &str,
        item: &str, operation: &'static str, revision: Option<u64>, command: Option<&str>, value: Option<&str>) -> Result<Response, &'static str> {
        self.request_bounded(auth,owner,epoch,item,operation,revision,command,value,Duration::from_secs(5))
    }
    fn request_bounded<B: CredentialBackend, T: SyntheticAuthTransport>(&mut self, auth: &AuthBroker<B,T>, owner: u64, epoch: &str,
        item: &str, operation: &'static str, revision: Option<u64>, command: Option<&str>, value: Option<&str>, timeout:Duration) -> Result<Response, &'static str> {
        let realm = auth.vault_realm(owner, epoch).ok_or("vault_unavailable")?;
        if !lower_hex(item,32) || command.is_some_and(|c| !lower_hex(c,32)) { return Err("vault_unavailable"); }
        if self.lease.realm != realm { return Err("vault_unavailable"); }
        self.lease.check()?;
        let req = Request { version:1, operation, namespace:"vault_login", reference:format!("vault-login:{realm}:{item}"), owner_epoch:epoch, expected_revision:revision, command_id:command, value };
        let body = Zeroizing::new(serde_json::to_vec(&req).map_err(|_| "vault_unavailable")?);
        let deadline=Instant::now()+timeout;
        crate::stable_broker::write_frame(&mut self.stream,&body,deadline).map_err(|_| "vault_unavailable")?;
        let reply=crate::stable_broker::read_frame(&mut self.stream,deadline).map_err(|_| "vault_unavailable")?;
        let response:Response=serde_json::from_slice(&reply).map_err(|_| "vault_unavailable")?;
        if !matches!(response.status.as_str(),"present"|"missing"|"stored"|"deleted"|"conflict"|"failed"|"unavailable")
            || response.command_id.as_ref().is_some_and(|c| !lower_hex(c,32)) { return Err("vault_unavailable"); }
        Ok(response)
    }
    fn card_request<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,operation:&'static str,revision:Option<u64>,command:Option<&str>,value:Option<&str>,timeout:Duration)->Result<Response,&'static str> {
        let realm=auth.vault_realm(owner,epoch).ok_or("vault_unavailable")?;
        if !lower_hex(item,32) || command.is_some_and(|c|!lower_hex(c,32)) || self.lease.realm!=realm {return Err("vault_unavailable");}
        self.lease.check()?;
        let req=Request{version:1,operation,namespace:"vault_card",reference:format!("vault-card:{realm}:{item}"),owner_epoch:epoch,expected_revision:revision,command_id:command,value};
        let body=Zeroizing::new(serde_json::to_vec(&req).map_err(|_| "vault_unavailable")?);let end=Instant::now()+timeout;
        crate::stable_broker::write_frame(&mut self.stream,&body,end).map_err(|_| "vault_unavailable")?;
        let reply=crate::stable_broker::read_frame(&mut self.stream,end).map_err(|_| "vault_unavailable")?;
        let response:Response=serde_json::from_slice(&reply).map_err(|_| "vault_unavailable")?;
        if !matches!(response.status.as_str(),"present"|"missing"|"stored"|"deleted"|"conflict"|"failed"|"unavailable") || response.command_id.as_ref().is_some_and(|v|!lower_hex(v,32)) {return Err("vault_unavailable");}
        Ok(response)
    }
    pub(crate) fn card_preflight<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str)->Result<(),&'static str> {
        let r=self.card_request(auth,owner,epoch,item,"status",None,None,None,Duration::from_millis(750))?;
        if r.value.is_some() {return Err("vault_unavailable");}
        match r.status.as_str() {
            "missing" if r.revision.is_none() && r.command_id.is_none()=>Ok(()),
            "present"|"deleted" if r.revision.is_some_and(|v|v>0) && r.command_id.is_some()=>Ok(()),
            _=>Err("vault_unavailable"),
        }
    }
    fn card_read<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,revision:u64)->Result<(crate::vault_card::CardRead,Option<String>),&'static str> {
        let mut r=self.card_request(auth,owner,epoch,item,"read_for_fill",Some(revision),None,None,Duration::from_millis(750))?;
        if r.status!="present" || r.revision!=Some(revision) {return Err("vault_unavailable");}
        let raw=r.value.take().filter(|v|!v.is_empty() && v.len()<=8192).ok_or("vault_unavailable")?;
        let card:crate::vault_card::CardRead=serde_json::from_str(&raw).map_err(|_| "vault_unavailable")?;
        if card.version!=1 || card.kind!="card" || !card.metadata.valid(false) || crate::vault_card::last4(&card.pan)!=card.metadata.last4.as_deref() || !crate::vault_card::valid_cvv(&card.cvv) {return Err("vault_unavailable");}
        Ok((card,r.command_id))
    }
    pub(crate) fn card_fill_secret<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,revision:u64,metadata:&crate::vault_card::CardMetadata)->Result<crate::vault_card::CardRead,&'static str> {
        let status=self.card_metadata_status(auth,owner,epoch,item,metadata)?;
        if status.status!="present" || status.revision!=Some(revision) {return Err("vault_unavailable");}
        let (card,command)=self.card_read(auth,owner,epoch,item,revision)?;
        if command!=status.command_id || !card.metadata.matches_expected(metadata) {return Err("vault_unavailable");}Ok(card)
    }
    pub(crate) fn card_metadata_status<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,metadata:&crate::vault_card::CardMetadata)->Result<crate::vault_card::CardMetadataStatus,&'static str> {
        if !metadata.valid(true) {return Err("vault_unavailable");}
        let status=self.card_request(auth,owner,epoch,item,"status",None,None,None,Duration::from_millis(750))?;
        if status.value.is_some() {return Err("vault_unavailable");}
        match status.status.as_str() {
            "missing" if status.revision.is_none() && status.command_id.is_none()=>return Ok(crate::vault_card::CardMetadataStatus{version:1,status:"missing",revision:None,command_id:None,last4:None}),
            "deleted" if status.revision.is_some_and(|v|v>0) && status.command_id.is_some()=>return Ok(crate::vault_card::CardMetadataStatus{version:1,status:"deleted",revision:status.revision,command_id:status.command_id,last4:None}),
            "present" if status.revision.is_some_and(|v|v>0) && status.command_id.is_some()=>{},
            _=>return Err("vault_unavailable"),
        }
        let revision=status.revision.ok_or("vault_unavailable")?;
        let (card,read_command)=self.card_read(auth,owner,epoch,item,revision)?;
        if read_command!=status.command_id {return Err("vault_unavailable");}
        let state=if card.metadata.matches_expected(metadata) {"present"} else {"metadata_mismatch"};
        let last4=if state=="present" {card.metadata.last4.clone()} else {None};
        Ok(crate::vault_card::CardMetadataStatus{version:1,status:state,revision:Some(revision),command_id:status.command_id,last4})
    }
    pub(crate) fn card_store<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,revision:u64,command:&str,metadata:&crate::vault_card::CardMetadata,input:&crate::vault_card::CardInput)->Result<crate::vault_card::CardStatus,&'static str> {
        use crate::vault_card::{CardStatus,CardWrite,normalize_pan,valid_cvv,last4};
        if !metadata.valid(revision==0) {return Err("vault_unavailable");}
        let invalid=||CardStatus{version:1,status:"invalid_input",revision:Some(revision),command_id:Some(command.to_owned()),last4:None};
        if !valid_cvv(&input.cvv) || (revision>0 && !input.pan.is_empty()) {return Ok(invalid());}
        let mut target=metadata.clone();
        let normalized=if revision==0 {match normalize_pan(&input.pan) {Some(pan)=>Some(pan),None=>return Ok(invalid())}} else {None};
        let prior=if revision==0 {
            if metadata.last4.is_some() {return Err("vault_unavailable");}
            target.last4=Some(last4(normalized.as_ref().ok_or("vault_unavailable")?).ok_or("vault_unavailable")?.to_owned());None
        } else {
            // Existing PAN may never be supplied or replaced, including same bytes.
            let (card,_)=self.card_read(auth,owner,epoch,item,revision)?;
            if card.metadata.last4!=metadata.last4 {return Err("vault_unavailable");}
            Some(card)
        };
        let pan=prior.as_ref().map(|v|v.pan.as_str()).unwrap_or_else(||normalized.as_ref().map(|v|v.as_str()).unwrap_or(""));
        let cvv=if input.cvv.is_empty() {prior.as_ref().map(|v|v.cvv.as_str()).unwrap_or("")} else {input.cvv.as_str()};
        let envelope=Zeroizing::new(serde_json::to_string(&CardWrite{version:1,kind:"card",metadata:&target,pan,cvv}).map_err(|_| "vault_unavailable")?);
        if envelope.len()>8192 {return Err("vault_unavailable");}
        let r=self.card_request(auth,owner,epoch,item,"replace",Some(revision),Some(command),Some(envelope.as_str()),Duration::from_secs(5))?;
        if r.value.is_some(){return Err("vault_unavailable");}
        if r.status=="stored" && (r.revision!=revision.checked_add(1) || r.command_id.as_deref()!=Some(command)) {return Err("vault_unavailable");}
        let status=match r.status.as_str(){"stored"=>"stored","conflict"=>"conflict",_=>"unavailable"};
        Ok(CardStatus{version:1,status,revision:r.revision,command_id:r.command_id,last4:if status=="stored" {target.last4} else {None}})
    }
    pub(crate) fn card_delete<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,revision:u64,command:&str)->Result<crate::vault_card::CardStatus,&'static str> {
        let r=self.card_request(auth,owner,epoch,item,"delete",Some(revision),Some(command),None,Duration::from_secs(5))?;
        if r.value.is_some(){return Err("vault_unavailable");}
        if r.status=="deleted" && (r.revision!=revision.checked_add(1) || r.command_id.as_deref()!=Some(command)) {return Err("vault_unavailable");}
        let status=match r.status.as_str(){"deleted"=>"deleted","conflict"=>"conflict",_=>"unavailable"};
        Ok(crate::vault_card::CardStatus{version:1,status,revision:r.revision,command_id:r.command_id,last4:None})
    }
    pub(crate) fn status<B: CredentialBackend,T: SyntheticAuthTransport>(&mut self, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str) -> Result<Status,&'static str> {
        let r=self.request(auth,owner,epoch,item,"status",None,None,None)?;
        if r.value.is_some() {return Err("vault_unavailable");} Ok(Status{status:r.status,revision:r.revision,command_id:r.command_id})
    }
    pub(crate) fn metadata_status<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str, website:&str, username:&str)->Result<MetadataStatus,&'static str> {
        let origin=login_origin(website)?;
        if !valid_username(username) {return Err("vault_unavailable");}
        let status=self.request_bounded(auth,owner,epoch,item,"status",None,None,None,Duration::from_millis(750))?;
        if status.value.is_some() {return Err("vault_unavailable");}
        match status.status.as_str() {
            "missing" if status.revision.is_none() && status.command_id.is_none()=>return Ok(MetadataStatus{version:1,status:"missing",revision:None,command_id:None}),
            "deleted" if status.revision.is_some_and(|v|v>0) && status.command_id.is_some()=>return Ok(MetadataStatus{version:1,status:"deleted",revision:status.revision,command_id:status.command_id}),
            "present" if status.revision.is_some_and(|v|v>0) && status.command_id.is_some()=>{},
            _=>return Err("vault_unavailable"),
        }
        let revision=status.revision.ok_or("vault_unavailable")?;
        let mut value=self.request_bounded(auth,owner,epoch,item,"read_for_fill",Some(revision),None,None,Duration::from_millis(750))?;
        if value.status!="present" || value.revision!=Some(revision) || value.command_id!=status.command_id {return Err("vault_unavailable");}
        let raw=value.value.take().filter(|v|!v.is_empty() && v.len()<=8192).ok_or("vault_unavailable")?;
        let envelope:LoginRead=serde_json::from_str(&raw).map_err(|_| "vault_unavailable")?;
        if envelope.version!=1 || envelope.kind!="login" || envelope.password.is_empty() || envelope.password.len()>8192
            || !valid_username(&envelope.username) || login_origin(&envelope.origin).ok().as_deref()!=Some(envelope.origin.as_str()) {return Err("vault_unavailable");}
        let state=if envelope.origin==origin && envelope.username==username {"present"} else {"metadata_mismatch"};
        // The parsed password and serialized secret are zeroized when this method returns.
        Ok(MetadataStatus{version:1,status:state,revision:Some(revision),command_id:status.command_id})
    }
    pub(crate) fn replace<B: CredentialBackend,T: SyntheticAuthTransport>(&mut self, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str, revision:u64, command:&str, website:&str, username:&str, secret:&Zeroizing<String>) -> Result<Status,&'static str> {
        if secret.is_empty() || secret.len()>8192 || !valid_username(username) { return Err("vault_unavailable"); }
        let origin=login_origin(website)?;
        let envelope=Zeroizing::new(serde_json::to_string(&LoginWrite{version:1,kind:"login",origin:&origin,username,password:secret.as_str()}).map_err(|_| "vault_unavailable")?);
        if envelope.len()>8192 {return Err("vault_unavailable");}
        let r=self.request(auth,owner,epoch,item,"replace",Some(revision),Some(command),Some(envelope.as_str()))?;
        if r.value.is_some() {return Err("vault_unavailable");} Ok(Status{status:r.status,revision:r.revision,command_id:r.command_id})
    }
    pub(crate) fn delete<B: CredentialBackend,T: SyntheticAuthTransport>(&mut self, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str, revision:u64, command:&str) -> Result<Status,&'static str> {
        let r=self.request(auth,owner,epoch,item,"delete",Some(revision),Some(command),None)?;
        if r.value.is_some() {return Err("vault_unavailable");} Ok(Status{status:r.status,revision:r.revision,command_id:r.command_id})
    }
    pub(crate) fn read_for_fill<B: CredentialBackend,T: SyntheticAuthTransport>(&mut self, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str, revision:u64, expected_origin:&str, expected_username:&str) -> Result<Zeroizing<String>,&'static str> {
        if !valid_username(expected_username) {return Err("vault_unavailable");}
        let origin=login_origin(expected_origin)?;
        // The fill caller must supply an already canonical origin, not an arbitrary URL.
        if origin!=expected_origin {return Err("vault_unavailable");}
        let mut r=self.request(auth,owner,epoch,item,"read_for_fill",Some(revision),None,None)?;
        if r.status!="present" || r.revision!=Some(revision) {return Err("vault_unavailable");}
        let raw=r.value.take().filter(|v| !v.is_empty() && v.len()<=8192).ok_or("vault_unavailable")?;
        let envelope:LoginRead=serde_json::from_str(&raw).map_err(|_| "vault_unavailable")?;
        if envelope.version!=1 || envelope.kind!="login" || envelope.origin!=origin || envelope.username!=expected_username || envelope.password.is_empty() || envelope.password.len()>8192 {return Err("vault_unavailable");}
        Ok(envelope.password)
    }
}

/// The helper must be stopped before the cross-process lease can be released.
/// The caller must retain the owner mutex while dispatching each operation.
pub(crate) struct VaultSession {
    broker: crate::broker_runtime::VaultBrokerSession,
    store: Option<VaultStore>,
}
impl VaultSession {
    pub(crate) fn start(core:&Path, engine:&Path, engine_hash:&str, engine_pid:u32,
        directory:&Path, realm:&str) -> Result<Self,&'static str> {
        let lease=RealmLease::acquire(directory,realm)?;
        let mut broker=match crate::broker_runtime::VaultBrokerSession::start(core,engine,engine_hash) {
            Ok(broker)=>broker, Err(code)=>{ if code=="vault_broker_cleanup_uncertain" {std::mem::forget(lease);} return Err("vault_unavailable"); }
        };
        if broker.bind(engine_pid).is_err() {
            // If shutdown cannot be confirmed, retain the lock until Core exits.
            if broker.shutdown().is_err() { std::mem::forget(lease); }
            return Err("vault_unavailable");
        }
        let stream=match broker.take_internal() { Ok(s)=>s, Err(_)=>{
            if broker.shutdown().is_err() {std::mem::forget(lease);}
            return Err("vault_unavailable");
        }};
        Ok(Self{broker,store:Some(VaultStore::new(stream,lease))})
    }
    /// No request is in flight when called. Readable bytes are unexpected and
    /// therefore poison the session just like an EOF or helper exit.
    pub(crate) fn reusable_manifest(&mut self)->Result<String,&'static str> {
        if !self.broker.is_live().map_err(|_| "vault_unavailable")? {return Err("vault_unavailable");}
        let stream=&self.store.as_ref().ok_or("vault_unavailable")?.stream;
        let mut fd=libc::pollfd{fd:stream.as_raw_fd(),events:libc::POLLIN,revents:0};
        let n=unsafe {libc::poll(&mut fd,1,0)};
        if n!=0 || fd.revents!=0 {return Err("vault_unavailable");}
        Ok(self.broker.trusted_manifest().map_err(|_| "vault_unavailable")?.to_owned())
    }
    pub(crate) fn authorize_peer(&self, peer:&UnixStream) -> Result<(),&'static str> {
        let manifest=self.broker.trusted_manifest().map_err(|_| "vault_unavailable")?;
        crate::vault_control::verify_peer(peer,manifest)?;
        self.broker.trusted_manifest().map_err(|_| "vault_unavailable")?;
        Ok(())
    }
    pub(crate) fn fill_secret<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self, auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,revision:u64,origin:&str,username:&str)->Result<Zeroizing<String>,&'static str> {
        self.reusable_manifest()?;
        self.store.as_mut().ok_or("vault_unavailable")?.read_for_fill(auth,owner,epoch,item,revision,origin,username)
    }
    pub(crate) fn metadata_status<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str, website:&str, username:&str)->Result<MetadataStatus,&'static str> {
        self.reusable_manifest()?;
        self.store.as_mut().ok_or("vault_unavailable")?.metadata_status(auth,owner,epoch,item,website,username)
    }
    pub(crate) fn card_preflight<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str)->Result<(),&'static str> {
        self.reusable_manifest()?;self.store.as_mut().ok_or("vault_unavailable")?.card_preflight(auth,owner,epoch,item)
    }
    pub(crate) fn card_fill_secret<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,revision:u64,metadata:&crate::vault_card::CardMetadata)->Result<crate::vault_card::CardRead,&'static str> {
        self.reusable_manifest()?;self.store.as_mut().ok_or("vault_unavailable")?.card_fill_secret(auth,owner,epoch,item,revision,metadata)
    }
    pub(crate) fn card_metadata_status<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,metadata:&crate::vault_card::CardMetadata)->Result<crate::vault_card::CardMetadataStatus,&'static str> {
        self.reusable_manifest()?;self.store.as_mut().ok_or("vault_unavailable")?.card_metadata_status(auth,owner,epoch,item,metadata)
    }
    pub(crate) fn card_store<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,peer:&UnixStream,stop:&std::sync::atomic::AtomicBool,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,revision:u64,command:&str,metadata:&crate::vault_card::CardMetadata,input:&crate::vault_card::CardInput)->Result<crate::vault_card::CardStatus,&'static str> {
        self.authorize_peer(peer)?;
        if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) {return Err("vault_unavailable");}
        self.store.as_mut().ok_or("vault_unavailable")?.card_store(auth,owner,epoch,item,revision,command,metadata,input)
    }
    pub(crate) fn card_delete<B:CredentialBackend,T:SyntheticAuthTransport>(&mut self,peer:&UnixStream,stop:&std::sync::atomic::AtomicBool,auth:&AuthBroker<B,T>,owner:u64,epoch:&str,item:&str,revision:u64,command:&str)->Result<crate::vault_card::CardStatus,&'static str> {
        self.authorize_peer(peer)?;
        if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) {return Err("vault_unavailable");}
        self.store.as_mut().ok_or("vault_unavailable")?.card_delete(auth,owner,epoch,item,revision,command)
    }
    pub(crate) fn status<B: CredentialBackend,T: SyntheticAuthTransport>(&mut self, peer:&UnixStream, stop:&std::sync::atomic::AtomicBool, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str) -> Result<Status,&'static str> {
        self.authorize_peer(peer)?;
        if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) {return Err("vault_unavailable");}
        self.store.as_mut().ok_or("vault_unavailable")?.status(auth,owner,epoch,item)
    }
    pub(crate) fn replace<B: CredentialBackend,T: SyntheticAuthTransport>(&mut self, peer:&UnixStream, stop:&std::sync::atomic::AtomicBool, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str, revision:u64, command:&str, website:&str, username:&str, secret:&Zeroizing<String>) -> Result<Status,&'static str> {
        self.authorize_peer(peer)?;
        if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) {return Err("vault_unavailable");}
        self.store.as_mut().ok_or("vault_unavailable")?.replace(auth,owner,epoch,item,revision,command,website,username,secret)
    }
    pub(crate) fn delete<B: CredentialBackend,T: SyntheticAuthTransport>(&mut self, peer:&UnixStream, stop:&std::sync::atomic::AtomicBool, auth:&AuthBroker<B,T>, owner:u64, epoch:&str, item:&str, revision:u64, command:&str) -> Result<Status,&'static str> {
        self.authorize_peer(peer)?;
        if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) {return Err("vault_unavailable");}
        self.store.as_mut().ok_or("vault_unavailable")?.delete(auth,owner,epoch,item,revision,command)
    }
}
impl Drop for VaultSession {
    fn drop(&mut self) {
        if self.broker.shutdown().is_err() {
            if let Some(store)=self.store.take() { std::mem::forget(store); }
        }
    }
}

#[derive(Clone)]
pub(crate) struct VaultConfig {
    pub core: PathBuf, pub engine: PathBuf, pub engine_hash: String,
    pub engine_pid: u32, pub directory: PathBuf,
}
