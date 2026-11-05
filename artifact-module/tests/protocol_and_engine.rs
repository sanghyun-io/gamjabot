use gamja_artifact::{
    read_frame, write_frame, ActionId, ArtifactError, ArtifactListItem, ArtifactListResponse,
    ArtifactListState, ArtifactOperation, ArtifactRequest, EngineAuth, EngineClient, RequestId,
    ResponseHeader, MAX_BODY_BYTES, MAX_EVENT_RESPONSE_BYTES, MAX_HEADER_BYTES,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(unix)]
use std::{
    ffi::{CStr, OsStr},
    os::unix::ffi::OsStrExt,
};
use std::{
    fs,
    io::{Cursor, Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const REQUEST_ID: &str = "550e8400-e29b-41d4-a716-446655440000";
const ACTION_ID: &str = "6ba7b810-9dad-41d1-80b4-00c04fd430c8";
const EVENT_ID: &str = "123e4567-e89b-42d3-a456-426614174000";
const DEVICE_ID: &str = "123e4567-e89b-42d3-b456-426614174001";
type EventMutation = (&'static str, Box<dyn Fn(&mut Value)>);

fn request_json(deadline_ms: u64) -> Value {
    json!({
        "version": 1,
        "request_id": REQUEST_ID,
        "deadline_ms": deadline_ms,
        "operation": "get",
        "room_id": 7,
        "action_id": ACTION_ID,
        "event_seq": 13,
        "auth": {
            "api_url": "http://127.0.0.1:41231",
            "session_token": "ab".repeat(32),
            "bearer_token": "synthetic-jwt"
        }
    })
}

fn list_request_json(deadline_ms: u64) -> Value {
    json!({
        "version": 1,
        "request_id": REQUEST_ID,
        "deadline_ms": deadline_ms,
        "operation": "list",
        "room_id": 7,
        "after_seq": 12,
        "limit": 100,
        "auth": {
            "api_url": "http://127.0.0.1:41231",
            "session_token": "ab".repeat(32),
            "bearer_token": "synthetic-jwt"
        }
    })
}

fn decode(value: &Value, now_ms: u64) -> Result<ArtifactRequest, ArtifactError> {
    ArtifactRequest::decode_json(&serde_json::to_vec(value).unwrap(), now_ms)
}

#[test]
fn protocol_accepts_one_strict_version_one_request() {
    let request = decode(&request_json(1_030_000), 1_000_000).unwrap();
    assert_eq!(request.request_id.as_str(), REQUEST_ID);
    assert_eq!(request.action_id.as_ref().unwrap().as_str(), ACTION_ID);
    assert_eq!(request.room_id, 7);
    assert_eq!(request.event_seq, Some(13));
}

#[test]
fn protocol_accepts_strict_list_scope_and_rejects_item_fields_or_bad_bounds() {
    let request = decode(&list_request_json(1_030_000), 1_000_000).unwrap();
    assert_eq!(request.operation, ArtifactOperation::List);
    assert_eq!((request.after_seq, request.limit), (Some(12), Some(100)));
    assert!(request.action_id.is_none() && request.event_seq.is_none());

    for (field, value) in [
        ("action_id", json!(ACTION_ID)),
        ("event_seq", json!(13)),
        ("limit", json!(0)),
        ("limit", json!(501)),
    ] {
        let mut malformed = list_request_json(1_020_000);
        malformed.as_object_mut().unwrap().insert(field.into(), value);
        assert_eq!(decode(&malformed, 1_000_000), Err(ArtifactError::InvalidRequest), "{field}");
    }
    for missing in ["after_seq", "limit"] {
        let mut malformed = list_request_json(1_020_000);
        malformed.as_object_mut().unwrap().remove(missing);
        assert_eq!(decode(&malformed, 1_000_000), Err(ArtifactError::InvalidRequest), "{missing}");
    }
    let mut item_with_cursor = request_json(1_020_000);
    item_with_cursor["after_seq"] = json!(0);
    assert_eq!(decode(&item_with_cursor, 1_000_000), Err(ArtifactError::InvalidRequest));
}

#[test]
fn protocol_list_body_is_strict_bounded_and_cursor_bound() {
    let response = ArtifactListResponse {
        version: 1,
        room_id: 7,
        after_seq: 12,
        scanned_through_seq: 14,
        next_after_seq: Some(14),
        items: vec![ArtifactListItem {
            room_id: 7,
            event_seq: 13,
            action_id: ActionId::parse(ACTION_ID).unwrap(),
            created_at: "2026-10-05T01:02:03.000000Z".into(),
            width: 1200,
            height: 800,
            state: ArtifactListState::Available,
        }],
    };
    let body = response.encode_json().unwrap();
    assert_eq!(
        ArtifactListResponse::decode_json(&body, 7, 12, 100).unwrap(),
        response
    );
    let header = ResponseHeader::json(RequestId::parse(REQUEST_ID).unwrap(), &body).unwrap();
    let mut framed = Vec::new();
    write_frame(&mut framed, &header, &body).unwrap();
    assert_eq!(read_frame(Cursor::new(framed)).unwrap().body, body);

    for mutate in [
        Box::new(|value: &mut Value| value["extra"] = json!(true)) as Box<dyn Fn(&mut Value)>,
        Box::new(|value: &mut Value| value["room_id"] = json!(8)),
        Box::new(|value: &mut Value| value["next_after_seq"] = json!(13)),
        Box::new(|value: &mut Value| value["items"][0]["event_seq"] = json!(12)),
        Box::new(|value: &mut Value| value["items"][0]["width"] = json!(0)),
    ] {
        let mut value = serde_json::to_value(&response).unwrap();
        mutate(&mut value);
        assert_eq!(
            ArtifactListResponse::decode_json(&serde_json::to_vec(&value).unwrap(), 7, 12, 100),
            Err(ArtifactError::InvalidMetadata)
        );
    }
}

#[test]
fn protocol_rejects_unknown_and_duplicate_fields() {
    let mut unknown = request_json(1_020_000);
    unknown
        .as_object_mut()
        .unwrap()
        .insert("extra".into(), json!(true));
    assert_eq!(
        decode(&unknown, 1_000_000),
        Err(ArtifactError::InvalidRequest)
    );

    let duplicate = format!(
        "{{\"version\":1,\"version\":1,\"request_id\":\"{REQUEST_ID}\",\"deadline_ms\":1020000,\"operation\":\"get\",\"room_id\":7,\"action_id\":\"{ACTION_ID}\",\"event_seq\":13,\"auth\":{{\"api_url\":\"http://127.0.0.1:41231\",\"session_token\":\"{}\",\"bearer_token\":\"synthetic-jwt\"}}}}",
        "ab".repeat(32)
    );
    assert_eq!(
        ArtifactRequest::decode_json(duplicate.as_bytes(), 1_000_000),
        Err(ArtifactError::InvalidRequest)
    );
}

#[test]
fn protocol_rejects_invalid_uuid_and_positive_identifiers() {
    for (field, value) in [
        ("request_id", json!("not-a-uuid")),
        ("action_id", json!("6BA7B810-9DAD-41D1-80B4-00C04FD430C8")),
        ("room_id", json!(0)),
        ("event_seq", json!(0)),
    ] {
        let mut malformed = request_json(1_020_000);
        malformed
            .as_object_mut()
            .unwrap()
            .insert(field.into(), value);
        assert_eq!(
            decode(&malformed, 1_000_000),
            Err(ArtifactError::InvalidRequest),
            "{field}"
        );
    }
}

#[test]
fn protocol_rejects_expired_and_more_than_thirty_second_deadlines() {
    assert_eq!(
        decode(&request_json(999_999), 1_000_000),
        Err(ArtifactError::DeadlineExpired)
    );
    assert_eq!(
        decode(&request_json(1_030_001), 1_000_000),
        Err(ArtifactError::InvalidRequest)
    );
    assert!(decode(&request_json(1_030_000), 1_000_000).is_ok());
}

#[test]
fn protocol_frames_exact_ten_megabytes_and_rejects_one_more() {
    let body = vec![0x5a; MAX_BODY_BYTES];
    let request_id = RequestId::parse(REQUEST_ID).unwrap();
    let header = ResponseHeader::png(request_id, 4000, 2500, &body).unwrap();
    let mut encoded = Vec::new();
    write_frame(&mut encoded, &header, &body).unwrap();
    let frame = read_frame(Cursor::new(encoded)).unwrap();
    assert_eq!(frame.body.len(), MAX_BODY_BYTES);
    assert_eq!(frame.header.byte_count, MAX_BODY_BYTES as u64);

    let oversized = vec![0; MAX_BODY_BYTES + 1];
    let request_id = RequestId::parse(REQUEST_ID).unwrap();
    assert_eq!(
        ResponseHeader::png(request_id, 1, 1, &oversized),
        Err(ArtifactError::BodyTooLarge)
    );
}

#[test]
fn protocol_rejects_malformed_private_frames() {
    let malformed = [
        vec![],
        vec![0, 0, 0],
        vec![0, 0, 0, 0],
        ((MAX_HEADER_BYTES as u32 + 1).to_be_bytes()).to_vec(),
        [4u32.to_be_bytes().as_slice(), b"null"].concat(),
        [32u32.to_be_bytes().as_slice(), b"{}"].concat(),
    ];
    for bytes in malformed {
        assert_eq!(
            read_frame(Cursor::new(bytes)),
            Err(ArtifactError::InvalidFrame)
        );
    }

    let body = b"abc";
    let header = ResponseHeader::png(RequestId::parse(REQUEST_ID).unwrap(), 1, 1, body).unwrap();
    let mut encoded = Vec::new();
    write_frame(&mut encoded, &header, body).unwrap();
    encoded.pop();
    assert_eq!(
        read_frame(Cursor::new(encoded)),
        Err(ArtifactError::InvalidFrame)
    );
}

#[test]
fn protocol_errors_are_fixed_and_do_not_echo_sensitive_input() {
    let secret = "secret-token-that-must-not-escape";
    let mut malformed = request_json(1_020_000);
    malformed["auth"]["bearer_token"] = json!(secret);
    malformed["operation"] = json!(secret);
    let error = decode(&malformed, 1_000_000).unwrap_err();
    assert_eq!(error.code(), "invalid_request");
    assert_eq!(error.to_string(), "invalid_request");
    assert!(!error.to_string().contains(secret));
}

fn canonical_event(width: u64, height: u64) -> Value {
    json!([{
        "event_id": EVENT_ID,
        "device_id": DEVICE_ID,
        "room_id": 7,
        "seq": 13,
        "actor_type": "tool",
        "actor_id": null,
        "actor_name_snapshot": "Tool",
        "kind": "tool_result",
        "payload": {
            "call_id": "screen-call",
            "state": "completed",
            "text": "Synthetic screenshot captured.",
            "screenshot_ref": format!("rooms/7/screenshots/{ACTION_ID}.png"),
            "image_mime": "image/png",
            "image_width": width,
            "image_height": height,
            "agent_id": null,
            "action_id": ACTION_ID,
            "trigger_event_id": EVENT_ID
        },
        "created_at": "2026-10-02T01:02:03+00:00",
        "idempotency_key": format!("tool-result:{ACTION_ID}")
    }])
}

struct OneResponse {
    base_url: String,
    request: mpsc::Receiver<Vec<u8>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Drop for OneResponse {
    fn drop(&mut self) {
        if let Some(handle) = self.thread.take() {
            handle.join().unwrap();
        }
    }
}

fn response(status: u16, body: Vec<u8>, headers: &[(&str, &str)]) -> OneResponse {
    response_after(status, body, headers, Duration::ZERO)
}

fn response_after(
    status: u16,
    body: Vec<u8>,
    headers: &[(&str, &str)],
    delay: Duration,
) -> OneResponse {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let (sender, request) = mpsc::channel();
    let header_lines = headers
        .iter()
        .map(|(name, value)| format!("{name}: {value}\r\n"))
        .collect::<String>();
    let thread = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let received = read_http_head(&mut stream);
        sender.send(received).unwrap();
        thread::sleep(delay);
        let reason = match status {
            200 => "OK",
            302 => "Found",
            401 => "Unauthorized",
            404 => "Not Found",
            _ => "Error",
        };
        let _ = write!(
            stream,
            "HTTP/1.1 {status} {reason}\r\nContent-Length: {}\r\nContent-Type: application/json\r\n{header_lines}Connection: close\r\n\r\n",
            body.len()
        );
        let _ = stream.write_all(&body);
    });
    OneResponse {
        base_url: format!("http://{address}"),
        request,
        thread: Some(thread),
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis()
        .try_into()
        .unwrap()
}

fn read_http_head(stream: &mut TcpStream) -> Vec<u8> {
    let mut all = Vec::new();
    let mut chunk = [0u8; 1024];
    while !all.windows(4).any(|window| window == b"\r\n\r\n") && all.len() < 16 * 1024 {
        let count = stream.read(&mut chunk).unwrap();
        if count == 0 {
            break;
        }
        all.extend_from_slice(&chunk[..count]);
    }
    all
}

fn local_auth() -> EngineAuth {
    EngineAuth::LocalSession {
        session_token: "ab".repeat(32),
        bearer_token: "synthetic-owner-jwt".into(),
    }
}

fn verify(server: &OneResponse) -> Result<gamja_artifact::VerifiedArtifact, ArtifactError> {
    EngineClient::new(&server.base_url, local_auth())?.verify_screenshot(
        7,
        ActionId::parse(ACTION_ID)?,
        13,
        now_ms() + 10_000,
    )
}

#[test]
fn engine_list_authorizes_empty_rooms_and_cursors_by_scanned_event_high_water() {
    let empty = response(200, b"[]".to_vec(), &[]);
    let page = EngineClient::new(&empty.base_url, local_auth())
        .unwrap()
        .list_screenshot_page(7, 40, 2, now_ms() + 10_000)
        .unwrap();
    assert_eq!((page.room_id, page.after_seq, page.scanned_through_seq), (7, 40, 40));
    assert!(page.artifacts.is_empty() && page.next_after_seq.is_none());

    let mut first = canonical_event(10, 20)[0].clone();
    first["seq"] = json!(13);
    let mut traversal = canonical_event(1, 1)[0].clone();
    traversal["seq"] = json!(14);
    traversal["payload"]["screenshot_ref"] = json!("../../private.png");
    let server = response(200, serde_json::to_vec(&json!([first, traversal])).unwrap(), &[]);
    let page = EngineClient::new(&server.base_url, local_auth())
        .unwrap()
        .list_screenshot_page(7, 12, 2, now_ms() + 10_000)
        .unwrap();
    assert_eq!(page.scanned_through_seq, 14);
    assert_eq!(page.next_after_seq, Some(14));
    assert_eq!(page.artifacts.len(), 1);
    assert_eq!(page.artifacts[0].event_seq, 13);
    let request = String::from_utf8(server.request.recv().unwrap()).unwrap();
    assert!(request.starts_with(
        "GET /api/v1/conversations/7/events?after_seq=12&limit=2 HTTP/1.1\r\n"
    ));
    assert!(request.to_ascii_lowercase().contains("x-gamja-session: "));
    assert!(request.to_ascii_lowercase().contains("authorization: bearer "));
}

#[test]
fn engine_private_list_rejects_a_malformed_full_page_instead_of_repeating_its_cursor() {
    let server = response(
        200,
        serde_json::to_vec(&json!([
            canonical_event(1, 1)[0].clone(),
            {"room_id": 7, "seq": 14, "kind": "message"}
        ]))
        .unwrap(),
        &[],
    );
    assert_eq!(
        EngineClient::new(&server.base_url, local_auth())
            .unwrap()
            .list_screenshot_page(7, 12, 2, now_ms() + 10_000),
        Err(ArtifactError::InvalidMetadata)
    );
}

#[test]
fn engine_list_foreign_or_expired_auth_fails_closed() {
    for status in [401, 404] {
        let server = response(status, br#"{"detail":"private"}"#.to_vec(), &[]);
        assert_eq!(
            EngineClient::new(&server.base_url, local_auth())
                .unwrap()
                .list_screenshot_page(7, 0, 100, now_ms() + 10_000),
            Err(ArtifactError::AccessDenied)
        );
    }
}

#[test]
fn engine_deadline_short_remaining_budget_bounds_request_without_auth_leak() {
    let secret = "deadline-secret-bearer";
    let server = response_after(
        200,
        serde_json::to_vec(&canonical_event(1, 1)).unwrap(),
        &[],
        Duration::from_millis(800),
    );
    let client = EngineClient::new(
        &server.base_url,
        EngineAuth::LocalSession {
            session_token: "ab".repeat(32),
            bearer_token: secret.into(),
        },
    )
    .unwrap();
    let started = Instant::now();
    let error = client
        .verify_screenshot(7, ActionId::parse(ACTION_ID).unwrap(), 13, now_ms() + 150)
        .unwrap_err();
    assert_eq!(error, ArtifactError::EngineUnavailable);
    assert!(started.elapsed() < Duration::from_millis(600));
    assert_eq!(error.to_string(), "engine_unavailable");
    assert!(!error.to_string().contains(secret));
}

#[test]
fn engine_deadline_expired_fails_before_send_without_auth_leak() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let base_url = format!("http://{}", listener.local_addr().unwrap());
    let secret = "expired-secret-bearer";
    let client = EngineClient::new(
        base_url,
        EngineAuth::LocalSession {
            session_token: "ab".repeat(32),
            bearer_token: secret.into(),
        },
    )
    .unwrap();
    let error = client
        .verify_screenshot(
            7,
            ActionId::parse(ACTION_ID).unwrap(),
            13,
            now_ms().saturating_sub(1),
        )
        .unwrap_err();
    assert_eq!(error, ArtifactError::DeadlineExpired);
    assert_eq!(error.to_string(), "deadline_expired");
    assert!(!error.to_string().contains(secret));
    assert!(
        matches!(listener.accept(), Err(error) if error.kind() == std::io::ErrorKind::WouldBlock)
    );
}

#[test]
fn engine_accepts_only_the_exact_canonical_event_and_queries_one_sequence() {
    let server = response(
        200,
        serde_json::to_vec(&canonical_event(5000, 5000)).unwrap(),
        &[],
    );
    let verified = verify(&server).unwrap();
    assert_eq!(verified.room_id, 7);
    assert_eq!(verified.event_seq, 13);
    assert_eq!(verified.action_id.as_str(), ACTION_ID);
    assert_eq!(
        verified.screenshot_ref,
        format!("rooms/7/screenshots/{ACTION_ID}.png")
    );
    assert_eq!((verified.width, verified.height), (5000, 5000));

    let request = String::from_utf8(server.request.recv().unwrap()).unwrap();
    assert!(
        request.starts_with("GET /api/v1/conversations/7/events?after_seq=12&limit=1 HTTP/1.1\r\n")
    );
    assert!(request.to_ascii_lowercase().contains("x-gamja-session: "));
    assert!(request
        .to_ascii_lowercase()
        .contains("authorization: bearer "));
}

#[test]
fn engine_maps_unauthorized_and_missing_rooms_to_one_denial() {
    for status in [401, 404] {
        let server = response(
            status,
            br#"{"detail":"sensitive server detail"}"#.to_vec(),
            &[],
        );
        let error = verify(&server).unwrap_err();
        assert_eq!(error, ArtifactError::AccessDenied);
        assert_eq!(error.to_string(), "access_denied");
    }
}

#[test]
fn engine_rejects_wrong_canonical_event_fields() {
    let cases: Vec<EventMutation> = vec![
        ("room", Box::new(|event| event[0]["room_id"] = json!(8))),
        ("sequence", Box::new(|event| event[0]["seq"] = json!(12))),
        (
            "actor",
            Box::new(|event| event[0]["actor_type"] = json!("agent")),
        ),
        (
            "actor identity",
            Box::new(|event| event[0]["actor_id"] = json!(4)),
        ),
        (
            "kind",
            Box::new(|event| event[0]["kind"] = json!("message")),
        ),
        (
            "idempotency",
            Box::new(|event| event[0]["idempotency_key"] = json!("other")),
        ),
        (
            "state",
            Box::new(|event| event[0]["payload"]["state"] = json!("failed")),
        ),
        (
            "action",
            Box::new(|event| event[0]["payload"]["action_id"] = json!(REQUEST_ID)),
        ),
        (
            "reference",
            Box::new(|event| {
                event[0]["payload"]["screenshot_ref"] = json!("rooms/7/screenshots/other.png")
            }),
        ),
        (
            "mime",
            Box::new(|event| event[0]["payload"]["image_mime"] = json!("image/jpeg")),
        ),
        (
            "zero width",
            Box::new(|event| event[0]["payload"]["image_width"] = json!(0)),
        ),
        (
            "created at",
            Box::new(|event| event[0]["created_at"] = json!("not-a-time")),
        ),
    ];
    for (name, mutate) in cases {
        let mut event = canonical_event(1, 1);
        mutate(&mut event);
        let server = response(200, serde_json::to_vec(&event).unwrap(), &[]);
        assert_eq!(
            verify(&server),
            Err(ArtifactError::InvalidMetadata),
            "{name}"
        );
    }
}

#[test]
fn engine_accepts_exact_pixel_limit_and_rejects_overflow() {
    let accepted = response(
        200,
        serde_json::to_vec(&canonical_event(5000, 5000)).unwrap(),
        &[],
    );
    assert!(verify(&accepted).is_ok());

    let rejected = response(
        200,
        serde_json::to_vec(&canonical_event(5001, 5000)).unwrap(),
        &[],
    );
    assert_eq!(verify(&rejected), Err(ArtifactError::InvalidMetadata));
}

#[test]
fn engine_rejects_redirects_non_loopback_and_more_than_one_event() {
    let redirect = response(
        302,
        Vec::new(),
        &[("Location", "https://example.invalid/private")],
    );
    assert_eq!(verify(&redirect), Err(ArtifactError::InvalidEngineResponse));

    assert_eq!(
        EngineClient::new("http://localhost:41231", local_auth()).unwrap_err(),
        ArtifactError::InvalidEndpoint
    );
    assert_eq!(
        EngineClient::new("https://127.0.0.1:41231", local_auth()).unwrap_err(),
        ArtifactError::InvalidEndpoint
    );
    assert_eq!(
        EngineClient::new("http://127.0.0.1:0", local_auth()).unwrap_err(),
        ArtifactError::InvalidEndpoint
    );

    let mut events = canonical_event(1, 1);
    let duplicate = events[0].clone();
    events.as_array_mut().unwrap().push(duplicate);
    let multiple = response(200, serde_json::to_vec(&events).unwrap(), &[]);
    assert_eq!(verify(&multiple), Err(ArtifactError::InvalidMetadata));
}

#[test]
fn engine_bounds_the_single_event_response() {
    let server = response(200, vec![b'x'; MAX_EVENT_RESPONSE_BYTES + 1], &[]);
    assert_eq!(verify(&server), Err(ArtifactError::EngineResponseTooLarge));
}

#[test]
fn engine_historical_screenshot_does_not_fetch_unpaginated_activity() {
    let server = response(
        200,
        serde_json::to_vec(&canonical_event(1, 1)).unwrap(),
        &[],
    );
    assert!(verify(&server).is_ok());
    let request = String::from_utf8(server.request.recv().unwrap()).unwrap();
    assert!(request.contains("/conversations/7/events?after_seq=12&limit=1"));
    assert!(!request.contains("/tools/rooms/7/activity"));
}

struct TestRoot(PathBuf);

impl TestRoot {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "gamja-artifact-{label}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct FrozenEngine {
    child: Child,
    base_url: String,
    session: String,
    capability: String,
    stopped: bool,
}

impl FrozenEngine {
    fn start(binary: &Path, app_data: &Path, home: &Path) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let session = "cd".repeat(32);
        let capability = "ef".repeat(32);
        let capability_hash = format!("{:x}", Sha256::digest(capability.as_bytes()));
        let temp = app_data.join("tmp");
        fs::create_dir_all(&temp).unwrap();
        let mut command = Command::new(binary);
        command
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", home)
            .env("TMPDIR", &temp)
            .env("GAMJA_APP_DATA_DIR", app_data)
            .env("GAMJA_PORT", port.to_string())
            .env("GAMJA_LOCAL_SESSION_TOKEN", &session)
            .env("GAMJA_CLI_CAPABILITY_HASH", capability_hash)
            .current_dir(app_data)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(unix)]
        command.process_group(0);
        let child = command.spawn().unwrap();
        let engine = Self {
            child,
            base_url: format!("http://127.0.0.1:{port}"),
            session,
            capability,
            stopped: false,
        };
        let deadline = Instant::now() + Duration::from_secs(20);
        while Instant::now() < deadline {
            if direct(
                &engine.base_url,
                "/health",
                &engine.session,
                None,
                None,
                None,
            )
            .is_some()
            {
                return engine;
            }
            thread::sleep(Duration::from_millis(50));
        }
        panic!("frozen_engine_start_timeout");
    }

    fn stop(mut self) {
        self.stop_owned_group();
    }

    fn stop_owned_group(&mut self) {
        if self.stopped {
            return;
        }
        self.stopped = true;
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        #[cfg(not(unix))]
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for FrozenEngine {
    fn drop(&mut self) {
        self.stop_owned_group();
    }
}

fn direct(
    base: &str,
    path: &str,
    session: &str,
    capability: Option<&str>,
    jwt: Option<&str>,
    body: Option<Value>,
) -> Option<(u16, Value)> {
    let client = reqwest::blocking::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap();
    let mut request = if body.is_some() {
        client.post(format!("{base}/api/v1{path}"))
    } else {
        client.get(format!("{base}{path}"))
    };
    request = if let Some(capability) = capability {
        request.header("X-Gamja-Cli-Capability", capability)
    } else {
        request.header("X-Gamja-Session", session)
    };
    if let Some(jwt) = jwt {
        request = request.bearer_auth(jwt);
    }
    if let Some(body) = body {
        request = request.json(&body);
    }
    let response = request.send().ok()?;
    let status = response.status().as_u16();
    let value = response.json().unwrap_or(Value::Null);
    Some((status, value))
}

fn api(engine: &FrozenEngine, path: &str, jwt: Option<&str>, body: Option<Value>) -> (u16, Value) {
    direct(&engine.base_url, path, &engine.session, None, jwt, body).unwrap()
}

fn register_and_login(engine: &FrozenEngine, email: &str, username: &str) -> String {
    let password = "synthetic-fixture-password";
    let (status, registration) = api(
        engine,
        "/auth/register",
        None,
        Some(json!({"email": email, "username": username, "password": password})),
    );
    let validation_shape = registration["detail"].as_array().map(|items| {
        items
            .iter()
            .map(|item| json!({"loc": item["loc"], "type": item["type"]}))
            .collect::<Vec<_>>()
    });
    assert!(
        status == 200 || status == 400,
        "register_status={status}, validation_shape={validation_shape:?}"
    );
    let (status, body) = api(
        engine,
        "/auth/login",
        None,
        Some(json!({
            "email": email,
            "password": password,
            "remember_device": false,
            "preserve_remembered_login": true
        })),
    );
    assert_eq!(status, 200);
    body["access_token"].as_str().unwrap().to_owned()
}

#[cfg(target_os = "macos")]
#[test]
fn frozen_engine_provenance_proves_both_local_auth_modes_and_owner_isolation() {
    const FROZEN_SHA: &str = "2f6b534d61ef0212248ec76b6f92b9a9e5dc9e15dae21622c8ab785fa2bfded0";
    let binary =
        passwd_home().join(".local/share/gamja-bot/core/runtime/libexec/gamja/gamja-engine");
    assert!(binary.is_file());
    let mut hasher = Sha256::new();
    let mut file = fs::File::open(&binary).unwrap();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer).unwrap();
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    assert_eq!(format!("{:x}", hasher.finalize()), FROZEN_SHA);

    let root = TestRoot::new("frozen-engine");
    let app_data = root.0.join("app-data");
    let home = root.0.join("home");
    fs::create_dir(&app_data).unwrap();
    fs::create_dir(&home).unwrap();
    let first = FrozenEngine::start(&binary, &app_data, &home);
    let owner_jwt = register_and_login(&first, "artifact-owner@example.com", "artifact-owner");
    let _other_jwt = register_and_login(&first, "artifact-other@example.com", "artifact-other");
    let (status, project) = api(
        &first,
        "/projects/",
        Some(&owner_jwt),
        Some(json!({"name":"Fixture","description":null})),
    );
    assert_eq!(status, 201);
    let project_id = project["id"].as_u64().unwrap();
    let (status, room) = api(
        &first,
        "/conversations/",
        Some(&owner_jwt),
        Some(json!({"project_id":project_id,"title":"Artifact fixture"})),
    );
    assert_eq!(status, 201);
    let room_id = room["id"].as_u64().unwrap();
    first.stop();

    let reference = format!("rooms/{room_id}/screenshots/{ACTION_ID}.png");
    let payload = json!({
        "call_id": "fixture-screen",
        "state": "completed",
        "text": "Synthetic screenshot captured.",
        "screenshot_ref": reference,
        "image_mime": "image/png",
        "image_width": 5,
        "image_height": 7,
        "agent_id": null,
        "action_id": ACTION_ID,
        "trigger_event_id": EVENT_ID
    });
    let sql = format!(
        "INSERT INTO room_events(event_id,device_id,room_id,seq,actor_type,actor_id,actor_name_snapshot,kind,payload,created_at,idempotency_key) VALUES('{EVENT_ID}','{DEVICE_ID}',{room_id},1,'tool',NULL,'Tool','tool_result',{},'2026-10-02 01:02:03.000000','tool-result:{ACTION_ID}');",
        sqlite_literal(&payload.to_string())
    );
    let result = Command::new("/usr/bin/sqlite3")
        .arg(app_data.join("gamja_bot.db"))
        .arg(sql)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .unwrap();
    assert!(result.success());

    let second = FrozenEngine::start(&binary, &app_data, &home);
    let owner_jwt = register_and_login(&second, "artifact-owner@example.com", "artifact-owner");
    let other_jwt = register_and_login(&second, "artifact-other@example.com", "artifact-other");
    let action = ActionId::parse(ACTION_ID).unwrap();

    let local = EngineClient::new(
        &second.base_url,
        EngineAuth::LocalSession {
            session_token: second.session.clone(),
            bearer_token: owner_jwt.clone(),
        },
    )
    .unwrap()
    .verify_screenshot(room_id, action.clone(), 1, now_ms() + 10_000)
    .unwrap();
    assert_eq!((local.width, local.height), (5, 7));

    let cli = EngineClient::new(
        &second.base_url,
        EngineAuth::CliCapability {
            capability: second.capability.clone(),
            bearer_token: owner_jwt,
        },
    )
    .unwrap()
    .verify_screenshot(room_id, action.clone(), 1, now_ms() + 10_000)
    .unwrap();
    assert_eq!(cli, local);

    for auth in [
        EngineAuth::LocalSession {
            session_token: second.session.clone(),
            bearer_token: other_jwt.clone(),
        },
        EngineAuth::CliCapability {
            capability: second.capability.clone(),
            bearer_token: other_jwt,
        },
    ] {
        assert_eq!(
            EngineClient::new(&second.base_url, auth)
                .unwrap()
                .verify_screenshot(room_id, action.clone(), 1, now_ms() + 10_000),
            Err(ArtifactError::AccessDenied)
        );
    }
    second.stop();
    assert!(!app_data.join("owner-cli.json").exists());
}

fn sqlite_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

#[cfg(unix)]
fn passwd_home() -> PathBuf {
    let configured = unsafe { libc::sysconf(libc::_SC_GETPW_R_SIZE_MAX) };
    let size = usize::try_from(configured)
        .ok()
        .filter(|size| *size >= 1024)
        .unwrap_or(16_384);
    let mut buffer = vec![0u8; size];
    let mut entry = std::mem::MaybeUninit::<libc::passwd>::uninit();
    let mut result = std::ptr::null_mut();
    let code = unsafe {
        libc::getpwuid_r(
            libc::geteuid(),
            entry.as_mut_ptr(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        )
    };
    assert_eq!(code, 0);
    assert!(!result.is_null());
    let entry = unsafe { entry.assume_init() };
    let bytes = unsafe { CStr::from_ptr(entry.pw_dir) }.to_bytes();
    PathBuf::from(OsStr::from_bytes(bytes))
}
