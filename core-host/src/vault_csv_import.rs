//! Owner-authorized CSV drafts. All secret-bearing fields stay private and zeroize.
use crate::vault_csv::{self,ParsedLogins};
use serde::Serialize;
use std::{fs::File,io::Read,time::{Duration,Instant}};
use zeroize::{Zeroize,Zeroizing};

pub struct OwnedCsvInput(Zeroizing<Vec<u8>>);
impl OwnedCsvInput {
    /// Reads only an already Owner-selected open file; no path is returned.
    pub fn read(file:&mut File,cancel:&std::os::unix::net::UnixStream)->Result<Self,&'static str> {
        use std::os::{fd::AsRawFd,unix::fs::MetadataExt};
        let before=file.metadata().map_err(|_| "vault_unavailable")?;
        let mut volume:libc::statfs=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatfs(file.as_raw_fd(),&mut volume)}!=0 || volume.f_flags & (libc::MNT_LOCAL as u32)==0
            || !before.is_file() || before.len()>=vault_csv::MAX_FILE_BYTES as u64 {return Err("vault_unavailable");}
        let mut data=Zeroizing::new(Vec::new());let mut chunk=Zeroizing::new([0u8;65536]);
        loop {
            if !crate::vault_control::active(cancel) {return Err("vault_unavailable");}
            let n=file.read(chunk.as_mut()).map_err(|_| "vault_unavailable")?;
            if n==0 {break;}if data.len()+n>=vault_csv::MAX_FILE_BYTES {return Err("vault_unavailable");}
            data.extend_from_slice(&chunk[..n]);
        }
        let after=file.metadata().map_err(|_| "vault_unavailable")?;
        if !crate::vault_control::active(cancel) || data.len() as u64!=before.len() || before.dev()!=after.dev() || before.ino()!=after.ino() || before.len()!=after.len() || before.mtime()!=after.mtime() || before.mtime_nsec()!=after.mtime_nsec() {return Err("vault_unavailable");}
        Ok(Self(data))
    }
    pub(crate) fn bytes(&self)->&[u8] {&self.0}
}

struct Item {row:vault_csv::LoginRow, state:&'static str, item:Option<String>,command:Option<String>,revision:Option<u64>}
pub(crate) struct Draft {id:String,owner:u64,epoch:String,deadline:Instant, rows:Vec<Item>, errors:Vec<vault_csv::SafeRowError>}
#[derive(Serialize)] pub(crate) struct Summary {version:u8,draft_id:String,total:usize,invalid_count:usize,expires_in_seconds:u64}
#[derive(Serialize)] struct Row<'a> {row:usize,website:&'a str,username:&'a str,nickname:&'a str,hint:&'a str,state:&'static str}
#[derive(Serialize)] struct ErrorRow {row:usize,reason:&'static str}
#[derive(Serialize)] pub(crate) struct Page<'a> {version:u8,rows:Vec<Row<'a>>,errors:Vec<ErrorRow>,next_offset:Option<usize>}
#[derive(Serialize)] pub(crate) struct RowStatus {pub version:u8,pub status:&'static str,pub revision:Option<u64>,pub command_id:Option<String>}
impl Draft {
    pub(crate) fn new(owner:u64,epoch:&str,parsed:ParsedLogins)->Result<Self,&'static str> {
        let mut bytes=Zeroizing::new([0u8;16]);getrandom::fill(bytes.as_mut()).map_err(|_| "vault_unavailable")?;
        let id=bytes.iter().map(|b|format!("{b:02x}")).collect::<String>();
        Ok(Self{id,owner,epoch:epoch.to_owned(),deadline:Instant::now()+Duration::from_secs(300),rows:parsed.rows.into_iter().map(|row|Item{row,state:"pending",item:None,command:None,revision:None}).collect(),errors:parsed.errors})
    }
    pub(crate) fn current(&self,owner:u64,epoch:&str)->bool {self.owner==owner && self.epoch==epoch && Instant::now()<self.deadline}
    pub(crate) fn matches(&self,id:&str,owner:u64,epoch:&str)->bool {self.id==id && self.current(owner,epoch)}
    // Called only after current Owner/draft and the specific operation are validated.
    pub(crate) fn touch(&mut self) {self.deadline=Instant::now()+Duration::from_secs(300);}
    pub(crate) fn summary(&self)->Summary {Summary{version:1,draft_id:self.id.clone(),total:self.rows.len(),invalid_count:self.errors.len(),expires_in_seconds:self.deadline.saturating_duration_since(Instant::now()).as_secs()}}
    pub(crate) fn page(&self,offset:usize,limit:usize)->Result<Page<'_>,&'static str> {
        let total=self.rows.len()+self.errors.len();
        if limit==0 || limit>50 || offset>total {return Err("vault_unavailable");}
        let mut rows=Vec::new();let mut errors=Vec::new();let mut next=offset;let mut bytes=0usize;
        while next<total && next-offset<limit {
            if next<self.rows.len() {
                let x=&self.rows[next];let row=Row{row:x.row.row,website:&x.row.website,username:&x.row.username,nickname:&x.row.nickname,hint:&x.row.hint,state:x.state};
                let size=serde_json::to_vec(&row).map_err(|_| "vault_unavailable")?.len()+2;
                if bytes+size>55000 {break;} bytes+=size;rows.push(row);
            } else {
                let e=&self.errors[next-self.rows.len()];let row=ErrorRow{row:e.row,reason:e.reason};
                let size=serde_json::to_vec(&row).map_err(|_| "vault_unavailable")?.len()+2;
                if bytes+size>55000 {break;} bytes+=size;errors.push(row);
            }
            next+=1;
        }
        if next==offset && next<total {return Err("vault_unavailable");}
        Ok(Page{version:1,rows,errors,next_offset:(next<total).then_some(next)})
    }
    pub(crate) fn skip(&mut self,row:usize)->Result<RowStatus,&'static str> {
        let entry=self.rows.iter_mut().find(|v|v.row.row==row).ok_or("vault_unavailable")?;
        if entry.item.is_some() || entry.command.is_some() || !matches!(entry.state,"pending"|"rejected") {return Err("vault_unavailable");}
        self.deadline=Instant::now()+Duration::from_secs(300);
        entry.row.password.zeroize();entry.state="rejected";
        Ok(RowStatus{version:1,status:"rejected",revision:None,command_id:None})
    }
    pub(crate) fn bind(&mut self,row:usize,item:&str,website:&str,username:&str,command:&str)->bool {
        if self.rows.iter().any(|other|other.row.row!=row && (other.item.as_deref()==Some(item) || other.command.as_deref()==Some(command))) {return false;}
        let Some(entry)=self.rows.iter_mut().find(|v|v.row.row==row) else {return false;};
        if entry.row.website!=website || entry.row.username!=username {return false;}
        match (&entry.item,&entry.command) {
            (Some(bound_item),Some(bound_command))=>bound_item==item && bound_command==command,
            (None,None) if entry.state=="pending"=>{entry.item=Some(item.to_owned());entry.command=Some(command.to_owned());true},
            _=>false,
        }
    }
    pub(crate) fn store<B:crate::credential::CredentialBackend,T:crate::auth_broker::SyntheticAuthTransport>(&mut self, session:&mut crate::vault::VaultSession, auth:&crate::auth_broker::AuthBroker<B,T>, peer:&std::os::unix::net::UnixStream, stop:&std::sync::atomic::AtomicBool, row:usize, item:&str, command:&str)->Result<RowStatus,&'static str> {
        let entry=self.rows.iter_mut().find(|v|v.row.row==row).ok_or("vault_unavailable")?;
        if entry.state!="pending" || entry.item.as_deref()!=Some(item) || entry.command.as_deref()!=Some(command) {return Err("vault_unavailable");}
        self.deadline=Instant::now()+Duration::from_secs(300);
        entry.state="unknown";
        let result=session.replace(peer,stop,auth,self.owner,&self.epoch,item,0,command,&entry.row.website,&entry.row.username,&entry.row.password);
        match result {
            Ok(status) if status.status=="stored" && status.revision==Some(1) && status.command_id.as_deref()==Some(command)=>{entry.state="stored";entry.revision=Some(1);entry.row.password.zeroize();},
            Ok(status) if matches!(status.status.as_str(),"conflict"|"failed")=>{entry.state="rejected";entry.row.password.zeroize();},
            _=>{},
        }
        Ok(RowStatus{version:1,status:entry.state,revision:entry.revision,command_id:entry.command.clone()})
    }
    pub(crate) fn reconcile<B:crate::credential::CredentialBackend,T:crate::auth_broker::SyntheticAuthTransport>(&mut self,session:&mut crate::vault::VaultSession,auth:&crate::auth_broker::AuthBroker<B,T>,row:usize,item:&str,command:&str)->Result<RowStatus,&'static str> {
        let entry=self.rows.iter_mut().find(|v|v.row.row==row).ok_or("vault_unavailable")?;
        if entry.state!="unknown" || entry.item.as_deref()!=Some(item) || entry.command.as_deref()!=Some(command) {return Err("vault_unavailable");}
        self.deadline=Instant::now()+Duration::from_secs(300);
        let item=entry.item.as_deref().ok_or("vault_unavailable")?;
        let status=session.metadata_status(auth,self.owner,&self.epoch,item,&entry.row.website,&entry.row.username)?;
        if status.status=="present" && status.revision==Some(1) && status.command_id==entry.command {
            entry.state="stored";entry.revision=Some(1);entry.row.password.zeroize();
        }
        Ok(RowStatus{version:1,status:entry.state,revision:entry.revision,command_id:entry.command.clone()})
    }
}

// Strict reply decoding prevents any unrecognized Core payload reaching React.
#[derive(serde::Deserialize,serde::Serialize)] #[serde(deny_unknown_fields)]
struct SafeSummary {version:u8,draft_id:String,total:usize,invalid_count:usize,expires_in_seconds:u64}
#[derive(serde::Deserialize,serde::Serialize)] #[serde(deny_unknown_fields)]
struct SafeRow {row:usize,website:String,username:String,nickname:String,hint:String,state:String}
#[derive(serde::Deserialize,serde::Serialize)] #[serde(deny_unknown_fields)]
struct SafeError {row:usize,reason:String}
#[derive(serde::Deserialize,serde::Serialize)] #[serde(deny_unknown_fields)]
struct SafePage {version:u8,rows:Vec<SafeRow>,errors:Vec<SafeError>,next_offset:Option<usize>}
#[derive(serde::Deserialize,serde::Serialize)] #[serde(deny_unknown_fields)]
struct SafeStatus {version:u8,status:String,revision:Option<u64>,command_id:Option<String>}
#[derive(serde::Deserialize,serde::Serialize)] #[serde(deny_unknown_fields)]
struct SafeCancel {version:u8,status:String}
fn hex32(s:&str)->bool {s.len()==32 && s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
pub(crate) fn safe_reply(operation:&str,bytes:&[u8])->Result<serde_json::Value,&'static str> {
    match operation {
        "csv_choose"=>{let v:SafeSummary=serde_json::from_slice(bytes).map_err(|_| "vault_unavailable")?;
            if v.version!=1 || !hex32(&v.draft_id) || v.total>5000 || v.invalid_count>5000 || v.total+v.invalid_count>5000 || v.expires_in_seconds>300 {return Err("vault_unavailable");}
            serde_json::to_value(v).map_err(|_| "vault_unavailable")},
        "csv_page"=>{let v:SafePage=serde_json::from_slice(bytes).map_err(|_| "vault_unavailable")?;
            if v.version!=1 || v.rows.len()+v.errors.len()>50 || v.rows.iter().any(|r|r.row<2 || crate::vault::login_origin(&r.website).ok().as_deref()!=Some(r.website.as_str()) || !crate::vault::valid_username(&r.username) || r.nickname.len()>200 || r.hint.len()>512 || !matches!(r.state.as_str(),"pending"|"stored"|"unknown"|"rejected"))
                || v.errors.iter().any(|e|e.row<2 || !matches!(e.reason.as_str(),"column_count"|"unsupported_item_type"|"archived"|"invalid_website"|"invalid_username"|"invalid_nickname"|"missing_password"|"secret_too_large"|"duplicate")) {return Err("vault_unavailable");}
            serde_json::to_value(v).map_err(|_| "vault_unavailable")},
        "csv_store_row"|"csv_reconcile"|"csv_skip_row"=>{let v:SafeStatus=serde_json::from_slice(bytes).map_err(|_| "vault_unavailable")?;
            if v.version!=1 || !matches!(v.status.as_str(),"stored"|"unknown"|"rejected"|"unavailable") || v.command_id.as_deref().is_some_and(|s|!hex32(s)) {return Err("vault_unavailable");}
            serde_json::to_value(v).map_err(|_| "vault_unavailable")},
        "csv_cancel"=>{let v:SafeCancel=serde_json::from_slice(bytes).map_err(|_| "vault_unavailable")?;
            if v.version!=1 || v.status!="cancelled" {return Err("vault_unavailable");}serde_json::to_value(v).map_err(|_| "vault_unavailable")},
        _=>Err("vault_unavailable"),
    }
}
