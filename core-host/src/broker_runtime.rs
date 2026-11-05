//! ARM Core owns a stable broker and delegates only the engine data endpoint.
use crate::{
    macos_spawn::OwnedChild,
    stable_broker::{read_frame, write_frame},
    Result,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{File, Metadata, OpenOptions},
    io::Read,
    net::Shutdown,
    os::{
        fd::AsRawFd,
        unix::{
            fs::{MetadataExt, OpenOptionsExt},
            net::UnixStream,
        },
    },
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

pub const BROKER_SHA256: &str = "2aa9a05ee834302b8eee08589642844611d1ad013248902f1ebd1813c1419553";
const ANCHOR: &str = "12035B8BBAB3E028E3433EDF37449688B3930438";
const CONTROL_TIMEOUT: Duration = Duration::from_secs(5);
fn lower_hash(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
fn identity(m: &Metadata) -> (u64, u64, u32, u32, u64, u64, i64, i64, i64, i64) {
    (
        m.dev(),
        m.ino(),
        m.uid(),
        m.mode(),
        m.nlink(),
        m.len(),
        m.mtime(),
        m.mtime_nsec(),
        m.ctime(),
        m.ctime_nsec(),
    )
}
pub struct SourcePin {
    path: PathBuf,
    file: File,
    metadata: Metadata,
    digest: String,
}
impl SourcePin {
    pub fn open(path: &Path, expected: Option<&str>) -> Result<Self> {
        if !path.is_absolute() || expected.is_some_and(|s| !lower_hash(s)) {
            return Err("broker_source_invalid");
        }
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "broker_source_invalid")?;
        let metadata = file.metadata().map_err(|_| "broker_source_invalid")?;
        if !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.mode() & 0o022 != 0
            || ![0, unsafe { libc::geteuid() }].contains(&metadata.uid())
        {
            return Err("broker_source_invalid");
        }
        let mut h = Sha256::new();
        let mut b = [0; 65536];
        loop {
            let n = file.read(&mut b).map_err(|_| "broker_source_invalid")?;
            if n == 0 {
                break;
            }
            h.update(&b[..n]);
        }
        let digest = format!("{:x}", h.finalize());
        if expected.is_some_and(|s| s != digest) {
            return Err("broker_source_mismatch");
        }
        let p = Self {
            path: path.to_owned(),
            file,
            metadata,
            digest,
        };
        p.revalidate()?;
        Ok(p)
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn sha256(&self) -> &str {
        &self.digest
    }
    pub fn revalidate(&self) -> Result<()> {
        let fd = self.file.metadata().map_err(|_| "broker_source_changed")?;
        let path = std::fs::symlink_metadata(&self.path).map_err(|_| "broker_source_changed")?;
        if identity(&fd) != identity(&self.metadata)
            || identity(&path) != identity(&self.metadata)
            || path.file_type().is_symlink()
        {
            return Err("broker_source_changed");
        }
        Ok(())
    }
    fn signed(&self, role: &str) -> Result<()> {
        use core_foundation::url::CFURL;
        use security_framework::os::macos::code_signing::{Flags, SecRequirement, SecStaticCode};
        use std::str::FromStr;
        let r =
            SecRequirement::from_str(&format!("anchor = H\"{ANCHOR}\" and identifier \"{role}\""))
                .map_err(|_| "broker_signature_invalid")?;
        let url = CFURL::from_path(&self.path, false).ok_or("broker_signature_invalid")?;
        let c =
            SecStaticCode::from_path(&url, Flags::NONE).map_err(|_| "broker_signature_invalid")?;
        c.check_validity(Flags::STRICT_VALIDATE | Flags::NO_NETWORK_ACCESS, &r)
            .map_err(|_| "broker_signature_invalid")?;
        self.revalidate()
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum State {
    Initial,
    Bootstrapped,
    Bound,
    Closed,
}
/// One bounded, terminal control channel. It never handles credential data.
pub struct ControlChannel {
    stream: Option<UnixStream>,
    timeout: Duration,
    state: State,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Status {
    status: String,
}
impl ControlChannel {
    pub fn new(stream: UnixStream, timeout: Duration) -> Result<Self> {
        if timeout.is_zero() || Instant::now().checked_add(timeout).is_none() {
            return Err("broker_control_invalid");
        }
        stream
            .set_nonblocking(true)
            .map_err(|_| "broker_control_unavailable")?;
        Ok(Self {
            stream: Some(stream),
            timeout,
            state: State::Initial,
        })
    }
    fn exchange(&mut self, request: impl Serialize, expected: &str) -> Result<()> {
        let deadline = Instant::now()
            .checked_add(self.timeout)
            .ok_or("broker_control_invalid")?;
        let result = (|| {
            let stream = self.stream.as_mut().ok_or("broker_control_closed")?;
            let body = serde_json::to_vec(&request).map_err(|_| "broker_control_invalid")?;
            write_frame(stream, &body, deadline).map_err(|_| "broker_control_unavailable")?;
            let response =
                read_frame(stream, deadline).map_err(|_| "broker_control_unavailable")?;
            let status: Status =
                serde_json::from_slice(&response).map_err(|_| "broker_control_invalid")?;
            if status.status != expected {
                return Err("broker_control_rejected");
            };
            Ok(())
        })();
        if result.is_err() {
            self.close();
        }
        result
    }
    pub fn bootstrap(&mut self, path: &Path, hash: &str) -> Result<()> {
        if self.state != State::Initial
            || !path.is_absolute()
            || path.to_str().is_none()
            || !lower_hash(hash)
        {
            return Err("broker_control_invalid");
        }
        self.exchange(serde_json::json!({"version":1,"operation":"bootstrap","authority_path":path,"authority_sha256":hash}),"bootstrapped")?;
        self.state = State::Bootstrapped;
        Ok(())
    }
    pub fn bind(
        &mut self,
        parent: &Path,
        parent_pid: u32,
        engine: &Path,
        engine_pid: u32,
    ) -> Result<()> {
        if self.state != State::Bootstrapped
            || !parent.is_absolute()
            || !engine.is_absolute()
            || parent.to_str().is_none()
            || engine.to_str().is_none()
            || !(2..=i32::MAX as u32).contains(&parent_pid)
            || !(2..=i32::MAX as u32).contains(&engine_pid)
        {
            return Err("broker_control_invalid");
        }
        self.exchange(serde_json::json!({"version":1,"operation":"bind","parent_path":parent,"parent_pid":parent_pid,"engine_path":engine,"engine_pid":engine_pid}),"bound")?;
        self.state = State::Bound;
        Ok(())
    }
    pub fn is_bound(&self) -> bool {
        self.state == State::Bound
    }
    pub fn shutdown(&mut self) -> Result<()> {
        if self.state != State::Bound {
            return Err("broker_control_closed");
        }
        let result = self.exchange(
            serde_json::json!({"version":1,"operation":"shutdown"}),
            "shutdown",
        );
        self.close();
        result
    }
    pub fn close(&mut self) {
        if let Some(s) = self.stream.take() {
            let _ = s.shutdown(Shutdown::Both);
        }
        self.state = State::Closed;
    }
}
impl Drop for ControlChannel {
    fn drop(&mut self) {
        self.close();
    }
}

struct Package {
    manifest: SourcePin,
    core: SourcePin,
    engine: SourcePin,
    broker: SourcePin,
    authority: SourcePin,
}
#[derive(Deserialize)]
struct Manifest {
    files: std::collections::BTreeMap<String, Entry>,
}
#[derive(Deserialize)]
struct Entry {
    sha256: Option<String>,
    bytes: Option<u64>,
    executable: Option<bool>,
}
impl Package {
    fn open(core: &Path, engine: &Path, engine_hash: &str) -> Result<Self> {
        let root = core
            .parent()
            .and_then(Path::parent)
            .ok_or("broker_package_invalid")?;
        let private = root.join("libexec/gamja");
        if engine != private.join("gamja-engine") {
            return Err("broker_package_invalid");
        }
        let path = root.join("core-manifest.json");
        // Check size before any unbounded read/hash of a hostile manifest.
        let m = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&path)
            .map_err(|_| "broker_package_invalid")?;
        if m.metadata().map_err(|_| "broker_package_invalid")?.len() > 2 * 1024 * 1024 {
            return Err("broker_package_invalid");
        }
        let mut data = Vec::new();
        m.take(2 * 1024 * 1024 + 1)
            .read_to_end(&mut data)
            .map_err(|_| "broker_package_invalid")?;
        if data.len() > 2 * 1024 * 1024 {
            return Err("broker_package_invalid");
        }
        let manifest = SourcePin::open(&path, Some(&format!("{:x}", Sha256::digest(&data))))?;
        let parsed: Manifest =
            serde_json::from_slice(&data).map_err(|_| "broker_package_invalid")?;
        let entry = |name: &str| -> Result<&str> {
            let e = parsed.files.get(name).ok_or("broker_package_invalid")?;
            let s = e.sha256.as_deref().ok_or("broker_package_invalid")?;
            if !lower_hash(s) || e.bytes.is_none() || e.executable != Some(true) {
                return Err("broker_package_invalid");
            };
            Ok(s)
        };
        if entry("libexec/gamja/gamja-engine")? != engine_hash
            || entry("libexec/gamja/gamja-credential-broker")? != BROKER_SHA256
        {
            return Err("broker_package_invalid");
        }
        let core = SourcePin::open(core, Some(entry("bin/gamja")?))?;
        let engine = SourcePin::open(engine, Some(engine_hash))?;
        let broker = SourcePin::open(
            &private.join("gamja-credential-broker"),
            Some(BROKER_SHA256),
        )?;
        let authority = SourcePin::open(
            &private.join("gamja-credential-release-authority"),
            Some(entry("libexec/gamja/gamja-credential-release-authority")?),
        )?;
        broker.signed("top.gamja.credential-broker.v1")?;
        authority.signed("top.gamja.credential-release-authority.v1")?;
        let p = Self {
            manifest,
            core,
            engine,
            broker,
            authority,
        };
        p.revalidate()?;
        Ok(p)
    }
    fn revalidate(&self) -> Result<()> {
        for p in [
            &self.manifest,
            &self.core,
            &self.engine,
            &self.broker,
            &self.authority,
        ] {
            p.revalidate()?;
        }
        Ok(())
    }
}

/// Independent MCP credential broker: no Engine credential descriptor is created.
/// Its binary pin is supplied at Core compile time, not by the package manifest.
struct McpPackage {
    manifest: SourcePin,
    core: SourcePin,
    engine: SourcePin,
    broker: SourcePin,
    authority: SourcePin,
}
impl McpPackage {
    fn open(core:&Path,engine:&Path,engine_hash:&str)->Result<Self> {
        let pin=option_env!("GAMJA_MCP_BROKER_SHA256").filter(|v|lower_hash(v)).ok_or("mcp_broker_pin_unavailable")?;
        if pin==BROKER_SHA256 {return Err("mcp_broker_pin_invalid");}
        let root=core.parent().and_then(Path::parent).ok_or("mcp_broker_package_invalid")?;
        let private=root.join("libexec/gamja");
        if engine!=private.join("gamja-engine") {return Err("mcp_broker_package_invalid");}
        let path=root.join("core-manifest.json");
        let file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&path).map_err(|_| "mcp_broker_package_invalid")?;
        if file.metadata().map_err(|_| "mcp_broker_package_invalid")?.len()>2*1024*1024 {return Err("mcp_broker_package_invalid");}
        let mut data=Vec::new();file.take(2*1024*1024+1).read_to_end(&mut data).map_err(|_| "mcp_broker_package_invalid")?;
        if data.len()>2*1024*1024 {return Err("mcp_broker_package_invalid");}
        let manifest=SourcePin::open(&path,Some(&format!("{:x}",Sha256::digest(&data))))?;
        let parsed:Manifest=serde_json::from_slice(&data).map_err(|_| "mcp_broker_package_invalid")?;
        let entry=|name:&str|->Result<&str>{
            let item=parsed.files.get(name).ok_or("mcp_broker_package_invalid")?;
            let hash=item.sha256.as_deref().ok_or("mcp_broker_package_invalid")?;
            if !lower_hash(hash)||item.bytes.is_none()||item.executable!=Some(true){return Err("mcp_broker_package_invalid");}Ok(hash)
        };
        if entry("libexec/gamja/gamja-engine")?!=engine_hash || entry("libexec/gamja/gamja-mcp-credential-broker")?!=pin {return Err("mcp_broker_package_invalid");}
        let core=SourcePin::open(core,Some(entry("bin/gamja")?))?;
        let engine=SourcePin::open(engine,Some(engine_hash))?;
        let broker=SourcePin::open(&private.join("gamja-mcp-credential-broker"),Some(pin))?;
        let authority=SourcePin::open(&private.join("gamja-mcp-credential-release-authority"),Some(entry("libexec/gamja/gamja-mcp-credential-release-authority")?))?;
        broker.signed("top.gamja.mcp-credential-broker.v1")?;
        authority.signed("top.gamja.mcp-credential-release-authority.v1")?;
        let package=Self{manifest,core,engine,broker,authority};package.revalidate()?;Ok(package)
    }
    fn revalidate(&self)->Result<()> {
        for pin in [&self.manifest,&self.core,&self.engine,&self.broker,&self.authority] {pin.revalidate()?;} Ok(())
    }
}

pub struct McpBrokerSession {
    child:OwnedChild,
    control:ControlChannel,
    package:McpPackage,
    internal:Option<UnixStream>,
    closed:bool,
}
impl McpBrokerSession {
    pub fn start(core:&Path,engine:&Path,engine_hash:&str)->Result<Self> {
        let package=McpPackage::open(core,engine,engine_hash)?;
        let (control,child_control)=UnixStream::pair().map_err(|_| "mcp_broker_channel_unavailable")?;
        let (internal,child_internal)=UnixStream::pair().map_err(|_| "mcp_broker_channel_unavailable")?;
        let mut control=ControlChannel::new(control,CONTROL_TIMEOUT)?;
        let stdin=File::open("/dev/null").map_err(|_| "mcp_broker_channel_unavailable")?;
        let stdout=OpenOptions::new().write(true).open("/dev/null").map_err(|_| "mcp_broker_channel_unavailable")?;
        let stderr=OpenOptions::new().write(true).open("/dev/null").map_err(|_| "mcp_broker_channel_unavailable")?;
        package.revalidate()?;
        let mut child=OwnedChild::spawn(package.broker.path(),&[] as &[&str],vec![],&[(stdin.as_raw_fd(),0),(stdout.as_raw_fd(),1),(stderr.as_raw_fd(),2),(child_control.as_raw_fd(),3),(child_internal.as_raw_fd(),4)],CONTROL_TIMEOUT).map_err(|_| "mcp_broker_spawn_failed")?;
        drop((stdin,stdout,stderr,child_control,child_internal));
        let result=package.revalidate().and_then(|_|crate::credential::verify_live_engine(child.pid(),&format!("anchor = H\"{ANCHOR}\" and identifier \"top.gamja.mcp-credential-broker.v1\""))).and_then(|_|control.bootstrap(package.authority.path(),package.authority.sha256())).and_then(|_|package.revalidate());
        if let Err(code)=result {control.close();return if child.shutdown(Duration::from_secs(2)).is_err(){Err("mcp_broker_cleanup_uncertain")}else{Err(code)};}
        Ok(Self{child,control,package,internal:Some(internal),closed:false})
    }
    pub fn take_internal(&mut self)->Result<UnixStream>{self.internal.take().ok_or("mcp_broker_channel_closed")}
    pub fn bind(&mut self,engine_pid:u32)->Result<()> {
        self.package.revalidate()?;
        self.control.bind(self.package.core.path(),std::process::id(),self.package.engine.path(),engine_pid)?;
        self.package.revalidate()
    }
    pub fn shutdown(&mut self)->Result<()> {
        if self.closed{return Ok(());}self.control.close();self.internal.take();
        self.child.shutdown(Duration::from_secs(2)).map_err(|_| "mcp_broker_cleanup_uncertain")?;self.closed=true;Ok(())
    }
}
impl Drop for McpBrokerSession {fn drop(&mut self){let _=self.shutdown();}}

/// Separately signed publisher broker; its internal socket never reaches Engine.
/// Independent Slack credential broker: no Engine credential descriptor is created.
/// Its binary pin is supplied at Core compile time, not by the package manifest.
struct SlackPackage {
    manifest: SourcePin,
    core: SourcePin,
    engine: SourcePin,
    broker: SourcePin,
    authority: SourcePin,
}
impl SlackPackage {
    fn open(core:&Path,engine:&Path,engine_hash:&str)->Result<Self> {
        let pin=option_env!("GAMJA_SLACK_BROKER_SHA256").filter(|v|lower_hash(v)).ok_or("slack_broker_pin_unavailable")?;
        if pin==BROKER_SHA256 || option_env!("GAMJA_MCP_BROKER_SHA256")==Some(pin) || option_env!("GAMJA_PUBLISHER_BROKER_SHA256")==Some(pin) || option_env!("GAMJA_VAULT_BROKER_SHA256")==Some(pin) {return Err("slack_broker_pin_invalid");}
        let root=core.parent().and_then(Path::parent).ok_or("slack_broker_package_invalid")?;
        let private=root.join("libexec/gamja");
        if engine!=private.join("gamja-engine") {return Err("slack_broker_package_invalid");}
        let path=root.join("core-manifest.json");
        let file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&path).map_err(|_| "slack_broker_package_invalid")?;
        if file.metadata().map_err(|_| "slack_broker_package_invalid")?.len()>2*1024*1024 {return Err("slack_broker_package_invalid");}
        let mut data=Vec::new();file.take(2*1024*1024+1).read_to_end(&mut data).map_err(|_| "slack_broker_package_invalid")?;
        if data.len()>2*1024*1024 {return Err("slack_broker_package_invalid");}
        let manifest=SourcePin::open(&path,Some(&format!("{:x}",Sha256::digest(&data))))?;
        let parsed:Manifest=serde_json::from_slice(&data).map_err(|_| "slack_broker_package_invalid")?;
        let entry=|name:&str|->Result<&str>{
            let item=parsed.files.get(name).ok_or("slack_broker_package_invalid")?;
            let hash=item.sha256.as_deref().ok_or("slack_broker_package_invalid")?;
            if !lower_hash(hash)||item.bytes.is_none()||item.executable!=Some(true){return Err("slack_broker_package_invalid");}Ok(hash)
        };
        if entry("libexec/gamja/gamja-engine")?!=engine_hash || entry("libexec/gamja/gamja-slack-credential-broker")?!=pin {return Err("slack_broker_package_invalid");}
        let core=SourcePin::open(core,Some(entry("bin/gamja")?))?;
        let engine=SourcePin::open(engine,Some(engine_hash))?;
        let broker=SourcePin::open(&private.join("gamja-slack-credential-broker"),Some(pin))?;
        let authority=SourcePin::open(&private.join("gamja-slack-credential-release-authority"),Some(entry("libexec/gamja/gamja-slack-credential-release-authority")?))?;
        broker.signed("top.gamja.slack-credential-broker.v1")?;
        authority.signed("top.gamja.slack-credential-release-authority.v1")?;
        let package=Self{manifest,core,engine,broker,authority};package.revalidate()?;Ok(package)
    }
    fn revalidate(&self)->Result<()> {
        for pin in [&self.manifest,&self.core,&self.engine,&self.broker,&self.authority] {pin.revalidate()?;} Ok(())
    }
}

pub struct SlackBrokerSession {
    child:OwnedChild,
    control:ControlChannel,
    package:SlackPackage,
    internal:Option<UnixStream>,
    closed:bool,
}
impl SlackBrokerSession {
    pub fn start(core:&Path,engine:&Path,engine_hash:&str)->Result<Self> {
        let package=SlackPackage::open(core,engine,engine_hash)?;
        let (control,child_control)=UnixStream::pair().map_err(|_| "slack_broker_channel_unavailable")?;
        let (internal,child_internal)=UnixStream::pair().map_err(|_| "slack_broker_channel_unavailable")?;
        let mut control=ControlChannel::new(control,CONTROL_TIMEOUT)?;
        let stdin=File::open("/dev/null").map_err(|_| "slack_broker_channel_unavailable")?;
        let stdout=OpenOptions::new().write(true).open("/dev/null").map_err(|_| "slack_broker_channel_unavailable")?;
        let stderr=OpenOptions::new().write(true).open("/dev/null").map_err(|_| "slack_broker_channel_unavailable")?;
        package.revalidate()?;
        let mut child=OwnedChild::spawn(package.broker.path(),&[] as &[&str],vec![],&[(stdin.as_raw_fd(),0),(stdout.as_raw_fd(),1),(stderr.as_raw_fd(),2),(child_control.as_raw_fd(),3),(child_internal.as_raw_fd(),4)],CONTROL_TIMEOUT).map_err(|_| "slack_broker_spawn_failed")?;
        drop((stdin,stdout,stderr,child_control,child_internal));
        let result=package.revalidate().and_then(|_|crate::credential::verify_live_engine(child.pid(),&format!("anchor = H\"{ANCHOR}\" and identifier \"top.gamja.slack-credential-broker.v1\""))).and_then(|_|control.bootstrap(package.authority.path(),package.authority.sha256())).and_then(|_|package.revalidate());
        if let Err(code)=result {control.close();return if child.shutdown(Duration::from_secs(2)).is_err(){Err("slack_broker_cleanup_uncertain")}else{Err(code)};}
        Ok(Self{child,control,package,internal:Some(internal),closed:false})
    }
    pub fn take_internal(&mut self)->Result<UnixStream>{self.internal.take().ok_or("slack_broker_channel_closed")}
    pub fn bind(&mut self,engine_pid:u32)->Result<()> {
        self.package.revalidate()?;
        self.control.bind(self.package.core.path(),std::process::id(),self.package.engine.path(),engine_pid)?;
        self.package.revalidate()
    }
    pub fn shutdown(&mut self)->Result<()> {
        if self.closed{return Ok(());}self.control.close();self.internal.take();
        self.child.shutdown(Duration::from_secs(2)).map_err(|_| "slack_broker_cleanup_uncertain")?;self.closed=true;Ok(())
    }
}
impl Drop for SlackBrokerSession {fn drop(&mut self){let _=self.shutdown();}}

/// Separately signed publisher broker; its internal socket never reaches Engine.
struct PublisherPackage {
    manifest: SourcePin,
    core: SourcePin,
    engine: SourcePin,
    broker: SourcePin,
    authority: SourcePin,
}
impl PublisherPackage {
    fn open(core:&Path,engine:&Path,engine_hash:&str)->Result<Self> {
        let pin=option_env!("GAMJA_PUBLISHER_BROKER_SHA256").filter(|v|lower_hash(v)).ok_or("publisher_broker_pin_unavailable")?;
        if pin==BROKER_SHA256 || option_env!("GAMJA_MCP_BROKER_SHA256")==Some(pin) {return Err("publisher_broker_pin_invalid");}
        let root=core.parent().and_then(Path::parent).ok_or("publisher_broker_package_invalid")?;
        let private=root.join("libexec/gamja");
        if engine!=private.join("gamja-engine") {return Err("publisher_broker_package_invalid");}
        let path=root.join("core-manifest.json");
        let file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&path).map_err(|_| "publisher_broker_package_invalid")?;
        if file.metadata().map_err(|_| "publisher_broker_package_invalid")?.len()>2*1024*1024 {return Err("publisher_broker_package_invalid");}
        let mut data=Vec::new();file.take(2*1024*1024+1).read_to_end(&mut data).map_err(|_| "publisher_broker_package_invalid")?;
        if data.len()>2*1024*1024 {return Err("publisher_broker_package_invalid");}
        let manifest=SourcePin::open(&path,Some(&format!("{:x}",Sha256::digest(&data))))?;
        let parsed:Manifest=serde_json::from_slice(&data).map_err(|_| "publisher_broker_package_invalid")?;
        let entry=|name:&str|->Result<&str>{
            let item=parsed.files.get(name).ok_or("publisher_broker_package_invalid")?;
            let hash=item.sha256.as_deref().ok_or("publisher_broker_package_invalid")?;
            if !lower_hash(hash)||item.bytes.is_none()||item.executable!=Some(true){return Err("publisher_broker_package_invalid");}Ok(hash)
        };
        if entry("libexec/gamja/gamja-engine")?!=engine_hash || entry("libexec/gamja/gamja-template-publisher-credential-broker")?!=pin {return Err("publisher_broker_package_invalid");}
        let core=SourcePin::open(core,Some(entry("bin/gamja")?))?;
        let engine=SourcePin::open(engine,Some(engine_hash))?;
        let broker=SourcePin::open(&private.join("gamja-template-publisher-credential-broker"),Some(pin))?;
        let authority=SourcePin::open(&private.join("gamja-template-publisher-credential-release-authority"),Some(entry("libexec/gamja/gamja-template-publisher-credential-release-authority")?))?;
        broker.signed("top.gamja.template-publisher-credential-broker.v1")?;
        authority.signed("top.gamja.template-publisher-credential-release-authority.v1")?;
        let package=Self{manifest,core,engine,broker,authority};package.revalidate()?;Ok(package)
    }
    fn revalidate(&self)->Result<()> {
        for pin in [&self.manifest,&self.core,&self.engine,&self.broker,&self.authority] {pin.revalidate()?;} Ok(())
    }
}

pub struct PublisherBrokerSession {
    child:OwnedChild,
    control:ControlChannel,
    package:PublisherPackage,
    internal:Option<UnixStream>,
    closed:bool,
}
impl PublisherBrokerSession {
    pub fn start(core:&Path,engine:&Path,engine_hash:&str)->Result<Self> {
        let package=PublisherPackage::open(core,engine,engine_hash)?;
        let (control,child_control)=UnixStream::pair().map_err(|_| "publisher_broker_channel_unavailable")?;
        let (internal,child_internal)=UnixStream::pair().map_err(|_| "publisher_broker_channel_unavailable")?;
        let mut control=ControlChannel::new(control,CONTROL_TIMEOUT)?;
        let stdin=File::open("/dev/null").map_err(|_| "publisher_broker_channel_unavailable")?;
        let stdout=OpenOptions::new().write(true).open("/dev/null").map_err(|_| "publisher_broker_channel_unavailable")?;
        let stderr=OpenOptions::new().write(true).open("/dev/null").map_err(|_| "publisher_broker_channel_unavailable")?;
        package.revalidate()?;
        let mut child=OwnedChild::spawn(package.broker.path(),&[] as &[&str],vec![],&[(stdin.as_raw_fd(),0),(stdout.as_raw_fd(),1),(stderr.as_raw_fd(),2),(child_control.as_raw_fd(),3),(child_internal.as_raw_fd(),4)],CONTROL_TIMEOUT).map_err(|_| "publisher_broker_spawn_failed")?;
        drop((stdin,stdout,stderr,child_control,child_internal));
        let result=package.revalidate().and_then(|_|crate::credential::verify_live_engine(child.pid(),&format!("anchor = H\"{ANCHOR}\" and identifier \"top.gamja.template-publisher-credential-broker.v1\""))).and_then(|_|control.bootstrap(package.authority.path(),package.authority.sha256())).and_then(|_|package.revalidate());
        if let Err(code)=result {control.close();return if child.shutdown(Duration::from_secs(2)).is_err(){Err("publisher_broker_cleanup_uncertain")}else{Err(code)};}
        Ok(Self{child,control,package,internal:Some(internal),closed:false})
    }
    pub fn take_internal(&mut self)->Result<UnixStream>{self.internal.take().ok_or("publisher_broker_channel_closed")}
    pub fn bind(&mut self,engine_pid:u32)->Result<()> {
        self.package.revalidate()?;
        self.control.bind(self.package.core.path(),std::process::id(),self.package.engine.path(),engine_pid)?;
        self.package.revalidate()
    }
    pub fn shutdown(&mut self)->Result<()> {
        if self.closed{return Ok(());}self.control.close();self.internal.take();
        self.child.shutdown(Duration::from_secs(2)).map_err(|_| "publisher_broker_cleanup_uncertain")?;self.closed=true;Ok(())
    }
}
impl Drop for PublisherBrokerSession {fn drop(&mut self){let _=self.shutdown();}}

// Dedicated Vault helper; never hands its data channel to Engine.
struct VaultPackage {
    manifest: SourcePin,
    core: SourcePin,
    engine: SourcePin,
    broker: SourcePin,
    authority: SourcePin,
}
impl VaultPackage {
    fn open(core:&Path,engine:&Path,engine_hash:&str)->Result<Self> {
        let pin=option_env!("GAMJA_VAULT_BROKER_SHA256").filter(|v|lower_hash(v)).ok_or("vault_broker_pin_unavailable")?;
        if pin==BROKER_SHA256 || (option_env!("GAMJA_MCP_BROKER_SHA256")==Some(pin) || option_env!("GAMJA_PUBLISHER_BROKER_SHA256")==Some(pin)) {return Err("vault_broker_pin_invalid");}
        let root=core.parent().and_then(Path::parent).ok_or("vault_broker_package_invalid")?;
        let private=root.join("libexec/gamja");
        if engine!=private.join("gamja-engine") {return Err("vault_broker_package_invalid");}
        let path=root.join("core-manifest.json");
        let file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&path).map_err(|_| "vault_broker_package_invalid")?;
        if file.metadata().map_err(|_| "vault_broker_package_invalid")?.len()>2*1024*1024 {return Err("vault_broker_package_invalid");}
        let mut data=Vec::new();file.take(2*1024*1024+1).read_to_end(&mut data).map_err(|_| "vault_broker_package_invalid")?;
        if data.len()>2*1024*1024 {return Err("vault_broker_package_invalid");}
        let manifest=SourcePin::open(&path,Some(&format!("{:x}",Sha256::digest(&data))))?;
        let parsed:Manifest=serde_json::from_slice(&data).map_err(|_| "vault_broker_package_invalid")?;
        let entry=|name:&str|->Result<&str>{
            let item=parsed.files.get(name).ok_or("vault_broker_package_invalid")?;
            let hash=item.sha256.as_deref().ok_or("vault_broker_package_invalid")?;
            if !lower_hash(hash)||item.bytes.is_none()||item.executable!=Some(true){return Err("vault_broker_package_invalid");}Ok(hash)
        };
        if entry("libexec/gamja/gamja-engine")?!=engine_hash || entry("libexec/gamja/gamja-vault-credential-broker")?!=pin {return Err("vault_broker_package_invalid");}
        let core=SourcePin::open(core,Some(entry("bin/gamja")?))?;
        let engine=SourcePin::open(engine,Some(engine_hash))?;
        let broker=SourcePin::open(&private.join("gamja-vault-credential-broker"),Some(pin))?;
        let authority=SourcePin::open(&private.join("gamja-vault-credential-release-authority"),Some(entry("libexec/gamja/gamja-vault-credential-release-authority")?))?;
        broker.signed("top.gamja.vault-credential-broker.v1")?;
        authority.signed("top.gamja.vault-credential-release-authority.v1")?;
        let package=Self{manifest,core,engine,broker,authority};package.revalidate()?;Ok(package)
    }
    fn revalidate(&self)->Result<()> {
        for pin in [&self.manifest,&self.core,&self.engine,&self.broker,&self.authority] {pin.revalidate()?;} Ok(())
    }
}

pub struct VaultBrokerSession {
    child:OwnedChild,
    control:ControlChannel,
    package:VaultPackage,
    internal:Option<UnixStream>,
    closed:bool,
}
impl VaultBrokerSession {
    pub fn start(core:&Path,engine:&Path,engine_hash:&str)->Result<Self> {
        let package=VaultPackage::open(core,engine,engine_hash)?;
        let (control,child_control)=UnixStream::pair().map_err(|_| "vault_broker_channel_unavailable")?;
        let (internal,child_internal)=UnixStream::pair().map_err(|_| "vault_broker_channel_unavailable")?;
        let mut control=ControlChannel::new(control,CONTROL_TIMEOUT)?;
        let stdin=File::open("/dev/null").map_err(|_| "vault_broker_channel_unavailable")?;
        let stdout=OpenOptions::new().write(true).open("/dev/null").map_err(|_| "vault_broker_channel_unavailable")?;
        let stderr=OpenOptions::new().write(true).open("/dev/null").map_err(|_| "vault_broker_channel_unavailable")?;
        package.revalidate()?;
        let mut child=OwnedChild::spawn(package.broker.path(),&[] as &[&str],vec![],&[(stdin.as_raw_fd(),0),(stdout.as_raw_fd(),1),(stderr.as_raw_fd(),2),(child_control.as_raw_fd(),3),(child_internal.as_raw_fd(),4)],CONTROL_TIMEOUT).map_err(|_| "vault_broker_spawn_failed")?;
        drop((stdin,stdout,stderr,child_control,child_internal));
        let result=package.revalidate().and_then(|_|crate::credential::verify_live_engine(child.pid(),&format!("anchor = H\"{ANCHOR}\" and identifier \"top.gamja.vault-credential-broker.v1\""))).and_then(|_|control.bootstrap(package.authority.path(),package.authority.sha256())).and_then(|_|package.revalidate());
        if let Err(code)=result {control.close();return if child.shutdown(Duration::from_secs(2)).is_err(){Err("vault_broker_cleanup_uncertain")}else{Err(code)};}
        Ok(Self{child,control,package,internal:Some(internal),closed:false})
    }
    pub(crate) fn is_live(&mut self)->Result<bool> {
        if self.closed {return Ok(false);}
        self.package.revalidate()?;
        self.child.try_wait().map(|status| status.is_none()).map_err(|_| "vault_broker_unavailable")
    }
    pub(crate) fn trusted_manifest(&self)->Result<&str> { self.package.revalidate()?; Ok(self.package.manifest.sha256()) }
    pub fn take_internal(&mut self)->Result<UnixStream>{self.internal.take().ok_or("vault_broker_channel_closed")}
    pub fn bind(&mut self,engine_pid:u32)->Result<()> {
        self.package.revalidate()?;
        self.control.bind(self.package.core.path(),std::process::id(),self.package.engine.path(),engine_pid)?;
        self.package.revalidate()
    }
    pub fn shutdown(&mut self)->Result<()> {
        if self.closed{return Ok(());}self.control.close();self.internal.take();
        self.child.shutdown(Duration::from_secs(2)).map_err(|_| "vault_broker_cleanup_uncertain")?;self.closed=true;Ok(())
    }
}
impl Drop for VaultBrokerSession {fn drop(&mut self){let _=self.shutdown();}}

pub struct BrokerSession {
    child: OwnedChild,
    control: ControlChannel,
    package: Package,
    engine_data: Option<UnixStream>,
    internal: Option<UnixStream>,
    closed: bool,
}
impl BrokerSession {
    pub fn start(core: &Path, engine: &Path, engine_hash: &str) -> Result<Self> {
        let package = Package::open(core, engine, engine_hash)?;
        let (control, broker_control) =
            UnixStream::pair().map_err(|_| "broker_channel_unavailable")?;
        let (engine_data, broker_data) =
            UnixStream::pair().map_err(|_| "broker_channel_unavailable")?;
        let (internal, broker_internal) =
            UnixStream::pair().map_err(|_| "broker_channel_unavailable")?;
        let mut control = ControlChannel::new(control, CONTROL_TIMEOUT)?;
        let n0 = File::open("/dev/null").map_err(|_| "broker_channel_unavailable")?;
        let n1 = OpenOptions::new()
            .write(true)
            .open("/dev/null")
            .map_err(|_| "broker_channel_unavailable")?;
        let n2 = OpenOptions::new()
            .write(true)
            .open("/dev/null")
            .map_err(|_| "broker_channel_unavailable")?;
        package.revalidate()?;
        let mut child = OwnedChild::spawn(
            package.broker.path(),
            &[] as &[&str],
            vec![],
            &[
                (n0.as_raw_fd(), 0),
                (n1.as_raw_fd(), 1),
                (n2.as_raw_fd(), 2),
                (broker_control.as_raw_fd(), 3),
                (broker_data.as_raw_fd(), 4),
                (broker_internal.as_raw_fd(), 5),
            ],
            CONTROL_TIMEOUT,
        )
        .map_err(|_| "broker_spawn_failed")?;
        drop((n0, n1, n2, broker_control, broker_data, broker_internal));
        let bootstrap = package
            .revalidate()
            .and_then(|_| {
                crate::credential::verify_live_engine(
                    child.pid(),
                    &format!(
                        "anchor = H\"{ANCHOR}\" and identifier \"top.gamja.credential-broker.v1\""
                    ),
                )
            })
            .and_then(|_| control.bootstrap(package.authority.path(), package.authority.sha256()))
            .and_then(|_| package.revalidate());
        if let Err(code) = bootstrap {
            control.close();
            return if child.shutdown(Duration::from_secs(2)).is_err() {
                Err("broker_cleanup_uncertain")
            } else {
                Err(code)
            };
        }
        Ok(Self {
            child,
            control,
            package,
            engine_data: Some(engine_data),
            internal: Some(internal),
            closed: false,
        })
    }
    pub fn take_engine_data(&mut self) -> Result<UnixStream> {
        self.engine_data.take().ok_or("broker_channel_closed")
    }
    pub fn take_internal(&mut self) -> Result<UnixStream> {
        self.internal.take().ok_or("broker_channel_closed")
    }
    pub fn bind(&mut self, engine_pid: u32) -> Result<()> {
        self.package.revalidate()?;
        self.control.bind(
            self.package.core.path(),
            std::process::id(),
            self.package.engine.path(),
            engine_pid,
        )?;
        self.package.revalidate()
    }
    pub fn available(&mut self) -> Result<bool> {
        Ok(!self.closed
            && self.control.is_bound()
            && self
                .child
                .try_wait()
                .map_err(|_| "broker_process_unavailable")?
                .is_none())
    }
    pub fn shutdown(&mut self) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        // The engine group is stopped first by the owner. EOF may already have
        // stopped the broker; retain the leader and explicitly reclaim its group.
        self.control.close();
        self.engine_data.take();
        self.internal.take();
        self.child
            .shutdown(Duration::from_secs(2))
            .map_err(|_| "broker_cleanup_uncertain")?;
        self.closed = true;
        Ok(())
    }
}
impl Drop for BrokerSession {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

#[cfg(test)]
mod mcp_isolation_tests {
    use super::*;

    #[test]
    fn legacy_broker_remains_exact_and_mcp_has_separate_pin() {
        assert_eq!(BROKER_SHA256, "2aa9a05ee834302b8eee08589642844611d1ad013248902f1ebd1813c1419553");
        if let Some(pin) = option_env!("GAMJA_MCP_BROKER_SHA256") {
            assert!(lower_hash(pin));
            assert_ne!(pin, BROKER_SHA256);
        }
    }
}
