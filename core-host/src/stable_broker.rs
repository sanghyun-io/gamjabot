//! Core-internal broker client for stable credential access.

use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::net::Shutdown;
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

use super::credential::{
    CredentialBackend, CredentialBackendError, CredentialFailureClass, CredentialOperation,
    SLACK_SERVICE, MCP_SERVICE, COMPLETION_SERVICE, MAX_FRAME, MAX_VALUE, PREPARED_SERVICE, PROVIDER_SERVICE, RECOVERY_SERVICE,
    TEMPLATE_PUBLISHER_SERVICE,
};

/// Version marker for binary-safe encoding in reserved namespaces.
/// Chosen to avoid collision with ENVELOPE_MAGIC ("GAR1") and PROVIDER_VALUES_MAGIC ("GPC1").
const BINARY_ENCODING_MARKER: &str = "GB64:";

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[derive(Clone, Copy)]
enum BrokerScope { Legacy, Mcp, Publisher, Slack }

/// The publisher relay must remain bound to the current Core Owner while the
/// credential operation runs. It never routes other credential namespaces.
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub struct PublisherRelayBackend<B: CredentialBackend, T: crate::auth_broker::SyntheticAuthTransport> {
    publisher: StableBrokerBackend,
    owner: std::sync::Arc<std::sync::Mutex<crate::auth_broker::AuthBroker<B, T>>>,
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl<B: CredentialBackend, T: crate::auth_broker::SyntheticAuthTransport> PublisherRelayBackend<B, T> {
    pub fn new(publisher: StableBrokerBackend, owner: std::sync::Arc<std::sync::Mutex<crate::auth_broker::AuthBroker<B, T>>>) -> Self {
        Self { publisher, owner }
    }
    fn owner_id(service: &str, account: &str) -> Result<u64, CredentialBackendError> {
        let invalid = || CredentialBackendError::unavailable(CredentialFailureClass::RequestValidation);
        if service != TEMPLATE_PUBLISHER_SERVICE || !validate_reference("template_publisher", account) { return Err(invalid()); }
        account.split(':').nth(1).and_then(|id| id.parse::<u64>().ok()).filter(|id| *id != 0).ok_or_else(invalid)
    }
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl<B: CredentialBackend, T: crate::auth_broker::SyntheticAuthTransport> CredentialBackend for PublisherRelayBackend<B, T> {
    fn get(&mut self, service: &str, account: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        let id = Self::owner_id(service, account)?;
        let owner = self.owner.lock().map_err(|_| CredentialBackendError::unavailable(CredentialFailureClass::StoreUnavailable))?;
        if !owner.publisher_owner_valid(id) { return Err(CredentialBackendError::unavailable(CredentialFailureClass::RequestValidation)); }
        self.publisher.get(service, account)
    }
    fn put(&mut self, service: &str, account: &str, value: &[u8]) -> Result<(), CredentialBackendError> {
        let id = Self::owner_id(service, account)?;
        let owner = self.owner.lock().map_err(|_| CredentialBackendError::unavailable(CredentialFailureClass::StoreUnavailable))?;
        if !owner.publisher_owner_valid(id) { return Err(CredentialBackendError::unavailable(CredentialFailureClass::RequestValidation)); }
        self.publisher.put(service, account, value)
    }
    fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError> {
        let id = Self::owner_id(service, account)?;
        let owner = self.owner.lock().map_err(|_| CredentialBackendError::unavailable(CredentialFailureClass::StoreUnavailable))?;
        if !owner.publisher_owner_valid(id) { return Err(CredentialBackendError::unavailable(CredentialFailureClass::RequestValidation)); }
        self.publisher.delete(service, account)
    }
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub struct SlackSplitCredentialBackend<L:CredentialBackend,S:CredentialBackend> {inner:L, slack:Option<S>}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl<L:CredentialBackend,S:CredentialBackend> SlackSplitCredentialBackend<L,S> {
    pub fn new(inner:L,slack:Option<S>)->Self {Self{inner,slack}}
    fn slack(&mut self)->Result<&mut S,CredentialBackendError> {self.slack.as_mut().ok_or_else(||CredentialBackendError::unavailable(CredentialFailureClass::BridgeTransport))}
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl<L:CredentialBackend,S:CredentialBackend> CredentialBackend for SlackSplitCredentialBackend<L,S> {
    fn get(&mut self,service:&str,account:&str)->Result<Option<Vec<u8>>,CredentialBackendError> {if service==SLACK_SERVICE {self.slack()?.get(service,account)} else {self.inner.get(service,account)}}
    fn put(&mut self,service:&str,account:&str,value:&[u8])->Result<(),CredentialBackendError> {if service==SLACK_SERVICE {self.slack()?.put(service,account,value)} else {self.inner.put(service,account,value)}}
    fn delete(&mut self,service:&str,account:&str)->Result<(),CredentialBackendError> {if service==SLACK_SERVICE {self.slack()?.delete(service,account)} else {self.inner.delete(service,account)}}
}

/// Core-internal broker backend.
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub struct StableBrokerBackend {
    stream: Option<UnixStream>,
    timeout: Duration,
    scope: BrokerScope,
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl StableBrokerBackend {
    pub fn new(stream: UnixStream, timeout: Duration) -> Result<Self, CredentialBackendError> {
        Self::with_scope(stream,timeout,BrokerScope::Legacy)
    }
    pub fn new_mcp(stream: UnixStream, timeout: Duration) -> Result<Self, CredentialBackendError> {
        Self::with_scope(stream,timeout,BrokerScope::Mcp)
    }
    pub fn new_slack(stream: UnixStream, timeout: Duration) -> Result<Self, CredentialBackendError> {
        Self::with_scope(stream,timeout,BrokerScope::Slack)
    }
    pub fn new_publisher(stream: UnixStream, timeout: Duration) -> Result<Self, CredentialBackendError> {
        Self::with_scope(stream,timeout,BrokerScope::Publisher)
    }
    fn with_scope(stream: UnixStream, timeout: Duration, scope:BrokerScope) -> Result<Self, CredentialBackendError> {
        if timeout.is_zero() || Instant::now().checked_add(timeout).is_none() {
            return Err(CredentialBackendError::unavailable(
                CredentialFailureClass::RequestValidation,
            ));
        }
        // This endpoint is owned exclusively by Core. Read/write must not block
        // past the poll deadline, even when readiness changes between calls.
        stream.set_nonblocking(true).map_err(|_| {
            CredentialBackendError::unavailable(CredentialFailureClass::BridgeTransport)
        })?;
        Ok(Self {
            stream: Some(stream),
            timeout,
            scope,
        })
    }

    fn terminal(&mut self) {
        if let Some(stream) = self.stream.take() {
            let _ = stream.shutdown(Shutdown::Both);
            drop(stream);
        }
    }

    fn exchange(
        &mut self,
        operation: &str,
        namespace: &str,
        reference: &str,
        value: Option<&[u8]>,
    ) -> Result<Response, CredentialBackendError> {
        let stream = self.stream.as_mut().ok_or_else(|| {
            CredentialBackendError::unavailable(CredentialFailureClass::BridgeTransport)
        })?;

        // Validate reference before any IO
        let scope_ok = match self.scope {
            BrokerScope::Legacy => namespace != "mcp" && namespace != "template_publisher" && namespace != "slack",
            BrokerScope::Mcp => namespace == "mcp",
            BrokerScope::Slack => namespace == "slack",
            BrokerScope::Publisher => namespace == "template_publisher",
        };
        if !scope_ok || !validate_reference(namespace, reference) {
            return Err(CredentialBackendError::unavailable(
                CredentialFailureClass::RequestValidation,
            ));
        }

        // Validate value before any IO
        if let Some(v) = value {
            if v.is_empty() || v.len() > MAX_VALUE {
                return Err(CredentialBackendError::unavailable(
                    CredentialFailureClass::RequestValidation,
                ));
            }
        }

        // Encode value for wire format
        let encoded_value = match value {
            None => None,
            Some(v) => Some(encode_value_for_namespace(namespace, v)?),
        };

        // Build request - borrow strings, skip None
        let request = Request {
            version: 1,
            operation,
            namespace,
            reference,
            value: encoded_value.as_deref().map(|s| s.as_str()),
        };
        let request_bytes = Zeroizing::new(serde_json::to_vec(&request).map_err(|_| {
            CredentialBackendError::unavailable(CredentialFailureClass::RequestValidation)
        })?);

        // Check size before IO
        if request_bytes.len() > MAX_FRAME {
            return Err(CredentialBackendError::unavailable(
                CredentialFailureClass::RequestValidation,
            ));
        }

        // Fresh deadline for this exchange
        let deadline = Instant::now().checked_add(self.timeout).ok_or_else(|| {
            CredentialBackendError::unavailable(CredentialFailureClass::RequestValidation)
        })?;

        // Write frame
        if write_frame(stream, &request_bytes, deadline).is_err() {
            self.terminal();
            return Err(CredentialBackendError::unavailable(
                CredentialFailureClass::BridgeTransport,
            ));
        }

        // Read frame
        let response_bytes = match read_frame(stream, deadline) {
            Ok(bytes) => bytes,
            Err(_) => {
                self.terminal();
                return Err(CredentialBackendError::unavailable(
                    CredentialFailureClass::BridgeTransport,
                ));
            }
        };

        // Parse response - allocate before parsing
        let response: Response = match serde_json::from_slice(&response_bytes) {
            Ok(r) => r,
            Err(_) => {
                self.terminal();
                return Err(CredentialBackendError::unavailable(
                    CredentialFailureClass::BridgeProtocol,
                ));
            }
        };
        match validate_response(&response, operation) {
            Ok(()) => Ok(response),
            Err(class) => {
                self.terminal();
                Err(CredentialBackendError::unavailable(class))
            }
        }
    }
}

/// The MCP process receives no legacy operation, and v1 receives no MCP operation.
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub struct SplitCredentialBackend<L:CredentialBackend,M:CredentialBackend> { legacy:L, mcp:Option<M> }
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl<L:CredentialBackend,M:CredentialBackend> SplitCredentialBackend<L,M> {
    pub fn new(legacy:L,mcp:Option<M>)->Self {Self{legacy,mcp}}
    fn mcp(&mut self)->Result<&mut M,CredentialBackendError> {
        self.mcp.as_mut().ok_or_else(||CredentialBackendError::unavailable(CredentialFailureClass::BridgeTransport))
    }
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl<L:CredentialBackend,M:CredentialBackend> CredentialBackend for SplitCredentialBackend<L,M> {
    fn get(&mut self,service:&str,account:&str)->Result<Option<Vec<u8>>,CredentialBackendError> {
        if service==MCP_SERVICE {self.mcp()?.get(service,account)} else {self.legacy.get(service,account)}
    }
    fn put(&mut self,service:&str,account:&str,value:&[u8])->Result<(),CredentialBackendError> {
        if service==MCP_SERVICE {self.mcp()?.put(service,account,value)} else {self.legacy.put(service,account,value)}
    }
    fn delete(&mut self,service:&str,account:&str)->Result<(),CredentialBackendError> {
        if service==MCP_SERVICE {self.mcp()?.delete(service,account)} else {self.legacy.delete(service,account)}
    }
}

impl Drop for StableBrokerBackend {
    fn drop(&mut self) {
        self.terminal();
    }
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl CredentialBackend for StableBrokerBackend {
    fn get(
        &mut self,
        service: &str,
        account: &str,
    ) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        let namespace = namespace_for_service(service)?;
        let response = self.exchange("get", namespace, account, None)?;

        match response.status.as_str() {
            "ok" => {
                let wire_value = response.value.ok_or_else(|| {
                    self.terminal();
                    CredentialBackendError::unavailable(CredentialFailureClass::BridgeProtocol)
                })?;
                if wire_value.is_empty() || wire_value.len() > MAX_VALUE {
                    self.terminal();
                    return Err(CredentialBackendError::unavailable(
                        CredentialFailureClass::BridgeProtocol,
                    ));
                }
                let decoded = match decode_value_from_namespace(namespace, &wire_value) {
                    Ok(decoded) => decoded,
                    Err(error) => {
                        self.terminal();
                        return Err(error);
                    }
                };
                if decoded.is_empty() || decoded.len() > MAX_VALUE {
                    self.terminal();
                    return Err(CredentialBackendError::unavailable(
                        CredentialFailureClass::BridgeProtocol,
                    ));
                }
                Ok(Some(decoded.to_vec()))
            }
            "missing" => Ok(None),
            "failed" | "interaction_required" => {
                let error = response_to_error(&response)?;
                Err(error)
            }
            _ => {
                self.terminal();
                Err(CredentialBackendError::unavailable(
                    CredentialFailureClass::BridgeProtocol,
                ))
            }
        }
    }

    fn put(
        &mut self,
        service: &str,
        account: &str,
        value: &[u8],
    ) -> Result<(), CredentialBackendError> {
        let namespace = namespace_for_service(service)?;
        let response = self.exchange("put", namespace, account, Some(value))?;

        match response.status.as_str() {
            "ok" => Ok(()),
            "failed" | "interaction_required" => {
                let error = response_to_error(&response)?;
                Err(error)
            }
            _ => {
                self.terminal();
                Err(CredentialBackendError::unavailable(
                    CredentialFailureClass::BridgeProtocol,
                ))
            }
        }
    }

    fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError> {
        let namespace = namespace_for_service(service)?;
        let response = self.exchange("delete", namespace, account, None)?;

        match response.status.as_str() {
            "ok" => Ok(()),
            "failed" | "interaction_required" => {
                let error = response_to_error(&response)?;
                Err(error)
            }
            _ => {
                self.terminal();
                Err(CredentialBackendError::unavailable(
                    CredentialFailureClass::BridgeProtocol,
                ))
            }
        }
    }
}

fn namespace_for_service(service: &str) -> Result<&'static str, CredentialBackendError> {
    match service {
        SLACK_SERVICE => Ok("slack"),
        MCP_SERVICE => Ok("mcp"),
        PROVIDER_SERVICE => Ok("provider"),
        TEMPLATE_PUBLISHER_SERVICE => Ok("template_publisher"),
        RECOVERY_SERVICE => Ok("recovery"),
        PREPARED_SERVICE => Ok("prepared"),
        COMPLETION_SERVICE => Ok("completion"),
        _ => Err(CredentialBackendError::unavailable(
            CredentialFailureClass::RequestValidation,
        )),
    }
}

/// Returns true if the namespace is a Core-internal reserved namespace that may carry binary envelopes.
fn is_reserved_namespace(namespace: &str) -> bool {
    matches!(namespace, "recovery" | "prepared" | "completion")
}

/// Preserve the v1 string contract for legacy UTF-8 values; carry binary
/// recovery envelopes (or marker-prefixed UTF-8) in an ASCII versioned wrapper.
fn encode_value_for_namespace(
    namespace: &str,
    value: &[u8],
) -> Result<Zeroizing<String>, CredentialBackendError> {
    let utf8 = std::str::from_utf8(value).ok();
    if !is_reserved_namespace(namespace) {
        return utf8.map(|s| Zeroizing::new(s.to_owned())).ok_or_else(|| {
            CredentialBackendError::unavailable(CredentialFailureClass::RequestValidation)
        });
    }
    if let Some(s) = utf8.filter(|s| !s.starts_with(BINARY_ENCODING_MARKER)) {
        return Ok(Zeroizing::new(s.to_owned()));
    }
    // Bound the wire value before allocating/encoding; the immutable broker's
    // existing MAX_VALUE limit remains unchanged after base64 expansion.
    let carrier_size = value.len().div_ceil(3) * 4 + BINARY_ENCODING_MARKER.len();
    if carrier_size > MAX_VALUE {
        return Err(CredentialBackendError::unavailable(
            CredentialFailureClass::RequestValidation,
        ));
    }
    let mut carrier = Zeroizing::new(String::with_capacity(carrier_size));
    carrier.push_str(BINARY_ENCODING_MARKER);
    BASE64_STANDARD.encode_string(value, &mut carrier);
    Ok(carrier)
}

/// Old UTF-8 envelopes remain readable. Malformed marked values fail closed;
/// decoded bytes, including partial output on error, stay in zeroizing storage.
fn decode_value_from_namespace(
    namespace: &str,
    wire_value: &str,
) -> Result<Zeroizing<Vec<u8>>, CredentialBackendError> {
    if is_reserved_namespace(namespace) {
        if let Some(encoded) = wire_value.strip_prefix(BINARY_ENCODING_MARKER) {
            let mut decoded = Zeroizing::new(Vec::new());
            BASE64_STANDARD
                .decode_vec(encoded, &mut decoded)
                .map_err(|_| {
                    CredentialBackendError::unavailable(CredentialFailureClass::BridgeProtocol)
                })?;
            if decoded.is_empty() {
                return Err(CredentialBackendError::unavailable(
                    CredentialFailureClass::BridgeProtocol,
                ));
            }
            return Ok(decoded);
        }
    }
    Ok(Zeroizing::new(wire_value.as_bytes().to_vec()))
}

fn validate_reference(namespace: &str, reference: &str) -> bool {
    match namespace {
        "slack" => reference.strip_prefix("slack:").is_some_and(decimal_identifier),
        "mcp" => {
            let mut parts = reference.split(':');
            matches!(parts.next(), Some("mcp" | "mcp-oauth"))
                && decimal_identifier(parts.next().unwrap_or_default())
                && legacy_uuid(parts.next().unwrap_or_default())
                && lower_hex(parts.next().unwrap_or_default(), 32)
                && parts.next().is_none()
        }
        "template_publisher" => {
            let mut fields = reference.split(':');
            fields.next() == Some("template-publisher")
                && decimal_identifier(fields.next().unwrap_or_default())
                && decimal_identifier(fields.next().unwrap_or_default())
                && lower_hex(fields.next().unwrap_or_default(), 32)
                && fields.next().is_none()
        }
        "provider" => {
            let mut fields = reference.split(':');
            let owner = fields.next().unwrap_or_default();
            let connection = fields.next().unwrap_or_default();
            let opaque = fields.next().unwrap_or_default();
            fields.next().is_none()
                && decimal_identifier(owner)
                && decimal_identifier(connection)
                && (lower_hex(opaque, 32) || legacy_uuid(opaque))
        }
        "recovery" | "prepared" | "completion" => reference
            .strip_prefix(namespace)
            .and_then(|rest| rest.strip_prefix(':'))
            .is_some_and(|op| lower_hex(op, 64)),
        _ => false,
    }
}

fn decimal_identifier(value: &str) -> bool {
    !value.is_empty() && !value.starts_with('0') && value.bytes().all(|b| b.is_ascii_digit())
}

fn lower_hex(value: &str, size: usize) -> bool {
    value.len() == size
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn legacy_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, b)| {
            matches!(i, 8 | 13 | 18 | 23) && b == b'-'
                || !matches!(i, 8 | 13 | 18 | 23)
                    && (b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
}

#[derive(Serialize)]
struct Request<'a> {
    version: u8,
    operation: &'a str,
    namespace: &'a str,
    reference: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: Option<&'a str>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Response {
    status: String,
    #[serde(default, deserialize_with = "deserialize_optional_non_null")]
    value: Option<Zeroizing<String>>,
    #[serde(default, deserialize_with = "deserialize_optional_non_null")]
    os_status: Option<i32>,
    #[serde(default, deserialize_with = "deserialize_optional_non_null")]
    operation: Option<CredentialOperation>,
    #[serde(default, deserialize_with = "deserialize_optional_non_null")]
    error_class: Option<String>,
}

fn deserialize_optional_non_null<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

fn validate_response(response: &Response, operation: &str) -> Result<(), CredentialFailureClass> {
    // Check status allowlist
    match response.status.as_str() {
        "ok" | "missing" | "failed" | "interaction_required" => {}
        _ => return Err(CredentialFailureClass::BridgeProtocol),
    }

    // Check status-specific fields
    match response.status.as_str() {
        "ok" => {
            if operation == "get" {
                if response.value.is_none() {
                    return Err(CredentialFailureClass::BridgeProtocol);
                }
            } else {
                if response.value.is_some() {
                    return Err(CredentialFailureClass::BridgeProtocol);
                }
            }
            if response.os_status.is_some()
                || response.operation.is_some()
                || response.error_class.is_some()
            {
                return Err(CredentialFailureClass::BridgeProtocol);
            }
        }
        "missing" => {
            if operation != "get" {
                return Err(CredentialFailureClass::BridgeProtocol);
            }
            if response.value.is_some()
                || response.os_status.is_some()
                || response.operation.is_some()
                || response.error_class.is_some()
            {
                return Err(CredentialFailureClass::BridgeProtocol);
            }
        }
        "failed" | "interaction_required" => {
            if response.value.is_some() {
                return Err(CredentialFailureClass::BridgeProtocol);
            }
            // Bare failed without extras
            if response.status == "failed"
                && response.os_status.is_none()
                && response.operation.is_none()
                && response.error_class.is_none()
            {
                return Err(CredentialFailureClass::StoreUnavailable);
            }

            // Must have all three diagnostic fields
            let os_status = response
                .os_status
                .ok_or(CredentialFailureClass::BridgeProtocol)?;
            let op = response
                .operation
                .as_ref()
                .ok_or(CredentialFailureClass::BridgeProtocol)?;
            let error_class = response
                .error_class
                .as_ref()
                .ok_or(CredentialFailureClass::BridgeProtocol)?;

            // Nonzero OS status
            if os_status == 0 {
                return Err(CredentialFailureClass::BridgeProtocol);
            }

            // Matching operation
            let expected_op = match operation {
                "get" => CredentialOperation::SecItemCopyMatching,
                "delete" => CredentialOperation::SecItemDelete,
                "put" => CredentialOperation::SecItemAdd,
                _ => return Err(CredentialFailureClass::BridgeProtocol),
            };
            if *op != expected_op
                && (*op != CredentialOperation::SecItemUpdate || operation != "put")
            {
                return Err(CredentialFailureClass::BridgeProtocol);
            }

            // Consistent error class
            let expected_class = if response.status == "interaction_required" {
                "interaction_required"
            } else {
                "native_os_status"
            };
            if error_class != expected_class {
                return Err(CredentialFailureClass::BridgeProtocol);
            }
            if (response.status == "interaction_required") != matches!(os_status, -25308 | -25291) {
                return Err(CredentialFailureClass::BridgeProtocol);
            }
        }
        _ => return Err(CredentialFailureClass::BridgeProtocol),
    }

    Ok(())
}

fn response_to_error(
    response: &Response,
) -> Result<CredentialBackendError, CredentialBackendError> {
    // Bare failed is store unavailable
    if response.status == "failed"
        && response.os_status.is_none()
        && response.operation.is_none()
        && response.error_class.is_none()
    {
        return Err(CredentialBackendError::unavailable(
            CredentialFailureClass::StoreUnavailable,
        ));
    }

    let os_status = response.os_status.ok_or_else(|| {
        CredentialBackendError::unavailable(CredentialFailureClass::BridgeProtocol)
    })?;
    let operation = response.operation.ok_or_else(|| {
        CredentialBackendError::unavailable(CredentialFailureClass::BridgeProtocol)
    })?;

    if response.status == "interaction_required" {
        Ok(CredentialBackendError::interaction_required(
            operation, os_status,
        ))
    } else {
        Ok(CredentialBackendError::failed(operation, os_status))
    }
}

pub(crate) fn read_frame(
    stream: &mut UnixStream,
    deadline: Instant,
) -> Result<Zeroizing<Vec<u8>>, ()> {
    let mut header = [0u8; 4];
    read_exact_until(stream, &mut header, deadline)?;
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > MAX_FRAME {
        return Err(());
    }
    let mut body = Zeroizing::new(vec![0; size]);
    read_exact_until(stream, &mut body, deadline)?;
    Ok(body)
}

pub(crate) fn write_frame(
    stream: &mut UnixStream,
    bytes: &[u8],
    deadline: Instant,
) -> Result<(), ()> {
    if bytes.is_empty() || bytes.len() > MAX_FRAME {
        return Err(());
    }
    write_all_until(stream, &(bytes.len() as u32).to_be_bytes(), deadline)?;
    write_all_until(stream, bytes, deadline)
}

fn read_exact_until(
    stream: &mut UnixStream,
    mut output: &mut [u8],
    deadline: Instant,
) -> Result<(), ()> {
    while !output.is_empty() {
        wait_ready(stream, libc::POLLIN, deadline)?;
        match stream.read(output) {
            Ok(0) => return Err(()),
            Ok(size) => output = &mut output[size..],
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::Interrupted | std::io::ErrorKind::WouldBlock
                ) =>
            {
                continue
            }
            Err(_) => return Err(()),
        }
    }
    Ok(())
}

fn write_all_until(stream: &mut UnixStream, mut input: &[u8], deadline: Instant) -> Result<(), ()> {
    while !input.is_empty() {
        wait_ready(stream, libc::POLLOUT, deadline)?;
        match stream.write(input) {
            Ok(0) => return Err(()),
            Ok(size) => input = &input[size..],
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::Interrupted | std::io::ErrorKind::WouldBlock
                ) =>
            {
                continue
            }
            Err(_) => return Err(()),
        }
    }
    Ok(())
}

fn wait_ready(stream: &UnixStream, events: i16, deadline: Instant) -> Result<(), ()> {
    loop {
        let remaining = deadline.checked_duration_since(Instant::now()).ok_or(())?;
        if remaining.is_zero() {
            return Err(());
        }
        let milliseconds = remaining
            .as_millis()
            .saturating_add(u128::from(remaining.subsec_nanos() % 1_000_000 != 0));
        let timeout = i32::try_from(milliseconds).unwrap_or(i32::MAX);
        let mut fd = libc::pollfd {
            fd: stream.as_raw_fd(),
            events,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut fd, 1, timeout) };
        if result < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        if result <= 0 || fd.revents & libc::POLLNVAL != 0 {
            return Err(());
        }
        // HUP may still have a complete response buffered. The nonblocking
        // read consumes those bytes; a zero read becomes terminal EOF.
        if fd.revents & (events | libc::POLLHUP | libc::POLLERR) != 0 {
            return Ok(());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use std::thread;

    const TIMEOUT: Duration = Duration::from_secs(2);
    fn pair() -> (StableBrokerBackend, UnixStream) {
        let (client, peer) = UnixStream::pair().unwrap();
        peer.set_nonblocking(true).unwrap();
        (StableBrokerBackend::new(client, TIMEOUT).unwrap(), peer)
    }
    fn read(peer: &mut UnixStream) -> Value {
        serde_json::from_slice(&read_frame(peer, Instant::now() + TIMEOUT).unwrap()).unwrap()
    }
    fn write(peer: &mut UnixStream, value: Value) {
        write_frame(
            peer,
            &serde_json::to_vec(&value).unwrap(),
            Instant::now() + TIMEOUT,
        )
        .unwrap();
    }
    fn reference(namespace: &str) -> String {
        if namespace == "provider" {
            format!("1:2:{}", "a".repeat(32))
        } else {
            format!("{namespace}:{}", "b".repeat(64))
        }
    }
    fn no_io(peer: &mut UnixStream) {
        let mut byte = [0];
        assert_eq!(
            peer.read(&mut byte).unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }

    // Real AuthBroker wire shape: GAR1 + big-endian JSON length + JSON + secret.
    // A valid UTF-8 API key still produces non-UTF-8 at length-byte 0x80.
    fn envelope(secret: &[u8], header_size: usize) -> Vec<u8> {
        let mut header = br#"{"binding":{"operation_id":"fixture"}}"#.to_vec();
        assert!(header.len() <= header_size);
        header.resize(header_size, b' ');
        let _: Value = serde_json::from_slice(&header).unwrap();
        let mut value = b"GAR1".to_vec();
        value.extend_from_slice(&(header.len() as u32).to_be_bytes());
        value.extend_from_slice(&header);
        value.extend_from_slice(secret);
        value
    }

    fn roundtrip(service: &str, namespace: &'static str, value: Vec<u8>, marked: bool) {
        let (mut client, mut peer) = pair();
        let account = reference(namespace);
        let peer_account = account.clone();
        let expected_wire = if marked {
            None
        } else {
            Some(String::from_utf8(value.clone()).unwrap())
        };
        // This peer accepts/stores only JSON strings, exactly like immutable v1.
        // get returns the actual put string; no independent encoding oracle.
        let server = thread::spawn(move || {
            let request = read(&mut peer);
            assert_eq!(request["version"], 1);
            assert_eq!(request["operation"], "put");
            assert_eq!(request["namespace"], namespace);
            assert_eq!(request["reference"], peer_account);
            let stored = request["value"].as_str().unwrap().to_owned();
            assert!(!stored.is_empty() && stored.len() <= MAX_VALUE);
            if marked {
                assert!(stored.starts_with(BINARY_ENCODING_MARKER));
            }
            if let Some(expected) = expected_wire {
                assert_eq!(stored, expected);
            }
            write(&mut peer, json!({"status":"ok"}));
            let request = read(&mut peer);
            assert_eq!(
                request,
                json!({"version":1,"operation":"get","namespace":namespace,"reference":peer_account})
            );
            write(&mut peer, json!({"status":"ok","value":stored}));
            let request = read(&mut peer);
            assert_eq!(request["operation"], "delete");
            assert_eq!(request["namespace"], namespace);
            assert_eq!(request["reference"], peer_account);
            write(&mut peer, json!({"status":"ok"}));
        });
        let put = client.put(service, &account, &value);
        if put.is_err() {
            drop(client);
            let _ = server.join();
            panic!(
                "valid envelope rejected before roundtrip: {}",
                put.unwrap_err().error_class()
            );
        }
        assert_eq!(client.get(service, &account).unwrap().unwrap(), value);
        client.delete(service, &account).unwrap();
        drop(client);
        server.join().unwrap();
    }

    #[test]
    fn binary_header_with_utf8_key_roundtrips_all_reserved_namespaces() {
        let value = envelope(b"synthetic-key-only", 128);
        assert!(std::str::from_utf8(&value).is_err());
        for (service, namespace) in [
            (RECOVERY_SERVICE, "recovery"),
            (PREPARED_SERVICE, "prepared"),
            (COMPLETION_SERVICE, "completion"),
        ] {
            roundtrip(service, namespace, value.clone(), true);
        }
    }
    #[test]
    fn opaque_binary_secret_roundtrips_reserved_namespace() {
        roundtrip(
            RECOVERY_SERVICE,
            "recovery",
            envelope(&[0xff, 0x80, 0, 0xfe], 64),
            true,
        );
    }
    #[test]
    fn existing_utf8_values_and_provider_marker_are_unchanged() {
        for (service, namespace) in [
            (PROVIDER_SERVICE, "provider"),
            (RECOVERY_SERVICE, "recovery"),
            (PREPARED_SERVICE, "prepared"),
            (COMPLETION_SERVICE, "completion"),
        ] {
            roundtrip(
                service,
                namespace,
                "가짜 fixture".as_bytes().to_vec(),
                false,
            );
        }
        roundtrip(
            PROVIDER_SERVICE,
            "provider",
            b"GB64:literal-provider-key".to_vec(),
            false,
        );
    }
    #[test]
    fn reserved_marker_prefixed_utf8_is_wrapped_without_collision() {
        roundtrip(
            RECOVERY_SERVICE,
            "recovery",
            b"GB64:literal-legacy-content".to_vec(),
            true,
        );
    }
    #[test]
    fn legacy_raw_utf8_envelopes_remain_readable() {
        for (service, namespace) in [
            (RECOVERY_SERVICE, "recovery"),
            (PREPARED_SERVICE, "prepared"),
            (COMPLETION_SERVICE, "completion"),
        ] {
            let expected = envelope(b"synthetic-key-only", 64);
            let wire = String::from_utf8(expected.clone()).unwrap();
            let (mut client, mut peer) = pair();
            let server = thread::spawn(move || {
                assert_eq!(read(&mut peer)["namespace"], namespace);
                write(&mut peer, json!({"status":"ok","value":wire}));
            });
            assert_eq!(
                client.get(service, &reference(namespace)).unwrap().unwrap(),
                expected
            );
            server.join().unwrap();
        }
    }
    #[test]
    fn malformed_or_empty_marked_values_close_channel() {
        for wire in ["GB64:", "GB64:%%%", "GB64:Zg=", "GB64:Zh==", "GB64:v2:Zg=="] {
            let (mut client, mut peer) = pair();
            let server = thread::spawn(move || {
                read(&mut peer);
                write(&mut peer, json!({"status":"ok","value":wire}));
            });
            assert_eq!(
                client
                    .get(RECOVERY_SERVICE, &reference("recovery"))
                    .unwrap_err()
                    .error_class(),
                "bridge_protocol"
            );
            assert_eq!(
                client
                    .get(RECOVERY_SERVICE, &reference("recovery"))
                    .unwrap_err()
                    .error_class(),
                "bridge_transport"
            );
            server.join().unwrap();
        }
    }
    #[test]
    fn size_utf8_and_frame_validation_send_nothing_then_allow_valid_request() {
        let (mut client, mut peer) = pair();
        let max_binary = ((MAX_VALUE - BINARY_ENCODING_MARKER.len()) / 4) * 3;
        for (service, namespace, value) in [
            (RECOVERY_SERVICE, "recovery", vec![]),
            (RECOVERY_SERVICE, "recovery", vec![0xff; MAX_VALUE + 1]),
            (RECOVERY_SERVICE, "recovery", vec![0xff; max_binary + 1]),
            (PROVIDER_SERVICE, "provider", vec![0xff]),
            (PROVIDER_SERVICE, "provider", vec![0; MAX_VALUE]),
        ] {
            assert_eq!(
                client
                    .put(service, &reference(namespace), &value)
                    .unwrap_err()
                    .error_class(),
                "request_validation"
            );
            no_io(&mut peer);
        }
        let server = thread::spawn(move || {
            assert_eq!(read(&mut peer)["operation"], "get");
            write(&mut peer, json!({"status":"missing"}));
        });
        assert!(client
            .get(PROVIDER_SERVICE, &reference("provider"))
            .unwrap()
            .is_none());
        server.join().unwrap();
    }
    #[test]
    fn largest_binary_carrier_fits_immutable_value_cap() {
        let size = ((MAX_VALUE - BINARY_ENCODING_MARKER.len()) / 4) * 3;
        roundtrip(RECOVERY_SERVICE, "recovery", vec![0xff; size], true);
    }
    #[test]
    fn oversized_wire_value_closes_channel() {
        let (mut client, mut peer) = pair();
        let server = thread::spawn(move || {
            read(&mut peer);
            write(
                &mut peer,
                json!({"status":"ok","value":"a".repeat(MAX_VALUE+1)}),
            );
        });
        assert_eq!(
            client
                .get(RECOVERY_SERVICE, &reference("recovery"))
                .unwrap_err()
                .error_class(),
            "bridge_protocol"
        );
        assert_eq!(
            client
                .get(RECOVERY_SERVICE, &reference("recovery"))
                .unwrap_err()
                .error_class(),
            "bridge_transport"
        );
        server.join().unwrap();
    }
    #[test]
    fn mcp_and_legacy_wire_scopes_do_not_cross() {
        let (mut legacy,mut legacy_peer)=pair();
        let (mcp_stream,mut mcp_peer)=UnixStream::pair().unwrap();mcp_peer.set_nonblocking(true).unwrap();
        let mut mcp=StableBrokerBackend::new_mcp(mcp_stream,TIMEOUT).unwrap();
        let r=format!("mcp:1:12345678-1234-4123-8123-123456789abc:{}","a".repeat(32));
        assert!(legacy.get(MCP_SERVICE,&r).is_err());no_io(&mut legacy_peer);
        assert!(mcp.get(PROVIDER_SERVICE,&reference("provider")).is_err());no_io(&mut mcp_peer);
    }
    #[test]
    fn absent_mcp_does_not_block_legacy_and_never_dispatches_mcp() {
        #[derive(Default)] struct Fake {calls:usize}
        impl CredentialBackend for Fake {
            fn get(&mut self,_:&str,_:&str)->Result<Option<Vec<u8>>,CredentialBackendError>{self.calls+=1;Ok(None)}
            fn put(&mut self,_:&str,_:&str,_:&[u8])->Result<(),CredentialBackendError>{self.calls+=1;Ok(())}
            fn delete(&mut self,_:&str,_:&str)->Result<(),CredentialBackendError>{self.calls+=1;Ok(())}
        }
        let mut split=SplitCredentialBackend::<Fake,Fake>::new(Fake::default(),None);
        assert!(split.get(PROVIDER_SERVICE,"1:2:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").is_ok());
        assert!(split.get(MCP_SERVICE,"mcp:1:12345678-1234-4123-8123-123456789abc:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").is_err());
        assert_eq!(split.legacy.calls,1);
    }

}
