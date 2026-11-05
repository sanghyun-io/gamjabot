//! Small bounded loopback HTTP control surface. No redirects, proxies, browser origins or log bodies.
use crate::{ControlRequest, EndpointFile, Result, MAX_REPORT_CONTROL_BYTES};
use serde_json::Value;
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;
const MAX_HEADER_BYTES: usize = 8192;
const IO_TIMEOUT: Duration = Duration::from_millis(300);

pub fn request(endpoint: &EndpointFile, request: &ControlRequest) -> Result<Value> {
    let timeout = Duration::from_secs(if matches!(request, ControlRequest::ReportSubmit { .. }) {
        12
    } else {
        2
    });
    request_with_timeout(endpoint, request, timeout)
}

fn request_with_timeout(
    endpoint: &EndpointFile,
    request: &ControlRequest,
    timeout: Duration,
) -> Result<Value> {
    let deadline = Instant::now() + timeout;
    endpoint.validate().map_err(|_| "invalid_endpoint")?;
    request.validate().map_err(|_| "invalid_request")?;
    let url = url::Url::parse(&endpoint.control_url).map_err(|_| "invalid_endpoint")?;
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, url.port().ok_or("invalid_endpoint")?));
    let mut socket =
        TcpStream::connect_timeout(&address, response_budget(deadline)?.min(IO_TIMEOUT))
            .map_err(|_| "core_unavailable")?;
    socket
        .set_write_timeout(Some(response_budget(deadline)?.min(IO_TIMEOUT)))
        .map_err(|_| "core_unavailable")?;
    let body = zeroize::Zeroizing::new(serde_json::to_vec(request).map_err(|_| "invalid_request")?);
    write!(socket,"POST /control HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nX-Gamja-CLI-Capability: {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",url.port().unwrap(),endpoint.capability,body.len()).and_then(|_|socket.write_all(&body)).map_err(|_|"core_unavailable")?;
    let response = read_response(&mut socket, deadline)?;
    let text = std::str::from_utf8(&response).map_err(|_| "invalid_response")?;
    let (headers, body) = text.split_once("\r\n\r\n").ok_or("invalid_response")?;
    let status = headers
        .split_whitespace()
        .nth(1)
        .ok_or("invalid_response")?;
    let value: Value = serde_json::from_str(body).map_err(|_| "invalid_response")?;
    if status != "200" {
        return Err(match value["code"].as_str() {
            Some("engine_unavailable") => "engine_unavailable",
            Some("reporting_unavailable") => "reporting_unavailable",
            Some("submission_unavailable") => "submission_unavailable",
            Some("submission_pending") => "submission_pending",
            Some("report_rejected") => "report_rejected",
            Some("report_operation_failed") => "report_operation_failed",
            Some("native_capability_unavailable") => "native_capability_unavailable",
            Some("native_control_unconfirmed") => "native_control_unconfirmed",
            Some("native_settings_unavailable") => "native_settings_unavailable",
            Some("native_settings_unconfirmed") => "native_settings_unconfirmed",
            _ => "control_denied",
        });
    }
    Ok(value)
}
fn response_budget(deadline: Instant) -> Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|budget| !budget.is_zero())
        .ok_or("core_unavailable")
}

fn read_response(socket: &mut TcpStream, deadline: Instant) -> Result<Vec<u8>> {
    const MAX_RESPONSE_BYTES: usize = 73_728;
    let mut response = Vec::new();
    let mut buffer = [0u8; 4096];
    loop {
        socket
            .set_read_timeout(Some(response_budget(deadline)?))
            .map_err(|_| "core_unavailable")?;
        let remaining = (MAX_RESPONSE_BYTES + 1 - response.len()).min(buffer.len());
        let size = socket
            .read(&mut buffer[..remaining])
            .map_err(|_| "core_unavailable")?;
        if size == 0 {
            return Ok(response);
        }
        response.extend_from_slice(&buffer[..size]);
        if response.len() > MAX_RESPONSE_BYTES {
            return Err("invalid_response");
        }
    }
}

fn read_request(
    socket: &mut TcpStream,
    capability: &str,
) -> std::result::Result<ControlRequest, u16> {
    socket
        .set_read_timeout(Some(IO_TIMEOUT))
        .map_err(|_| 400u16)?;
    socket
        .set_write_timeout(Some(IO_TIMEOUT))
        .map_err(|_| 400u16)?;
    let deadline = Instant::now() + IO_TIMEOUT;
    let mut bytes = Vec::new();
    let mut next = [0u8; 1];
    while !bytes.ends_with(b"\r\n\r\n") {
        if bytes.len() >= MAX_HEADER_BYTES {
            return Err(413);
        }
        if read_before(socket, &mut next, deadline)? != 1 {
            return Err(400);
        }
        bytes.push(next[0]);
    }
    let headers = std::str::from_utf8(&bytes).map_err(|_| 400u16)?;
    let mut lines = headers.split("\r\n");
    if lines.next() != Some("POST /control HTTP/1.1") {
        return Err(400);
    }
    let mut length = None;
    let mut token = None;
    let mut host = false;
    for line in lines.filter(|line| !line.is_empty()) {
        let (name, value) = line.split_once(':').ok_or(400u16)?;
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err(400);
        }
        match name.to_ascii_lowercase().as_str() {
            "origin" => return Err(401),
            "transfer-encoding" => return Err(400),
            "content-length" => {
                if length.is_some() {
                    return Err(400);
                }
                length = Some(value.trim().parse::<usize>().map_err(|_| 400u16)?);
            }
            "x-gamja-cli-capability" => {
                if token.is_some() {
                    return Err(401);
                }
                token = Some(value.trim());
            }
            "host" => {
                let value = value.trim();
                let bare = value == "127.0.0.1";
                let with_port = value
                    .strip_prefix("127.0.0.1:")
                    .and_then(|port| port.parse::<u16>().ok())
                    .is_some_and(|port| socket.local_addr().is_ok_and(|addr| addr.port() == port));
                if host || !(bare || with_port) {
                    return Err(400);
                }
                host = true;
            }
            _ => {}
        }
    }
    let token = token.ok_or(401u16)?;
    if !host
        || token.len() != capability.len()
        || !bool::from(token.as_bytes().ct_eq(capability.as_bytes()))
    {
        return Err(401);
    }
    let length = length.ok_or(400u16)?;
    if length > MAX_REPORT_CONTROL_BYTES {
        return Err(413);
    }
    let mut body = zeroize::Zeroizing::new(vec![0u8; length]);
    let mut offset = 0;
    while offset < length {
        let size = read_before(socket, &mut body[offset..], deadline)?;
        if size == 0 {
            return Err(400);
        }
        offset += size;
    }
    ControlRequest::from_json(&body).map_err(|_| 400)
}
fn read_before(
    socket: &mut TcpStream,
    buffer: &mut [u8],
    deadline: Instant,
) -> std::result::Result<usize, u16> {
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return Err(400);
    }
    socket
        .set_read_timeout(Some(remaining))
        .map_err(|_| 400u16)?;
    socket.read(buffer).map_err(|_| 400)
}
pub fn serve_connection(
    mut socket: TcpStream,
    capability: &str,
    handle: impl FnOnce(ControlRequest) -> (u16, Value),
) {
    let (status, value) = match read_request(&mut socket, capability) {
        Ok(request) => handle(request),
        Err(status) => (
            status,
            serde_json::json!({"code":if status==401{"control_denied"}else{"invalid_request"}}),
        ),
    };
    let Ok(body) = serde_json::to_vec(&value) else {
        return;
    };
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        413 => "Payload Too Large",
        _ => "Service Unavailable",
    };
    let _=write!(socket,"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n",body.len()).and_then(|_|socket.write_all(&body));
    // Close the response half before bounded draining. Unread denied request bodies
    // otherwise cause a TCP reset that hides the typed rejection from clients.
    let _ = socket.shutdown(std::net::Shutdown::Write);
    if status != 200 {
        let deadline = Instant::now() + Duration::from_millis(50);
        let mut buffer = [0u8; MAX_REPORT_CONTROL_BYTES + 1];
        let mut drained = 0;
        while drained < buffer.len() {
            match read_before(&mut socket, &mut buffer[drained..], deadline) {
                Ok(0) | Err(_) => break,
                Ok(size) => drained += size,
            }
        }
    }
}
pub fn engine_ready(port: u16, session: &str) -> bool {
    let Ok(mut socket) =
        TcpStream::connect_timeout(&SocketAddr::from((Ipv4Addr::LOCALHOST, port)), IO_TIMEOUT)
    else {
        return false;
    };
    let _ = socket.set_read_timeout(Some(IO_TIMEOUT));
    let _ = socket.set_write_timeout(Some(IO_TIMEOUT));
    if write!(socket,"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Gamja-Session: {session}\r\nConnection: close\r\n\r\n").is_err(){return false}
    let mut head = [0u8; 12];
    socket.read_exact(&mut head).is_ok() && head == *b"HTTP/1.1 200"
}

#[cfg(test)]
mod response_deadline_tests {
    use super::*;
    use std::{net::TcpListener, thread};
    #[test]
    fn reporting_control_response_deadline_bounds_continuously_progressing_peer() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let peer = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            for _ in 0..20 {
                if socket.write_all(b"x").is_err() {
                    break;
                }
                thread::sleep(Duration::from_millis(25));
            }
        });
        let mut socket = TcpStream::connect(address).unwrap();
        let timeout = Duration::from_millis(100);
        socket.set_read_timeout(Some(timeout)).unwrap();
        let start = Instant::now();
        assert_eq!(
            read_response(&mut socket, start + timeout),
            Err("core_unavailable")
        );
        assert!(
            start.elapsed() < Duration::from_millis(300),
            "slow progress extended the total deadline"
        );
        drop(socket);
        peer.join().unwrap();
    }
}
