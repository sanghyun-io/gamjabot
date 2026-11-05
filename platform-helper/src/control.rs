use crate::protocol::{
    hex, OwnerControl, OwnerOperation, PermissionStatus, SettingsPanel, VERSION,
};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
const TIMEOUT: Duration = Duration::from_secs(1);
const MAX_RESPONSE: usize = 4096;

pub(crate) fn call(
    endpoint: &str,
    key: &str,
    operation: OwnerOperation,
    panel: Option<SettingsPanel>,
) -> Result<Value, &'static str> {
    let mut id = [0; 16];
    getrandom::fill(&mut id).map_err(|_| "native_capability_unavailable")?;
    let payload = OwnerControl {
        version: VERSION,
        request_id: hex(&id),
        deadline_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
            + 1_000,
        operation,
        panel,
    };
    let body = serde_json::to_vec(&payload).map_err(|_| "native_control_unconfirmed")?;
    let mut key_bytes = [0; 32];
    if key.len() != 64 || !key.is_ascii() {
        return Err("native_capability_unavailable");
    }
    for (index, byte) in key_bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&key[index * 2..index * 2 + 2], 16)
            .map_err(|_| "native_capability_unavailable")?;
    }
    let mut inner = [0x36; 64];
    let mut outer = [0x5c; 64];
    for (index, byte) in key_bytes.iter().enumerate() {
        inner[index] ^= byte;
        outer[index] ^= byte;
    }
    let digest = Sha256::new()
        .chain_update(inner)
        .chain_update(&body)
        .finalize();
    let mac = Sha256::new()
        .chain_update(outer)
        .chain_update(digest)
        .finalize();
    let envelope =
        serde_json::to_vec(&serde_json::json!({"body":STANDARD.encode(body), "mac":hex(&mac)}))
            .map_err(|_| "native_control_unconfirmed")?;
    let mut stream = Stream::connect(endpoint)?;
    stream.write_all(&(envelope.len() as u32).to_be_bytes())?;
    stream.write_all(&envelope)?;
    let mut header = [0; 4];
    stream
        .read_exact(&mut header)
        .map_err(|_| "native_control_unconfirmed")?;
    let size = u32::from_be_bytes(header) as usize;
    if size > MAX_RESPONSE {
        return Err("native_control_unconfirmed");
    }
    let mut response = vec![0; size];
    stream.read_exact(&mut response)?;
    let response: Value =
        serde_json::from_slice(&response).map_err(|_| "native_control_unconfirmed")?;
    if response["state"] != "completed" {
        return Err(match response["code"].as_str() {
            Some("native_settings_unavailable") => "native_settings_unavailable",
            Some("native_settings_unconfirmed") => "native_settings_unconfirmed",
            _ => "native_control_unconfirmed",
        });
    }
    Ok(response)
}
pub(crate) fn permissions(value: Value) -> Result<PermissionStatus, &'static str> {
    serde_json::from_value(value["permissions"].clone()).map_err(|_| "native_control_unconfirmed")
}
#[cfg(unix)]
struct Stream {
    stream: std::os::unix::net::UnixStream,
    deadline: std::time::Instant,
}
#[cfg(unix)]
impl Stream {
    fn connect(endpoint: &str) -> Result<Self, &'static str> {
        use std::os::fd::FromRawFd;
        let deadline = std::time::Instant::now() + TIMEOUT;
        let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
        if endpoint.as_bytes().contains(&0) || endpoint.len() >= address.sun_path.len() {
            return Err("native_capability_unavailable");
        }
        address.sun_family = libc::AF_UNIX as libc::sa_family_t;
        #[cfg(target_os = "macos")]
        {
            address.sun_len = std::mem::size_of::<libc::sockaddr_un>() as u8;
        }
        for (slot, byte) in address.sun_path.iter_mut().zip(endpoint.bytes()) {
            *slot = byte as libc::c_char;
        }
        let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM, 0) };
        if fd < 0 {
            return Err("native_capability_unavailable");
        }
        let stream = unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) };
        if unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) } < 0 {
            return Err("native_capability_unavailable");
        }
        let result = unsafe {
            libc::connect(
                fd,
                (&address as *const libc::sockaddr_un).cast(),
                std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t,
            )
        };
        if result != 0 {
            let error = std::io::Error::last_os_error().raw_os_error();
            if !matches!(error, Some(libc::EINPROGRESS | libc::EAGAIN)) {
                return Err("native_capability_unavailable");
            }
            let mut ready = libc::pollfd {
                fd,
                events: libc::POLLOUT,
                revents: 0,
            };
            if unsafe { libc::poll(&mut ready, 1, TIMEOUT.as_millis() as i32) } <= 0 {
                return Err("native_control_unconfirmed");
            }
            let mut error = 0i32;
            let mut size = std::mem::size_of::<i32>() as libc::socklen_t;
            if unsafe {
                libc::getsockopt(
                    fd,
                    libc::SOL_SOCKET,
                    libc::SO_ERROR,
                    (&mut error as *mut i32).cast(),
                    &mut size,
                )
            } != 0
                || error != 0
            {
                return Err("native_capability_unavailable");
            }
        }
        Ok(Self { stream, deadline })
    }
    fn ready(&self, events: i16) -> Result<(), &'static str> {
        use std::os::fd::AsRawFd;
        loop {
            let remaining = self
                .deadline
                .saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                return Err("native_control_unconfirmed");
            }
            let mut descriptor = libc::pollfd {
                fd: self.stream.as_raw_fd(),
                events,
                revents: 0,
            };
            let result =
                unsafe { libc::poll(&mut descriptor, 1, remaining.as_millis().max(1) as i32) };
            if result > 0 && descriptor.revents & (events | libc::POLLHUP) != 0 {
                return Ok(());
            }
            if result < 0
                && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted
            {
                continue;
            }
            return Err("native_control_unconfirmed");
        }
    }
    fn write_all(&mut self, bytes: &[u8]) -> Result<(), &'static str> {
        let mut offset = 0;
        while offset < bytes.len() {
            self.ready(libc::POLLOUT)?;
            match self.stream.write(&bytes[offset..]) {
                Ok(0) => return Err("native_control_unconfirmed"),
                Ok(size) => offset += size,
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::Interrupted | std::io::ErrorKind::WouldBlock
                    ) => {}
                Err(_) => return Err("native_control_unconfirmed"),
            }
        }
        Ok(())
    }
    fn read_exact(&mut self, bytes: &mut [u8]) -> Result<(), &'static str> {
        let mut offset = 0;
        while offset < bytes.len() {
            self.ready(libc::POLLIN)?;
            match self.stream.read(&mut bytes[offset..]) {
                Ok(0) => return Err("native_control_unconfirmed"),
                Ok(size) => offset += size,
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::Interrupted | std::io::ErrorKind::WouldBlock
                    ) => {}
                Err(_) => return Err("native_control_unconfirmed"),
            }
        }
        Ok(())
    }
}
#[cfg(windows)]
struct Stream {
    file: std::fs::File,
    deadline: std::time::Instant,
}
#[cfg(windows)]
impl Stream {
    fn connect(endpoint: &str) -> Result<Self, &'static str> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::Pipes::SetNamedPipeHandleState;
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(endpoint)
            .map_err(|_| "native_capability_unavailable")?;
        let mode = 1u32; // PIPE_NOWAIT: every I/O loop shares one bounded deadline.
        if unsafe {
            SetNamedPipeHandleState(
                file.as_raw_handle().cast(),
                &mode,
                std::ptr::null(),
                std::ptr::null(),
            )
        } == 0
        {
            return Err("native_control_unconfirmed");
        }
        Ok(Self {
            file,
            deadline: std::time::Instant::now() + TIMEOUT,
        })
    }
    fn write_all(&mut self, bytes: &[u8]) -> Result<(), &'static str> {
        let mut offset = 0;
        while offset < bytes.len() {
            if std::time::Instant::now() >= self.deadline {
                return Err("native_control_unconfirmed");
            }
            match self.file.write(&bytes[offset..]) {
                Ok(size) if size > 0 => offset += size,
                _ => std::thread::sleep(Duration::from_millis(10)),
            }
        }
        Ok(())
    }
    fn read_exact(&mut self, bytes: &mut [u8]) -> Result<(), &'static str> {
        let mut offset = 0;
        while offset < bytes.len() {
            if std::time::Instant::now() >= self.deadline {
                return Err("native_control_unconfirmed");
            }
            match self.file.read(&mut bytes[offset..]) {
                Ok(size) if size > 0 => offset += size,
                _ => std::thread::sleep(Duration::from_millis(10)),
            }
        }
        Ok(())
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn completed_peer_frame_remains_readable_after_peer_close() {
        let (left, mut right) = std::os::unix::net::UnixStream::pair().unwrap();
        left.set_nonblocking(true).unwrap();
        right.write_all(&[0, 0, 0, 2]).unwrap();
        right.write_all(b"ok").unwrap();
        drop(right);
        let mut stream = Stream {
            stream: left,
            deadline: std::time::Instant::now() + TIMEOUT,
        };
        let mut header = [0; 4];
        stream.read_exact(&mut header).unwrap();
        assert_eq!(u32::from_be_bytes(header), 2);
        let mut body = [0; 2];
        stream
            .read_exact(&mut body)
            .expect("a completed peer frame must remain readable after close");
        assert_eq!(&body, b"ok");
    }
    #[test]
    fn slow_peer_progress_cannot_extend_control_deadline() {
        let (left, mut right) = std::os::unix::net::UnixStream::pair().unwrap();
        left.set_nonblocking(true).unwrap();
        let writer = std::thread::spawn(move || {
            for _ in 0..12 {
                std::thread::sleep(Duration::from_millis(150));
                if right.write_all(&[1]).is_err() {
                    break;
                }
            }
        });
        let start = std::time::Instant::now();
        let mut stream = Stream {
            stream: left,
            deadline: start + TIMEOUT,
        };
        let mut body = [0; 12];
        assert_eq!(
            stream.read_exact(&mut body),
            Err("native_control_unconfirmed")
        );
        assert!(start.elapsed() < Duration::from_millis(1400));
        drop(stream);
        writer.join().unwrap();
    }
}
