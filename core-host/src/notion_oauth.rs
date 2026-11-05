//! Owner-only Notion OAuth. No credential or callback data crosses the Engine channel.
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::HashMap, io::{Read, Write}, net::{Ipv4Addr, TcpListener}, sync::{Arc, Mutex, atomic::{AtomicBool, AtomicUsize, Ordering}}, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};
use zeroize::Zeroizing;

pub(crate) const ENDPOINT: &str = "https://mcp.notion.com/mcp";
const AUTHORIZE: &str = "https://mcp.notion.com/authorize";
const TOKEN: &str = "https://mcp.notion.com/token";
const REGISTER: &str = "https://mcp.notion.com/register";
const MAX_BODY: usize = 32768;
type Guard = Arc<dyn Fn() -> bool + Send + Sync>;
type Store = Arc<dyn Fn(&[u8]) -> Result<(), &'static str> + Send + Sync>;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Bundle {
    pub version: u8, pub owner_id: i64, pub server_id: String, pub endpoint: String,
    pub client_id: String, pub redirect_uri: String, pub access_token: String, pub refresh_token: String,
    pub expires_at: u64, pub workspace_id: Option<String>, pub workspace_name: Option<String>,
    pub user_id: Option<String>, pub bot_id: Option<String>, pub refresh_pending: bool,
}
impl Drop for Bundle { fn drop(&mut self) { use zeroize::Zeroize; self.access_token.zeroize(); self.refresh_token.zeroize(); self.client_id.zeroize(); } }
impl Bundle {
    pub fn valid(&self, owner: i64, server: &str) -> bool {
        self.version == 1 && self.owner_id == owner && self.server_id == server && self.endpoint == ENDPOINT
            && valid_text(&self.client_id, 2048) && parse_redirect(&self.redirect_uri).is_ok() && valid_text(&self.access_token, 16384)
            && valid_text(&self.refresh_token, 16384) && self.workspace_id.as_deref().is_none_or(|v| valid_text(v, 256))
            && self.workspace_name.as_deref().is_none_or(|v| valid_text(v, 256))
            && self.user_id.as_deref().is_none_or(|v| valid_text(v,256)) && self.bot_id.as_deref().is_none_or(|v| valid_text(v,256))
    }
}
pub(crate) struct Reuse { client_id: Zeroizing<String>, redirect_uri: String }
impl Reuse {
    pub fn from_bundle(bundle:&Bundle, owner:i64, server:&str) -> Result<Self,&'static str> {
        if !bundle.valid(owner,server) {return Err("mcp_oauth_invalid");}
        Ok(Self {client_id:Zeroizing::new(bundle.client_id.clone()),redirect_uri:bundle.redirect_uri.clone()})
    }
}
fn parse_redirect(value:&str) -> Result<(u16,String),&'static str> {
    let url=url::Url::parse(value).map_err(|_| "mcp_oauth_invalid")?;
    let port=url.port().filter(|p|*p>0).ok_or("mcp_oauth_invalid")?;
    let path=url.path().strip_prefix('/').ok_or("mcp_oauth_invalid")?;
    if url.scheme()!="http" || url.host_str()!=Some("127.0.0.1") || !url.username().is_empty() || url.password().is_some() || url.query().is_some() || url.fragment().is_some()
        || path.len()!=43 || !path.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'-' || b==b'_')
        || value!=format!("http://127.0.0.1:{port}/{path}") {return Err("mcp_oauth_invalid");}
    Ok((port,path.to_owned()))
}
fn valid_text(s: &str, max: usize) -> bool { !s.is_empty() && s.len() <= max && !s.chars().any(char::is_control) }
fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() }
fn random() -> Result<String, &'static str> { let mut b=Zeroizing::new([0u8;32]); getrandom::fill(b.as_mut()).map_err(|_| "mcp_oauth_unavailable")?; Ok(URL_SAFE_NO_PAD.encode(b.as_ref())) }
fn client() -> Result<reqwest::Client, &'static str> { reqwest::Client::builder().redirect(reqwest::redirect::Policy::none()).no_proxy().connect_timeout(Duration::from_secs(5)).timeout(Duration::from_secs(20)).build().map_err(|_| "mcp_oauth_unavailable") }
async fn body(response: reqwest::Response) -> Result<Value, &'static str> {
    if !response.status().is_success() || response.content_length().is_some_and(|n| n > MAX_BODY as u64) { return Err("mcp_oauth_provider_failed"); }
    let mut response=response; let mut data=Zeroizing::new(Vec::new());
    while let Some(chunk)=response.chunk().await.map_err(|_| "mcp_oauth_provider_failed")? { if data.len()+chunk.len()>MAX_BODY { return Err("mcp_oauth_provider_failed"); } data.extend_from_slice(&chunk); }
    serde_json::from_slice(&data).map_err(|_| "mcp_oauth_provider_failed")
}
async fn post_form(client: &reqwest::Client, fields: &[(&str, &str)]) -> Result<Value, &'static str> {
    let encoded=Zeroizing::new(url::form_urlencoded::Serializer::new(String::new()).extend_pairs(fields.iter().copied()).finish());
    let r=client.post(TOKEN).header("Content-Type","application/x-www-form-urlencoded").body(encoded.to_string()).send().await.map_err(|_| "mcp_oauth_provider_failed")?; body(r).await
}
fn token_string(v:&Value, key:&str) -> Result<String, &'static str> { let s=v.get(key).and_then(Value::as_str).filter(|s|valid_text(s,16384)).ok_or("mcp_oauth_provider_failed")?; Ok(s.to_owned()) }
fn expiry(v:&Value) -> Result<u64, &'static str> { let seconds=v.get("expires_in").and_then(Value::as_u64).filter(|v| *v>0 && *v<=31_536_000).ok_or("mcp_oauth_provider_failed")?; Ok(now().saturating_add(seconds)) }
fn workspace(v:&Value, key:&str) -> Result<Option<String>, &'static str> { match v.get(key) { None|Some(Value::Null)=>Ok(None), Some(Value::String(s)) if valid_text(s,256)=>Ok(Some(s.clone())), _=>Err("mcp_oauth_provider_failed") } }
fn token_type(v:&Value) -> bool { v.get("token_type").and_then(Value::as_str).is_some_and(|s| s.eq_ignore_ascii_case("bearer")) }

#[derive(Clone)]
struct Operation { state: &'static str, url: Option<String>, reference: Option<String>, workspace: Option<Value>, code: Option<&'static str>, cancelled: Arc<AtomicBool>, revision: u64, guard: Guard }
#[derive(Default)]
pub(crate) struct Manager { operations: Mutex<HashMap<(String,String),Operation>>, active: AtomicUsize, pub refresh_lock: Mutex<()> }
impl Manager {
    pub fn begin(self:&Arc<Self>, server:String, revision:u64, op:String, owner:i64, reference:String, reuse:Option<Reuse>, guard:Guard, store:Store) -> Result<Value,&'static str> {
        if !(guard)() { return Err("mcp_configuration_changed"); }
        let cancelled=Arc::new(AtomicBool::new(false));
        {
            let mut entries=self.operations.lock().map_err(|_| "mcp_oauth_unavailable")?;
            if entries.contains_key(&(server.clone(),op.clone())) { return Err("mcp_oauth_operation_conflict"); }
            for ((s,_), old) in entries.iter_mut() { if s==&server && matches!(old.state,"preparing"|"authorizing") { old.cancelled.store(true,Ordering::Release); old.state="cancelled"; old.url=None; } }
            if entries.len() >= 64 { entries.retain(|_,v| matches!(v.state,"preparing"|"authorizing")); }
            if entries.len() >= 64 { return Err("mcp_oauth_busy"); }
            entries.insert((server.clone(),op.clone()),Operation{state:"preparing",url:None,reference:None,workspace:None,code:None,cancelled:cancelled.clone(),revision,guard:guard.clone()});
        }
        if self.active.fetch_add(1,Ordering::AcqRel) >= 4 { self.active.fetch_sub(1,Ordering::AcqRel); if let Ok(mut entries)=self.operations.lock() { entries.remove(&(server.clone(),op.clone())); } return Err("mcp_oauth_busy"); }
        let this=self.clone(); let s=server.clone(); let o=op.clone();
        std::thread::spawn(move || {
            let outcome=run(&this,&s,&o,owner,&reference,reuse,&guard,&store,&cancelled);
            if let Ok(mut entries)=this.operations.lock() { if let Some(item)=entries.get_mut(&(s,o)) { item.url=None; if item.state!="cancelled" && item.state!="authorized" { match outcome { Ok(_)=>{}, Err(code)=>{item.state=if code=="mcp_oauth_expired" {"expired"} else {"failed"}; item.code=Some(code); } } } } }
            this.active.fetch_sub(1,Ordering::AcqRel);
        });
        Ok(json!({"version":1,"provider":"notion","server_id":server,"config_revision":revision,"operation_id":op,"state":"preparing"}))
    }
    pub fn status(&self, server:&str, revision:u64, op:&str, cancel:bool) -> Result<Value,&'static str> {
        let mut entries=self.operations.lock().map_err(|_| "mcp_oauth_unavailable")?;
        let item=entries.get_mut(&(server.to_owned(),op.to_owned())).ok_or("mcp_oauth_not_found")?;
        if item.revision!=revision { return Err("mcp_configuration_changed"); }
        if !(item.guard)() { item.cancelled.store(true,Ordering::Release); item.state="failed"; item.url=None; item.reference=None; item.workspace=None; item.code=Some("mcp_configuration_changed"); }
        if cancel && matches!(item.state,"preparing"|"authorizing") { item.cancelled.store(true,Ordering::Release); item.state="cancelled"; item.url=None; }
        let mut result=json!({"version":1,"provider":"notion","server_id":server,"config_revision":revision,"operation_id":op,"state":item.state});
        let out=result.as_object_mut().unwrap();
        if let Some(url)=&item.url { out.insert("authorization_url".into(),json!(url)); }
        if let Some(reference)=&item.reference { out.insert("credential_ref".into(),json!(reference)); }
        if let Some(workspace)=&item.workspace { out.insert("workspace".into(),workspace.clone()); }
        if let Some(code)=item.code { out.insert("code".into(),json!(code)); }
        Ok(result)
    }
}
fn read_callback_headers(mut read:impl FnMut(&mut [u8],Duration)->std::io::Result<usize>) -> Result<Zeroizing<Vec<u8>>,&'static str> {
    const LIMIT:usize=8192;
    let deadline=Instant::now()+Duration::from_secs(3);
    let mut data=Zeroizing::new(Vec::new());
    let mut buffer=[0u8;1024];
    loop {
        if data.windows(4).any(|w|w==b"\r\n\r\n") {return Ok(data);}
        if data.len()>=LIMIT {return Err("mcp_oauth_callback_invalid");}
        let remaining=deadline.checked_duration_since(Instant::now()).filter(|d|!d.is_zero()).ok_or("mcp_oauth_callback_invalid")?;
        let capacity=(LIMIT-data.len()).min(buffer.len());
        let size=read(&mut buffer[..capacity],remaining).map_err(|_| "mcp_oauth_callback_invalid")?;
        if size==0 || size>capacity {return Err("mcp_oauth_callback_invalid");}
        data.extend_from_slice(&buffer[..size]);
    }
}
fn callback_code(target:&str, path:&str, state:&str) -> Result<Zeroizing<String>,&'static str> {
    let callback=url::Url::parse(&format!("http://127.0.0.1{}",target)).map_err(|_| "mcp_oauth_callback_invalid")?;
    if callback.path()!=format!("/{}",path) || callback.fragment().is_some() {return Err("mcp_oauth_callback_invalid");}
    let pairs:Vec<_>=callback.query_pairs().collect();
    let states:Vec<_>=pairs.iter().filter(|(k,_)|k=="state").collect(); let codes:Vec<_>=pairs.iter().filter(|(k,_)|k=="code").collect(); let issuers:Vec<_>=pairs.iter().filter(|(k,_)|k=="iss").collect();
    use subtle::ConstantTimeEq;
    if states.len()!=1 || codes.len()!=1 || issuers.len()>1 || issuers.first().is_some_and(|(_,v)| v.as_ref()!="https://mcp.notion.com") || pairs.iter().any(|(k,_)|k=="error") || !bool::from(states[0].1.as_bytes().ct_eq(state.as_bytes())) || !valid_text(&codes[0].1,8192) {return Err("mcp_oauth_callback_invalid");}
    Ok(Zeroizing::new(codes[0].1.to_string()))
}
fn alive(guard:&Guard, cancelled:&AtomicBool) -> Result<(),&'static str> { if cancelled.load(Ordering::Acquire) { Err("mcp_oauth_cancelled") } else if !(guard)() { Err("mcp_configuration_changed") } else { Ok(()) } }
fn run(manager:&Manager, server:&str, op:&str, owner:i64, reference:&str, reuse:Option<Reuse>, guard:&Guard, store:&Store, cancelled:&AtomicBool) -> Result<Option<Value>,&'static str> {
    let started=Instant::now();
    let (listener,path,redirect,existing_client) = match reuse {
        Some(existing) => {
            let (port,path)=parse_redirect(&existing.redirect_uri)?;
            let mut listener=None;
            for _ in 0..20 {
                alive(guard,cancelled)?;
                match TcpListener::bind((Ipv4Addr::LOCALHOST,port)) {
                    Ok(value)=>{listener=Some(value);break},
                    Err(e) if e.kind()==std::io::ErrorKind::AddrInUse=>std::thread::sleep(Duration::from_millis(100)),
                    Err(_)=>return Err("mcp_oauth_callback_unavailable"),
                }
            }
            (listener.ok_or("mcp_oauth_callback_unavailable")?,path,existing.redirect_uri,Some(existing.client_id))
        },
        None => {
            let listener=TcpListener::bind((Ipv4Addr::LOCALHOST,0)).map_err(|_| "mcp_oauth_callback_unavailable")?;
            let path=random()?; let redirect=format!("http://127.0.0.1:{}/{}",listener.local_addr().map_err(|_| "mcp_oauth_callback_unavailable")?.port(),path);
            (listener,path,redirect,None)
        },
    };
    listener.set_nonblocking(true).map_err(|_| "mcp_oauth_unavailable")?;
    let state=Zeroizing::new(random()?); let verifier=Zeroizing::new(random()?);
    let challenge=URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let rt=tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|_| "mcp_oauth_unavailable")?;
    let client=client()?; alive(guard,cancelled)?;
    let client_id=if let Some(id)=existing_client { id } else {
        let registration=rt.block_on(async { let r=client.post(REGISTER).json(&json!({"client_name":"GamjaBot","redirect_uris":[redirect],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none","scope":"default"})).send().await.map_err(|_| "mcp_oauth_provider_failed")?; body(r).await })?;
        alive(guard,cancelled)?;
        if registration.get("token_endpoint_auth_method").is_some_and(|v|v.as_str()!=Some("none")) || registration.get("client_secret").is_some_and(|v|!v.is_null()) {return Err("mcp_oauth_provider_failed");}
        Zeroizing::new(token_string(&registration,"client_id")?)
    };
    let mut url=url::Url::parse(AUTHORIZE).map_err(|_| "mcp_oauth_unavailable")?;
    url.query_pairs_mut().append_pair("response_type","code").append_pair("scope","default").append_pair("client_id",&client_id).append_pair("redirect_uri",&redirect).append_pair("state",&state).append_pair("code_challenge",&challenge).append_pair("code_challenge_method","S256");
    { let mut entries=manager.operations.lock().map_err(|_| "mcp_oauth_unavailable")?; let item=entries.get_mut(&(server.into(),op.into())).ok_or("mcp_oauth_cancelled")?; if item.state!="preparing" {return Err("mcp_oauth_cancelled");} item.state="authorizing"; item.url=Some(url.into()); }
    let code=loop {
        alive(guard,cancelled)?; if started.elapsed()>=Duration::from_secs(600) {return Err("mcp_oauth_expired");}
        match listener.accept() {
            Ok((mut stream, peer)) => {
                if !peer.ip().is_loopback() {return Err("mcp_oauth_callback_invalid");}
                let data=read_callback_headers(|buffer,remaining| {
                    stream.set_read_timeout(Some(remaining))?;
                    stream.read(buffer)
                })?;
                let request=std::str::from_utf8(&data).map_err(|_| "mcp_oauth_callback_invalid")?;
                let line=request.lines().next().ok_or("mcp_oauth_callback_invalid")?;
                let mut fields=line.split_whitespace(); let method=fields.next(); let target=fields.next().ok_or("mcp_oauth_callback_invalid")?;
                if method!=Some("GET") || fields.next()!=Some("HTTP/1.1") || fields.next().is_some() { return Err("mcp_oauth_callback_invalid"); }
                let code=callback_code(target,&path,&state)?;
                let _=stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: 46\r\nConnection: close\r\n\r\nYou may return to GamjaBot and close this tab.");
                break code;
            }, Err(e) if e.kind()==std::io::ErrorKind::WouldBlock=>std::thread::sleep(Duration::from_millis(100)), Err(_)=>return Err("mcp_oauth_callback_invalid")
        }
    };
    alive(guard,cancelled)?;
    let token=rt.block_on(post_form(&client,&[("grant_type","authorization_code"),("client_id",&client_id),("code",&code),("redirect_uri",&redirect),("code_verifier",&verifier)]))?;
    alive(guard,cancelled)?; if !token_type(&token) {return Err("mcp_oauth_provider_failed");}
    let bundle=Bundle {version:1,owner_id:owner,server_id:server.into(),endpoint:ENDPOINT.into(),client_id:client_id.to_string(),redirect_uri:redirect.clone(),access_token:token_string(&token,"access_token")?,refresh_token:token_string(&token,"refresh_token")?,expires_at:expiry(&token)?,workspace_id:workspace(&token,"workspace_id")?,workspace_name:workspace(&token,"workspace_name")?,user_id:workspace(&token,"user_id")?,bot_id:workspace(&token,"bot_id")?,refresh_pending:false};
    if !bundle.valid(owner,server) {return Err("mcp_oauth_provider_failed");}
    let data=Zeroizing::new(serde_json::to_vec(&bundle).map_err(|_| "mcp_oauth_provider_failed")?);
    let public=match (&bundle.workspace_id,&bundle.workspace_name) { (Some(id),name)=>Some(json!({"id":id,"name":name})), _=>None };
    // Serialize cancellation against the final owner/config check and durable write.
    let mut entries=manager.operations.lock().map_err(|_| "mcp_oauth_unavailable")?;
    let item=entries.get_mut(&(server.into(),op.into())).ok_or("mcp_oauth_cancelled")?;
    if item.state!="authorizing" || started.elapsed()>=Duration::from_secs(600) {return Err("mcp_oauth_expired");}
    alive(guard,cancelled)?; store(&data)?;
    item.state="authorized"; item.url=None; item.reference=Some(reference.into()); item.workspace=public.clone(); Ok(public)
}

pub(crate) fn access(mut bundle:Bundle, owner:i64, server:&str, guard:&Guard, store:&Store) -> Result<Zeroizing<String>,&'static str> {
    if !bundle.valid(owner,server) || !(guard)() {return Err("mcp_oauth_invalid");}
    if bundle.refresh_pending { return Err("mcp_oauth_reauthorization_required"); }
    if bundle.expires_at <= now().saturating_add(60) {
        // A crashed or uncertain refresh must never cause a silent replay of a rotated token.
        bundle.refresh_pending=true;
        let pending=Zeroizing::new(serde_json::to_vec(&bundle).map_err(|_| "mcp_oauth_invalid")?);
        if !(guard)() {return Err("mcp_configuration_changed");} store(&pending)?;
        let client=client()?; let rt=tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|_| "mcp_oauth_unavailable")?;
        let token=rt.block_on(post_form(&client,&[("grant_type","refresh_token"),("client_id",&bundle.client_id),("refresh_token",&bundle.refresh_token)]))?;
        if !(guard)() || !token_type(&token) {return Err("mcp_oauth_invalid");}
        for (key, original) in [("workspace_id",&bundle.workspace_id),("user_id",&bundle.user_id),("bot_id",&bundle.bot_id)] {
            let id=workspace(&token,key)?; if id.as_ref().is_some_and(|id| Some(id)!=original.as_ref()) {return Err("mcp_oauth_invalid");}
        }
        bundle.access_token=token_string(&token,"access_token")?;
        if token.get("refresh_token").is_some() {bundle.refresh_token=token_string(&token,"refresh_token")?;}
        bundle.expires_at=expiry(&token)?; bundle.refresh_pending=false;
        let data=Zeroizing::new(serde_json::to_vec(&bundle).map_err(|_| "mcp_oauth_invalid")?); if !(guard)() {return Err("mcp_configuration_changed");} store(&data)?;
    }
    if !(guard)() {return Err("mcp_configuration_changed");} Ok(Zeroizing::new(bundle.access_token.clone()))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn bundle() -> Bundle { Bundle { version:1,owner_id:7,server_id:"server".into(),endpoint:ENDPOINT.into(),client_id:"client".into(),redirect_uri:format!("http://127.0.0.1:12345/{}","a".repeat(43)),access_token:"access".into(),refresh_token:"refresh".into(),expires_at:1,workspace_id:Some("workspace".into()),workspace_name:None,user_id:None,bot_id:None,refresh_pending:false } }
    #[test]
    fn callback_headers_accept_split_reads_and_reject_incomplete_or_oversized() {
        let chunks:Vec<&[u8]>=vec![b"GET /p?sta",b"te=s&code=c HTTP/1.1\r",b"\nHost: 127.0.0.1\r\n",b"\r\n"];
        let mut iter=chunks.into_iter();
        let data=read_callback_headers(|out,_| { let chunk=iter.next().unwrap_or_default(); out[..chunk.len()].copy_from_slice(chunk); Ok(chunk.len()) }).unwrap();
        let line=std::str::from_utf8(&data).unwrap().lines().next().unwrap();
        let target=line.split_whitespace().nth(1).unwrap(); assert!(callback_code(target,"p","s").is_ok());
        assert!(read_callback_headers(|_,_|Ok(0)).is_err());
        assert!(read_callback_headers(|out,_|{out.fill(b'x');Ok(out.len())}).is_err());
    }
    #[test]
    fn callback_requires_one_matching_state_and_code() {
        assert!(callback_code("/p?state=s&code=c","p","s").is_ok());
        assert!(callback_code("/p?state=s&code=c&iss=https%3A%2F%2Fmcp.notion.com","p","s").is_ok());
        for target in ["/wrong?state=s&code=c","/p?state=wrong&code=c","/p?state=s&state=s&code=c","/p?state=s&code=c&code=d","/p?state=s&code=c&error=denied","/p?state=s&code=c&iss=https%3A%2F%2Fevil.example","/p?state=s&code=c&iss=https%3A%2F%2Fmcp.notion.com&iss=https%3A%2F%2Fmcp.notion.com"] {
            assert!(callback_code(target,"p","s").is_err());
        }
    }
    #[test]
    fn reauthorization_reuses_registered_client_and_redirect_only_for_same_binding() {
        let b=bundle(); let reuse=Reuse::from_bundle(&b,7,"server").unwrap();
        assert_eq!(reuse.client_id.as_str(),b.client_id); assert_eq!(reuse.redirect_uri,b.redirect_uri);
        assert!(Reuse::from_bundle(&b,8,"server").is_err()); assert!(Reuse::from_bundle(&b,7,"other").is_err());
    }
    #[test]
    fn redirect_reuse_requires_exact_loopback_origin_and_path() {
        let valid=format!("http://127.0.0.1:12345/{}","a".repeat(43)); assert!(parse_redirect(&valid).is_ok());
        for invalid in [valid.replace("127.0.0.1","localhost"),valid.replace("http:","https:"),format!("{valid}?x=1"),valid.replace(":12345/",":012345/")] {assert!(parse_redirect(&invalid).is_err());}
    }
    #[test]
    fn bundle_is_bound_to_owner_server_and_endpoint() {
        let mut b=bundle(); assert!(b.valid(7,"server")); assert!(!b.valid(8,"server")); assert!(!b.valid(7,"other")); b.endpoint="https://example.com/mcp".into(); assert!(!b.valid(7,"server"));
    }
    #[test]
    fn interrupted_refresh_never_replays() {
        let mut b=bundle(); b.refresh_pending=true;
        let guard:Guard=Arc::new(||true); let store:Store=Arc::new(|_|panic!("must not store or access network"));
        assert_eq!(access(b,7,"server",&guard,&store).unwrap_err(),"mcp_oauth_reauthorization_required");
    }
    #[test]
    fn stale_operation_cannot_release_authorization_url() {
        let manager=Manager::default(); let cancelled=Arc::new(AtomicBool::new(false));
        manager.operations.lock().unwrap().insert(("server".into(),"op".into()),Operation{state:"authorizing",url:Some("private".into()),reference:None,workspace:None,code:None,cancelled:cancelled.clone(),revision:1,guard:Arc::new(||false)});
        let result=manager.status("server",1,"op",false).unwrap(); assert_eq!(result["state"],"failed"); assert!(result.get("authorization_url").is_none()); assert!(cancelled.load(Ordering::Acquire));
    }
    #[test]
    fn cancelled_operation_has_no_authorization_url() {
        let manager=Manager::default(); let cancelled=Arc::new(AtomicBool::new(false));
        manager.operations.lock().unwrap().insert(("server".into(),"op".into()),Operation{state:"authorizing",url:Some("private".into()),reference:None,workspace:None,code:None,cancelled:cancelled.clone(),revision:1,guard:Arc::new(||true)});
        let result=manager.status("server",1,"op",true).unwrap(); assert_eq!(result["state"],"cancelled"); assert!(result.get("authorization_url").is_none()); assert!(cancelled.load(Ordering::Acquire));
    }
}
