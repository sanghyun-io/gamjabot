//! Single-reader CDP transport for the approved, proxied profile handoff.
//! This module is not used by the legacy Vault browser transport.
use serde::Serialize;
use serde_json::Value;
use std::{collections::HashSet,io::{Read,Write},os::{fd::AsRawFd,unix::net::UnixStream},sync::{Arc,atomic::{AtomicBool,Ordering},mpsc::{self,Receiver,SyncSender}},thread::{self,JoinHandle},time::{Duration,Instant}};
use zeroize::{Zeroize,Zeroizing};

const MAX_PACKET:usize=262_144;
const MAX_CAPTURE_PACKET:usize=13_500_000;
const MAX_CONTEXTS:usize=256;
const MAX_INTERNAL:usize=128;
type PumpResult<T>=std::result::Result<T,&'static str>;
pub(crate) type WriteGuard=Box<dyn FnOnce(&mut UnixStream,&[u8])->PumpResult<()> + Send>;

/// Only these non-secret fields from context events may cross the pump boundary.
pub(crate) struct ContextEvent {pub session:String,pub name:String,pub frame:String,pub id:i64,pub unique_id:String}
pub(crate) struct DocumentResponse {pub session:String,pub frame:String,pub loader:String,pub origin:String,pub status:u16}
pub(crate) struct DocumentLoaded {pub session:String,pub frame:String,pub loader:String}
pub(crate) struct FrameStarted {pub session:String,pub frame:String}
pub(crate) struct PumpReply {pub response:SensitiveValue,pub contexts:Vec<ContextEvent>,pub documents:Vec<DocumentResponse>,pub loaded:Vec<DocumentLoaded>,pub started:Vec<FrameStarted>}
enum Work {
    Command {id:u64,frame:Zeroizing<Vec<u8>>,deadline:Instant,guard:Option<WriteGuard>,capture:bool,reply:SyncSender<PumpResult<PumpReply>>},
    Bind {session:String,reply:SyncSender<PumpResult<()>>},
    Unbind {session:String,reply:SyncSender<PumpResult<()>>},
}

/// The pump owns both pipe ends for its entire lifetime. A caller must never
/// retain or read/write duplicate pipe descriptors after construction.
pub(crate) struct ProxyPump {tx:SyncSender<Work>,cancel:Arc<AtomicBool>,thread:Option<JoinHandle<()>>}
impl ProxyPump {
    pub(crate) fn start(input:UnixStream,output:UnixStream,proxy_port:u16,secret:Zeroizing<String>,stop:Arc<AtomicBool>,cancel:Arc<AtomicBool>)->PumpResult<Self> {
        if proxy_port==0 || secret.is_empty() || secret.len()>256 || !secret.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'-'||b==b'_') {return Err("vault_unavailable");}
        input.set_write_timeout(Some(Duration::from_millis(100))).map_err(|_|"vault_unavailable")?;
        output.set_nonblocking(true).map_err(|_|"vault_unavailable")?;
        let (tx,rx)=mpsc::sync_channel(1);let flag=cancel.clone();
        let thread=thread::Builder::new().name("vault-profile-cdp".into()).spawn(move || {
            let _=run(input,output,rx,proxy_port,secret,&stop,&flag);
            flag.store(true,Ordering::SeqCst);
        }).map_err(|_|"vault_unavailable")?;
        Ok(Self{tx,cancel,thread:Some(thread)})
    }
    pub(crate) fn bind_session(&self,session:String,deadline:Instant)->PumpResult<()> {
        if session.is_empty() || session.len()>256 || self.cancel.load(Ordering::SeqCst) {return Err("vault_unavailable");}
        let (tx,rx)=mpsc::sync_channel(1);
        self.tx.try_send(Work::Bind{session,reply:tx}).map_err(|_|"vault_unavailable")?;
        match rx.recv_timeout(deadline.saturating_duration_since(Instant::now())) {Ok(value)=>value,Err(_)=>{self.cancel.store(true,Ordering::SeqCst);Err("vault_unavailable")}}
    }
    /// Release only the exact previously detached session. In-flight or
    /// partially received traffic poisons the pump instead of crossing grants.
    pub(crate) fn unbind_session(&self,session:String,deadline:Instant)->PumpResult<()> {
        if session.is_empty() || session.len()>256 || self.cancel.load(Ordering::SeqCst) {return Err("vault_unavailable");}
        let (tx,rx)=mpsc::sync_channel(1);
        self.tx.try_send(Work::Unbind{session,reply:tx}).map_err(|_|"vault_unavailable")?;
        match rx.recv_timeout(deadline.saturating_duration_since(Instant::now())) {Ok(value)=>value,Err(_)=>{self.cancel.store(true,Ordering::SeqCst);Err("vault_unavailable")}}
    }
    /// Caller supplies an already serialized CDP command in a zeroizing buffer.
    /// Any timeout poisons this transport: the command is never replayed.
    pub(crate) fn command(&self,id:u64,frame:Zeroizing<Vec<u8>>,deadline:Instant)->PumpResult<PumpReply> {self.command_guarded(id,frame,deadline,None)}
    /// Guard runs in the sole writer immediately before the frame write.
    pub(crate) fn command_guarded(&self,id:u64,frame:Zeroizing<Vec<u8>>,deadline:Instant,guard:Option<WriteGuard>)->PumpResult<PumpReply> {
        self.command_inner(id,frame,deadline,guard,false)
    }
    pub(crate) fn capture_guarded(&self,id:u64,frame:Zeroizing<Vec<u8>>,deadline:Instant,guard:WriteGuard)->PumpResult<PumpReply> {
        self.command_inner(id,frame,deadline,Some(guard),true)
    }
    fn command_inner(&self,id:u64,frame:Zeroizing<Vec<u8>>,deadline:Instant,guard:Option<WriteGuard>,capture:bool)->PumpResult<PumpReply> {
        if id==0 || id>=1_000_000_000 || frame.is_empty() || frame.len()>MAX_PACKET || self.cancel.load(Ordering::SeqCst) {return Err("vault_unavailable");}
        let (tx,rx)=mpsc::sync_channel(1);
        self.tx.try_send(Work::Command{id,frame,deadline,guard,capture,reply:tx}).map_err(|_|"vault_unavailable")?;
        match rx.recv_timeout(deadline.saturating_duration_since(Instant::now())) {Ok(value)=>value,Err(_)=>{self.cancel.store(true,Ordering::SeqCst);Err("vault_unavailable")}}
    }
    pub(crate) fn cancel(&self) {self.cancel.store(true,Ordering::SeqCst);}
}
impl Drop for ProxyPump {fn drop(&mut self){self.cancel();if let Some(t)=self.thread.take(){let _=t.join();}}}

fn wipe(value:Value) {match value {Value::String(mut s)=>s.zeroize(),Value::Array(v)=>for x in v {wipe(x)},Value::Object(v)=>for (mut k,x) in v {k.zeroize();wipe(x)},_=>{}}}
// Fetch events may contain page request data. Wipe even on early errors.
pub(crate) struct SensitiveValue(Option<Value>);
impl std::ops::Deref for SensitiveValue {type Target=Value;fn deref(&self)->&Value {self.0.as_ref().expect("value present")}}
impl SensitiveValue {fn take(&mut self)->Value {self.0.take().expect("value present")} pub(crate) fn into_value(mut self)->Value {self.take()}}
impl Drop for SensitiveValue {fn drop(&mut self){if let Some(value)=self.0.take(){wipe(value);}}}
#[derive(Serialize)] struct Resume<'a>{id:i64,method:&'static str,params:ResumeParams<'a>,#[serde(rename="sessionId")] session:&'a str}
#[derive(Serialize)] struct ResumeParams<'a>{#[serde(rename="requestId")] request:&'a str}
#[derive(Serialize)] struct Auth<'a>{id:i64,method:&'static str,params:AuthParams<'a>,#[serde(rename="sessionId")] session:&'a str}
#[derive(Serialize)] struct AuthParams<'a>{#[serde(rename="requestId")] request:&'a str,#[serde(rename="authChallengeResponse")] response:AuthResponse<'a>}
#[derive(Serialize)] struct AuthResponse<'a>{response:&'static str,#[serde(skip_serializing_if="Option::is_none")] username:Option<&'static str>,#[serde(skip_serializing_if="Option::is_none")] password:Option<&'a str>}
fn write_command<T:Serialize>(socket:&mut UnixStream,value:&T)->PumpResult<()> {
    let data=Zeroizing::new(serde_json::to_vec(value).map_err(|_|"vault_unavailable")?);
    if data.len()>MAX_PACKET {return Err("vault_unavailable");}
    socket.write_all(&data).and_then(|_|socket.write_all(&[0])).map_err(|_|"vault_unavailable")
}
fn is_expected_proxy(challenge:&Value,port:u16)->bool {
    if challenge.get("source").and_then(Value::as_str)!=Some("Proxy") || !challenge.get("scheme").and_then(Value::as_str).is_some_and(|s|s.eq_ignore_ascii_case("basic")) || challenge.get("realm").and_then(Value::as_str)!=Some("gamja") {return false;}
    let Some(origin)=challenge.get("origin").and_then(Value::as_str) else{return false;};
    let expected=format!("http://127.0.0.1:{port}");
    origin==expected || origin.strip_suffix('/').is_some_and(|value|value==expected)
}
fn bounded(s:&str)->bool {!s.is_empty() && s.len()<=256 && !s.chars().any(char::is_control)}

fn run(mut input:UnixStream,mut output:UnixStream,rx:Receiver<Work>,port:u16,secret:Zeroizing<String>,stop:&AtomicBool,cancel:&AtomicBool)->PumpResult<()> {
    let mut pending:Option<(u64,Option<String>,Instant,SyncSender<PumpResult<PumpReply>>,bool)>=None;
    let mut contexts=Vec::<ContextEvent>::new();let mut documents=Vec::<DocumentResponse>::new();let mut loaded=Vec::<DocumentLoaded>::new();let mut started=Vec::<FrameStarted>::new();let mut bound_session:Option<String>=None;
    let mut internal=HashSet::<i64>::new();let mut next_internal=2_000_000_000i64;let mut frame=Zeroizing::new(Vec::<u8>::new());
    while !stop.load(Ordering::SeqCst) && !cancel.load(Ordering::SeqCst) {
        match rx.try_recv() {
            Ok(Work::Bind{session,reply})=>{
                if bound_session.is_some() || pending.is_some() || !bounded(&session) {let _=reply.send(Err("vault_unavailable"));return Err("vault_unavailable");}
                bound_session=Some(session);let _=reply.send(Ok(()));
            },
            Ok(Work::Unbind{session,reply})=>{
                if bound_session.as_deref()!=Some(session.as_str()) || pending.is_some() || !internal.is_empty() || !frame.is_empty() {
                    let _=reply.send(Err("vault_unavailable"));return Err("vault_unavailable");
                }
                bound_session=None;contexts.clear();documents.clear();loaded.clear();started.clear();let _=reply.send(Ok(()));
            },
            Ok(Work::Command{id,frame:out,deadline,guard,capture,reply})=>{
                if pending.is_some() || Instant::now()>=deadline {let _=reply.send(Err("vault_unavailable"));return Err("vault_unavailable");}
                // Validate the envelope without retaining any caller parameters.
                let parsed:Value=serde_json::from_slice(&out).map_err(|_|"vault_unavailable")?;
                let valid=parsed.get("id").and_then(Value::as_u64)==Some(id) && parsed.get("method").and_then(Value::as_str).is_some_and(bounded)
                    && parsed.get("sessionId").map_or(true,|v|v.as_str()==bound_session.as_deref())
                    && (!capture || (parsed.get("method").and_then(Value::as_str)==Some("Page.captureScreenshot") && parsed.get("sessionId").and_then(Value::as_str)==bound_session.as_deref() && bound_session.is_some()));
                let expected_session=parsed.get("sessionId").and_then(Value::as_str).map(str::to_owned);
                wipe(parsed);if !valid {let _=reply.send(Err("vault_unavailable"));return Err("vault_unavailable");}
                if stop.load(Ordering::SeqCst) || cancel.load(Ordering::SeqCst) {return Err("vault_unavailable");}
                if let Some(guard)=guard {guard(&mut input,&out)?;} else {input.write_all(&out).and_then(|_|input.write_all(&[0])).map_err(|_|"vault_unavailable")?;}
                pending=Some((id,expected_session,deadline,reply,capture));
            },
            Err(mpsc::TryRecvError::Disconnected)=>return Ok(()),
            Err(mpsc::TryRecvError::Empty)=>{},
        }
        if pending.as_ref().is_some_and(|(_,_,deadline,_,_)|Instant::now()>=*deadline) {return Err("vault_unavailable");}
        let mut p=libc::pollfd{fd:output.as_raw_fd(),events:libc::POLLIN,revents:0};
        let result=unsafe{libc::poll(&mut p,1,25)};
        if result<0 || (result>0 && p.revents&(libc::POLLHUP|libc::POLLERR|libc::POLLNVAL)!=0) {return Err("vault_unavailable");}
        if result==0 {continue;}
        let mut chunk=Zeroizing::new([0u8;8192]);let n=match output.read(&mut *chunk) {Ok(0)=>return Err("vault_unavailable"),Ok(n)=>n,Err(e) if e.kind()==std::io::ErrorKind::WouldBlock=>continue,Err(_)=>return Err("vault_unavailable")};
        for byte in &chunk[..n] {
            if *byte!=0 {let limit=if pending.as_ref().is_some_and(|(_,_,_,_,capture)|*capture) {MAX_CAPTURE_PACKET}else{MAX_PACKET};if frame.len()>=limit {return Err("vault_unavailable");}frame.push(*byte);continue;}
            let frame_len=frame.len();
            let mut event=SensitiveValue(Some(serde_json::from_slice(&frame).map_err(|_|"vault_unavailable")?));frame.zeroize();frame.clear();
            if frame_len>MAX_PACKET && !pending.as_ref().is_some_and(|(id,session,_,_,capture)|*capture && event.get("id").and_then(Value::as_u64)==Some(*id) && event.get("sessionId").and_then(Value::as_str)==session.as_deref() && event.get("error").is_none()) {return Err("vault_unavailable");}
            if let Some(id)=event.get("id").and_then(Value::as_i64) {
                if internal.contains(&id) {
                    let valid=internal.remove(&id) && event.get("sessionId").and_then(Value::as_str)==bound_session.as_deref() && event.get("error").is_none();drop(event);if !valid{return Err("vault_unavailable");}continue;
                }
                if pending.as_ref().is_some_and(|(expected,_,_,_,_)|Some(*expected)==u64::try_from(id).ok()) {
                    let (_,expected_session,_,reply,_)=pending.take().ok_or("vault_unavailable")?;
                    if event.get("sessionId").and_then(Value::as_str)!=expected_session.as_deref() {drop(event);let _=reply.send(Err("vault_unavailable"));return Err("vault_unavailable");}
                    // A matched application-level CDP error is a response to this
                    // call, not loss of transport. The invalid-context probe
                    // deliberately requires one before checking the valid context.
                    if let Some(error)=event.get("error") {
                        let valid=event.get("result").is_none() && error.is_object() && error.get("code").and_then(Value::as_i64).is_some() && error.get("message").and_then(Value::as_str).is_some();
                        drop(event);let _=reply.send(Err("vault_unavailable"));
                        if !valid {return Err("vault_unavailable");}continue;
                    }
                    let _=reply.send(Ok(PumpReply{response:SensitiveValue(Some(event.take())),contexts:std::mem::take(&mut contexts),documents:std::mem::take(&mut documents),loaded:std::mem::take(&mut loaded),started:std::mem::take(&mut started)}));continue;
                }
                drop(event);return Err("vault_unavailable");
            }
            let method=event.get("method").and_then(Value::as_str).unwrap_or("");
            if matches!(method,"Fetch.requestPaused"|"Fetch.authRequired") {
                let session=event.get("sessionId").and_then(Value::as_str).ok_or("vault_unavailable")?;
                if Some(session)!=bound_session.as_deref() {drop(event);return Err("vault_unavailable");}
                let request=event.pointer("/params/requestId").and_then(Value::as_str).filter(|v|bounded(v)).ok_or("vault_unavailable")?;
                if internal.len()>=MAX_INTERNAL {drop(event);return Err("vault_unavailable");}
                let internal_id=next_internal;next_internal=next_internal.checked_sub(1).filter(|id|*id>=1_000_000_000).ok_or("vault_unavailable")?;
                if method=="Fetch.requestPaused" {
                    write_command(&mut input,&Resume{id:internal_id,method:"Fetch.continueRequest",params:ResumeParams{request},session})?;
                } else {
                    let allowed=event.pointer("/params/authChallenge").is_some_and(|v|is_expected_proxy(v,port));
                    let response=if allowed {AuthResponse{response:"ProvideCredentials",username:Some("gamja"),password:Some(secret.as_str())}} else {AuthResponse{response:"CancelAuth",username:None,password:None}};
                    write_command(&mut input,&Auth{id:internal_id,method:"Fetch.continueWithAuth",params:AuthParams{request,response},session})?;
                }
                internal.insert(internal_id);drop(event);continue;
            }
            if method=="Page.frameStartedLoading" && event.get("sessionId").and_then(Value::as_str)==bound_session.as_deref() {
                if let (Some(session),Some(frame_id))=(event.get("sessionId").and_then(Value::as_str),event.pointer("/params/frameId").and_then(Value::as_str)) {
                    if bounded(session)&&bounded(frame_id) {if started.len()>=MAX_CONTEXTS {drop(event);return Err("vault_unavailable");}started.push(FrameStarted{session:session.into(),frame:frame_id.into()});}
                }
            }
            if method=="Page.lifecycleEvent" && event.get("sessionId").and_then(Value::as_str)==bound_session.as_deref() && event.pointer("/params/name").and_then(Value::as_str)==Some("DOMContentLoaded") {
                if let (Some(session),Some(frame_id),Some(loader))=(event.get("sessionId").and_then(Value::as_str),event.pointer("/params/frameId").and_then(Value::as_str),event.pointer("/params/loaderId").and_then(Value::as_str)) {
                    if bounded(session)&&bounded(frame_id)&&bounded(loader) {if loaded.len()>=MAX_CONTEXTS {drop(event);return Err("vault_unavailable");}loaded.push(DocumentLoaded{session:session.into(),frame:frame_id.into(),loader:loader.into()});}
                }
            }
            if method=="Network.responseReceived" && event.get("sessionId").and_then(Value::as_str)==bound_session.as_deref() && event.pointer("/params/type").and_then(Value::as_str)==Some("Document") {
                if let (Some(session),Some(frame_id),Some(loader),Some(url),Some(status))=(event.get("sessionId").and_then(Value::as_str),event.pointer("/params/frameId").and_then(Value::as_str),event.pointer("/params/loaderId").and_then(Value::as_str),event.pointer("/params/response/url").and_then(Value::as_str),event.pointer("/params/response/status").and_then(Value::as_f64)) {
                    if bounded(session)&&bounded(frame_id)&&bounded(loader) && (100.0..=599.0).contains(&status) && status.fract()==0.0 {
                        if let Ok(origin)=crate::vault::login_origin(url) {if documents.len()>=MAX_CONTEXTS {drop(event);return Err("vault_unavailable");}documents.push(DocumentResponse{session:session.into(),frame:frame_id.into(),loader:loader.into(),origin,status:status as u16});}
                    }
                }
            }
            if method=="Runtime.executionContextCreated" && event.get("sessionId").and_then(Value::as_str)==bound_session.as_deref() {
                if let (Some(session),Some(name),Some(frame_id),Some(id),Some(unique_id))=(event.get("sessionId").and_then(Value::as_str),event.pointer("/params/context/name").and_then(Value::as_str),event.pointer("/params/context/auxData/frameId").and_then(Value::as_str),event.pointer("/params/context/id").and_then(Value::as_i64),event.pointer("/params/context/uniqueId").and_then(Value::as_str)) {
                    if bounded(session)&&bounded(name)&&bounded(frame_id)&&bounded(unique_id) {if contexts.len()>=MAX_CONTEXTS {drop(event);return Err("vault_unavailable");}contexts.push(ContextEvent{session:session.into(),name:name.into(),frame:frame_id.into(),id,unique_id:unique_id.into()});}
                }
            }
            drop(event);
        }
        chunk.zeroize();
    }
    Err("vault_unavailable")
}

#[cfg(test)]
mod tests {
    use super::*;
    fn send(s:&mut UnixStream,v:&Value) {let mut b=serde_json::to_vec(v).unwrap();b.push(0);s.write_all(&b).unwrap();}
    fn receive(s:&mut UnixStream)->Value {let mut b=Vec::new();loop {let mut one=[0];s.read_exact(&mut one).unwrap();if one[0]==0 {break;}b.push(one[0]);}serde_json::from_slice(&b).unwrap()}
    fn channel()->(ProxyPump,UnixStream,UnixStream) {
        let (input,from_pump)=UnixStream::pair().unwrap();let (output,to_pump)=UnixStream::pair().unwrap();
        from_pump.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let pump=ProxyPump::start(input,output,34567,Zeroizing::new("fixture-secret".into()),Arc::new(AtomicBool::new(false)),Arc::new(AtomicBool::new(false))).unwrap();
        (pump,from_pump,to_pump)
    }
    #[test]
    fn interleaved_context_and_proxy_only_auth() {
        let (pump,mut from,mut to)=channel();pump.bind_session("session".into(),Instant::now()+Duration::from_secs(1)).unwrap();
        let (ready_tx,ready_rx)=mpsc::sync_channel(1);
        let mock=thread::spawn(move || {
            send(&mut to,&serde_json::json!({"method":"Fetch.authRequired","sessionId":"session","params":{"requestId":"server","authChallenge":{"source":"Server","scheme":"basic","realm":"gamja","origin":"http://127.0.0.1:34567"}}}));
            let denied=receive(&mut from);assert_eq!(denied.pointer("/params/authChallengeResponse/response").and_then(Value::as_str),Some("CancelAuth"));assert!(denied.pointer("/params/authChallengeResponse/password").is_none());send(&mut to,&serde_json::json!({"id":denied["id"],"sessionId":"session","result":{}}));
            send(&mut to,&serde_json::json!({"method":"Fetch.authRequired","sessionId":"session","params":{"requestId":"proxy","authChallenge":{"source":"Proxy","scheme":"basic","realm":"gamja","origin":"http://127.0.0.1:34567"}}}));
            let allowed=receive(&mut from);assert_eq!(allowed.pointer("/params/authChallengeResponse/password").and_then(Value::as_str),Some("fixture-secret"));send(&mut to,&serde_json::json!({"id":allowed["id"],"sessionId":"session","result":{}}));
            send(&mut to,&serde_json::json!({"method":"Runtime.executionContextCreated","sessionId":"session","params":{"context":{"name":"world","id":7,"uniqueId":"unique","auxData":{"frameId":"frame"}}}}));
            ready_tx.send(()).unwrap();let request=receive(&mut from);send(&mut to,&serde_json::json!({"id":request["id"],"sessionId":"session","result":{"ok":true}}));
        });
        ready_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        let frame=Zeroizing::new(serde_json::to_vec(&serde_json::json!({"id":1,"method":"Page.enable","sessionId":"session","params":{}})).unwrap());
        let reply=pump.command(1,frame,Instant::now()+Duration::from_secs(2)).unwrap();
        assert_eq!(reply.contexts.len(),1);assert_eq!(reply.contexts[0].unique_id,"unique");assert_eq!(reply.response.pointer("/result/ok").and_then(Value::as_bool),Some(true));mock.join().unwrap();
    }
    #[test]
    fn paused_event_and_wrong_proxy_are_interleaved_with_command() {
        let (pump,mut from,mut to)=channel();pump.bind_session("session".into(),Instant::now()+Duration::from_secs(1)).unwrap();
        let mock=thread::spawn(move || {
            let outer=receive(&mut from);
            send(&mut to,&serde_json::json!({"method":"Fetch.requestPaused","sessionId":"session","params":{"requestId":"paused"}}));
            let resumed=receive(&mut from);assert_eq!(resumed["method"],"Fetch.continueRequest");
            send(&mut to,&serde_json::json!({"method":"Fetch.authRequired","sessionId":"session","params":{"requestId":"wrong","authChallenge":{"source":"Proxy","scheme":"basic","realm":"gamja","origin":"http://127.0.0.1:34568"}}}));
            let denied=receive(&mut from);assert_eq!(denied.pointer("/params/authChallengeResponse/response").and_then(Value::as_str),Some("CancelAuth"));assert!(denied.pointer("/params/authChallengeResponse/password").is_none());
            send(&mut to,&serde_json::json!({"id":resumed["id"],"sessionId":"session","result":{}}));
            send(&mut to,&serde_json::json!({"id":denied["id"],"sessionId":"session","result":{}}));
            send(&mut to,&serde_json::json!({"id":outer["id"],"sessionId":"session","result":{"ok":true}}));
        });
        let frame=Zeroizing::new(serde_json::to_vec(&serde_json::json!({"id":1,"method":"Page.enable","sessionId":"session","params":{}})).unwrap());
        assert_eq!(pump.command(1,frame,Instant::now()+Duration::from_secs(2)).unwrap().response.pointer("/result/ok").and_then(Value::as_bool),Some(true));mock.join().unwrap();
    }
    #[test]
    fn invalid_context_error_keeps_actor_for_valid_probe() {
        let (pump,mut from,mut to)=channel();pump.bind_session("session".into(),Instant::now()+Duration::from_secs(1)).unwrap();
        let mock=thread::spawn(move || {
            let invalid=receive(&mut from);
            assert_eq!(invalid.pointer("/params/uniqueContextId").and_then(Value::as_str),Some("invalid"));
            send(&mut to,&serde_json::json!({"id":invalid["id"],"sessionId":"session","error":{"code":-32000,"message":"fixture"}}));
            let valid=receive(&mut from);
            assert_eq!(valid.pointer("/params/uniqueContextId").and_then(Value::as_str),Some("valid"));
            send(&mut to,&serde_json::json!({"id":valid["id"],"sessionId":"session","result":{"result":{"value":true}}}));
        });
        let invalid=Zeroizing::new(serde_json::to_vec(&serde_json::json!({"id":1,"method":"Runtime.callFunctionOn","sessionId":"session","params":{"uniqueContextId":"invalid"}})).unwrap());
        assert!(pump.command(1,invalid,Instant::now()+Duration::from_secs(2)).is_err());
        let valid=Zeroizing::new(serde_json::to_vec(&serde_json::json!({"id":2,"method":"Runtime.callFunctionOn","sessionId":"session","params":{"uniqueContextId":"valid"}})).unwrap());
        assert_eq!(pump.command(2,valid,Instant::now()+Duration::from_secs(2)).unwrap().response.pointer("/result/result/value").and_then(Value::as_bool),Some(true));
        mock.join().unwrap();
    }
    #[test]
    fn explicit_cancel_prevents_later_command() {
        let (pump,_from,_to)=channel();pump.cancel();
        let frame=Zeroizing::new(serde_json::to_vec(&serde_json::json!({"id":1,"method":"Target.getTargets","params":{}})).unwrap());
        assert!(pump.command(1,frame,Instant::now()+Duration::from_secs(1)).is_err());
    }
    #[test]
    fn timeout_poisons_transport_without_replay() {
        let (pump,_from,_to)=channel();let frame=Zeroizing::new(serde_json::to_vec(&serde_json::json!({"id":1,"method":"Target.getTargets","params":{}})).unwrap());
        assert!(pump.command(1,frame,Instant::now()+Duration::from_millis(100)).is_err());
        let next=Zeroizing::new(serde_json::to_vec(&serde_json::json!({"id":2,"method":"Target.getTargets","params":{}})).unwrap());
        assert!(pump.command(2,next,Instant::now()+Duration::from_secs(1)).is_err());
    }
}
