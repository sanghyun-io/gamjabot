use serde::{de, Deserialize, Deserializer, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fmt,
    io::{Read, Write},
};

pub const MAX_REQUEST_BYTES: usize = 16 * 1024;
pub const MAX_HEADER_BYTES: usize = 16 * 1024;
pub const MAX_BODY_BYTES: usize = 10_000_000;
pub const MAX_LIST_BODY_BYTES: usize = 256 * 1024;
pub const MAX_PIXELS: u64 = 25_000_000;
pub const LIST_MEDIA_TYPE: &str = "application/json";
const MAX_DEADLINE_AHEAD_MS: u64 = 30_000;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactError {
    InvalidRequest,
    DeadlineExpired,
    InvalidEndpoint,
    AccessDenied,
    InvalidMetadata,
    InvalidEngineResponse,
    EngineResponseTooLarge,
    EngineUnavailable,
    ArtifactRemoved,
    ArtifactConflict,
    ArtifactUnavailable,
    InvalidFrame,
    BodyTooLarge,
}

impl ArtifactError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid_request",
            Self::DeadlineExpired => "deadline_expired",
            Self::InvalidEndpoint => "invalid_endpoint",
            Self::AccessDenied => "access_denied",
            Self::InvalidMetadata => "invalid_metadata",
            Self::InvalidEngineResponse => "invalid_engine_response",
            Self::EngineResponseTooLarge => "engine_response_too_large",
            Self::EngineUnavailable => "engine_unavailable",
            Self::ArtifactRemoved => "artifact_removed",
            Self::ArtifactConflict => "artifact_conflict",
            Self::ArtifactUnavailable => "artifact_unavailable",
            Self::InvalidFrame => "invalid_frame",
            Self::BodyTooLarge => "body_too_large",
        }
    }
}

impl fmt::Display for ArtifactError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for ArtifactError {}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(transparent)]
pub struct RequestId(String);

impl RequestId {
    pub fn parse(value: impl Into<String>) -> Result<Self, ArtifactError> {
        let value = value.into();
        canonical_uuid(&value)
            .then_some(Self(value))
            .ok_or(ArtifactError::InvalidRequest)
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl<'de> Deserialize<'de> for RequestId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Self::parse(String::deserialize(deserializer)?)
            .map_err(|_| de::Error::custom("invalid request id"))
    }
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(transparent)]
pub struct ActionId(String);

impl ActionId {
    pub fn parse(value: impl Into<String>) -> Result<Self, ArtifactError> {
        let value = value.into();
        canonical_uuid(&value)
            .then_some(Self(value))
            .ok_or(ArtifactError::InvalidRequest)
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl<'de> Deserialize<'de> for ActionId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Self::parse(String::deserialize(deserializer)?)
            .map_err(|_| de::Error::custom("invalid action id"))
    }
}

pub(crate) fn canonical_uuid(value: &str) -> bool {
    if value.len() != 36 {
        return false;
    }
    let bytes = value.as_bytes();
    for (index, byte) in bytes.iter().copied().enumerate() {
        if matches!(index, 8 | 13 | 18 | 23) {
            if byte != b'-' {
                return false;
            }
        } else if !(byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
            return false;
        }
    }
    matches!(bytes[14], b'1'..=b'8') && matches!(bytes[19], b'8' | b'9' | b'a' | b'b')
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactOperation {
    List,
    Get,
    Delete,
}

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RequestAuth {
    pub api_url: String,
    pub session_token: String,
    pub bearer_token: String,
}

impl fmt::Debug for RequestAuth {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RequestAuth")
            .field("api_url", &self.api_url)
            .field("session_token", &"[redacted]")
            .field("bearer_token", &"[redacted]")
            .finish()
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArtifactRequest {
    pub request_id: RequestId,
    pub deadline_ms: u64,
    pub operation: ArtifactOperation,
    pub room_id: u64,
    pub action_id: Option<ActionId>,
    pub event_seq: Option<u64>,
    pub after_seq: Option<u64>,
    pub limit: Option<u16>,
    pub auth: RequestAuth,
}

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case")]
enum RequestWire {
    List(ListRequestWire),
    Get(ItemRequestWire),
    Delete(ItemRequestWire),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ItemRequestWire {
    version: u16,
    request_id: RequestId,
    deadline_ms: u64,
    room_id: u64,
    action_id: ActionId,
    event_seq: u64,
    auth: RequestAuth,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListRequestWire {
    version: u16,
    request_id: RequestId,
    deadline_ms: u64,
    room_id: u64,
    after_seq: u64,
    limit: u16,
    auth: RequestAuth,
}

impl ArtifactRequest {
    pub fn decode_json(input: &[u8], now_ms: u64) -> Result<Self, ArtifactError> {
        if input.is_empty() || input.len() > MAX_REQUEST_BYTES {
            return Err(ArtifactError::InvalidRequest);
        }
        let wire: RequestWire =
            serde_json::from_slice(input).map_err(|_| ArtifactError::InvalidRequest)?;
        let (
            version,
            request_id,
            deadline_ms,
            operation,
            room_id,
            action_id,
            event_seq,
            after_seq,
            limit,
            auth,
        ) = match wire {
            RequestWire::List(wire) => (
                wire.version,
                wire.request_id,
                wire.deadline_ms,
                ArtifactOperation::List,
                wire.room_id,
                None,
                None,
                Some(wire.after_seq),
                Some(wire.limit),
                wire.auth,
            ),
            RequestWire::Get(wire) => (
                wire.version,
                wire.request_id,
                wire.deadline_ms,
                ArtifactOperation::Get,
                wire.room_id,
                Some(wire.action_id),
                Some(wire.event_seq),
                None,
                None,
                wire.auth,
            ),
            RequestWire::Delete(wire) => (
                wire.version,
                wire.request_id,
                wire.deadline_ms,
                ArtifactOperation::Delete,
                wire.room_id,
                Some(wire.action_id),
                Some(wire.event_seq),
                None,
                None,
                wire.auth,
            ),
        };
        if version != 1 || room_id == 0 {
            return Err(ArtifactError::InvalidRequest);
        }
        if deadline_ms <= now_ms {
            return Err(ArtifactError::DeadlineExpired);
        }
        if deadline_ms - now_ms > MAX_DEADLINE_AHEAD_MS || !valid_private_auth(&auth) {
            return Err(ArtifactError::InvalidRequest);
        }
        let valid_scope = match operation {
            ArtifactOperation::List => limit.is_some_and(|limit| (1..=500).contains(&limit)),
            ArtifactOperation::Get | ArtifactOperation::Delete => {
                event_seq.is_some_and(|seq| seq > 0)
            }
        };
        if !valid_scope {
            return Err(ArtifactError::InvalidRequest);
        }
        Ok(Self {
            request_id,
            deadline_ms,
            operation,
            room_id,
            action_id,
            event_seq,
            after_seq,
            limit,
            auth,
        })
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactListState {
    Available,
    Removed,
    Unavailable,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactListItem {
    pub room_id: u64,
    pub event_seq: u64,
    pub action_id: ActionId,
    pub created_at: String,
    pub width: u32,
    pub height: u32,
    pub state: ArtifactListState,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactListResponse {
    pub version: u16,
    pub room_id: u64,
    pub after_seq: u64,
    pub scanned_through_seq: u64,
    pub next_after_seq: Option<u64>,
    pub items: Vec<ArtifactListItem>,
}

impl ArtifactListResponse {
    pub fn encode_json(&self) -> Result<Vec<u8>, ArtifactError> {
        self.validate(self.room_id, self.after_seq, 500)?;
        let body = serde_json::to_vec(self).map_err(|_| ArtifactError::InvalidMetadata)?;
        if body.is_empty() || body.len() > MAX_LIST_BODY_BYTES {
            return Err(ArtifactError::InvalidMetadata);
        }
        Ok(body)
    }

    pub fn decode_json(
        input: &[u8],
        expected_room_id: u64,
        expected_after_seq: u64,
        limit: u16,
    ) -> Result<Self, ArtifactError> {
        if input.is_empty() || input.len() > MAX_LIST_BODY_BYTES {
            return Err(ArtifactError::InvalidMetadata);
        }
        let response: Self =
            serde_json::from_slice(input).map_err(|_| ArtifactError::InvalidMetadata)?;
        response.validate(expected_room_id, expected_after_seq, limit)?;
        Ok(response)
    }

    fn validate(
        &self,
        expected_room_id: u64,
        expected_after_seq: u64,
        limit: u16,
    ) -> Result<(), ArtifactError> {
        if self.version != 1
            || expected_room_id == 0
            || !(1..=500).contains(&limit)
            || self.room_id != expected_room_id
            || self.after_seq != expected_after_seq
            || self.scanned_through_seq < self.after_seq
            || self.items.len() > usize::from(limit)
            || self.next_after_seq.is_some_and(|cursor| {
                cursor != self.scanned_through_seq || cursor <= self.after_seq
            })
        {
            return Err(ArtifactError::InvalidMetadata);
        }
        let mut previous = self.after_seq;
        for item in &self.items {
            if item.room_id != self.room_id
                || item.event_seq <= previous
                || item.event_seq > self.scanned_through_seq
                || item.width == 0
                || item.height == 0
                || u64::from(item.width).saturating_mul(u64::from(item.height)) > MAX_PIXELS
                || !canonical_timestamp(&item.created_at)
            {
                return Err(ArtifactError::InvalidMetadata);
            }
            previous = item.event_seq;
        }
        Ok(())
    }
}

pub(crate) fn canonical_timestamp(value: &str) -> bool {
    let bytes = value.as_bytes();
    if !(20..=35).contains(&bytes.len())
        || bytes.get(4) != Some(&b'-')
        || bytes.get(7) != Some(&b'-')
        || bytes.get(10) != Some(&b'T')
        || bytes.get(13) != Some(&b':')
        || bytes.get(16) != Some(&b':')
    {
        return false;
    }
    let decimal = |slice: &[u8]| {
        slice.iter().try_fold(0u32, |value, byte| {
            byte.is_ascii_digit()
                .then_some(value * 10 + u32::from(byte - b'0'))
        })
    };
    let (Some(year), Some(month), Some(day), Some(hour), Some(minute), Some(second)) = (
        decimal(&bytes[0..4]),
        decimal(&bytes[5..7]),
        decimal(&bytes[8..10]),
        decimal(&bytes[11..13]),
        decimal(&bytes[14..16]),
        decimal(&bytes[17..19]),
    ) else {
        return false;
    };
    if year == 0
        || !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return false;
    }
    let mut timezone = &bytes[19..];
    if timezone.first() == Some(&b'.') {
        let Some(offset) = timezone
            .iter()
            .position(|byte| matches!(byte, b'Z' | b'+' | b'-'))
        else {
            return false;
        };
        if offset < 2 || !timezone[1..offset].iter().all(u8::is_ascii_digit) {
            return false;
        }
        timezone = &timezone[offset..];
    }
    if timezone == b"Z" {
        return true;
    }
    if timezone.len() != 6 || !matches!(timezone[0], b'+' | b'-') || timezone[3] != b':' {
        return false;
    }
    let (Some(offset_hour), Some(offset_minute)) =
        (decimal(&timezone[1..3]), decimal(&timezone[4..6]))
    else {
        return false;
    };
    offset_hour <= 23 && offset_minute <= 59
}

fn valid_private_auth(auth: &RequestAuth) -> bool {
    crate::engine::validate_loopback_endpoint(&auth.api_url).is_ok()
        && lower_hex(&auth.session_token, 64)
        && bounded_header(&auth.bearer_token, 1, 8192)
}

pub(crate) fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(crate) fn bounded_header(value: &str, minimum: usize, maximum: usize) -> bool {
    (minimum..=maximum).contains(&value.len()) && value.bytes().all(|byte| byte.is_ascii_graphic())
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ResponseState {
    Ok,
    Error,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ResponseHeader {
    pub version: u16,
    pub request_id: RequestId,
    pub state: ResponseState,
    pub error_code: Option<ArtifactError>,
    pub media_type: Option<String>,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub byte_count: u64,
    pub sha256: Option<String>,
}

impl ResponseHeader {
    pub fn png(
        request_id: RequestId,
        width: u32,
        height: u32,
        body: &[u8],
    ) -> Result<Self, ArtifactError> {
        if body.len() > MAX_BODY_BYTES
            || width == 0
            || height == 0
            || u64::from(width).saturating_mul(u64::from(height)) > MAX_PIXELS
        {
            return Err(if body.len() > MAX_BODY_BYTES {
                ArtifactError::BodyTooLarge
            } else {
                ArtifactError::InvalidMetadata
            });
        }
        Ok(Self {
            version: 1,
            request_id,
            state: ResponseState::Ok,
            error_code: None,
            media_type: Some("image/png".into()),
            width: Some(width),
            height: Some(height),
            byte_count: body.len() as u64,
            sha256: Some(format!("{:x}", Sha256::digest(body))),
        })
    }

    pub fn json(request_id: RequestId, body: &[u8]) -> Result<Self, ArtifactError> {
        if body.is_empty() || body.len() > MAX_LIST_BODY_BYTES {
            return Err(ArtifactError::BodyTooLarge);
        }
        Ok(Self {
            version: 1,
            request_id,
            state: ResponseState::Ok,
            error_code: None,
            media_type: Some(LIST_MEDIA_TYPE.into()),
            width: None,
            height: None,
            byte_count: body.len() as u64,
            sha256: Some(format!("{:x}", Sha256::digest(body))),
        })
    }

    pub fn success(request_id: RequestId) -> Self {
        Self {
            version: 1,
            request_id,
            state: ResponseState::Ok,
            error_code: None,
            media_type: None,
            width: None,
            height: None,
            byte_count: 0,
            sha256: None,
        }
    }

    pub fn error(request_id: RequestId, error: ArtifactError) -> Self {
        Self {
            version: 1,
            request_id,
            state: ResponseState::Error,
            error_code: Some(error),
            media_type: None,
            width: None,
            height: None,
            byte_count: 0,
            sha256: None,
        }
    }

    fn validate(&self, body: Option<&[u8]>) -> Result<(), ArtifactError> {
        if self.version != 1 || self.byte_count > MAX_BODY_BYTES as u64 {
            return Err(ArtifactError::InvalidFrame);
        }
        match self.state {
            ResponseState::Error => {
                if self.error_code.is_none()
                    || self.media_type.is_some()
                    || self.width.is_some()
                    || self.height.is_some()
                    || self.byte_count != 0
                    || self.sha256.is_some()
                {
                    return Err(ArtifactError::InvalidFrame);
                }
            }
            ResponseState::Ok if self.byte_count == 0 => {
                if self.error_code.is_some()
                    || self.media_type.is_some()
                    || self.width.is_some()
                    || self.height.is_some()
                    || self.sha256.is_some()
                {
                    return Err(ArtifactError::InvalidFrame);
                }
            }
            ResponseState::Ok if self.media_type.as_deref() == Some(LIST_MEDIA_TYPE) => {
                if self.error_code.is_some()
                    || self.width.is_some()
                    || self.height.is_some()
                    || self.byte_count > MAX_LIST_BODY_BYTES as u64
                    || !self.sha256.as_deref().is_some_and(|digest| lower_hex(digest, 64))
                {
                    return Err(ArtifactError::InvalidFrame);
                }
            }
            ResponseState::Ok => {
                let (Some(width), Some(height), Some(digest)) =
                    (self.width, self.height, self.sha256.as_deref())
                else {
                    return Err(ArtifactError::InvalidFrame);
                };
                if self.error_code.is_some()
                    || self.media_type.as_deref() != Some("image/png")
                    || width == 0
                    || height == 0
                    || u64::from(width).saturating_mul(u64::from(height)) > MAX_PIXELS
                    || !lower_hex(digest, 64)
                {
                    return Err(ArtifactError::InvalidFrame);
                }
            }
        }
        if let Some(body) = body {
            if body.len() as u64 != self.byte_count {
                return Err(ArtifactError::InvalidFrame);
            }
            if let Some(expected) = &self.sha256 {
                if format!("{:x}", Sha256::digest(body)) != *expected {
                    return Err(ArtifactError::InvalidFrame);
                }
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArtifactResponse {
    pub header: ResponseHeader,
    pub body: Vec<u8>,
}

pub fn write_frame(
    mut output: impl Write,
    header: &ResponseHeader,
    body: &[u8],
) -> Result<(), ArtifactError> {
    header.validate(Some(body))?;
    let encoded = serde_json::to_vec(header).map_err(|_| ArtifactError::InvalidFrame)?;
    if encoded.is_empty() || encoded.len() > MAX_HEADER_BYTES {
        return Err(ArtifactError::InvalidFrame);
    }
    output
        .write_all(&(encoded.len() as u32).to_be_bytes())
        .map_err(|_| ArtifactError::InvalidFrame)?;
    output
        .write_all(&encoded)
        .map_err(|_| ArtifactError::InvalidFrame)?;
    output
        .write_all(body)
        .map_err(|_| ArtifactError::InvalidFrame)?;
    Ok(())
}

pub fn read_frame(mut input: impl Read) -> Result<ArtifactResponse, ArtifactError> {
    let mut prefix = [0u8; 4];
    input
        .read_exact(&mut prefix)
        .map_err(|_| ArtifactError::InvalidFrame)?;
    let header_len = u32::from_be_bytes(prefix) as usize;
    if header_len == 0 || header_len > MAX_HEADER_BYTES {
        return Err(ArtifactError::InvalidFrame);
    }
    let mut encoded = vec![0u8; header_len];
    input
        .read_exact(&mut encoded)
        .map_err(|_| ArtifactError::InvalidFrame)?;
    let header: ResponseHeader =
        serde_json::from_slice(&encoded).map_err(|_| ArtifactError::InvalidFrame)?;
    header.validate(None)?;
    let body_len = usize::try_from(header.byte_count).map_err(|_| ArtifactError::InvalidFrame)?;
    let mut body = vec![0u8; body_len];
    input
        .read_exact(&mut body)
        .map_err(|_| ArtifactError::InvalidFrame)?;
    let mut trailing = [0u8; 1];
    if input
        .read(&mut trailing)
        .map_err(|_| ArtifactError::InvalidFrame)?
        != 0
    {
        return Err(ArtifactError::InvalidFrame);
    }
    header.validate(Some(&body))?;
    Ok(ArtifactResponse { header, body })
}
