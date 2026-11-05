//! Native-only peer identity. No public control capability grants this channel.
use core_foundation::url::CFURL;
use security_framework::os::macos::code_signing::{Flags, SecRequirement, SecStaticCode};
use sha2::{Digest, Sha256};
use std::ffi::CString;
use std::fs::OpenOptions;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::str::FromStr;

const GUI: &str = "/Applications/Gamja Bot.app";
const REQUIREMENT: &str = "anchor = H\"12035B8BBAB3E028E3433EDF37449688B3930438\" and identifier \"top.gamja.bot\"";
extern "C" {
    fn gamja_upgrade_code(pid:i32,path:*const libc::c_char,requirement:*const libc::c_char)->i32;
    fn gamja_upgrade_process(pid:i32,seconds:*mut u64,micros:*mut u64,parent:*mut i32)->i32;
}
#[derive(Clone, Copy, PartialEq, Eq)]
struct Process { pid:i32, seconds:u64, micros:u64, parent:i32 }
fn process(pid:i32)->Result<Process,&'static str> {
    let mut p=Process{pid,seconds:0,micros:0,parent:0};
    if pid<=1 || unsafe {gamja_upgrade_process(pid,&mut p.seconds,&mut p.micros,&mut p.parent)}!=0 {return Err("vault_unavailable");}
    Ok(p)
}
fn peer_pid(stream:&UnixStream)->Result<i32,&'static str> {
    let mut uid:libc::uid_t=0; let mut gid:libc::gid_t=0;
    if unsafe {libc::getpeereid(stream.as_raw_fd(),&mut uid,&mut gid)}!=0 || uid!=unsafe{libc::geteuid()} {return Err("vault_unavailable");}
    let mut pid:libc::pid_t=0;let mut n=std::mem::size_of_val(&pid) as libc::socklen_t;
    // Darwin SOL_LOCAL / LOCAL_PEERPID, returned by the kernel, never the peer.
    if unsafe {libc::getsockopt(stream.as_raw_fd(),0,2,&mut pid as *mut _ as *mut _,&mut n)}!=0 || n as usize!=std::mem::size_of_val(&pid) {return Err("vault_unavailable");}
    Ok(pid)
}
fn manifest_hash(path:&Path)->Result<String,&'static str> {
    if !path.is_absolute() || path.canonicalize().ok().as_deref()!=Some(path) {return Err("vault_unavailable");}
    let f=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(path).map_err(|_| "vault_unavailable")?;
    let before=f.metadata().map_err(|_| "vault_unavailable")?;
    if !before.is_file() || before.nlink()!=1 || before.mode()&0o022!=0 || !(before.uid()==0 || before.uid()==unsafe{libc::geteuid()}) || before.len()==0 || before.len()>2*1024*1024 {return Err("vault_unavailable");}
    let mut bytes=Vec::new(); f.take(2*1024*1024+1).read_to_end(&mut bytes).map_err(|_| "vault_unavailable")?;
    let after=std::fs::symlink_metadata(path).map_err(|_| "vault_unavailable")?;
    if bytes.len() as u64!=before.len() || before.dev()!=after.dev() || before.ino()!=after.ino() || before.len()!=after.len() || before.mtime()!=after.mtime() || before.mtime_nsec()!=after.mtime_nsec() {return Err("vault_unavailable");}
    Ok(format!("{:x}",Sha256::digest(&bytes)))
}
/// Recheck immediately before every effect, while the owner generation mutex is held.
/// `trusted_manifest` comes exclusively from the verified live VaultPackage.
pub(crate) fn verify_peer(stream:&UnixStream, trusted_manifest:&str)->Result<(),&'static str> {
    if trusted_manifest.len()!=64 || !trusted_manifest.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) {return Err("vault_unavailable");}
    let pid=peer_pid(stream)?; let first=process(pid)?;
    let path=Path::new(GUI);
    if path.canonicalize().ok().as_deref()!=Some(path) {return Err("vault_unavailable");}
    let cpath=CString::new(GUI).map_err(|_| "vault_unavailable")?;
    let rule=CString::new(REQUIREMENT).map_err(|_| "vault_unavailable")?;
    if unsafe {gamja_upgrade_code(pid,cpath.as_ptr(),rule.as_ptr())}!=0 {return Err("vault_unavailable");}
    let url=CFURL::from_path(path, true).ok_or("vault_unavailable")?;
    let requirement=SecRequirement::from_str(REQUIREMENT).map_err(|_| "vault_unavailable")?;
    let app=SecStaticCode::from_path(&url,Flags::NONE).map_err(|_| "vault_unavailable")?;
    app.check_validity(Flags::STRICT_VALIDATE|Flags::CHECK_ALL_ARCHITECTURES|Flags::CHECK_NESTED_CODE|Flags::RESTRICT_SYMLINKS,&requirement).map_err(|_| "vault_unavailable")?;
    let embedded=path.join("Contents/Resources/gamja-core/core-manifest.json");
    if manifest_hash(&embedded)?!=trusted_manifest {return Err("vault_unavailable");}
    app.check_validity(Flags::STRICT_VALIDATE|Flags::CHECK_ALL_ARCHITECTURES|Flags::CHECK_NESTED_CODE|Flags::RESTRICT_SYMLINKS,&requirement).map_err(|_| "vault_unavailable")?;
    if peer_pid(stream)?!=pid || process(pid)?!=first
        || unsafe {gamja_upgrade_code(pid,cpath.as_ptr(),rule.as_ptr())}!=0 {return Err("vault_unavailable");}
    Ok(())
}

#[derive(serde::Deserialize,serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    pub version:u8, pub operation:String, pub owner_id:u64, pub owner_epoch:String,
    pub item_id:String, pub expected_revision:Option<u64>, pub command_id:Option<String>,
    pub website:Option<String>, pub username:Option<String>,
    pub draft_id:Option<String>,pub row:Option<u64>,pub offset:Option<u64>,pub limit:Option<u64>,
    pub card_metadata:Option<crate::vault_card::CardMetadata>,
    pub handoff_id:Option<String>,
}
impl Request {
    pub(crate) fn valid(&self)->bool {
        let hex=|s:&str,n:usize|s.len()==n && s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b));
        self.version==1 && self.owner_id>0 && hex(&self.owner_epoch,64) && hex(&self.item_id,32)
            && (self.operation.starts_with("csv_") || (self.draft_id.is_none() && self.row.is_none() && self.offset.is_none() && self.limit.is_none()))
            && (self.operation.starts_with("card_") || self.card_metadata.is_none())
            && (self.operation.starts_with("profile_") || self.handoff_id.is_none())
            && match self.operation.as_str() {
                "profile_open_and_fill"|"profile_close"|"profile_park" => self.handoff_id.as_deref().is_some_and(|s|hex(s,32)) && self.item_id=="00000000000000000000000000000000" && self.expected_revision.is_none() && self.command_id.is_none() && self.website.is_none() && self.username.is_none(),
                "status" => self.expected_revision.is_none() && self.command_id.is_none() && self.website.is_none() && self.username.is_none(),
                "delete" => self.expected_revision.is_some() && self.command_id.as_deref().is_some_and(|s|hex(s,32)) && self.website.is_none() && self.username.is_none(),
                "replace"|"open_and_fill" => self.expected_revision.is_some() && self.command_id.as_deref().is_some_and(|s|hex(s,32))
                    && self.website.as_deref().is_some_and(|s|crate::vault::login_origin(s).is_ok())
                    && self.username.as_deref().is_some_and(crate::vault::valid_username),
                "close_browser" => self.expected_revision.is_none() && self.command_id.as_deref().is_some_and(|s|hex(s,32)) && self.website.is_none() && self.username.is_none(),
                "card_open_and_fill" => self.expected_revision.is_some_and(|v|v>0) && self.command_id.as_deref().is_some_and(|s|hex(s,32)) && self.card_metadata.as_ref().is_some_and(|m|m.valid(false)) && self.website.as_deref().is_some_and(|s|crate::vault::login_origin(s).ok().as_deref()==Some(s)) && self.username.is_none(),
                "card_create" => self.expected_revision==Some(0) && self.command_id.as_deref().is_some_and(|s|hex(s,32)) && self.card_metadata.as_ref().is_some_and(|m|m.valid(true) && m.last4.is_none()) && self.website.is_none() && self.username.is_none(),
                "card_update" => self.expected_revision.is_some_and(|v|v>0) && self.command_id.as_deref().is_some_and(|s|hex(s,32)) && self.card_metadata.as_ref().is_some_and(|m|m.valid(false)) && self.website.is_none() && self.username.is_none(),
                "card_status" => self.expected_revision.is_none() && self.command_id.is_none() && self.card_metadata.as_ref().is_some_and(|m|m.valid(true)) && self.website.is_none() && self.username.is_none(),
                "card_delete" => self.expected_revision.is_some() && self.command_id.as_deref().is_some_and(|s|hex(s,32)) && self.card_metadata.is_none() && self.website.is_none() && self.username.is_none(),
                "csv_choose" => self.draft_id.is_none() && self.row.is_none() && self.offset.is_none() && self.limit.is_none() && self.command_id.is_none() && self.expected_revision.is_none() && self.website.is_none() && self.username.is_none(),
                "csv_page" => self.draft_id.as_deref().is_some_and(|s|hex(s,32)) && self.row.is_none() && self.offset.is_some() && self.limit.is_some_and(|v|(1..=50).contains(&v)) && self.command_id.is_none() && self.expected_revision.is_none() && self.website.is_none() && self.username.is_none(),
                "csv_cancel" => self.draft_id.as_deref().is_some_and(|s|hex(s,32)) && self.row.is_none() && self.offset.is_none() && self.limit.is_none() && self.command_id.is_none() && self.expected_revision.is_none() && self.website.is_none() && self.username.is_none(),
                "csv_store_row" => self.draft_id.as_deref().is_some_and(|s|hex(s,32)) && self.row.is_some_and(|v|v>=2) && self.offset.is_none() && self.limit.is_none() && self.command_id.as_deref().is_some_and(|s|hex(s,32)) && self.expected_revision==Some(0) && self.website.is_none() && self.username.is_none(),
                "csv_skip_row" => self.draft_id.as_deref().is_some_and(|s|hex(s,32)) && self.row.is_some_and(|v|v>=2) && self.offset.is_none() && self.limit.is_none() && self.command_id.is_none() && self.expected_revision.is_none() && self.website.is_none() && self.username.is_none(),
                "csv_reconcile" => self.draft_id.as_deref().is_some_and(|s|hex(s,32)) && self.row.is_some_and(|v|v>=2) && self.offset.is_none() && self.limit.is_none() && self.command_id.as_deref().is_some_and(|s|hex(s,32)) && self.expected_revision.is_none() && self.website.is_none() && self.username.is_none(),
                _=>false,
            }
    }
}

use crate::auth_broker::{AuthBroker,SyntheticAuthTransport};
use crate::credential::CredentialBackend;
use std::sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}};
use std::time::{Duration,Instant};
use zeroize::{Zeroize,Zeroizing};

pub(crate) fn active(stream:&UnixStream)->bool {
    let mut fd=libc::pollfd{fd:stream.as_raw_fd(),events:libc::POLLIN,revents:0};
    if (unsafe{libc::poll(&mut fd,1,0)})<0 || fd.revents & (libc::POLLHUP|libc::POLLERR|libc::POLLNVAL)!=0 {return false;}
    if fd.revents & libc::POLLIN!=0 {
        let mut byte=0u8;
        if unsafe{libc::recv(fd.fd,&mut byte as *mut _ as *mut _,1,libc::MSG_PEEK|libc::MSG_DONTWAIT)}==0 {return false;}
    }
    true
}
fn read_csv_frame(stream:&mut UnixStream,stop:&AtomicBool)->Result<Zeroizing<Vec<u8>>,()> {
    use std::io::Read;
    let deadline=Instant::now()+Duration::from_secs(300);
    fn exact(stream:&mut UnixStream,stop:&AtomicBool,deadline:Instant,output:&mut [u8])->Result<(),()> {
        let mut offset=0;
        while offset<output.len() {
            if stop.load(Ordering::SeqCst) {return Err(());}
            let remaining=deadline.checked_duration_since(Instant::now()).ok_or(())?;
            let mut fd=libc::pollfd{fd:stream.as_raw_fd(),events:libc::POLLIN,revents:0};
            let n=unsafe{libc::poll(&mut fd,1,remaining.as_millis().min(100).max(1) as i32)};
            if n<0 || (n>0 && fd.revents & (libc::POLLERR|libc::POLLNVAL)!=0) {return Err(());}
            if n==0 {continue;}let read=stream.read(&mut output[offset..]).map_err(|_|())?;if read==0{return Err(());}offset+=read;
        }
        Ok(())
    }
    let mut header=[0u8;4];exact(stream,stop,deadline,&mut header)?;
    let size=u32::from_be_bytes(header) as usize;
    if size==0 || size>=crate::vault_csv::MAX_FILE_BYTES {return Err(());}
    let mut bytes=Zeroizing::new(vec![0u8;size]);exact(stream,stop,deadline,&mut bytes)?;Ok(bytes)
}

// Fixed stage only; recording failure never changes the control result.
fn park_failure(request:&Request,config:&crate::vault::VaultConfig,stage:crate::vault_browser::FailureStage) {
    if request.operation=="profile_park" {crate::vault_browser::record_failure(&config.directory,stage);}
}

fn profile_execute<B:CredentialBackend,T:SyntheticAuthTransport>(stream:&UnixStream,broker:&Arc<Mutex<AuthBroker<B,T>>>,stop:&Arc<AtomicBool>,request:&Request,diagnostic_config:&crate::vault::VaultConfig)->Result<serde_json::Value,()> {
    let id=request.handoff_id.as_deref().ok_or(())?;
    let (mut handoff,mut session,config)=broker.lock().map_err(|_|{park_failure(request,diagnostic_config,crate::vault_browser::FailureStage::ParkPrepare);()})?.vault_profile_take(stream,request.owner_id,&request.owner_epoch,id,matches!(request.operation.as_str(),"profile_close"|"profile_park")).map_err(|_|{park_failure(request,diagnostic_config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
    let outcome=(|| -> Result<(),()> {
        if request.operation=="profile_park" {
            let proof=handoff.grant.proof();
            let id={let custody=handoff.custody.as_mut().ok_or_else(||{park_failure(request,&config,crate::vault_browser::FailureStage::ParkState);()})?;if custody.phase!="sealed" || handoff.state!="active" {park_failure(request,&config,crate::vault_browser::FailureStage::ParkState);return Err(());}custody.phase="unknown";custody.id.clone()};
            let browser=handoff.browser.as_mut().ok_or_else(||{park_failure(request,&config,crate::vault_browser::FailureStage::ParkState);()})?;
            let pre=(|| -> Result<_,()> {
                browser.authorize_peer(stream).map_err(|_|())?;
                let manifest=browser.checked_manifest().map_err(|_|())?;
                let lease=browser.profile_lease().ok_or(())?;
                {let auth=broker.lock().map_err(|_|())?;let locked=lease.lock().map_err(|_|())?;
                    auth.vault_profile_effect_guard(stream,request.owner_id,&request.owner_epoch,&handoff.realm,&manifest,&proof,&locked).map_err(|_|())?;}
                Ok((manifest,lease))
            })();
            let (manifest,lease)=match pre {Ok(value)=>value,Err(())=>{browser.record_park_guard_failure();return Err(());}};
            browser.park_targets().map_err(|_|())?;
            let post=(|| -> Result<(),()> {
                let auth=broker.lock().map_err(|_|())?;let locked=lease.lock().map_err(|_|())?;
                auth.vault_profile_effect_guard(stream,request.owner_id,&request.owner_epoch,&handoff.realm,&manifest,&proof,&locked).map_err(|_|())?;
                crate::vault_profile::persist_parked(&proof,config.directory.parent().ok_or(())?,request.owner_id,&request.owner_epoch,&id,&locked).map_err(|_|())?;
                auth.vault_profile_effect_guard(stream,request.owner_id,&request.owner_epoch,&handoff.realm,&manifest,&proof,&locked).map_err(|_|())
            })();
            if post.is_err(){browser.record_park_guard_failure();return Err(());}
            let mut random=[0u8;16];getrandom::fill(&mut random).map_err(|_|{park_failure(request,&config,crate::vault_browser::FailureStage::ParkToken);()})?;let token=random.iter().map(|b|format!("{b:02x}")).collect::<String>();
            let custody=handoff.custody.as_mut().ok_or_else(||{park_failure(request,&config,crate::vault_browser::FailureStage::ParkState);()})?;custody.context_id=Some(token);custody.phase="parked";return Ok(());
        }
        if request.operation=="profile_close" {
            if matches!(handoff.state,"prepared"|"unknown") && !handoff.attempted && handoff.browser.is_none() {handoff.state="closed";handoff.fill_status="not_started";return Ok(());}
            if handoff.state=="closed" {return Ok(());}
            let browser=handoff.browser.as_mut().ok_or(())?;
            browser.authorize_peer(stream).map_err(|_|())?;
            browser.shutdown_confirmed().map_err(|_|())?;
            handoff.browser.take();handoff.state="closed";return Ok(());
        }
        if request.operation!="profile_open_and_fill" || stop.load(Ordering::SeqCst) || handoff.cancel.load(Ordering::SeqCst) {return Err(());}
        let manifest=session.reusable_manifest().map_err(|_|())?;
        let mut nonce=[0u8;16];getrandom::fill(&mut nonce).map_err(|_|())?;
        let command=nonce.iter().map(|b|format!("{b:02x}")).collect::<String>();
        let mut browser=if handoff.state=="prepared" {
            match handoff.start_browser(&config,&manifest,&command,stream,stop.clone()) {
                Ok(browser)=>browser,
                Err(crate::vault_browser::StartFailure::ConfirmedClosed(proof))=>{
                    if proof.manifest==manifest && proof.item==handoff.grant.record.item_id && proof.command==command {
                        let auth=broker.lock().map_err(|_|())?;
                        if auth.vault_realm(request.owner_id,&request.owner_epoch)==Some(handoff.realm.as_str()) && !handoff.cancel.load(Ordering::SeqCst) && active(stream) && handoff.grant.validate(config.directory.parent().ok_or(())?,request.owner_id,&request.owner_epoch).is_ok() && proof.browser.authorize_peer(stream).is_ok() {
                            handoff.state="closed";handoff.fill_status="unknown";
                        }
                    }
                    return Err(());
                },
                Err(_)=>return Err(()),
            }
        } else if handoff.state=="active" && handoff.fill_status=="not_filled" {
            let mut browser=handoff.browser.take().ok_or(())?;
            match browser.rearm(&handoff.grant.record.item_id,&command,handoff.grant.record.secret_revision,&handoff.grant.record.origin,stream,stop.clone()) {
                Ok(())=>browser,
                Err("vault_origin_changed")=>{browser.quiet().map_err(|_|())?;browser.disarm();handoff.browser=Some(browser);handoff.fill_status="not_filled";return Ok(());},
                Err(_)=>{browser.mark_unknown();handoff.browser=Some(browser);return Err(());},
            }
        } else {return Err(());};
        if !browser.has_context() {browser.quiet().map_err(|_|())?;browser.disarm();handoff.browser=Some(browser);handoff.state="active";handoff.fill_status="not_filled";return Ok(());}
        let lease=browser.profile_lease().ok_or(())?;let proof=handoff.grant.proof();
        let password={
            let auth=broker.lock().map_err(|_|())?;let locked=lease.lock().map_err(|_|())?;
            auth.vault_profile_effect_guard(stream,request.owner_id,&request.owner_epoch,&handoff.realm,&manifest,&proof,&locked).map_err(|_|())?;
            session.fill_secret(&*auth,request.owner_id,&request.owner_epoch,&proof.record.item_id,proof.record.secret_revision,&proof.record.origin,&proof.record.username).map_err(|_|())?
        };
        let writer_broker=broker.clone();let writer_peer=stream.try_clone().map_err(|_|())?;let writer_stop=stop.clone();let writer_cancel=handoff.cancel.clone();
        let owner=request.owner_id;let epoch=request.owner_epoch.clone();let realm=handoff.realm.clone();let writer_manifest=manifest.clone();let writer_proof=proof.clone();let writer_lease=lease.clone();
        let guard:crate::vault_proxy_pump::WriteGuard=Box::new(move |pipe,frame| {
            if writer_stop.load(Ordering::SeqCst) || writer_cancel.load(Ordering::SeqCst) {return Err("vault_unavailable");}
            let auth=writer_broker.lock().map_err(|_|"vault_unavailable")?;let lease=writer_lease.lock().map_err(|_|"vault_unavailable")?;
            auth.vault_profile_effect_guard(&writer_peer,owner,&epoch,&realm,&writer_manifest,&writer_proof,&lease)?;
            if writer_stop.load(Ordering::SeqCst) || writer_cancel.load(Ordering::SeqCst) {return Err("vault_unavailable");}
            pipe.write_all(frame).and_then(|_|pipe.write_all(&[0])).map_err(|_|"vault_unavailable")
        });
        let state=browser.fill_profile(&proof.record.username,&password,guard);drop(password);
        if state=="unknown" {browser.mark_unknown();handoff.state="unknown";} else {handoff.state="active";}
        handoff.fill_status=state;handoff.browser=Some(browser);Ok(())
    })();
    if outcome.is_err() && handoff.state!="closed" {handoff.state="unknown";handoff.fill_status="unknown";}
    if handoff.state=="closed" {handoff.grant.proxy_secret.zeroize();}
    let parked=if request.operation=="profile_park" && outcome.is_ok() {Some(serde_json::json!({"version":1,"status":"parked","handoff_id":id,"profile_id":handoff.grant.record.profile_id,"profile_revision":handoff.grant.record.profile_revision,"fill_status":handoff.fill_status}))}else{None};
    let restored=broker.lock().map_err(|_|{if outcome.is_ok(){park_failure(request,&config,crate::vault_browser::FailureStage::ParkRestore);}()})?.vault_profile_restore(stream,request.owner_id,&request.owner_epoch,handoff,session).map_err(|_|{if outcome.is_ok(){park_failure(request,&config,crate::vault_browser::FailureStage::ParkRestore);}()})?;
    Ok(parked.unwrap_or(restored))
}

fn serve_one<B:CredentialBackend,T:SyntheticAuthTransport>(mut stream:UnixStream, broker:&Arc<Mutex<AuthBroker<B,T>>>, config:&crate::vault::VaultConfig, stop:&Arc<AtomicBool>)->Result<(),()> {
    let deadline=Instant::now()+Duration::from_secs(8);
    let bytes=crate::stable_broker::read_frame(&mut stream,deadline)?;
    let request:Request=serde_json::from_slice(&bytes).map_err(|_|())?;
    if stop.load(Ordering::SeqCst) || !request.valid() {return Err(());}
    if request.operation=="close_browser" {
        broker.lock().map_err(|_|())?.vault_close_prepare(&stream,&request).map_err(|_|())?;
        crate::stable_broker::write_frame(&mut stream,br#"{"version":1,"state":"ready"}"#,deadline)?;
        let commit=crate::stable_broker::read_frame(&mut stream,Instant::now()+Duration::from_secs(30))?;
        if commit.as_slice()!=b"commit" || stop.load(Ordering::SeqCst) || !active(&stream) {return Err(());}
        let result=broker.lock().map_err(|_|())?.vault_browser_close(&stream,&request).map_err(|_|())?;
        let reply=serde_json::json!({"version":1,"status":result.status,"revision":result.revision,"command_id":result.command_id});
        return crate::stable_broker::write_frame(&mut stream,&serde_json::to_vec(&reply).map_err(|_|())?,Instant::now()+Duration::from_secs(8));
    }
    let (realm,existing)=broker.lock().map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?.vault_snapshot(&stream,&request).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
    // Spawn, signature/resource verification and socket reads do not hold the Owner mutex.
    if let Some(manifest)=existing {
        verify_peer(&stream,&manifest).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
        if stop.load(Ordering::SeqCst) || !active(&stream) {park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);return Err(());}
        broker.lock().map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?.vault_validate_existing(&request,&realm,&manifest).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
    } else {
        let session=crate::vault::VaultSession::start(&config.core,&config.engine,&config.engine_hash,config.engine_pid,&config.directory,&realm).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
        session.authorize_peer(&stream).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
        if stop.load(Ordering::SeqCst) || !active(&stream) {park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);return Err(());}
        broker.lock().map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?.vault_install(&request,&realm,session).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
    }
    if request.operation.starts_with("profile_") {
        let ready=broker.lock().map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?.vault_profile_view(&stream,request.owner_id,&request.owner_epoch,request.handoff_id.as_deref().ok_or(())?,matches!(request.operation.as_str(),"profile_close"|"profile_park")).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
        crate::stable_broker::write_frame(&mut stream,&serde_json::to_vec(&ready).map_err(|_|())?,deadline).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
        let commit=crate::stable_broker::read_frame(&mut stream,Instant::now()+Duration::from_secs(300)).map_err(|_|{park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);()})?;
        if commit.as_slice()!=b"commit" || stop.load(Ordering::SeqCst) || !active(&stream) {park_failure(&request,config,crate::vault_browser::FailureStage::ParkPrepare);return Err(());}
        let result=profile_execute(&stream,broker,stop,&request,config)?;
        return crate::stable_broker::write_frame(&mut stream,&serde_json::to_vec(&result).map_err(|_|())?,Instant::now()+Duration::from_secs(8)).map_err(|_|{if result.get("status").and_then(serde_json::Value::as_str)==Some("parked"){park_failure(&request,config,crate::vault_browser::FailureStage::ParkReply);}()});
    }
    if request.operation.starts_with("card_") {broker.lock().map_err(|_|())?.vault_card_preflight(&stream,stop,&request).map_err(|_|())?;}
    crate::stable_broker::write_frame(&mut stream,br#"{"version":1,"state":"ready"}"#,deadline)?;
    if request.operation.starts_with("card_") && request.operation!="card_open_and_fill" {
        let input=if matches!(request.operation.as_str(),"card_create"|"card_update") {
            let raw=crate::stable_broker::read_frame(&mut stream,Instant::now()+Duration::from_secs(300))?;
            Some(serde_json::from_slice::<crate::vault_card::CardInput>(&raw).map_err(|_|())?)
        } else {
            let commit=crate::stable_broker::read_frame(&mut stream,Instant::now()+Duration::from_secs(30))?;
            if commit.as_slice()!=b"commit" {return Err(());}None
        };
        if stop.load(Ordering::SeqCst) || !active(&stream) {return Err(());}
        let result=broker.lock().map_err(|_|())?.vault_card_native(&stream,stop,&request,input.as_ref()).map_err(|_|())?;
        return crate::stable_broker::write_frame(&mut stream,&serde_json::to_vec(&result).map_err(|_|())?,Instant::now()+Duration::from_secs(8));
    }
    if request.operation.starts_with("csv_") {
        let raw=if request.operation=="csv_choose" {Some(read_csv_frame(&mut stream,stop)?)} else {
            let commit=crate::stable_broker::read_frame(&mut stream,Instant::now()+Duration::from_secs(30))?;
            if commit.as_slice()!=b"commit" {return Err(());}None
        };
        if stop.load(Ordering::SeqCst) || !active(&stream) {return Err(());}
        let mut auth=broker.lock().map_err(|_|())?;
        let result=match request.operation.as_str() {
            "csv_choose"=>auth.vault_csv_create(&stream,request.owner_id,&request.owner_epoch,raw.ok_or(())?),
            "csv_page"=>auth.vault_csv_page(&stream,request.owner_id,&request.owner_epoch,request.draft_id.as_deref().ok_or(())?,usize::try_from(request.offset.ok_or(())?).map_err(|_|())?,usize::try_from(request.limit.ok_or(())?).map_err(|_|())?),
            "csv_cancel"=>auth.vault_csv_cancel(&stream,request.owner_id,&request.owner_epoch,request.draft_id.as_deref().ok_or(())?),
            "csv_store_row"|"csv_reconcile"|"csv_skip_row"=>auth.vault_csv_row(&stream,stop,&request),
            _=>Err("vault_unavailable"),
        }.map_err(|_|())?;
        drop(auth);
        return crate::stable_broker::write_frame(&mut stream,&serde_json::to_vec(&result).map_err(|_|())?,Instant::now()+Duration::from_secs(8));
    }
    let secret=if request.operation=="replace" {
        let raw=crate::stable_broker::read_frame(&mut stream,Instant::now()+Duration::from_secs(300))?;
        let text=std::str::from_utf8(&raw).map_err(|_|())?;
        if text.is_empty() || text.len()>8192 {return Err(());}
        Some(Zeroizing::new(text.to_owned()))
    } else {
        let commit=crate::stable_broker::read_frame(&mut stream,Instant::now()+Duration::from_secs(30))?;
        if commit.as_slice()!=b"commit" {return Err(());}
        None
    };
    if stop.load(Ordering::SeqCst) || !active(&stream) {return Err(());}
    let result=match request.operation.as_str() {
        "open_and_fill"|"card_open_and_fill"=>{
            let (realm,browser_config,manifest,existing)=broker.lock().map_err(|_|())?.vault_browser_prepare(&stream,&request).map_err(|_|{crate::vault_browser::record_failure(&config.directory,crate::vault_browser::FailureStage::BrowserPrepare);()})?;
            let command=request.command_id.as_deref().ok_or(())?;let revision=request.expected_revision.ok_or(())?;let website=request.website.as_deref().ok_or(())?;
            let mut browser=if let Some(mut browser)=existing {
                match browser.rearm(&request.item_id,command,revision,website,&stream,stop.clone()) {
                    Ok(())|Err("vault_origin_changed")=>browser,
                    Err(_)=>{browser.record_cdp_failure();return Err(());},
                }
            } else {
                match crate::vault_browser::Browser::start(&browser_config,&manifest,&realm,&request.item_id,command,revision,website,&stream,stop.clone()) {
                    Ok(browser)=>browser,
                    Err(crate::vault_browser::StartFailure::ConfirmedClosed(proof))=>{
                        // Only the exact first window may create the existing close receipt.
                        // The original fill remains unknown regardless of receipt outcome.
                        if proof.manifest==manifest && proof.item==request.item_id && proof.command==command && !stop.load(Ordering::SeqCst) && active(&stream) {
                            if let Ok(mut auth)=broker.lock() {
                                if auth.vault_realm(request.owner_id,&request.owner_epoch)==Some(realm.as_str()) {
                                    let close=Request{version:1,operation:"close_browser".to_owned(),owner_id:request.owner_id,owner_epoch:request.owner_epoch.clone(),item_id:proof.item.clone(),expected_revision:None,command_id:Some(proof.command.clone()),website:None,username:None,draft_id:None,row:None,offset:None,limit:None,card_metadata:None,handoff_id:None};
                                    if auth.vault_browser_preserve(&stream,&request,proof.browser).is_ok() {
                                        let _=auth.vault_browser_close(&stream,&close);
                                    }
                                }
                            }
                        }
                        return Err(());
                    },
                    Err(crate::vault_browser::StartFailure::Unconfirmed)=>return Err(()),
                }
            };
            if request.operation=="card_open_and_fill" {browser.card=true;}
            if stop.load(Ordering::SeqCst) || !active(&stream) {return Err(());}
            if !browser.has_context() {
                browser.quiet().map_err(|_|())?;
                browser.disarm();
                broker.lock().map_err(|_|())?.vault_browser_preserve(&stream,&request,browser).map_err(|_|())?
            } else {
                if request.operation=="card_open_and_fill" {broker.lock().map_err(|_|())?.vault_browser_fill_card(&stream,stop,&request,browser).map_err(|_|())?} else {broker.lock().map_err(|_|())?.vault_browser_fill(&stream,stop,&request,browser).map_err(|_|())?}
            }
        },
        _=>broker.lock().map_err(|_|())?.vault_native_execute(&stream,stop,&request,secret.as_ref()).map_err(|_|())?,
    };
    let reply=if request.operation=="card_open_and_fill" {serde_json::json!({"version":1,"status":result.status,"revision":result.revision,"command_id":result.command_id,"last4":null})} else {serde_json::json!({"version":1,"status":result.status,"revision":result.revision,"command_id":result.command_id})};
    let sent=crate::stable_broker::write_frame(&mut stream,&serde_json::to_vec(&reply).map_err(|_|())?,Instant::now()+Duration::from_secs(8));
    if sent.is_err() && matches!(request.operation.as_str(),"open_and_fill"|"card_open_and_fill") {
        if let Ok(mut auth)=broker.lock() {auth.vault_browser_delivery_unknown(&request);}
    }
    sent
}

struct SocketGuard { path:std::path::PathBuf, dev:u64, ino:u64, armed:bool }
impl Drop for SocketGuard { fn drop(&mut self) {
    use std::os::unix::fs::FileTypeExt;
    if !self.armed {return;}
    if let Ok(m)=std::fs::symlink_metadata(&self.path) {
        if m.file_type().is_socket() && m.dev()==self.dev && m.ino()==self.ino && m.uid()==unsafe{libc::geteuid()} {let _=std::fs::remove_file(&self.path);}
    }
} }
// Runtime owns gamja-host.lock before calling Listener::start. Only the exact
// user-owned stale socket is removed; live or ambiguous endpoints remain.
fn clear_stale_endpoint(path:&Path)->Result<(),&'static str> {
    use std::os::unix::fs::FileTypeExt;
    let before=match std::fs::symlink_metadata(path) {
        Ok(m)=>m,
        Err(e) if e.kind()==std::io::ErrorKind::NotFound=>return Ok(()),
        Err(_)=>return Err("vault_unavailable"),
    };
    if !before.file_type().is_socket() || before.uid()!=unsafe{libc::geteuid()} || before.mode()&0o777!=0o600 {return Err("vault_unavailable");}
    for _ in 0..2 {
        match UnixStream::connect(path) {
            Err(e) if e.kind()==std::io::ErrorKind::ConnectionRefused=>{},
            _=>return Err("vault_unavailable"),
        }
        let now=std::fs::symlink_metadata(path).map_err(|_| "vault_unavailable")?;
        if !now.file_type().is_socket() || now.dev()!=before.dev() || now.ino()!=before.ino() || now.uid()!=before.uid() || now.mode()!=before.mode() {return Err("vault_unavailable");}
    }
    std::fs::remove_file(path).map_err(|_| "vault_unavailable")
}
pub(crate) struct Listener { stop:Arc<AtomicBool>, active:Arc<Mutex<Option<UnixStream>>>, thread:Option<std::thread::JoinHandle<()>>, expiry:Option<std::thread::JoinHandle<()>>, path:std::path::PathBuf, dev:u64, ino:u64 }
impl Listener {
    pub(crate) fn start<B:CredentialBackend,T:SyntheticAuthTransport>(app_data:&Path, broker:Arc<Mutex<AuthBroker<B,T>>>, config:crate::vault::VaultConfig)->Result<Self,&'static str> {
        use std::os::unix::fs::PermissionsExt;
        use std::os::unix::net::UnixListener;
        crate::platform::verify_directory(app_data).map_err(|_| "vault_unavailable")?;
        if app_data.canonicalize().ok().as_deref()!=Some(app_data) {return Err("vault_unavailable");}
        let path=app_data.join("vault-native.sock");
        clear_stale_endpoint(&path)?;
        let listener=UnixListener::bind(&path).map_err(|_| "vault_unavailable")?;
        let initial=std::fs::symlink_metadata(&path).map_err(|_| "vault_unavailable")?;
        let mut cleanup=SocketGuard{path:path.clone(),dev:initial.dev(),ino:initial.ino(),armed:true};
        std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).map_err(|_| "vault_unavailable")?;
        let meta=std::fs::symlink_metadata(&path).map_err(|_| "vault_unavailable")?;
        if meta.dev()!=initial.dev() || meta.ino()!=initial.ino() {return Err("vault_unavailable");}
        use std::os::unix::fs::FileTypeExt;
        if !meta.file_type().is_socket() || meta.uid()!=unsafe{libc::geteuid()} || meta.mode()&0o777!=0o600 {return Err("vault_unavailable");}
        listener.set_nonblocking(true).map_err(|_| "vault_unavailable")?;
        let stop=Arc::new(AtomicBool::new(false)); let flag=stop.clone();
        let active=Arc::new(Mutex::new(None::<UnixStream>));let running=active.clone();
        let expiry_flag=stop.clone();let expiry_broker=broker.clone();
        let expiry=std::thread::spawn(move || while !expiry_flag.load(Ordering::SeqCst) {
            if let Ok(mut auth)=expiry_broker.lock() {auth.vault_csv_expire();}
            std::thread::sleep(Duration::from_millis(500));
        });
        let thread=std::thread::spawn(move || { while !flag.load(Ordering::SeqCst) {
            match listener.accept() {
                Ok((stream,_))=>{
                    if let Ok(mut slot)=running.lock() {*slot=stream.try_clone().ok();}
                    let _=serve_one(stream,&broker,&config,&flag);
                    if let Ok(mut auth)=broker.lock() {auth.vault_unregister();}
                    if let Ok(mut slot)=running.lock() {slot.take();}
                },
                Err(e) if e.kind()==std::io::ErrorKind::WouldBlock=>std::thread::sleep(Duration::from_millis(50)),
                Err(_)=>break,
            }
        }
        if let Ok(mut auth)=broker.lock() {auth.vault_stop();}
        });
        cleanup.armed=false;
        Ok(Self{stop,active,thread:Some(thread),expiry:Some(expiry),path,dev:meta.dev(),ino:meta.ino()})
    }
}
impl Drop for Listener {
    fn drop(&mut self) {
        self.stop.store(true,Ordering::SeqCst);
        if let Ok(mut active)=self.active.lock() {if let Some(socket)=active.take(){let _=socket.shutdown(std::net::Shutdown::Both);}}
        if let Some(thread)=self.thread.take() { let _=thread.join(); }
        if let Some(expiry)=self.expiry.take() {let _=expiry.join();}
        if let Ok(m)=std::fs::symlink_metadata(&self.path) {
            use std::os::unix::fs::FileTypeExt;
            if m.file_type().is_socket() && m.dev()==self.dev && m.ino()==self.ino && m.uid()==unsafe{libc::geteuid()} {let _=std::fs::remove_file(&self.path);}
        }
    }
}

const CORE_REQUIREMENT: &str = "anchor = H\"12035B8BBAB3E028E3433EDF37449688B3930438\" and identifier \"top.gamja.credential-core\"";
fn trusted_core(stream:&UnixStream, core:&Path)->Result<Process,&'static str> {
    if !core.is_absolute() || core.canonicalize().ok().as_deref()!=Some(core) {return Err("vault_unavailable");}
    let pid=peer_pid(stream)?;let before=process(pid)?;
    use std::os::unix::ffi::OsStrExt;
    let path=CString::new(core.as_os_str().as_bytes()).map_err(|_| "vault_unavailable")?;
    let rule=CString::new(CORE_REQUIREMENT).map_err(|_| "vault_unavailable")?;
    if unsafe{gamja_upgrade_code(pid,path.as_ptr(),rule.as_ptr())}!=0 || process(pid)?!=before || peer_pid(stream)?!=pid {return Err("vault_unavailable");}
    Ok(before)
}
/// A Native GUI caller must verify the installed runtime pins immediately before
/// `begin_native` and again before `finish_native`. No credentials are in argv.
pub struct NativeExchange { stream:UnixStream, core:std::path::PathBuf, peer:Process, operation:String, revision:Option<u64>, command_id:Option<String> }
#[derive(serde::Deserialize,serde::Serialize)]
#[serde(deny_unknown_fields)]
pub struct NativeStatus { pub version:u8, pub status:String, pub revision:Option<u64>, pub command_id:Option<String> }
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileReady {version:u8,state:String,pub handoff_id:String,pub profile_id:String,pub profile_revision:u64,pub profile_status:String,pub fill_status:String,pub origin:String,pub username:String,pub profile_label:String,pub account_label:String}
pub struct ProfileExchange {stream:UnixStream,core:std::path::PathBuf,peer:Process,operation:String,pub ready:ProfileReady}
pub fn begin_profile(app_data:&Path,core:&Path,operation:&str,owner_id:u64,owner_epoch:&str,handoff_id:&str)->Result<ProfileExchange,&'static str> {
    let internal=match operation {"open_and_fill"=>"profile_open_and_fill","close"=>"profile_close","park"=>"profile_park",_=>return Err("vault_unavailable")};
    let request=Request{version:1,operation:internal.into(),owner_id,owner_epoch:owner_epoch.into(),item_id:"00000000000000000000000000000000".into(),expected_revision:None,command_id:None,website:None,username:None,draft_id:None,row:None,offset:None,limit:None,card_metadata:None,handoff_id:Some(handoff_id.into())};
    if !request.valid(){return Err("vault_unavailable");}crate::platform::verify_directory(app_data).map_err(|_|"vault_unavailable")?;
    let socket=app_data.join("vault-native.sock");use std::os::unix::fs::FileTypeExt;
    let m=std::fs::symlink_metadata(&socket).map_err(|_|"vault_unavailable")?;
    if !m.file_type().is_socket() || m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o777!=0o600{return Err("vault_unavailable");}
    let mut stream=UnixStream::connect(&socket).map_err(|_|"vault_unavailable")?;let peer=trusted_core(&stream,core)?;
    let deadline=Instant::now()+Duration::from_secs(8);crate::stable_broker::write_frame(&mut stream,&serde_json::to_vec(&request).map_err(|_|"vault_unavailable")?,deadline).map_err(|_|"vault_unavailable")?;
    let data=crate::stable_broker::read_frame(&mut stream,deadline).map_err(|_|"vault_unavailable")?;
    let ready:ProfileReady=serde_json::from_slice(&data).map_err(|_|"vault_unavailable")?;
    if ready.version!=1 || ready.state!="ready" || ready.handoff_id!=handoff_id || !ready.profile_id.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)) || ready.profile_id.len()!=32 || ready.profile_revision==0
        || !matches!(ready.profile_status.as_str(),"prepared"|"active"|"unknown"|"closed") || !matches!(ready.fill_status.as_str(),"not_started"|"filled"|"filled_password_only"|"not_filled"|"unknown"|"cancelled") || crate::vault::login_origin(&ready.origin).ok().as_deref()!=Some(ready.origin.as_str()) || !crate::vault::valid_username(&ready.username)
        || [&ready.profile_label,&ready.account_label].iter().any(|s|s.len()>512 || s.chars().any(char::is_control)) || trusted_core(&stream,core)?!=peer {return Err("vault_unavailable");}
    Ok(ProfileExchange{stream,core:core.to_owned(),peer,operation:internal.into(),ready})
}
impl ProfileExchange {
    pub fn cancellation_socket(&self)->Result<UnixStream,&'static str>{self.stream.try_clone().map_err(|_|"vault_unavailable")}
    pub fn finish(mut self)->Result<serde_json::Value,&'static str>{
        if trusted_core(&self.stream,&self.core)?!=self.peer{return Err("vault_unavailable");}
        let deadline=Instant::now()+Duration::from_secs(match self.operation.as_str() {"profile_open_and_fill"=>45,"profile_park"=>30,_=>8});
        crate::stable_broker::write_frame(&mut self.stream,b"commit",deadline).map_err(|_|"vault_unavailable")?;
        let data=crate::stable_broker::read_frame(&mut self.stream,deadline).map_err(|_|"vault_unavailable")?;
        #[derive(serde::Deserialize,serde::Serialize)] #[serde(deny_unknown_fields)]
        struct Safe {version:u8,status:String,handoff_id:String,profile_id:Option<String>,profile_revision:Option<u64>,fill_status:String}
        let value:Safe=serde_json::from_slice(&data).map_err(|_|"vault_unavailable")?;
        if value.version!=1 || value.handoff_id!=self.ready.handoff_id || !matches!(value.status.as_str(),"prepared"|"active"|"closed"|"parked"|"unknown"|"unavailable") || !matches!(value.fill_status.as_str(),"not_started"|"filled"|"filled_password_only"|"not_filled"|"unknown"|"cancelled") {return Err("vault_unavailable");}
        if value.status=="unavailable" {if value.profile_id.is_some() || value.profile_revision.is_some() || value.fill_status!="unknown" {return Err("vault_unavailable");}}
        else if value.profile_id.as_deref()!=Some(self.ready.profile_id.as_str()) || value.profile_revision!=Some(self.ready.profile_revision) {return Err("vault_unavailable");}
        serde_json::to_value(value).map_err(|_|"vault_unavailable")
    }
}

#[allow(clippy::too_many_arguments)]
pub fn begin_native(app_data:&Path,core:&Path, operation:&str, owner_id:u64, owner_epoch:&str,item_id:&str,expected_revision:Option<u64>,command_id:Option<&str>,website:Option<&str>,username:Option<&str>)->Result<NativeExchange,&'static str> {
    let request=Request{version:1,operation:operation.to_owned(),owner_id,owner_epoch:owner_epoch.to_owned(),item_id:item_id.to_owned(),expected_revision,command_id:command_id.map(str::to_owned),website:website.map(str::to_owned),username:username.map(str::to_owned),draft_id:None,row:None,offset:None,limit:None,card_metadata:None,handoff_id:None};
    if !request.valid() {return Err("vault_unavailable");}
    crate::platform::verify_directory(app_data).map_err(|_| "vault_unavailable")?;
    let socket=app_data.join("vault-native.sock");
    use std::os::unix::fs::FileTypeExt;
    let meta=std::fs::symlink_metadata(&socket).map_err(|_| "vault_unavailable")?;
    if !meta.file_type().is_socket() || meta.uid()!=unsafe{libc::geteuid()} || meta.mode()&0o777!=0o600 {return Err("vault_unavailable");}
    let mut stream=UnixStream::connect(&socket).map_err(|_| "vault_unavailable")?;
    let peer=trusted_core(&stream,core)?;
    let body=serde_json::json!({"version":1,"operation":operation,"owner_id":owner_id,"owner_epoch":owner_epoch,"item_id":item_id,"expected_revision":expected_revision,"command_id":command_id,"website":website,"username":username});
    let deadline=Instant::now()+Duration::from_secs(8);
    crate::stable_broker::write_frame(&mut stream,&serde_json::to_vec(&body).map_err(|_| "vault_unavailable")?,deadline).map_err(|_| "vault_unavailable")?;
    let reply=crate::stable_broker::read_frame(&mut stream,deadline).map_err(|_| "vault_unavailable")?;
    #[derive(serde::Deserialize)] #[serde(deny_unknown_fields)] struct Ready {version:u8,state:String}
    let ready:Ready=serde_json::from_slice(&reply).map_err(|_| "vault_unavailable")?;
    if ready.version!=1 || ready.state!="ready" || trusted_core(&stream,core)?!=peer {return Err("vault_unavailable");}
    Ok(NativeExchange{stream,core:core.to_owned(),peer,operation:operation.to_owned(),revision:expected_revision,command_id:command_id.map(str::to_owned)})
}
#[allow(clippy::too_many_arguments)]
pub fn begin_card(app_data:&Path,core:&Path,operation:&str,owner_id:u64,owner_epoch:&str,item_id:&str,expected_revision:Option<u64>,command_id:Option<&str>,metadata:Option<crate::vault_card::CardMetadata>,website:Option<&str>)->Result<NativeExchange,&'static str> {
    let operation=match operation {"create"=>"card_create","update"=>"card_update","status"=>"card_status","delete"=>"card_delete","open_and_fill"=>"card_open_and_fill",_=>return Err("vault_unavailable")};
    let request=Request{version:1,operation:operation.to_owned(),owner_id,owner_epoch:owner_epoch.to_owned(),item_id:item_id.to_owned(),expected_revision,command_id:command_id.map(str::to_owned),website:website.map(str::to_owned),username:None,draft_id:None,row:None,offset:None,limit:None,card_metadata:metadata,handoff_id:None};
    if !request.valid() {return Err("vault_unavailable");}
    crate::platform::verify_directory(app_data).map_err(|_| "vault_unavailable")?;
    let socket=app_data.join("vault-native.sock");use std::os::unix::fs::FileTypeExt;
    let m=std::fs::symlink_metadata(&socket).map_err(|_| "vault_unavailable")?;
    if !m.file_type().is_socket() || m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o777!=0o600 {return Err("vault_unavailable");}
    let mut stream=UnixStream::connect(&socket).map_err(|_| "vault_unavailable")?;let peer=trusted_core(&stream,core)?;
    let body=serde_json::to_vec(&request).map_err(|_| "vault_unavailable")?;let deadline=Instant::now()+Duration::from_secs(8);
    crate::stable_broker::write_frame(&mut stream,&body,deadline).map_err(|_| "vault_unavailable")?;
    let reply=crate::stable_broker::read_frame(&mut stream,deadline).map_err(|_| "vault_unavailable")?;
    #[derive(serde::Deserialize)] #[serde(deny_unknown_fields)] struct Ready {version:u8,state:String}
    let ready:Ready=serde_json::from_slice(&reply).map_err(|_| "vault_unavailable")?;
    if ready.version!=1 || ready.state!="ready" || trusted_core(&stream,core)?!=peer {return Err("vault_unavailable");}
    Ok(NativeExchange{stream,core:core.to_owned(),peer,operation:operation.to_owned(),revision:expected_revision,command_id:request.command_id})
}

#[allow(clippy::too_many_arguments)]
pub fn begin_csv(app_data:&Path,core:&Path,operation:&str,owner_id:u64,owner_epoch:&str,draft_id:Option<&str>,row:Option<u64>,offset:Option<u64>,limit:Option<u64>,item_id:Option<&str>,command_id:Option<&str>)->Result<NativeExchange,&'static str> {
    let request=Request{version:1,operation:operation.to_owned(),owner_id,owner_epoch:owner_epoch.to_owned(),item_id:item_id.unwrap_or("00000000000000000000000000000000").to_owned(),expected_revision:(operation=="csv_store_row").then_some(0),command_id:command_id.map(str::to_owned),website:None,username:None,draft_id:draft_id.map(str::to_owned),row,offset,limit,card_metadata:None,handoff_id:None};
    if !request.valid() {return Err("vault_unavailable");}
    crate::platform::verify_directory(app_data).map_err(|_| "vault_unavailable")?;
    let socket=app_data.join("vault-native.sock");use std::os::unix::fs::FileTypeExt;
    let meta=std::fs::symlink_metadata(&socket).map_err(|_| "vault_unavailable")?;
    if !meta.file_type().is_socket() || meta.uid()!=unsafe{libc::geteuid()} || meta.mode()&0o777!=0o600 {return Err("vault_unavailable");}
    let mut stream=UnixStream::connect(&socket).map_err(|_| "vault_unavailable")?;let peer=trusted_core(&stream,core)?;
    let body=serde_json::to_vec(&request).map_err(|_| "vault_unavailable")?;let deadline=Instant::now()+Duration::from_secs(8);
    crate::stable_broker::write_frame(&mut stream,&body,deadline).map_err(|_| "vault_unavailable")?;
    let reply=crate::stable_broker::read_frame(&mut stream,deadline).map_err(|_| "vault_unavailable")?;
    #[derive(serde::Deserialize)] #[serde(deny_unknown_fields)] struct Ready {version:u8,state:String}
    let ready:Ready=serde_json::from_slice(&reply).map_err(|_| "vault_unavailable")?;
    if ready.version!=1 || ready.state!="ready" || trusted_core(&stream,core)?!=peer {return Err("vault_unavailable");}
    Ok(NativeExchange{stream,core:core.to_owned(),peer,operation:operation.to_owned(),revision:request.expected_revision,command_id:request.command_id})
}
impl NativeExchange {
    pub fn finish_card(mut self,input:Option<crate::vault_card::NativeCardSecret>)->Result<serde_json::Value,&'static str> {
        if trusted_core(&self.stream,&self.core)?!=self.peer || !self.operation.starts_with("card_") {return Err("vault_unavailable");}
        let deadline=Instant::now()+Duration::from_secs(30);
        if matches!(self.operation.as_str(),"card_create"|"card_update") {
            let secret=input.ok_or("vault_unavailable")?;
            #[derive(serde::Serialize)] struct Wire<'a>{pan:&'a str,cvv:&'a str}
            let bytes=Zeroizing::new(serde_json::to_vec(&Wire{pan:secret.pan(),cvv:secret.cvv()}).map_err(|_| "vault_unavailable")?);
            crate::stable_broker::write_frame(&mut self.stream,&bytes,deadline).map_err(|_| "vault_unavailable")?;
        } else {
            if input.is_some(){return Err("vault_unavailable");}
            crate::stable_broker::write_frame(&mut self.stream,b"commit",deadline).map_err(|_| "vault_unavailable")?;
        }
        #[derive(serde::Deserialize,serde::Serialize)] #[serde(deny_unknown_fields)]
        struct Safe {version:u8,status:String,revision:Option<u64>,command_id:Option<String>,last4:Option<String>}
        let data=crate::stable_broker::read_frame(&mut self.stream,deadline).map_err(|_| "vault_unavailable")?;
        let result:Safe=serde_json::from_slice(&data).map_err(|_| "vault_unavailable")?;
        let allowed=match self.operation.as_str(){"card_create"|"card_update"=>matches!(result.status.as_str(),"stored"|"invalid_input"|"conflict"|"failed"|"unavailable"),"card_status"=>matches!(result.status.as_str(),"present"|"missing"|"deleted"|"metadata_mismatch"|"unavailable"),"card_delete"=>matches!(result.status.as_str(),"deleted"|"conflict"|"failed"|"unavailable"),"card_open_and_fill"=>matches!(result.status.as_str(),"filled"|"not_filled"|"unknown"),_=>false};
        if result.version!=1 || !allowed || (self.operation=="card_open_and_fill" && (result.revision!=self.revision || result.command_id!=self.command_id)) || result.command_id.as_deref().is_some_and(|s|s.len()!=32 || !s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)))
            || result.last4.as_deref().is_some_and(|s|s.len()!=4 || !s.bytes().all(|b|b.is_ascii_digit()))
            || (result.status=="stored" && (result.command_id!=self.command_id || result.revision!=self.revision.and_then(|v|v.checked_add(1)) || result.last4.is_none()))
            || (result.status=="invalid_input" && (result.command_id!=self.command_id || result.revision!=self.revision))
            || (result.status=="present" && result.last4.is_none())
            || (!matches!(result.status.as_str(),"stored"|"present") && result.last4.is_some()) {return Err("vault_unavailable");}
        serde_json::to_value(result).map_err(|_| "vault_unavailable")
    }
    pub fn finish_csv(mut self,input:Option<crate::vault_csv_import::OwnedCsvInput>)->Result<serde_json::Value,&'static str> {
        if trusted_core(&self.stream,&self.core)?!=self.peer || !self.operation.starts_with("csv_") {return Err("vault_unavailable");}
        let deadline=Instant::now()+Duration::from_secs(30);
        if self.operation=="csv_choose" {
            let input=input.ok_or("vault_unavailable")?;let bytes=input.bytes();
            if bytes.is_empty() || bytes.len()>=crate::vault_csv::MAX_FILE_BYTES {return Err("vault_unavailable");}
            self.stream.set_write_timeout(Some(Duration::from_secs(30))).map_err(|_| "vault_unavailable")?;
            self.stream.write_all(&(bytes.len() as u32).to_be_bytes()).and_then(|_|self.stream.write_all(bytes)).map_err(|_| "vault_unavailable")?;
        } else {
            if input.is_some() {return Err("vault_unavailable");}
            crate::stable_broker::write_frame(&mut self.stream,b"commit",deadline).map_err(|_| "vault_unavailable")?;
        }
        let reply=crate::stable_broker::read_frame(&mut self.stream,deadline).map_err(|_| "vault_unavailable")?;
        crate::vault_csv_import::safe_reply(&self.operation,&reply)
    }
    pub fn cancellation_socket(&self)->Result<UnixStream, &'static str> {self.stream.try_clone().map_err(|_| "vault_unavailable")}
    pub fn finish_native(mut self, secret:Option<crate::OwnerSecret>)->Result<NativeStatus,&'static str> {
        if trusted_core(&self.stream,&self.core)?!=self.peer {return Err("vault_unavailable");}
        let deadline=Instant::now()+Duration::from_secs(if self.operation=="open_and_fill" {45} else {8});
        if self.operation=="replace" {
            let secret=secret.ok_or("vault_unavailable")?;
            if secret.as_str().is_empty() || secret.as_str().len()>8192 {return Err("vault_unavailable");}
            crate::stable_broker::write_frame(&mut self.stream,secret.as_str().as_bytes(),deadline).map_err(|_| "vault_unavailable")?;
        } else if secret.is_some() {return Err("vault_unavailable");}
        else {crate::stable_broker::write_frame(&mut self.stream,b"commit",deadline).map_err(|_| "vault_unavailable")?;}
        let data=crate::stable_broker::read_frame(&mut self.stream,deadline).map_err(|_| "vault_unavailable")?;
        let reply:NativeStatus=serde_json::from_slice(&data).map_err(|_| "vault_unavailable")?;
        let allowed=match self.operation.as_str() {
            "status"=>matches!(reply.status.as_str(),"present"|"missing"|"deleted"|"unavailable"),
            "replace"=>matches!(reply.status.as_str(),"stored"|"conflict"|"failed"|"unavailable"),
            "delete"=>matches!(reply.status.as_str(),"deleted"|"conflict"|"failed"|"unavailable"),
            "open_and_fill"=>matches!(reply.status.as_str(),"filled"|"filled_password_only"|"not_filled"|"cancelled"|"unknown"|"unavailable"),
            "close_browser"=>matches!(reply.status.as_str(),"closed"|"unavailable"),_=>false,
        };
        if reply.version!=1 || !allowed
            || reply.command_id.as_ref().is_some_and(|v|v.len()!=32 || !v.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b)))
            || (matches!(reply.status.as_str(),"filled"|"filled_password_only") && (reply.revision!=self.revision || reply.command_id!=self.command_id))
            || (reply.status=="closed" && reply.command_id!=self.command_id) {return Err("vault_unavailable");}
        Ok(reply)
    }
}
