use crate::protocol::{
    bounded_header, canonical_timestamp, canonical_uuid, lower_hex, ActionId, ArtifactError,
    MAX_PIXELS,
};
use reqwest::{blocking::Client, redirect::Policy, StatusCode, Url};
use serde::Deserialize;
use serde_json::{Map, Value};
use std::{
    fmt,
    io::Read,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub const MAX_EVENT_RESPONSE_BYTES: usize = 65_536;
const MAX_LIST_RESPONSE_BYTES: usize = 1_048_576;
const MAX_RESTORE_RESPONSE_BYTES: usize = 16_384;
const MAX_ENGINE_REQUEST_MS: u64 = 10_000;

#[derive(Clone, PartialEq, Eq)]
pub enum EngineAuth {
    LocalSession {
        session_token: String,
        bearer_token: String,
    },
    CliCapability {
        capability: String,
        bearer_token: String,
    },
}

impl fmt::Debug for EngineAuth {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::LocalSession { .. } => formatter.write_str("LocalSession([redacted])"),
            Self::CliCapability { .. } => formatter.write_str("CliCapability([redacted])"),
        }
    }
}

impl EngineAuth {
    fn validate(&self) -> Result<(), ArtifactError> {
        let (local, bearer) = match self {
            Self::LocalSession {
                session_token,
                bearer_token,
            } => (session_token, bearer_token),
            Self::CliCapability {
                capability,
                bearer_token,
            } => (capability, bearer_token),
        };
        if !lower_hex(local, 64) || !bounded_header(bearer, 1, 8192) {
            return Err(ArtifactError::InvalidRequest);
        }
        Ok(())
    }

    fn bearer(&self) -> &str {
        match self {
            Self::LocalSession { bearer_token, .. } | Self::CliCapability { bearer_token, .. } => {
                bearer_token
            }
        }
    }
}

pub struct EngineClient {
    api_url: String,
    auth: EngineAuth,
    client: Client,
}

impl fmt::Debug for EngineClient {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("EngineClient")
            .field("api_url", &self.api_url)
            .field("auth", &self.auth)
            .finish_non_exhaustive()
    }
}

impl EngineClient {
    pub fn new(api_url: impl AsRef<str>, auth: EngineAuth) -> Result<Self, ArtifactError> {
        let api_url = validate_loopback_endpoint(api_url.as_ref())?;
        auth.validate()?;
        let client = build_client()?;
        Ok(Self {
            api_url,
            auth,
            client,
        })
    }

    pub fn restore_cli(
        api_url: impl AsRef<str>,
        capability: impl Into<String>,
        deadline_ms: u64,
    ) -> Result<Self, ArtifactError> {
        let api_url = validate_loopback_endpoint(api_url.as_ref())?;
        let capability = capability.into();
        if !lower_hex(&capability, 64) {
            return Err(ArtifactError::InvalidRequest);
        }
        let client = build_client()?;
        let response = client
            .post(format!("{api_url}/api/v1/auth/restore"))
            .header("X-Gamja-Cli-Capability", &capability)
            .header("Accept", "application/json")
            .timeout(remaining_timeout(deadline_ms)?)
            .send()
            .map_err(|_| ArtifactError::EngineUnavailable)?;
        match response.status() {
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN | StatusCode::NOT_FOUND => {
                return Err(ArtifactError::AccessDenied)
            }
            StatusCode::OK => {}
            status if status.is_redirection() => return Err(ArtifactError::InvalidEngineResponse),
            _ => return Err(ArtifactError::EngineUnavailable),
        }
        let body = read_bounded(response, MAX_RESTORE_RESPONSE_BYTES)?;
        let token: RestoreWire =
            serde_json::from_slice(&body).map_err(|_| ArtifactError::InvalidEngineResponse)?;
        if token.token_type != "bearer" || !bounded_header(&token.access_token, 1, 8192) {
            return Err(ArtifactError::InvalidEngineResponse);
        }
        Ok(Self {
            api_url,
            auth: EngineAuth::CliCapability {
                capability,
                bearer_token: token.access_token,
            },
            client,
        })
    }

    pub fn list_screenshots(
        &self,
        room_id: u64,
        after_seq: u64,
        limit: u16,
        deadline_ms: u64,
    ) -> Result<Vec<VerifiedArtifact>, ArtifactError> {
        Ok(self
            .list_screenshot_page_inner(room_id, after_seq, limit, deadline_ms, false)?
            .artifacts)
    }

    pub fn list_screenshot_page(
        &self,
        room_id: u64,
        after_seq: u64,
        limit: u16,
        deadline_ms: u64,
    ) -> Result<VerifiedArtifactPage, ArtifactError> {
        self.list_screenshot_page_inner(room_id, after_seq, limit, deadline_ms, true)
    }

    fn list_screenshot_page_inner(
        &self,
        room_id: u64,
        after_seq: u64,
        limit: u16,
        deadline_ms: u64,
        strict_page: bool,
    ) -> Result<VerifiedArtifactPage, ArtifactError> {
        if room_id == 0 || !(1..=500).contains(&limit) {
            return Err(ArtifactError::InvalidRequest);
        }
        let response = self
            .authenticated(
                self.client
                    .get(format!(
                        "{}/api/v1/conversations/{room_id}/events",
                        self.api_url
                    ))
                    .query(&[("after_seq", after_seq), ("limit", u64::from(limit))]),
            )
            .timeout(remaining_timeout(deadline_ms)?)
            .send()
            .map_err(|_| ArtifactError::EngineUnavailable)?;
        validate_event_status(response.status())?;
        let body = read_bounded(response, MAX_LIST_RESPONSE_BYTES)?;
        let events: Vec<Value> =
            serde_json::from_slice(&body).map_err(|_| ArtifactError::InvalidMetadata)?;
        if events.len() > usize::from(limit) {
            return Err(ArtifactError::InvalidMetadata);
        }
        let full_page = events.len() == usize::from(limit);
        let mut scanned_through_seq = after_seq;
        let mut artifacts = Vec::new();
        for value in events {
            let event = match serde_json::from_value::<EventWire>(value) {
                Ok(event) => event,
                Err(_) if !strict_page => continue,
                Err(_) => return Err(ArtifactError::InvalidMetadata),
            };
            if !canonical_uuid(&event.event_id)
                || event
                    .device_id
                    .as_deref()
                    .is_some_and(|value| !canonical_uuid(value))
                || event.room_id != room_id
                || event.seq <= scanned_through_seq
                || !canonical_timestamp(&event.created_at)
            {
                if strict_page {
                    return Err(ArtifactError::InvalidMetadata);
                }
                continue;
            }
            scanned_through_seq = event.seq;
            if event.payload.contains_key("screenshot_ref") {
                if let Some(action_id) = string_field(&event.payload, "action_id")
                    .and_then(|value| ActionId::parse(value.to_owned()).ok())
                {
                    if let Ok(artifact) =
                        validate_event(event, room_id, action_id, scanned_through_seq)
                    {
                        artifacts.push(artifact);
                    }
                }
            }
        }
        Ok(VerifiedArtifactPage {
            room_id,
            after_seq,
            scanned_through_seq,
            next_after_seq: (full_page && scanned_through_seq > after_seq)
                .then_some(scanned_through_seq),
            artifacts,
        })
    }

    pub fn verify_screenshot(
        &self,
        room_id: u64,
        action_id: ActionId,
        event_seq: u64,
        deadline_ms: u64,
    ) -> Result<VerifiedArtifact, ArtifactError> {
        if room_id == 0 || event_seq == 0 {
            return Err(ArtifactError::InvalidRequest);
        }
        let timeout = remaining_timeout(deadline_ms)?;
        let url = format!("{}/api/v1/conversations/{room_id}/events", self.api_url);
        let request = self
            .authenticated(
                self.client
                    .get(url)
                    .query(&[("after_seq", event_seq - 1), ("limit", 1)]),
            )
            .timeout(timeout);
        let response = request
            .send()
            .map_err(|_| ArtifactError::EngineUnavailable)?;
        validate_event_status(response.status())?;
        let body = read_bounded(response, MAX_EVENT_RESPONSE_BYTES)?;
        let mut events: Vec<EventWire> =
            serde_json::from_slice(&body).map_err(|_| ArtifactError::InvalidMetadata)?;
        if events.len() != 1 {
            return Err(ArtifactError::InvalidMetadata);
        }
        validate_event(events.pop().unwrap(), room_id, action_id, event_seq)
    }

    fn authenticated(
        &self,
        request: reqwest::blocking::RequestBuilder,
    ) -> reqwest::blocking::RequestBuilder {
        let request = request.bearer_auth(self.auth.bearer());
        match &self.auth {
            EngineAuth::LocalSession { session_token, .. } => {
                request.header("X-Gamja-Session", session_token)
            }
            EngineAuth::CliCapability { capability, .. } => {
                request.header("X-Gamja-Cli-Capability", capability)
            }
        }
    }
}

fn build_client() -> Result<Client, ArtifactError> {
    Client::builder()
        .redirect(Policy::none())
        .no_proxy()
        .connect_timeout(Duration::from_secs(2))
        .build()
        .map_err(|_| ArtifactError::EngineUnavailable)
}

fn validate_event_status(status: StatusCode) -> Result<(), ArtifactError> {
    match status {
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN | StatusCode::NOT_FOUND => {
            Err(ArtifactError::AccessDenied)
        }
        StatusCode::OK => Ok(()),
        status if status.is_redirection() => Err(ArtifactError::InvalidEngineResponse),
        _ => Err(ArtifactError::EngineUnavailable),
    }
}

fn read_bounded(
    response: reqwest::blocking::Response,
    maximum: usize,
) -> Result<Vec<u8>, ArtifactError> {
    if response
        .content_length()
        .is_some_and(|length| length > maximum as u64)
    {
        return Err(ArtifactError::EngineResponseTooLarge);
    }
    let mut bounded = response.take((maximum + 1) as u64);
    let mut body = Vec::new();
    bounded
        .read_to_end(&mut body)
        .map_err(|_| ArtifactError::EngineUnavailable)?;
    if body.len() > maximum {
        return Err(ArtifactError::EngineResponseTooLarge);
    }
    Ok(body)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreWire {
    access_token: String,
    token_type: String,
}

fn remaining_timeout(deadline_ms: u64) -> Result<Duration, ArtifactError> {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ArtifactError::DeadlineExpired)?
        .as_millis();
    let now_ms = u64::try_from(now_ms).map_err(|_| ArtifactError::DeadlineExpired)?;
    let remaining_ms = deadline_ms
        .checked_sub(now_ms)
        .filter(|remaining| *remaining > 0)
        .ok_or(ArtifactError::DeadlineExpired)?;
    Ok(Duration::from_millis(
        remaining_ms.min(MAX_ENGINE_REQUEST_MS),
    ))
}

pub(crate) fn validate_loopback_endpoint(value: &str) -> Result<String, ArtifactError> {
    let parsed = Url::parse(value).map_err(|_| ArtifactError::InvalidEndpoint)?;
    let Some(port) = parsed.port().filter(|port| *port != 0) else {
        return Err(ArtifactError::InvalidEndpoint);
    };
    let canonical = format!("http://127.0.0.1:{port}");
    if parsed.scheme() != "http"
        || parsed.host_str() != Some("127.0.0.1")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.path() != "/"
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || value != canonical
    {
        return Err(ArtifactError::InvalidEndpoint);
    }
    Ok(canonical)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedArtifact {
    pub room_id: u64,
    pub action_id: ActionId,
    pub event_seq: u64,
    pub screenshot_ref: String,
    pub width: u32,
    pub height: u32,
    pub created_at: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedArtifactPage {
    pub room_id: u64,
    pub after_seq: u64,
    pub scanned_through_seq: u64,
    pub next_after_seq: Option<u64>,
    pub artifacts: Vec<VerifiedArtifact>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventWire {
    event_id: String,
    device_id: Option<String>,
    room_id: u64,
    seq: u64,
    actor_type: String,
    actor_id: Option<u64>,
    actor_name_snapshot: String,
    kind: String,
    payload: Map<String, Value>,
    created_at: String,
    idempotency_key: Option<String>,
}

fn validate_event(
    event: EventWire,
    room_id: u64,
    action_id: ActionId,
    event_seq: u64,
) -> Result<VerifiedArtifact, ArtifactError> {
    let expected_reference = format!("rooms/{room_id}/screenshots/{}.png", action_id.as_str());
    let expected_key = format!("tool-result:{}", action_id.as_str());
    if !canonical_uuid(&event.event_id)
        || event
            .device_id
            .as_deref()
            .is_some_and(|value| !canonical_uuid(value))
        || event.room_id != room_id
        || event.seq != event_seq
        || event.actor_type != "tool"
        || event.actor_id.is_some()
        || event.actor_name_snapshot != "Tool"
        || event.kind != "tool_result"
        || event.idempotency_key.as_deref() != Some(expected_key.as_str())
        || string_field(&event.payload, "state") != Some("completed")
        || string_field(&event.payload, "action_id") != Some(action_id.as_str())
        || string_field(&event.payload, "screenshot_ref") != Some(expected_reference.as_str())
        || string_field(&event.payload, "image_mime") != Some("image/png")
        || !canonical_timestamp(&event.created_at)
    {
        return Err(ArtifactError::InvalidMetadata);
    }
    let width = dimension(&event.payload, "image_width")?;
    let height = dimension(&event.payload, "image_height")?;
    if u64::from(width).saturating_mul(u64::from(height)) > MAX_PIXELS {
        return Err(ArtifactError::InvalidMetadata);
    }
    Ok(VerifiedArtifact {
        room_id,
        action_id,
        event_seq,
        screenshot_ref: expected_reference,
        width,
        height,
        created_at: event.created_at,
    })
}

fn string_field<'a>(payload: &'a Map<String, Value>, field: &str) -> Option<&'a str> {
    payload.get(field)?.as_str()
}

fn dimension(payload: &Map<String, Value>, field: &str) -> Result<u32, ArtifactError> {
    let value = payload
        .get(field)
        .and_then(Value::as_u64)
        .ok_or(ArtifactError::InvalidMetadata)?;
    u32::try_from(value)
        .ok()
        .filter(|value| *value > 0)
        .ok_or(ArtifactError::InvalidMetadata)
}
