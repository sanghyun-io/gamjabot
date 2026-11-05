//! Internal, one-shot authenticated upgrade. No exported credential values or public login route.
use crate::Result;
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::os::fd::AsRawFd;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

const FRAME_LIMIT: usize = 8192;
const CHANNEL_TIMEOUT: Duration = Duration::from_secs(10);
const SOURCE_PROOF_TTL: Duration = Duration::from_secs(60);

// Startup is distinct from the authenticated transfer. Neither phase renews
// the original source proof or can run past its expiry.
fn source_phase_deadline(captured: Instant, now: Instant, budget: Duration) -> Result<Instant> {
    let expiry = captured + SOURCE_PROOF_TTL;
    if now >= expiry {
        return Err("upgrade_source_expired");
    }
    Ok((now + budget.min(SOURCE_PROOF_TTL)).min(expiry))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Begin {
    version: u8,
    kind: String,
    nonce: String,
    generation: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Complete {
    version: u8,
    kind: String,
    nonce: String,
    generation: String,
    status: String,
}
#[derive(Serialize)]
struct Ack<'a> {
    version: u8,
    kind: &'static str,
    nonce: &'a str,
    generation: &'a str,
    status: &'static str,
}
#[derive(Clone, PartialEq, Eq)]
struct LoginRow {
    owner_id: i64,
    revision: i64,
    reference: String,
    hash: Zeroizing<String>,
    expiry: String,
}
#[derive(Serialize)]
struct Grant<'a> {
    version: u8,
    kind: &'static str,
    nonce: &'a str,
    generation: &'a str,
    owner_id: i64,
    old_secret_ref: &'a str,
    revision: i64,
    expires_at: &'a str,
    old_secret_hash: &'a str,
    deadline_unix_ms: i64,
}
fn hex(value: &str, size: usize) -> bool {
    value.len() == size
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn read_frame<T: for<'a> Deserialize<'a>>(
    channel: &mut UnixStream,
    deadline: Instant,
) -> Result<T> {
    let mut length = [0; 4];
    read_exact(channel, &mut length, deadline)?;
    let size = u32::from_be_bytes(length) as usize;
    if size == 0 || size > FRAME_LIMIT {
        return Err("upgrade_channel_invalid");
    }
    let mut data = Zeroizing::new(vec![0; size]);
    read_exact(channel, &mut data, deadline)?;
    serde_json::from_slice(&data).map_err(|_| "upgrade_channel_invalid")
}
fn read_exact(channel: &mut UnixStream, data: &mut [u8], deadline: Instant) -> Result<()> {
    let mut position = 0;
    while position < data.len() {
        let remaining = deadline.checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero()).ok_or("upgrade_channel_unavailable")?;
        // macOS rejects SO_RCVTIMEO after peer close even with buffered bytes.
        // MSG_DONTWAIT leaves the socket's blocking mode unchanged; poll waits
        // only for the remaining total budget, and HUP still permits draining.
        let count = unsafe { libc::recv(channel.as_raw_fd(), data[position..].as_mut_ptr().cast(),
            data.len()-position, libc::MSG_DONTWAIT) };
        if count < 0 {
            match std::io::Error::last_os_error().kind() {
                std::io::ErrorKind::Interrupted => continue,
                std::io::ErrorKind::WouldBlock => {
                    let mut ready = libc::pollfd { fd: channel.as_raw_fd(), events: libc::POLLIN, revents: 0 };
                    let milliseconds = (remaining.as_millis()+1).min(i32::MAX as u128) as i32;
                    let status = unsafe { libc::poll(&mut ready, 1, milliseconds) };
                    if status < 0 && std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted {
                        return Err("upgrade_channel_unavailable");
                    }
                    continue;
                }
                _ => return Err("upgrade_channel_unavailable"),
            }
        }
        if count == 0 {
            return Err("upgrade_channel_unavailable");
        }
        position += count as usize;
    }
    Ok(())
}
fn write_frame<T: Serialize>(channel: &mut UnixStream, value: &T, deadline: Instant) -> Result<()> {
    let data = Zeroizing::new(serde_json::to_vec(value).map_err(|_| "upgrade_channel_invalid")?);
    if data.is_empty() || data.len() > FRAME_LIMIT {
        return Err("upgrade_channel_invalid");
    }
    let mut frame = Zeroizing::new(Vec::with_capacity(data.len() + 4));
    frame.extend_from_slice(&(data.len() as u32).to_be_bytes());
    frame.extend_from_slice(&data);
    let mut position = 0;
    while position < frame.len() {
        channel
            .set_write_timeout(Some(
                deadline
                    .checked_duration_since(Instant::now())
                    .filter(|d| !d.is_zero())
                    .ok_or("upgrade_channel_unavailable")?,
            ))
            .map_err(|_| "upgrade_channel_unavailable")?;
        let count = channel
            .write(&frame[position..])
            .map_err(|_| "upgrade_channel_unavailable")?;
        if count == 0 {
            return Err("upgrade_channel_unavailable");
        }
        position += count;
    }
    Ok(())
}

fn at_phase<T>(result: Result<T>, unavailable: &'static str) -> Result<T> {
    result.map_err(|code| if code == "upgrade_channel_unavailable" { unavailable } else { code })
}

fn read_frame_at_phase<T: for<'de> Deserialize<'de>>(
    channel: &mut UnixStream, deadline: Instant, unavailable: &'static str,
) -> Result<T> {
    at_phase(read_frame(channel, deadline), unavailable)
}

fn write_frame_at_phase<T: Serialize>(
    channel: &mut UnixStream, value: &T, deadline: Instant, unavailable: &'static str,
) -> Result<()> {
    at_phase(write_frame(channel, value, deadline), unavailable)
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
mod native {
    use super::*;
    use crate::{capability, control, ControlRequest};
    use chrono::{NaiveDateTime, Utc};
    use sha2::{Digest, Sha256};
    use std::{
        ffi::{CStr, CString},
        fs::{File, OpenOptions},
        net::{Ipv4Addr, SocketAddr, TcpStream},
        os::{
            fd::AsRawFd,
            unix::fs::{MetadataExt, OpenOptionsExt},
        },
        path::{Path, PathBuf},
    };

    const CORE_SHA: &str = "3949bc127f0033336a7bd8ca62f056ff3be889df7c9c46b26342561440031015";
    const ENGINE_SHA: &str = "d51286a02ceb39f405c451b250358761c43e28982a6178d8f17eac5d4f6a3349";
    const CORE_RULE: &str = "identifier \"top.gamja.credential-core\" and certificate leaf = H\"12035B8BBAB3E028E3433EDF37449688B3930438\" and cdhash H\"5808552eaf4f46dd5977ef482909ec61666e1979\"";
    const ENGINE_RULE: &str = "identifier \"top.gamja.engine\" and certificate leaf = H\"12035B8BBAB3E028E3433EDF37449688B3930438\" and cdhash H\"744cd8534c6518fb6103ca51e7994ff06f5e2142\"";
    #[derive(Clone, Copy, PartialEq, Eq)]
    struct Process {
        pid: i32,
        seconds: u64,
        micros: u64,
        parent: i32,
    }
    #[repr(C)]
    struct NativeRow {
        owner: i64,
        revision: i64,
        reference: [libc::c_char; 33],
        hash: [libc::c_char; 65],
        expiry: [libc::c_char; 65],
    }
    extern "C" {
        fn gamja_upgrade_process(
            pid: i32,
            seconds: *mut u64,
            micros: *mut u64,
            parent: *mut i32,
        ) -> i32;
        fn gamja_upgrade_peer(fd: i32, pid: i32, seconds: u64, micros: u64, parent: i32) -> i32;
        fn gamja_upgrade_file(pid: i32, device: u64, inode: u64) -> i32;
        fn gamja_upgrade_code(
            pid: i32,
            path: *const libc::c_char,
            requirement: *const libc::c_char,
        ) -> i32;
        fn gamja_upgrade_children(parent: i32, out: *mut i32, capacity: i32) -> i32;
        fn gamja_upgrade_row_read(path: *const libc::c_char, out: *mut NativeRow) -> i32;
        fn gamja_upgrade_home(out: *mut libc::c_char, capacity: i32) -> i32;
    }
    fn process(pid: i32) -> Result<Process> {
        let mut p = Process {
            pid,
            seconds: 0,
            micros: 0,
            parent: 0,
        };
        if unsafe { gamja_upgrade_process(pid, &mut p.seconds, &mut p.micros, &mut p.parent) } != 0
        {
            return Err("upgrade_source_untrusted");
        }
        Ok(p)
    }
    fn live(p: Process, path: &Path, rule: &str) -> Result<()> {
        let path = CString::new(path.as_os_str().as_encoded_bytes())
            .map_err(|_| "upgrade_source_untrusted")?;
        let rule = CString::new(rule).map_err(|_| "upgrade_source_untrusted")?;
        if process(p.pid)? != p
            || unsafe { gamja_upgrade_code(p.pid, path.as_ptr(), rule.as_ptr()) } != 0
        {
            return Err("upgrade_source_untrusted");
        }
        Ok(())
    }
    fn peer(socket: &TcpStream, p: Process) -> bool {
        (unsafe { gamja_upgrade_peer(socket.as_raw_fd(), p.pid, p.seconds, p.micros, p.parent) })
            == 0
    }
    #[derive(Clone, PartialEq, Eq)]
    struct Identity {
        device: u64,
        inode: u64,
        uid: u32,
        mode: u32,
        size: u64,
    }
    fn identity(file: &File, private: bool) -> Result<Identity> {
        let m = file.metadata().map_err(|_| "upgrade_source_untrusted")?;
        if !m.is_file()
            || m.uid() != unsafe { libc::geteuid() }
            || m.nlink() != 1
            || (private && m.mode() & 0o777 != 0o600)
            || (!private && m.mode() & 0o022 != 0)
        {
            return Err("upgrade_source_untrusted");
        }
        Ok(Identity {
            device: m.dev(),
            inode: m.ino(),
            uid: m.uid(),
            mode: m.mode(),
            size: m.len(),
        })
    }
    fn open(path: &Path) -> Result<File> {
        OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "upgrade_source_untrusted")
    }
    fn digest(mut file: File) -> Result<String> {
        let mut hash = Sha256::new();
        let mut buffer = [0u8; 8192];
        loop {
            let count = file
                .read(&mut buffer)
                .map_err(|_| "upgrade_source_untrusted")?;
            if count == 0 {
                break;
            }
            hash.update(&buffer[..count]);
        }
        Ok(format!("{:x}", hash.finalize()))
    }
    struct PinnedFile {
        path: PathBuf,
        file: File,
        identity: Identity,
        hash: String,
        private: bool,
    }
    impl PinnedFile {
        fn capture(path: PathBuf, private: bool, expected: Option<&str>) -> Result<Self> {
            let file = open(&path)?;
            let id = identity(&file, private)?;
            let hash = digest(open(&path)?)?;
            if expected.is_some_and(|e| e != hash) {
                return Err("upgrade_source_untrusted");
            }
            let pinned = Self {
                path,
                file,
                identity: id,
                hash,
                private,
            };
            pinned.check()?;
            Ok(pinned)
        }
        fn check(&self) -> Result<()> {
            if identity(&self.file, self.private)? != self.identity
                || identity(&open(&self.path)?, self.private)? != self.identity
                || digest(open(&self.path)?)? != self.hash
            {
                return Err("upgrade_source_changed");
            }
            Ok(())
        }
    }
    fn no_journal(path: &Path) -> Result<()> {
        for suffix in ["-wal", "-journal", "-shm"] {
            let mut name = path.as_os_str().to_os_string();
            name.push(suffix);
            if Path::new(&name)
                .try_exists()
                .map_err(|_| "upgrade_source_untrusted")?
            {
                return Err("upgrade_snapshot_not_quiescent");
            }
        }
        Ok(())
    }
    fn row(path: &Path) -> Result<LoginRow> {
        let path = CString::new(path.as_os_str().as_encoded_bytes())
            .map_err(|_| "upgrade_source_untrusted")?;
        let mut value: NativeRow = unsafe { std::mem::zeroed() };
        if unsafe { gamja_upgrade_row_read(path.as_ptr(), &mut value) } != 0 {
            return Err("upgrade_source_login_unavailable");
        }
        let reference = unsafe { CStr::from_ptr(value.reference.as_ptr()) }
            .to_str()
            .map_err(|_| "upgrade_source_untrusted")?
            .to_owned();
        let hash = Zeroizing::new(
            unsafe { CStr::from_ptr(value.hash.as_ptr()) }
                .to_str()
                .map_err(|_| "upgrade_source_untrusted")?
                .to_owned(),
        );
        let expiry = unsafe { CStr::from_ptr(value.expiry.as_ptr()) }
            .to_str()
            .map_err(|_| "upgrade_source_untrusted")?
            .to_owned();
        let date = NaiveDateTime::parse_from_str(&expiry, "%Y-%m-%d %H:%M:%S%.f")
            .or_else(|_| NaiveDateTime::parse_from_str(&expiry, "%Y-%m-%dT%H:%M:%S%.f"))
            .map_err(|_| "upgrade_source_untrusted")?;
        if value.owner <= 0
            || value.revision <= 0
            || !(reference == "current" || hex(&reference, 32))
            || !hex(&hash, 64)
            || date <= Utc::now().naive_utc()
        {
            return Err("upgrade_source_login_unavailable");
        }
        Ok(LoginRow {
            owner_id: value.owner,
            revision: value.revision,
            reference,
            hash,
            expiry,
        })
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Token {
        access_token: Zeroizing<String>,
        token_type: String,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct User {
        id: i64,
        is_active: bool,
        #[serde(rename = "email")]
        _email: serde::de::IgnoredAny,
        #[serde(rename = "username")]
        _username: serde::de::IgnoredAny,
        #[serde(rename = "created_at")]
        _created_at: serde::de::IgnoredAny,
    }
    fn request<T: for<'a> Deserialize<'a>>(
        socket: &mut TcpStream,
        p: Process,
        capability: &str,
        method: &str,
        path: &str,
        token: Option<&str>,
    ) -> Result<T> {
        live(p, &source_paths()?.2, ENGINE_RULE)?;
        if !peer(socket, p) {
            return Err("upgrade_source_untrusted");
        }
        let body = if method == "POST" { "{}" } else { "" };
        let port = socket
            .peer_addr()
            .map_err(|_| "upgrade_source_untrusted")?
            .port();
        let mut headers=Zeroizing::new(format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Gamja-CLI-Capability: {capability}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: keep-alive\r\n",body.len()));
        if let Some(token) = token {
            if token.len() < 32
                || token.len() > 4096
                || !token
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            {
                return Err("upgrade_source_login_unavailable");
            }
            headers.push_str("Authorization: Bearer ");
            headers.push_str(token);
            headers.push_str("\r\n");
        }
        headers.push_str("\r\n");
        headers.push_str(body);
        socket
            .write_all(headers.as_bytes())
            .map_err(|_| "upgrade_source_unavailable")?;
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut response = Zeroizing::new(Vec::new());
        let mut byte = [0];
        while !response.ends_with(b"\r\n\r\n") {
            if response.len() >= 8192 {
                return Err("upgrade_source_response_invalid");
            }
            socket
                .set_read_timeout(Some(
                    deadline
                        .checked_duration_since(Instant::now())
                        .ok_or("upgrade_source_unavailable")?,
                ))
                .map_err(|_| "upgrade_source_unavailable")?;
            socket
                .read_exact(&mut byte)
                .map_err(|_| "upgrade_source_unavailable")?;
            response.push(byte[0]);
        }
        let header =
            std::str::from_utf8(&response).map_err(|_| "upgrade_source_response_invalid")?;
        let mut lines = header.split("\r\n");
        if lines.next() != Some("HTTP/1.1 200 OK") {
            return Err("upgrade_source_login_unavailable");
        }
        let mut length = None;
        for line in lines.filter(|l| !l.is_empty()) {
            let (name, value) = line
                .split_once(':')
                .ok_or("upgrade_source_response_invalid")?;
            if name.eq_ignore_ascii_case("transfer-encoding") {
                return Err("upgrade_source_response_invalid");
            }
            if name.eq_ignore_ascii_case("content-length") {
                if length.is_some() {
                    return Err("upgrade_source_response_invalid");
                }
                length = Some(
                    value
                        .trim()
                        .parse::<usize>()
                        .map_err(|_| "upgrade_source_response_invalid")?,
                );
            }
        }
        let length = length
            .filter(|n| *n > 0 && *n <= 65536)
            .ok_or("upgrade_source_response_invalid")?;
        let mut body = Zeroizing::new(vec![0; length]);
        let mut position = 0;
        while position < body.len() {
            socket
                .set_read_timeout(Some(
                    deadline
                        .checked_duration_since(Instant::now())
                        .ok_or("upgrade_source_unavailable")?,
                ))
                .map_err(|_| "upgrade_source_unavailable")?;
            let n = socket
                .read(&mut body[position..])
                .map_err(|_| "upgrade_source_unavailable")?;
            if n == 0 {
                return Err("upgrade_source_response_invalid");
            }
            position += n;
        }
        live(p, &source_paths()?.2, ENGINE_RULE)?;
        serde_json::from_slice(&body).map_err(|_| "upgrade_source_response_invalid")
    }
    fn source_paths() -> Result<(PathBuf, PathBuf, PathBuf)> {
        let mut buffer = [0 as libc::c_char; 4096];
        if unsafe { gamja_upgrade_home(buffer.as_mut_ptr(), buffer.len() as i32) } != 0 {
            return Err("upgrade_source_untrusted");
        }
        let home = PathBuf::from(
            unsafe { CStr::from_ptr(buffer.as_ptr()) }
                .to_str()
                .map_err(|_| "upgrade_source_untrusted")?,
        );
        let runtime = home.join(".local/share/gamja-bot/core/runtime");
        Ok((
            home.join("Library/Application Support/top.gamja.bot"),
            runtime.join("bin/gamja"),
            runtime.join("libexec/gamja/gamja-engine"),
        ))
    }
    pub struct VerifiedSource {
        core: Process,
        launcher: Process,
        engine: Process,
        core_file: PinnedFile,
        engine_file: PinnedFile,
        database: PinnedFile,
        expected: LoginRow,
        captured: Instant,
    }
    impl VerifiedSource {
        pub fn authenticate(candidate: Option<&Path>) -> Result<Self> {
            let (data, core_path, engine_path) = source_paths()?;
            crate::platform::verify_directory(&data)?;
            let core_file = PinnedFile::capture(core_path, false, Some(CORE_SHA))?;
            let engine_file = PinnedFile::capture(engine_path, false, Some(ENGINE_SHA))?;
            let endpoint = capability::read(&data)?;
            let health = control::request(&endpoint, &ControlRequest::Health {})?;
            let pid = |key: &str| {
                health[key]
                    .as_i64()
                    .and_then(|v| i32::try_from(v).ok())
                    .filter(|v| *v > 1)
                    .ok_or("upgrade_source_untrusted")
            };
            let core = process(pid("core_pid")?)?;
            live(core, &core_file.path, CORE_RULE)?;
            let launcher = process(pid("engine_pid")?)?;
            live(launcher, &engine_file.path, ENGINE_RULE)?;
            if launcher.parent != core.pid {
                return Err("upgrade_source_untrusted");
            }
            let path = data.join("gamja_bot.db");
            no_journal(&path)?;
            let database = PinnedFile::capture(path, true, None)?;
            let expected = row(&database.path)?;
            if let Some(candidate) = candidate {
                crate::platform::verify_directory(candidate)?;
                if candidate
                    .canonicalize()
                    .map_err(|_| "upgrade_candidate_invalid")?
                    == data
                        .canonicalize()
                        .map_err(|_| "upgrade_source_untrusted")?
                {
                    return Err("upgrade_candidate_invalid");
                }
                let target = candidate.join("gamja_bot.db");
                no_journal(&target)?;
                let copy = PinnedFile::capture(target, true, None)?;
                if copy.identity.device == database.identity.device
                    && copy.identity.inode == database.identity.inode
                    || copy.hash != database.hash
                    || row(&copy.path)? != expected
                {
                    return Err("upgrade_candidate_invalid");
                }
            }
            let port = url::Url::parse(&endpoint.api_url)
                .map_err(|_| "upgrade_source_untrusted")?
                .port()
                .ok_or("upgrade_source_untrusted")?;
            let mut socket = TcpStream::connect_timeout(
                &SocketAddr::from((Ipv4Addr::LOCALHOST, port)),
                Duration::from_millis(200),
            )
            .map_err(|_| "upgrade_source_unavailable")?;
            socket
                .set_write_timeout(Some(Duration::from_secs(3)))
                .map_err(|_| "upgrade_source_unavailable")?;
            let mut candidates = [0; 32];
            let count =
                unsafe { gamja_upgrade_children(launcher.pid, candidates.as_mut_ptr(), 32) };
            if count < 0 {
                return Err("upgrade_source_untrusted");
            }
            let mut found = None;
            let deadline = Instant::now() + Duration::from_millis(100);
            while found.is_none() && Instant::now() < deadline {
                for pid in std::iter::once(launcher.pid)
                    .chain(candidates[..count as usize].iter().copied())
                {
                    if let Ok(p) = process(pid) {
                        if peer(&socket, p) {
                            live(p, &engine_file.path, ENGINE_RULE)?;
                            if found.is_some() {
                                return Err("upgrade_source_untrusted");
                            }
                            found = Some(p);
                        }
                    }
                }
                if found.is_none() {
                    std::thread::sleep(Duration::from_millis(1));
                }
            }
            let engine = found.ok_or("upgrade_source_untrusted")?;
            if unsafe {
                gamja_upgrade_file(
                    engine.pid,
                    database.identity.device,
                    database.identity.inode,
                )
            } != 0
            {
                return Err("upgrade_source_untrusted");
            }
            let value = Self {
                core,
                launcher,
                engine,
                core_file,
                engine_file,
                database,
                expected,
                captured: Instant::now(),
            };
            let token: Token = request(
                &mut socket,
                engine,
                &endpoint.capability,
                "POST",
                "/api/v1/auth/restore",
                None,
            )?;
            if token.token_type != "bearer" {
                return Err("upgrade_source_login_unavailable");
            }
            let user: User = request(
                &mut socket,
                engine,
                &endpoint.capability,
                "GET",
                "/api/v1/auth/me",
                Some(&token.access_token),
            )?;
            if user.id != value.expected.owner_id || !user.is_active {
                return Err("upgrade_source_login_unavailable");
            }
            value.check()?;
            Ok(value)
        }
        fn check(&self) -> Result<()> {
            if self.captured.elapsed() > SOURCE_PROOF_TTL {
                return Err("upgrade_source_expired");
            }
            live(self.core, &self.core_file.path, CORE_RULE)?;
            live(self.launcher, &self.engine_file.path, ENGINE_RULE)?;
            live(self.engine, &self.engine_file.path, ENGINE_RULE)?;
            self.core_file.check()?;
            self.engine_file.check()?;
            self.database.check()?;
            no_journal(&self.database.path)?;
            if row(&self.database.path)? != self.expected
                || unsafe {
                    gamja_upgrade_file(
                        self.engine.pid,
                        self.database.identity.device,
                        self.database.identity.inode,
                    )
                } != 0
            {
                return Err("upgrade_source_changed");
            }
            Ok(())
        }
        pub fn serve(self, mut channel: UnixStream, generation: &str, startup_timeout: Duration) -> Result<()> {
            let begin_deadline = source_phase_deadline(self.captured, Instant::now(), startup_timeout)?;
            let begin: Begin = read_frame_at_phase(&mut channel, begin_deadline, "upgrade_begin_unavailable")?;
            if begin.version != 1
                || begin.kind != "begin"
                || !hex(&begin.nonce, 64)
                || !hex(generation, 64)
                || begin.generation != generation
            {
                return Err("upgrade_channel_invalid");
            }
            self.check()?;
            let deadline = source_phase_deadline(self.captured, Instant::now(), CHANNEL_TIMEOUT)?;
            let remaining_ms = deadline.saturating_duration_since(Instant::now()).as_millis();
            if remaining_ms == 0 {
                return Err("upgrade_grant_unavailable");
            }
            write_frame_at_phase(
                &mut channel,
                &Grant {
                    version: 1,
                    kind: "grant",
                    nonce: &begin.nonce,
                    generation,
                    owner_id: self.expected.owner_id,
                    old_secret_ref: &self.expected.reference,
                    revision: self.expected.revision,
                    expires_at: &self.expected.expiry,
                    old_secret_hash: &self.expected.hash,
                    deadline_unix_ms: Utc::now().timestamp_millis() + remaining_ms as i64,
                },
                deadline,
                "upgrade_grant_unavailable",
            )?;
            let complete: Complete = read_frame_at_phase(&mut channel, deadline, "upgrade_complete_unavailable")?;
            if complete.version != 1
                || complete.kind != "complete"
                || complete.nonce != begin.nonce
                || complete.generation != generation
            {
                return Err("upgrade_channel_invalid");
            }
            if complete.status != "committed" {
                return Err("upgrade_credential_unavailable");
            }
            self.check()?;
            write_frame_at_phase(
                &mut channel,
                &Ack {
                    version: 1,
                    kind: "ack",
                    nonce: &begin.nonce,
                    generation,
                    status: "accepted",
                },
                deadline,
                "upgrade_ack_unavailable",
            )
        }
    }
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub use native::VerifiedSource;

#[cfg(test)]
mod tests {
    use super::*;
    fn raw(socket: &mut UnixStream, value: &[u8]) {
        socket
            .write_all(&(value.len() as u32).to_be_bytes())
            .unwrap();
        socket.write_all(value).unwrap();
    }
    #[test]
    fn typed_begin_rejects_duplicates_boolean_and_unknown() {
        for value in [
            br#"{"version":1,"version":1,"kind":"begin","nonce":"n","generation":"g"}"#.as_slice(),
            br#"{"version":true,"kind":"begin","nonce":"n","generation":"g"}"#.as_slice(),
            br#"{"version":1,"kind":"begin","nonce":"n","generation":"g","extra":1}"#.as_slice(),
            br#"{"version":1.0,"kind":"begin","nonce":"n","generation":"g"}"#.as_slice(),
        ] {
            let (mut core, mut engine) = UnixStream::pair().unwrap();
            raw(&mut engine, value);
            assert!(matches!(
                read_frame::<Begin>(&mut core, Instant::now() + Duration::from_millis(50)),
                Err("upgrade_channel_invalid")
            ));
        }
    }
    #[test]
    fn bounded_reader_rejects_large_empty_and_partial_frames() {
        for size in [0, 8193] {
            let (mut core, mut engine) = UnixStream::pair().unwrap();
            engine.write_all(&(size as u32).to_be_bytes()).unwrap();
            assert!(matches!(
                read_frame::<Begin>(&mut core, Instant::now() + Duration::from_millis(20)),
                Err("upgrade_channel_invalid")
            ));
        }
        let (mut core, mut engine) = UnixStream::pair().unwrap();
        engine.write_all(&10u32.to_be_bytes()).unwrap();
        engine.write_all(b"{").unwrap();
        drop(engine);
        assert!(matches!(
            read_frame::<Begin>(&mut core, Instant::now() + Duration::from_millis(20)),
            Err("upgrade_channel_unavailable")
        ));
    }
    #[test]
    fn partial_frame_has_one_total_deadline() {
        let (mut core, mut engine) = UnixStream::pair().unwrap();
        engine.write_all(&100u32.to_be_bytes()).unwrap();
        let start = Instant::now();
        assert!(matches!(
            read_frame::<Begin>(&mut core, start + Duration::from_millis(20)),
            Err("upgrade_channel_unavailable")
        ));
        assert!(start.elapsed() < Duration::from_secs(1));
    }
    #[test]
    fn actual_grant_writer_and_complete_reader_exchange() {
        let (mut core, mut engine) = UnixStream::pair().unwrap();
        let nonce = "a".repeat(64);
        let generation = "b".repeat(64);
        let thread = std::thread::spawn(move || {
            let grant: serde_json::Value =
                read_frame(&mut engine, Instant::now() + Duration::from_secs(1)).unwrap();
            assert_eq!(grant["owner_id"], 1);
            assert_eq!(grant["revision"], 3);
            assert_eq!(grant["kind"], "grant");
            write_frame(&mut engine,&serde_json::json!({"version":1,"kind":"complete","nonce":grant["nonce"],"generation":grant["generation"],"status":"committed"}),Instant::now()+Duration::from_secs(1)).unwrap();
        });
        write_frame(
            &mut core,
            &Grant {
                version: 1,
                kind: "grant",
                nonce: &nonce,
                generation: &generation,
                owner_id: 1,
                old_secret_ref: "current",
                revision: 3,
                expires_at: "2030-01-01 00:00:00",
                old_secret_hash: &"c".repeat(64),
                deadline_unix_ms: 100,
            },
            Instant::now() + Duration::from_secs(1),
        )
        .unwrap();
        // The complete frame remains valid after the sending engine closes.
        thread.join().unwrap();
        let complete: Complete =
            read_frame(&mut core, Instant::now() + Duration::from_secs(1)).unwrap();
        assert_eq!(complete.nonce, nonce);
        assert_eq!(complete.generation, generation);
        assert_eq!(complete.status, "committed");
    }
    #[test]
    fn transport_failure_identifies_begin_and_complete_waits() {
        let (mut core, engine) = UnixStream::pair().unwrap();
        drop(engine);
        assert!(matches!(read_frame_at_phase::<Begin>(&mut core,
            Instant::now()+Duration::from_secs(1), "upgrade_begin_unavailable"),
            Err("upgrade_begin_unavailable")));
        let (mut core, mut engine) = UnixStream::pair().unwrap();
        engine.write_all(&100u32.to_be_bytes()).unwrap();
        let start = Instant::now();
        assert!(matches!(read_frame_at_phase::<Complete>(&mut core,
            start+Duration::from_millis(20), "upgrade_complete_unavailable"),
            Err("upgrade_complete_unavailable")));
        assert!(start.elapsed()<Duration::from_secs(1));
    }
    #[test]
    fn invalid_frames_keep_validation_errors_instead_of_transport_phase() {
        let (mut core, mut engine) = UnixStream::pair().unwrap();
        raw(&mut engine, br#"{"version":true}"#);
        assert!(matches!(read_frame_at_phase::<Begin>(&mut core,
            Instant::now()+Duration::from_secs(1), "upgrade_begin_unavailable"),
            Err("upgrade_channel_invalid")));
    }
    #[test]
    fn write_failure_identifies_grant_and_ack_without_payload_details() {
        for phase in ["upgrade_grant_unavailable", "upgrade_ack_unavailable"] {
            let (mut core, engine) = UnixStream::pair().unwrap();
            drop(engine);
            assert_eq!(write_frame_at_phase(&mut core, &serde_json::json!({"version":1}),
                Instant::now()+Duration::from_secs(1), phase).unwrap_err(), phase);
        }
    }
    #[test]
    fn buffered_frame_survives_sender_close_without_changing_socket_mode() {
        let (mut core, mut engine) = UnixStream::pair().unwrap();
        let before = unsafe { libc::fcntl(core.as_raw_fd(), libc::F_GETFL) };
        raw(&mut engine, br#"{"version":1,"kind":"begin","nonce":"n","generation":"g"}"#);
        drop(engine);
        let begin: Begin = read_frame_at_phase(&mut core, Instant::now()+Duration::from_secs(1),
            "upgrade_begin_unavailable").unwrap();
        assert_eq!(begin.kind,"begin");
        assert_eq!(unsafe { libc::fcntl(core.as_raw_fd(), libc::F_GETFL) },before);
    }

    #[test]
    fn startup_wait_and_transfer_have_separate_budgets_without_renewing_source() {
        let captured = Instant::now();
        let after_startup = captured + Duration::from_secs(12);
        assert_eq!(source_phase_deadline(captured, captured, Duration::from_secs(45)).unwrap(), captured + Duration::from_secs(45));
        assert_eq!(source_phase_deadline(captured, after_startup, CHANNEL_TIMEOUT).unwrap(), captured + Duration::from_secs(22));
        let late = captured + Duration::from_secs(57);
        assert_eq!(source_phase_deadline(captured, late, CHANNEL_TIMEOUT).unwrap(), captured + SOURCE_PROOF_TTL);
        assert_eq!(source_phase_deadline(captured, captured, Duration::from_secs(120)).unwrap(), captured + SOURCE_PROOF_TTL);
        assert_eq!(source_phase_deadline(captured, captured, Duration::MAX).unwrap(), captured + SOURCE_PROOF_TTL);
        assert_eq!(source_phase_deadline(captured, captured + SOURCE_PROOF_TTL, CHANNEL_TIMEOUT).unwrap_err(), "upgrade_source_expired");
    }

    #[test]
    fn delayed_begin_uses_startup_window_and_incomplete_frame_keeps_one_deadline() {
        let (mut core, mut engine) = UnixStream::pair().unwrap();
        let captured = Instant::now();
        let sender = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(40));
            raw(&mut engine, br#"{"version":1,"kind":"begin","nonce":"n","generation":"g"}"#);
        });
        let begin: Begin = read_frame_at_phase(&mut core,
            source_phase_deadline(captured, captured, Duration::from_secs(1)).unwrap(), "upgrade_begin_unavailable").unwrap();
        assert_eq!(begin.kind, "begin");
        sender.join().unwrap();
        let (mut core, mut engine) = UnixStream::pair().unwrap();
        engine.write_all(&100u32.to_be_bytes()).unwrap();
        engine.write_all(b"{").unwrap();
        let now = Instant::now();
        let expiring_source = now - SOURCE_PROOF_TTL + Duration::from_millis(20);
        assert_eq!(read_frame_at_phase::<Begin>(&mut core,
            source_phase_deadline(expiring_source, now, Duration::from_secs(45)).unwrap(), "upgrade_begin_unavailable").err(), Some("upgrade_begin_unavailable"));
        assert!(now.elapsed() < Duration::from_secs(1));
    }
}
