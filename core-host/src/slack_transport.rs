//! Owner-bound Slack Socket Mode. Engine receives only validated event metadata.
#![cfg(all(target_os="macos",target_arch="aarch64"))]
use std::{collections::{HashMap,HashSet,VecDeque},fs::{File,OpenOptions},io::{Read,Write},os::{fd::{AsRawFd,FromRawFd},unix::fs::{MetadataExt,OpenOptionsExt}},path::{Path,PathBuf},sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}},time::Duration};
use serde::{Deserialize,Serialize};
use serde_json::{json,Value};
use futures_util::{SinkExt,StreamExt};
use tokio_tungstenite::tungstenite::{Message,protocol::WebSocketConfig};
use crate::{auth_broker::{AuthBroker,SyntheticAuthTransport},credential::CredentialBackend};
const ERR:&str="slack_unavailable";
const MAX_TEXT:usize=4000;
const MAX_REPLY:usize=32768;
const MAX_FRAME:usize=65536;
fn uuid(s:&str)->bool {s.len()==36 && s.bytes().enumerate().all(|(i,b)|if matches!(i,8|13|18|23){b==b'-'}else{b.is_ascii_digit()||(b'a'..=b'f').contains(&b)})}
fn slack_id(s:&str,prefix:u8)->bool {s.len()>=2 && s.len()<=64 && s.as_bytes()[0]==prefix && s.bytes().all(|b|b.is_ascii_uppercase()||b.is_ascii_digit())}
fn timestamp(s:&str)->bool {let mut p=s.split('.');p.next().is_some_and(|x|!x.is_empty() && x.len()<=20 && x.bytes().all(|b|b.is_ascii_digit())) && p.next().is_some_and(|x|!x.is_empty() && x.len()<=12 && x.bytes().all(|b|b.is_ascii_digit())) && p.next().is_none()}
fn bounded(s:&str,n:usize)->bool {!s.is_empty() && s.len()<=n && !s.bytes().any(|b|b.is_ascii_control())}
#[derive(Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Config {
 pub version:u8,pub owner_id:u64,pub owner_epoch:String,pub core_epoch:String,pub account_id:String,pub credential_revision:u64,
 pub expected_config_revision:u64,pub operation_id:String,pub workspace_id:String,pub app_id:String,pub channel_id:String,pub sender_ids:Vec<String>,
 pub agent_id:u64,pub project_id:u64,pub connection_id:u64,pub model_id:String,pub enabled:bool,
}
impl Config {
 fn valid(&self)->bool {self.version==1 && self.owner_id>0 && self.credential_revision>0 && uuid(&self.account_id) && uuid(&self.operation_id) && self.owner_epoch.len()==64 && self.core_epoch.len()==64 && slack_id(&self.workspace_id,b'T') && slack_id(&self.app_id,b'A') && (slack_id(&self.channel_id,b'C')||slack_id(&self.channel_id,b'G')) && !self.sender_ids.is_empty() && self.sender_ids.len()<=64 && self.sender_ids.iter().all(|s|slack_id(s,b'U')||slack_id(s,b'W')) && self.agent_id>0 && self.project_id>0 && self.connection_id>0 && bounded(&self.model_id,200)}
}
#[derive(Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {version:u8,revision:u64,config:Config}
#[derive(Clone,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct Intent {version:u8,owner_id:u64,account_id:String,expected_revision:u64,operation_id:String}
fn dir(path:&Path)->Result<File,&'static str>{let f=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(path).map_err(|_|ERR)?;let m=f.metadata().map_err(|_|ERR)?;if m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o077!=0{return Err(ERR)}Ok(f)}
fn filename(path:&Path)->Result<std::ffi::CString,&'static str> {let name=path.file_name().and_then(|v|v.to_str()).ok_or(ERR)?;std::ffi::CString::new(name).map_err(|_|ERR)}
fn read<T:serde::de::DeserializeOwned>(path:&Path)->Result<Option<T>,&'static str>{let d=dir(path.parent().ok_or(ERR)?)?;let name=filename(path)?;let fd=unsafe{libc::openat(d.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};if fd<0 {return if std::io::Error::last_os_error().kind()==std::io::ErrorKind::NotFound {Ok(None)} else {Err(ERR)}}let f=unsafe{File::from_raw_fd(fd)};let m=f.metadata().map_err(|_|ERR)?;if !m.is_file()||m.uid()!=unsafe{libc::geteuid()}||m.mode()&0o777!=0o600||m.nlink()!=1||m.len()>16384{return Err(ERR)}let mut bytes=Vec::new();f.take(16385).read_to_end(&mut bytes).map_err(|_|ERR)?;serde_json::from_slice(&bytes).map(Some).map_err(|_|ERR)}
fn save_new<T:Serialize>(path:&Path,v:&T)->Result<(),&'static str>{let d=dir(path.parent().ok_or(ERR)?)?;let name=filename(path)?;let fd=unsafe{libc::openat(d.as_raw_fd(),name.as_ptr(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_NOFOLLOW|libc::O_CLOEXEC,0o600)};if fd<0{return Err(ERR)}let mut f=unsafe{File::from_raw_fd(fd)};let b=serde_json::to_vec(v).map_err(|_|ERR)?;if b.len()>16384{return Err(ERR)}f.write_all(&b).and_then(|_|f.sync_all()).map_err(|_|ERR)}
fn remove(path:&Path)->Result<(),&'static str>{let d=dir(path.parent().ok_or(ERR)?)?;let name=filename(path)?;if unsafe{libc::unlinkat(d.as_raw_fd(),name.as_ptr(),0)}!=0{return Err(ERR)}d.sync_all().map_err(|_|ERR)}
fn save_record(base:&Path,record:&Record)->Result<(),&'static str>{let d=dir(base)?;let p=base.join(format!("slack-config-{}.json",record.config.owner_id));let tmp=base.join(format!(".slack-config-{}-{}",record.config.owner_id,record.config.operation_id));let _:Option<Record>=read(&p)?;save_new(&tmp,record)?;let old=filename(&tmp)?;let new=filename(&p)?;if unsafe{libc::renameat(d.as_raw_fd(),old.as_ptr(),d.as_raw_fd(),new.as_ptr())}!=0{return Err(ERR)}d.sync_all().map_err(|_|ERR)}
fn intent_path(base:&Path,owner:u64)->PathBuf {base.join(format!("slack-config-pending-{owner}.json"))}
fn record_path(base:&Path,owner:u64)->PathBuf {base.join(format!("slack-config-{owner}.json"))}
fn read_record(base:&Path,owner:u64)->Result<Option<Record>,&'static str>{let r:Option<Record>=read(&record_path(base,owner))?;if r.as_ref().is_some_and(|r|r.version!=1||r.revision==0||r.config.owner_id!=owner||!r.config.valid()){return Err(ERR)}Ok(r)}
fn pending(base:&Path,owner:u64,record:Option<&Record>)->Result<Option<Intent>,&'static str>{let p=intent_path(base,owner);let Some(i)=read::<Intent>(&p)? else{return Ok(None)};if i.version!=1||i.owner_id!=owner||!uuid(&i.account_id)||!uuid(&i.operation_id){return Err(ERR)}if record.is_some_and(|r|r.config.account_id==i.account_id && r.config.operation_id==i.operation_id && r.revision==i.expected_revision.saturating_add(1)){remove(&p)?;Ok(None)}else{Ok(Some(i))}}
#[derive(Clone,Serialize,PartialEq,Eq)]
struct Event {delivery_id:String,event_id:String,workspace_id:String,app_id:String,channel_id:String,user_id:String,event_ts:String,thread_ts:String,text:String,config_revision:u64,credential_revision:u64}
struct Receipt {event:Event,accepted:bool}
struct Live {stop:Arc<AtomicBool>,status:&'static str, revision:u64,queue:VecDeque<String>,events:HashMap<String,Receipt>,attempts:HashSet<String>,attempt_order:VecDeque<String>,inflight:HashSet<String>,ack:Option<tokio::sync::mpsc::UnboundedSender<(String,std::sync::mpsc::SyncSender<bool>)>>,running:bool, owner_epoch:String, core_epoch:String, credential_generation:u64}
impl Default for Live {fn default()->Self{Self{stop:Arc::new(AtomicBool::new(false)),status:"disabled",revision:0,queue:VecDeque::new(),events:HashMap::new(),attempts:HashSet::new(),attempt_order:VecDeque::new(),inflight:HashSet::new(),ack:None,running:false,owner_epoch:String::new(),core_epoch:String::new(),credential_generation:0}}}
pub(crate) struct Manager {base:PathBuf,live:Mutex<Live>}
impl Manager {pub(crate) fn new(base:PathBuf)->Arc<Self>{Arc::new(Self{base,live:Mutex::new(Live::default())})}}
fn active<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,c:&Config,generation:u64)->bool {
 let start=std::time::Instant::now();loop {match broker.try_lock() {Ok(b)=>return b.mcp_owner_valid(c.owner_id,&c.owner_epoch,&c.core_epoch) && b.slack_generation()==generation,Err(std::sync::TryLockError::Poisoned(_))=>return false,Err(_) if start.elapsed()<Duration::from_millis(100)=>std::thread::sleep(Duration::from_millis(2)),Err(_)=>return false}}
}
fn event_from(raw:&Value,c:&Config,revision:u64)->Option<Event>{
 if raw.get("type")?.as_str()? != "events_api" {return None} let payload=raw.get("payload")?;let event=payload.get("event")?;
 if payload.get("team_id")?.as_str()?!=c.workspace_id || payload.get("api_app_id")?.as_str()?!=c.app_id || event.get("type")?.as_str()? !="app_mention" || event.get("channel")?.as_str()?!=c.channel_id || event.get("subtype").is_some_and(|v|!v.is_null()) || event.get("bot_id").is_some_and(|v|!v.is_null()) || event.get("bot_profile").is_some_and(|v|!v.is_null()) {return None}
 let user=event.get("user")?.as_str()?;if !c.sender_ids.iter().any(|s|s==user){return None}
 let event_id=payload.get("event_id")?.as_str()?;let delivery=raw.get("envelope_id")?.as_str()?;let event_ts=event.get("ts")?.as_str()?;let thread_ts=event.get("thread_ts").and_then(Value::as_str).unwrap_or(event_ts);let text=event.get("text")?.as_str()?;
 if !bounded(event_id,100)||!bounded(delivery,100)||!timestamp(event_ts)||!timestamp(thread_ts)||text.is_empty()||text.len()>MAX_TEXT{return None}
 Some(Event{delivery_id:delivery.to_owned(),event_id:event_id.to_owned(),workspace_id:c.workspace_id.clone(),app_id:c.app_id.clone(),channel_id:c.channel_id.clone(),user_id:user.to_owned(),event_ts:event_ts.to_owned(),thread_ts:thread_ts.to_owned(),text:text.to_owned(),config_revision:revision,credential_revision:c.credential_revision})
}
fn http()->Result<reqwest::Client,&'static str>{reqwest::Client::builder().retry(reqwest::retry::never()).no_proxy().redirect(reqwest::redirect::Policy::none()).connect_timeout(Duration::from_secs(5)).timeout(Duration::from_secs(15)).build().map_err(|_|ERR)}
async fn body(mut r:reqwest::Response)->Result<Value,&'static str>{if !r.status().is_success()||r.content_length().is_some_and(|n|n>MAX_FRAME as u64){return Err(ERR)}let mut data=Vec::new();while let Some(chunk)=r.chunk().await.map_err(|_|ERR)? {if data.len()+chunk.len()>MAX_FRAME{return Err(ERR)}data.extend_from_slice(&chunk);}serde_json::from_slice(&data).map_err(|_|ERR)}
async fn verify_bot(client:&reqwest::Client,bot:&crate::OwnerSecret,c:&Config)->Result<(),&'static str> {
 let response=client.post("https://slack.com/api/auth.test").bearer_auth(bot.as_str()).send().await.map_err(|_|"unavailable")?;
 let identity=body(response).await.map_err(|_|"unavailable")?;
 if identity.get("ok")!=Some(&Value::Bool(true)){return Err("unavailable")}
 let id=identity.get("bot_id").and_then(Value::as_str).filter(|s|slack_id(s,b'B')).ok_or("identity_mismatch")?;
 if identity.get("team_id").and_then(Value::as_str)!=Some(c.workspace_id.as_str()){return Err("identity_mismatch")}
 let mut url=url::Url::parse("https://slack.com/api/bots.info").map_err(|_|"unavailable")?;url.query_pairs_mut().append_pair("bot",id);
 let info=body(client.get(url).bearer_auth(bot.as_str()).send().await.map_err(|_|"unavailable")?).await.map_err(|_|"unavailable")?;
 if info.get("ok")!=Some(&Value::Bool(true)){return Err(if info.get("error").and_then(Value::as_str)==Some("missing_scope"){"permission_required"}else{"unavailable"})}
 if info.pointer("/bot/deleted")==Some(&Value::Bool(true)) || info.pointer("/bot/id").and_then(Value::as_str)!=Some(id)||info.pointer("/bot/app_id").and_then(Value::as_str)!=Some(c.app_id.as_str()){return Err("identity_mismatch")}Ok(())
}
fn ws_url(s:&str)->bool {let Ok(u)=url::Url::parse(s)else{return false};let Some(host)=u.host_str()else{return false};u.scheme()=="wss" && (matches!(host,"wss.slack.com"|"wss-primary.slack.com"|"wss-backup.slack.com"|"wss-mobile.slack.com") || host.strip_prefix("wss-").and_then(|v|v.strip_suffix(".slack.com")).is_some_and(|v|!v.is_empty()&&v.bytes().all(|b|b.is_ascii_digit()))) && u.username().is_empty() && u.password().is_none() && u.port().is_none_or(|p|p==443) && u.path().starts_with("/link/") && u.fragment().is_none() && s.len()<=4096}
async fn socket<B:CredentialBackend,T:SyntheticAuthTransport>(manager:Arc<Manager>,broker:Arc<Mutex<AuthBroker<B,T>>>,record:Record,generation:u64,app:crate::OwnerSecret,bot:crate::OwnerSecret,stop:Arc<AtomicBool>){
 let c=record.config;let Ok(client)=http() else{return};
 while !stop.load(Ordering::Acquire) && active(&broker,&c,generation) {
  if let Err(code)=verify_bot(&client,&bot,&c).await {
   if let Ok(mut live)=manager.live.lock(){if live.revision==record.revision && Arc::ptr_eq(&live.stop,&stop){live.status=code;live.ack=None;if code!="unavailable"{live.running=false;}}}
   if code!="unavailable"{return}for _ in 0..10 {if stop.load(Ordering::Acquire)||!active(&broker,&c,generation){break}tokio::time::sleep(Duration::from_millis(500)).await;}continue
  }
  if stop.load(Ordering::Acquire)||!active(&broker,&c,generation){break}
  if let Ok(mut live)=manager.live.lock(){if live.revision==record.revision && Arc::ptr_eq(&live.stop,&stop){live.status="connecting";}}
  let response=client.post("https://slack.com/api/apps.connections.open").bearer_auth(app.as_str()).send().await;
  let opened=match response {Ok(r)=>body(r).await.ok(),Err(_)=>None};
  let url=opened.as_ref().filter(|v|v.get("ok")==Some(&Value::Bool(true))).and_then(|v|v.get("url")).and_then(Value::as_str).filter(|v|ws_url(v)).map(str::to_owned);
  if let Some(url)=url {
   let url=zeroize::Zeroizing::new(url);
   if stop.load(Ordering::Acquire)||!active(&broker,&c,generation){break}
   let ws=tokio::time::timeout(Duration::from_secs(10),tokio_tungstenite::connect_async_with_config(url.as_str(),Some(WebSocketConfig::default().max_frame_size(Some(MAX_FRAME)).max_message_size(Some(MAX_FRAME))),false)).await;
   if let Ok(Ok((mut ws,_)))=ws {
    let (tx,mut rx)=tokio::sync::mpsc::unbounded_channel();
    let mut hello=false;let hello_deadline=tokio::time::Instant::now()+Duration::from_secs(10);
    let mut last_seen=tokio::time::Instant::now();let mut last_ping=tokio::time::Instant::now();
    loop {
     if stop.load(Ordering::Acquire)||!active(&broker,&c,generation){break}
     tokio::select! {
      msg=ws.next()=>{last_seen=tokio::time::Instant::now();match msg {
       Some(Ok(Message::Text(data)))=>{
        let Ok(value)=serde_json::from_str::<Value>(&data) else{break};
        if value.get("type").and_then(Value::as_str)==Some("disconnect"){break}
        if !hello {
         if value.get("type").and_then(Value::as_str)!=Some("hello") {break}
         if value.get("app_id").is_some_and(|v|v.as_str()!=Some(c.app_id.as_str())) {if let Ok(mut live)=manager.live.lock(){if Arc::ptr_eq(&live.stop,&stop){live.status="identity_mismatch";}}stop.store(true,Ordering::Release);break}
         hello=true;if let Ok(mut live)=manager.live.lock(){if live.revision==record.revision && Arc::ptr_eq(&live.stop,&stop){live.status="connected";live.ack=Some(tx.clone());}}continue
        }
        if value.get("type").and_then(Value::as_str)==Some("events_api") && value.pointer("/payload/api_app_id").and_then(Value::as_str).is_some_and(|id|id!=c.app_id.as_str()) {if let Ok(mut live)=manager.live.lock(){if Arc::ptr_eq(&live.stop,&stop){live.status="identity_mismatch";}}stop.store(true,Ordering::Release);break}
        let delivery=value.get("envelope_id").and_then(Value::as_str).filter(|s|bounded(s,128)).map(str::to_owned);
        if let Some(event)=event_from(&value,&c,record.revision) {
         let Ok(mut live)=manager.live.lock()else{break};if live.revision!=record.revision || !Arc::ptr_eq(&live.stop,&stop) {break}
         if let Some(prior)=live.events.get(&event.delivery_id) {
          if prior.event!=event {break} let acknowledged=prior.accepted;let id=event.delivery_id.clone();drop(live);
          if acknowledged {if stop.load(Ordering::Acquire)||!active(&broker,&c,generation){break}if ws.send(Message::Text(json!({"envelope_id":id}).to_string().into())).await.is_err(){break}if let Ok(mut live)=manager.live.lock(){live.queue.retain(|item|item!=&id);live.events.remove(&id);}}
         }else {if live.events.len()>=512 || live.queue.len()>=32 {break} live.queue.push_back(event.delivery_id.clone());live.events.insert(event.delivery_id.clone(),Receipt{event,accepted:false});}
        }else if let Some(id)=delivery {if stop.load(Ordering::Acquire)||!active(&broker,&c,generation){break}if ws.send(Message::Text(json!({"envelope_id":id}).to_string().into())).await.is_err(){break}}
       },
       Some(Ok(Message::Ping(p)))=>{if ws.send(Message::Pong(p)).await.is_err(){break}},Some(Ok(Message::Close(_)))|None|Some(Err(_))=>break,_=>{}
      }},
      Some((id,done))=rx.recv()=>{if stop.load(Ordering::Acquire)||!active(&broker,&c,generation){let _=done.send(false);break}let ok=ws.send(Message::Text(json!({"envelope_id":id}).to_string().into())).await.is_ok();let _=done.send(ok);if !ok {break}},
      _=tokio::time::sleep(Duration::from_millis(250))=>{if (!hello && tokio::time::Instant::now()>hello_deadline)||last_seen.elapsed()>Duration::from_secs(90){break}if last_ping.elapsed()>Duration::from_secs(25){if ws.send(Message::Ping(Vec::new().into())).await.is_err(){break}last_ping=tokio::time::Instant::now();}},
     }
    }
    let _=ws.close(None).await;
   }
  }
  if let Ok(mut live)=manager.live.lock(){if live.revision==record.revision && Arc::ptr_eq(&live.stop,&stop){if live.status!="identity_mismatch"{live.status="disconnected";}live.ack=None;}}
  for _ in 0..10 {if stop.load(Ordering::Acquire)||!active(&broker,&c,generation){break}tokio::time::sleep(Duration::from_millis(500)).await;}
 }
 if let Ok(mut live)=manager.live.lock(){if live.revision==record.revision && Arc::ptr_eq(&live.stop,&stop){live.running=false;if live.status!="identity_mismatch"{live.status="disconnected";}live.ack=None;}}
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {version:u8,owner_id:u64,owner_epoch:String,core_epoch:String,config_revision:u64,credential_revision:u64,#[serde(default)] operation_id:Option<String>}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Ack {version:u8,owner_id:u64,owner_epoch:String,core_epoch:String,config_revision:u64,credential_revision:u64,delivery_id:String,event_id:String}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Reply {version:u8,owner_id:u64,owner_epoch:String,core_epoch:String,config_revision:u64,credential_revision:u64,event_id:String,attempt_id:String,workspace_id:String,app_id:String,channel_id:String,user_id:String,thread_ts:String,text:String}
fn binding<B:CredentialBackend,T:SyntheticAuthTransport>(m:&Manager,broker:&Arc<Mutex<AuthBroker<B,T>>>,b:&Binding)->Result<Record,&'static str>{
 if b.version!=1||b.owner_id==0{return Err("slack_invalid")};let guard=broker.lock().map_err(|_|ERR)?;if !guard.mcp_owner_valid(b.owner_id,&b.owner_epoch,&b.core_epoch){return Err("slack_owner_changed")};let mut record=read_record(&m.base,b.owner_id)?.ok_or("slack_config_unavailable")?;if pending(&m.base,b.owner_id,Some(&record))?.is_some()||record.revision!=b.config_revision||record.config.credential_revision!=b.credential_revision {return Err("slack_config_changed")};if guard.slack_pending(&m.base,b.owner_id)? {return Err("slack_credential_changed")};record.config.owner_epoch=b.owner_epoch.clone();record.config.core_epoch=b.core_epoch.clone();Ok(record)
}
impl Manager {
 fn configure<B:CredentialBackend,T:SyntheticAuthTransport>(self:&Arc<Self>,broker:Arc<Mutex<AuthBroker<B,T>>>,c:Config)->Result<Value,&'static str>{
  if !c.valid(){return Err("slack_invalid")};let mut auth=broker.lock().map_err(|_|ERR)?;if !auth.mcp_owner_valid(c.owner_id,&c.owner_epoch,&c.core_epoch)||auth.slack_pending(&self.base,c.owner_id)? {return Err("slack_owner_changed")}
  let old=read_record(&self.base,c.owner_id)?;if pending(&self.base,c.owner_id,old.as_ref())?.is_some()||old.as_ref().map_or(0,|r|r.revision)!=c.expected_config_revision||old.as_ref().is_some_and(|r|r.config.operation_id==c.operation_id) {return Err("slack_config_changed")}
  let tokens=if c.enabled {Some(auth.slack_tokens(c.owner_id,&c.owner_epoch,&c.core_epoch,&c.account_id,c.credential_revision)?)} else {None};let generation=auth.slack_generation();
  {let mut live=self.live.lock().map_err(|_|ERR)?;live.stop.store(true,Ordering::Release);live.status="disconnected";}
  let intent=Intent{version:1,owner_id:c.owner_id,account_id:c.account_id.clone(),expected_revision:c.expected_config_revision,operation_id:c.operation_id.clone()};let journal=intent_path(&self.base,c.owner_id);save_new(&journal,&intent)?;dir(&self.base)?.sync_all().map_err(|_|ERR)?;
  let record=Record{version:1,revision:c.expected_config_revision.checked_add(1).ok_or("slack_config_changed")?,config:c};save_record(&self.base,&record)?;
  remove(&journal)?;
  let mut live=self.live.lock().map_err(|_|ERR)?;live.stop.store(true,Ordering::Release);let stop=Arc::new(AtomicBool::new(false));*live=Live{stop:stop.clone(),status:if record.config.enabled{"connecting"}else{"disabled"},revision:record.revision,running:record.config.enabled,owner_epoch:record.config.owner_epoch.clone(),core_epoch:record.config.core_epoch.clone(),credential_generation:generation,..Live::default()};
  let enabled=record.config.enabled;let mut out=status_value(&record,live.status,None);if !enabled {out["credential_state"]=json!("unknown");}drop(live);drop(auth);
  if let Some((app,bot))=tokens.filter(|_|enabled) {let manager=self.clone();std::thread::spawn(move || {if let Ok(rt)=tokio::runtime::Builder::new_current_thread().enable_all().build(){rt.block_on(socket(manager,broker,record,generation,app,bot,stop));}});}
  Ok(out)
 }
 fn status<B:CredentialBackend,T:SyntheticAuthTransport>(&self,broker:&Arc<Mutex<AuthBroker<B,T>>>,b:Binding)->Result<Value,&'static str>{
  if b.version!=1||b.owner_id==0{return Err("slack_invalid")};let mut guard=broker.lock().map_err(|_|ERR)?;if !guard.mcp_owner_valid(b.owner_id,&b.owner_epoch,&b.core_epoch){return Err("slack_owner_changed")};let r=read_record(&self.base,b.owner_id)?;let p=pending(&self.base,b.owner_id,r.as_ref())?;
  if let Some(record)=r {let status=self.live.lock().map_err(|_|ERR).map(|live|if live.revision==record.revision && live.owner_epoch==b.owner_epoch && live.core_epoch==b.core_epoch {live.status}else{"disconnected"})?;let mut out=status_value(&record,status,p.as_ref());if guard.slack_pending(&self.base,b.owner_id)? || guard.slack_tokens(b.owner_id,&b.owner_epoch,&b.core_epoch,&record.config.account_id,record.config.credential_revision).is_err() {out["credential_state"]=json!("unavailable");}out["owner_epoch"]=json!(b.owner_epoch);out["core_epoch"]=json!(b.core_epoch);if let Some(op)=b.operation_id {out["receipt_match"]=json!(record.config.operation_id==op && p.is_none());}Ok(out)}
  else {Ok(json!({"version":1,"owner_id":b.owner_id,"owner_epoch":b.owner_epoch,"core_epoch":b.core_epoch,"config_revision":0,"operation_id":null,"enabled":false,"credential_state":"unknown","transport_state":"disabled","pending":p,"receipt_match":false}))}
 }
}
fn status_value(r:&Record,transport:&str,pending:Option<&Intent>)->Value {let c=&r.config;json!({"version":1,"owner_id":c.owner_id,"owner_epoch":c.owner_epoch,"core_epoch":c.core_epoch,"account_id":c.account_id,"credential_revision":c.credential_revision,"config_revision":r.revision,"operation_id":c.operation_id,"enabled":c.enabled,"workspace_id":c.workspace_id,"app_id":c.app_id,"channel_id":c.channel_id,"sender_ids":c.sender_ids,"agent_id":c.agent_id,"project_id":c.project_id,"connection_id":c.connection_id,"model_id":c.model_id,"credential_state":"stored","transport_state":transport,"pending":pending})}
impl Manager {
 fn poll<B:CredentialBackend,T:SyntheticAuthTransport>(self:&Arc<Self>,broker:&Arc<Mutex<AuthBroker<B,T>>>,b:Binding)->Result<Value,&'static str>{let record=binding(self,broker,&b)?;if !record.config.enabled{return Err("slack_disabled")};
  let current_generation=broker.lock().map_err(|_|ERR)?.slack_generation();
  let mut live=self.live.lock().map_err(|_|ERR)?;
  if (!live.running && !matches!(live.status,"permission_required"|"identity_mismatch"|"unavailable")) || live.revision!=record.revision || live.owner_epoch!=record.config.owner_epoch || live.core_epoch!=record.config.core_epoch || live.credential_generation!=current_generation {
   live.stop.store(true,Ordering::Release);drop(live);
   let ((app,bot),generation)={let mut a=broker.lock().map_err(|_|ERR)?;let tokens=a.slack_tokens(b.owner_id,&b.owner_epoch,&b.core_epoch,&record.config.account_id,b.credential_revision)?;(tokens,a.slack_generation())};
   live=self.live.lock().map_err(|_|ERR)?;let stop=Arc::new(AtomicBool::new(false));*live=Live{stop:stop.clone(),status:"connecting",revision:record.revision,running:true,owner_epoch:record.config.owner_epoch.clone(),core_epoch:record.config.core_epoch.clone(),credential_generation:generation,..Live::default()};let manager=self.clone();let owned=broker.clone();let r=record.clone();std::thread::spawn(move ||{if let Ok(rt)=tokio::runtime::Builder::new_current_thread().enable_all().build(){rt.block_on(socket(manager,owned,r,generation,app,bot,stop));}});
  }
  let event=live.queue.front().and_then(|id|live.events.get(id)).map(|r|r.event.clone());Ok(json!({"event":event}))}
 fn ack<B:CredentialBackend,T:SyntheticAuthTransport>(&self,broker:&Arc<Mutex<AuthBroker<B,T>>>,a:Ack)->Result<Value,&'static str>{
  let b=Binding{version:a.version,owner_id:a.owner_id,owner_epoch:a.owner_epoch,core_epoch:a.core_epoch,config_revision:a.config_revision,credential_revision:a.credential_revision,operation_id:None};let record=binding(self,broker,&b)?;if !record.config.enabled||!bounded(&a.delivery_id,128)||!bounded(&a.event_id,128){return Err("slack_invalid")}
  let (tx,rx)=std::sync::mpsc::sync_channel(1);let mut live=self.live.lock().map_err(|_|ERR)?;if live.revision!=record.revision || live.owner_epoch!=b.owner_epoch || live.core_epoch!=b.core_epoch {return Err("slack_config_changed")};let receipt=live.events.get_mut(&a.delivery_id).ok_or("slack_event_unavailable")?;if receipt.event.event_id!=a.event_id{return Err("slack_event_changed")};receipt.accepted=true;let sender=live.ack.as_ref().cloned().ok_or("slack_ack_uncertain")?;sender.send((a.delivery_id.clone(),tx)).map_err(|_|"slack_ack_uncertain")?;drop(live);
  if !rx.recv_timeout(Duration::from_secs(2)).map_err(|_|"slack_ack_uncertain")? {return Err("slack_ack_uncertain")};let mut live=self.live.lock().map_err(|_|ERR)?;live.queue.retain(|id|id!=&a.delivery_id);live.events.remove(&a.delivery_id);Ok(json!({"acknowledged":true}))
 }
 fn reply<B:CredentialBackend,T:SyntheticAuthTransport>(&self,broker:&Arc<Mutex<AuthBroker<B,T>>>,p:Reply)->Result<Value,&'static str>{
  let b=Binding{version:p.version,owner_id:p.owner_id,owner_epoch:p.owner_epoch,core_epoch:p.core_epoch,config_revision:p.config_revision,credential_revision:p.credential_revision,operation_id:None};let record=binding(self,broker,&b)?;if !record.config.enabled||!uuid(&p.attempt_id)||!bounded(&p.event_id,128)||p.text.is_empty()||p.text.len()>MAX_REPLY||!timestamp(&p.thread_ts){return Err("slack_invalid")}
  if p.workspace_id!=record.config.workspace_id || p.app_id!=record.config.app_id || p.channel_id!=record.config.channel_id || !record.config.sender_ids.iter().any(|id|id==&p.user_id) {return Err("slack_event_changed")}
  let stop={let live=self.live.lock().map_err(|_|ERR)?;if live.revision!=record.revision || live.owner_epoch!=b.owner_epoch || live.core_epoch!=b.core_epoch || live.attempts.contains(&p.attempt_id)||live.inflight.contains(&p.attempt_id){return Err("slack_attempt_changed")};let mut hits=live.events.values().filter(|r|r.event.event_id==p.event_id);if let Some(r)=hits.next(){if !r.accepted || r.event.workspace_id!=p.workspace_id || r.event.app_id!=p.app_id || r.event.channel_id!=p.channel_id || r.event.user_id!=p.user_id || r.event.thread_ts!=p.thread_ts {return Err("slack_event_changed")}}live.stop.clone()};
  let (bot,generation)={let mut auth=broker.lock().map_err(|_|ERR)?;if auth.slack_pending(&self.base,b.owner_id)?{return Err("slack_credential_changed")};let (_,bot)=auth.slack_tokens(b.owner_id,&b.owner_epoch,&b.core_epoch,&record.config.account_id,b.credential_revision)?;(bot,auth.slack_generation())};
  if stop.load(Ordering::Acquire)||!active(broker,&record.config,generation){return Ok(json!({"state":"failed_before_dispatch"}))}let client=http()?;let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|_|ERR)?;
  {let mut live=self.live.lock().map_err(|_|ERR)?;if live.revision!=record.revision || live.owner_epoch!=b.owner_epoch || live.core_epoch!=b.core_epoch || live.attempts.contains(&p.attempt_id) || live.inflight.len()>=512 || !live.inflight.insert(p.attempt_id.clone()){return Err("slack_attempt_changed")}}
  let attempt_id=p.attempt_id.clone();let event_id=p.event_id.clone();
  let outcome=runtime.block_on(async {if verify_bot(&client,&bot,&record.config).await.is_err() || stop.load(Ordering::Acquire) || !active(broker,&record.config,generation) {return json!({"state":"failed_before_dispatch"});}let request=client.post("https://slack.com/api/chat.postMessage").bearer_auth(bot.as_str()).json(&json!({"channel":p.channel_id,"thread_ts":p.thread_ts,"text":p.text}));let future=request.send();tokio::pin!(future);loop {tokio::select!{r=&mut future=>return match r {Ok(v)=>match body(v).await {Ok(v) if v.get("ok")==Some(&Value::Bool(true)) && v.get("channel").and_then(Value::as_str)==Some(record.config.channel_id.as_str()) && v.get("ts").and_then(Value::as_str).is_some_and(timestamp)=>json!({"state":"sent","message_ts":v.get("ts")}),_=>json!({"state":"unknown"})},Err(_)=>json!({"state":"unknown"})},_=tokio::time::sleep(Duration::from_millis(250))=>if stop.load(Ordering::Acquire)||!active(broker,&record.config,generation){return json!({"state":"unknown"})}}}});
  if let Ok(mut live)=self.live.lock(){live.inflight.remove(&attempt_id);if live.attempts.insert(attempt_id.clone()){live.attempt_order.push_back(attempt_id);}while live.attempt_order.len()>512 {if let Some(old)=live.attempt_order.pop_front(){live.attempts.remove(&old);}}if outcome.get("state").and_then(Value::as_str)==Some("sent"){live.events.retain(|_,receipt|receipt.event.event_id!=event_id);}}
  Ok(outcome)
 }
 pub(crate) fn command<B:CredentialBackend,T:SyntheticAuthTransport>(self:&Arc<Self>,broker:Arc<Mutex<AuthBroker<B,T>>>,op:&str,payload:Value)->Result<Value,&'static str>{
  let result=match op {"slack_configure"=>self.configure(broker,serde_json::from_value(payload).map_err(|_|"slack_invalid")?)?,"slack_status"=>self.status(&broker,serde_json::from_value(payload).map_err(|_|"slack_invalid")?)?,"slack_poll"=>self.poll(&broker,serde_json::from_value(payload).map_err(|_|"slack_invalid")?)?,"slack_ack"=>self.ack(&broker,serde_json::from_value(payload).map_err(|_|"slack_invalid")?)?,"slack_reply"=>self.reply(&broker,serde_json::from_value(payload).map_err(|_|"slack_invalid")?)?,_=>return Err("slack_invalid")};Ok(json!({"state":"completed","result":result}))
 }
 pub(crate) fn shutdown(&self){if let Ok(mut l)=self.live.lock(){l.stop.store(true,Ordering::Release);l.ack=None;l.status="disabled";}}
}
