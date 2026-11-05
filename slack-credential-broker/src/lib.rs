//! Separate stable macOS Slack-only credential boundary.
//!
//! The engine-facing and Core-internal sockets are capabilities.  They have
//! disjoint namespace unions and are not read until the release binding has
//! been authenticated on the control socket.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

pub const MAX_FRAME: usize = 65_536;
pub const MAX_VALUE: usize = 32_768;
pub const MAX_AUTHORITY_OUTPUT: usize = 4_096;
pub const SLACK_SERVICE: &str = "cloud.gamja.slack-credential-broker.v1.slack";
const PROOF_SERVICE_PREFIX: &str = "cloud.gamja.slack-credential-broker.v1.proof.";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ChannelKind {
    Engine,
    CoreInternal,
}

#[derive(Clone, Copy, Debug)]
pub struct CredentialBackendError {
    interaction_required: bool,
    operation: CredentialOperation,
    os_status: i32,
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialOperation {
    SecItemAdd,
    SecItemUpdate,
    SecItemCopyMatching,
    SecItemDelete,
}

impl CredentialBackendError {
    pub fn interaction_required(operation: CredentialOperation, os_status: i32) -> Self {
        Self {
            interaction_required: true,
            operation,
            os_status,
        }
    }

    pub fn failed(operation: CredentialOperation, os_status: i32) -> Self {
        Self {
            interaction_required: false,
            operation,
            os_status,
        }
    }

    fn from_os_status(operation: CredentialOperation, os_status: i32) -> Self {
        if matches!(os_status, -25308 | -25291) {
            Self::interaction_required(operation, os_status)
        } else {
            Self::failed(operation, os_status)
        }
    }
}

pub trait CredentialBackend: Send + 'static {
    fn get(
        &mut self,
        service: &str,
        account: &str,
    ) -> Result<Option<Vec<u8>>, CredentialBackendError>;
    fn put(
        &mut self,
        service: &str,
        account: &str,
        value: &[u8],
    ) -> Result<(), CredentialBackendError>;
    fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError>;
}

#[cfg(target_os = "macos")]
pub struct MacKeychainBackend;

#[cfg(target_os = "macos")]
impl MacKeychainBackend {
    pub fn new() -> Result<Self, ()> {
        let status =
            unsafe { security_framework_sys::keychain::SecKeychainSetUserInteractionAllowed(0) };
        if status == security_framework_sys::base::errSecSuccess {
            Ok(Self)
        } else {
            Err(())
        }
    }
}

#[cfg(target_os = "macos")]
impl CredentialBackend for MacKeychainBackend {
    fn get(
        &mut self,
        service: &str,
        account: &str,
    ) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        use security_framework::passwords::get_generic_password;
        match get_generic_password(service, account) {
            Ok(value) => Ok(Some(value)),
            Err(error) if error.code() == security_framework_sys::base::errSecItemNotFound => {
                Ok(None)
            }
            Err(error) => Err(CredentialBackendError::from_os_status(
                CredentialOperation::SecItemCopyMatching,
                error.code(),
            )),
        }
    }

    fn put(
        &mut self,
        service: &str,
        account: &str,
        value: &[u8],
    ) -> Result<(), CredentialBackendError> {
        use core_foundation::data::CFData;
        use security_framework::item::{
            update_item, ItemAddOptions, ItemAddValue, ItemClass, ItemSearchOptions,
            ItemUpdateOptions, ItemUpdateValue,
        };
        let mut add = ItemAddOptions::new(ItemAddValue::Data {
            class: ItemClass::generic_password(),
            data: CFData::from_buffer(value),
        });
        add.set_service(service).set_account_name(account);
        match add.add() {
            Ok(()) => Ok(()),
            Err(error) if error.code() == security_framework_sys::base::errSecDuplicateItem => {
                let mut search = ItemSearchOptions::new();
                search
                    .class(ItemClass::generic_password())
                    .service(service)
                    .account(account);
                let mut update = ItemUpdateOptions::new();
                update.set_value(ItemUpdateValue::Data(CFData::from_buffer(value)));
                update_item(&search, &update).map_err(|error| {
                    CredentialBackendError::from_os_status(
                        CredentialOperation::SecItemUpdate,
                        error.code(),
                    )
                })
            }
            Err(error) => Err(CredentialBackendError::from_os_status(
                CredentialOperation::SecItemAdd,
                error.code(),
            )),
        }
    }

    fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError> {
        match security_framework::passwords::delete_generic_password(service, account) {
            Ok(()) => Ok(()),
            Err(error) if error.code() == security_framework_sys::base::errSecItemNotFound => {
                Ok(())
            }
            Err(error) => Err(CredentialBackendError::from_os_status(
                CredentialOperation::SecItemDelete,
                error.code(),
            )),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DataRequest {
    version: u8,
    operation: DataOperation,
    namespace: String,
    reference: String,
    value: Option<Zeroizing<String>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum DataOperation {
    Get,
    Put,
    Delete,
}

#[derive(Serialize)]
struct DataResponse {
    status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: Option<Zeroizing<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    os_status: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    operation: Option<CredentialOperation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error_class: Option<&'static str>,
}

impl DataResponse {
    fn plain(status: &'static str) -> Self {
        Self {
            status,
            value: None,
            os_status: None,
            operation: None,
            error_class: None,
        }
    }

    fn error(error: CredentialBackendError) -> Self {
        Self {
            status: if error.interaction_required {
                "interaction_required"
            } else {
                "failed"
            },
            value: None,
            os_status: Some(error.os_status),
            operation: Some(error.operation),
            error_class: Some(if error.interaction_required {
                "interaction_required"
            } else {
                "native_os_status"
            }),
        }
    }
}

fn service_for(channel: ChannelKind, namespace: &str, reference: &str) -> Option<&'static str> {
    match (channel, namespace) {
        (ChannelKind::CoreInternal, "slack") if slack_reference(reference) => Some(SLACK_SERVICE),
        _ => None,
    }
}

fn slack_reference(value: &str) -> bool {
    value.strip_prefix("slack:").is_some_and(decimal_identifier)
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
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte),
        })
}

fn dispatch(
    channel: ChannelKind,
    backend: &mut impl CredentialBackend,
    bytes: &[u8],
) -> DataResponse {
    let Ok(request) = serde_json::from_slice::<DataRequest>(bytes) else {
        return DataResponse::plain("failed");
    };
    let Some(service) = service_for(channel, &request.namespace, &request.reference) else {
        return DataResponse::plain("failed");
    };
    if request.version != 1
        || request
            .value
            .as_ref()
            .is_some_and(|v| v.is_empty() || v.len() > MAX_VALUE)
        || !matches!(
            (&request.operation, &request.value),
            (DataOperation::Put, Some(_)) | (DataOperation::Get | DataOperation::Delete, None)
        )
    {
        return DataResponse::plain("failed");
    }
    match request.operation {
        DataOperation::Get => match backend.get(service, &request.reference) {
            Ok(Some(value)) if !value.is_empty() && value.len() <= MAX_VALUE => {
                let value = Zeroizing::new(value);
                match std::str::from_utf8(&value) {
                    Ok(value) => DataResponse {
                        status: "ok",
                        value: Some(Zeroizing::new(value.to_owned())),
                        os_status: None,
                        operation: None,
                        error_class: None,
                    },
                    Err(_) => DataResponse::plain("failed"),
                }
            }
            Ok(None) => DataResponse::plain("missing"),
            Ok(Some(_)) => DataResponse::plain("failed"),
            Err(error) => DataResponse::error(error),
        },
        DataOperation::Put => match backend.put(
            service,
            &request.reference,
            request.value.as_deref().unwrap().as_bytes(),
        ) {
            Ok(()) => DataResponse::plain("ok"),
            Err(error) => DataResponse::error(error),
        },
        DataOperation::Delete => match backend.delete(service, &request.reference) {
            Ok(()) => DataResponse::plain("ok"),
            Err(error) => DataResponse::error(error),
        },
    }
}

pub fn serve_data_channel_with_timeout(
    channel: ChannelKind,
    mut stream: UnixStream,
    mut backend: impl CredentialBackend,
    timeout: Duration,
) -> Result<(), ()> {
    serve_data_channel_inner(channel, &mut stream, &mut backend, timeout)
}

fn serve_data_channel_inner(
    channel: ChannelKind,
    stream: &mut UnixStream,
    backend: &mut impl CredentialBackend,
    timeout: Duration,
) -> Result<(), ()> {
    loop {
        let Some(request) = read_frame(stream, timeout)? else {
            return Ok(());
        };
        let request = Zeroizing::new(request);
        let response = Zeroizing::new(
            serde_json::to_vec(&dispatch(channel, backend, &request)).map_err(|_| ())?,
        );
        write_frame(stream, &response, timeout)?;
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BootstrapRequestV1 {
    version: u8,
    operation: String,
    authority_path: String,
    authority_sha256: String,
}

impl BootstrapRequestV1 {
    pub fn authority_path(&self) -> &str {
        &self.authority_path
    }
    pub fn authority_sha256(&self) -> &str {
        &self.authority_sha256
    }

    fn validate(&self) -> Result<(), &'static str> {
        if self.version != 1
            || self.operation != "bootstrap"
            || !Path::new(&self.authority_path).is_absolute()
            || !lower_hex(&self.authority_sha256, 64)
        {
            return Err("bootstrap_invalid");
        }
        Ok(())
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BindRequestV1 {
    version: u8,
    operation: String,
    parent_path: String,
    parent_pid: i32,
    engine_path: String,
    engine_pid: i32,
}

impl BindRequestV1 {
    pub fn parent_path(&self) -> &str {
        &self.parent_path
    }
    pub fn parent_pid(&self) -> i32 {
        self.parent_pid
    }
    pub fn engine_path(&self) -> &str {
        &self.engine_path
    }
    pub fn engine_pid(&self) -> i32 {
        self.engine_pid
    }

    fn validate(&self) -> Result<(), &'static str> {
        if self.version != 1
            || self.operation != "bind"
            || !Path::new(&self.parent_path).is_absolute()
            || !Path::new(&self.engine_path).is_absolute()
            || self.parent_pid <= 1
            || self.engine_pid <= 1
        {
            return Err("bind_invalid");
        }
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ShutdownRequestV1 {
    version: u8,
    operation: String,
}

pub trait BindingVerifier {
    type Bootstrap;
    type Bound;
    fn bootstrap(&mut self, request: &BootstrapRequestV1) -> Result<Self::Bootstrap, &'static str>;
    fn bind(
        &mut self,
        bootstrap: &Self::Bootstrap,
        request: &BindRequestV1,
    ) -> Result<Self::Bound, &'static str>;
    fn check_bound(&mut self, bound: &Self::Bound) -> Result<(), &'static str>;
}

#[derive(Serialize)]
struct ControlResponse {
    status: &'static str,
}

/// Serves one bootstrap/bind lifetime. Data-channel bytes remain unread until
/// the exact release authority and both live children have been accepted.
pub fn serve_broker_with_timeout<B: CredentialBackend, V: BindingVerifier>(
    mut control: UnixStream,
    mut internal: UnixStream,
    mut backend: B,
    mut verifier: V,
    timeout: Duration,
) -> Result<(), ()> {
    let bytes = read_frame(&mut control, timeout)?.ok_or(())?;
    let bootstrap: BootstrapRequestV1 = serde_json::from_slice(&bytes).map_err(|_| ())?;
    bootstrap.validate().map_err(|_| ())?;
    let proof = verifier.bootstrap(&bootstrap).map_err(|_| ())?;
    write_control(&mut control, "bootstrapped", timeout)?;
    let bytes = read_frame(&mut control, timeout)?.ok_or(())?;
    let request: BindRequestV1 = serde_json::from_slice(&bytes).map_err(|_| ())?;
    request.validate().map_err(|_| ())?;
    let bound = verifier.bind(&proof, &request).map_err(|_| ())?;
    write_control(&mut control, "bound", timeout)?;
    loop {
        let mut fds = [
            libc::pollfd {
                fd: control.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: internal.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        let millis = i32::try_from(timeout.as_millis().max(1)).unwrap_or(i32::MAX);
        let ready = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as _, millis) };
        if ready == 0 {
            verifier.check_bound(&bound).map_err(|_| ())?;
            continue;
        }
        if ready < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(());
        }
        if fds[0].revents & (libc::POLLIN | libc::POLLHUP) != 0 {
            let Some(bytes) = read_frame(&mut control, timeout)? else {
                return Ok(());
            };
            let request: ShutdownRequestV1 = serde_json::from_slice(&bytes).map_err(|_| ())?;
            if request.version != 1 || request.operation != "shutdown" {
                return Err(());
            }
            write_control(&mut control, "shutdown", timeout)?;
            return Ok(());
        }
        if fds[1].revents & libc::POLLIN != 0 {
            verifier.check_bound(&bound).map_err(|_| ())?;
            serve_one_data_frame(
                ChannelKind::CoreInternal,
                &mut internal,
                &mut backend,
                timeout,
            )?;
            verifier.check_bound(&bound).map_err(|_| ())?;
        } else if fds[1].revents & libc::POLLHUP != 0 {
            return Ok(());
        }
        if fds
            .iter()
            .any(|fd| fd.revents & (libc::POLLERR | libc::POLLNVAL) != 0)
        {
            return Err(());
        }
    }
}

fn write_control(
    stream: &mut UnixStream,
    status: &'static str,
    timeout: Duration,
) -> Result<(), ()> {
    let bytes = serde_json::to_vec(&ControlResponse { status }).map_err(|_| ())?;
    write_frame(stream, &bytes, timeout)
}

fn serve_one_data_frame(
    channel: ChannelKind,
    stream: &mut UnixStream,
    backend: &mut impl CredentialBackend,
    timeout: Duration,
) -> Result<(), ()> {
    let request = Zeroizing::new(read_frame(stream, timeout)?.ok_or(())?);
    let response =
        Zeroizing::new(serde_json::to_vec(&dispatch(channel, backend, &request)).map_err(|_| ())?);
    write_frame(stream, &response, timeout)
}

fn read_frame(stream: &mut UnixStream, timeout: Duration) -> Result<Option<Vec<u8>>, ()> {
    let mut header = [0u8; 4];
    stream.set_read_timeout(Some(timeout)).map_err(|_| ())?;
    match stream.read(&mut header[..1]) {
        Ok(0) => return Ok(None),
        Ok(1) => {}
        Ok(_) => unreachable!(),
        Err(_) => return Err(()),
    }
    let deadline = Instant::now() + timeout;
    read_exact_until(stream, &mut header[1..], deadline)?;
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > MAX_FRAME {
        return Err(());
    }
    let mut body = vec![0; size];
    read_exact_until(stream, &mut body, deadline)?;
    Ok(Some(body))
}

fn write_frame(stream: &mut UnixStream, bytes: &[u8], timeout: Duration) -> Result<(), ()> {
    if bytes.is_empty() || bytes.len() > MAX_FRAME {
        return Err(());
    }
    let deadline = Instant::now() + timeout;
    write_all_until(stream, &(bytes.len() as u32).to_be_bytes(), deadline)?;
    write_all_until(stream, bytes, deadline)
}

fn read_exact_until(
    stream: &mut UnixStream,
    mut output: &mut [u8],
    deadline: Instant,
) -> Result<(), ()> {
    while !output.is_empty() {
        let remaining = deadline.checked_duration_since(Instant::now()).ok_or(())?;
        stream.set_read_timeout(Some(remaining)).map_err(|_| ())?;
        match stream.read(output) {
            Ok(0) => return Err(()),
            Ok(size) => output = &mut output[size..],
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(()),
        }
    }
    Ok(())
}

fn write_all_until(stream: &mut UnixStream, mut input: &[u8], deadline: Instant) -> Result<(), ()> {
    while !input.is_empty() {
        let remaining = deadline.checked_duration_since(Instant::now()).ok_or(())?;
        stream.set_write_timeout(Some(remaining)).map_err(|_| ())?;
        match stream.write(input) {
            Ok(0) => return Err(()),
            Ok(size) => input = &input[size..],
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(()),
        }
    }
    Ok(())
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseAuthorizationV1 {
    protocol_version: u8,
    build_id: String,
    broker_sha256: String,
    core_sha256: String,
    engine_sha256: String,
}

impl ReleaseAuthorizationV1 {
    pub fn parse(bytes: &[u8]) -> Result<Self, &'static str> {
        if bytes.is_empty() || bytes.len() > MAX_AUTHORITY_OUTPUT {
            return Err("authority_output_invalid");
        }
        let value: Self = serde_json::from_slice(bytes).map_err(|_| "authority_output_invalid")?;
        if value.protocol_version != 1
            || value.build_id.is_empty()
            || value.build_id.len() > 128
            || !value
                .build_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
            || ![
                &value.broker_sha256,
                &value.core_sha256,
                &value.engine_sha256,
            ]
            .into_iter()
            .all(|v| lower_hex(v, 64))
        {
            return Err("authority_output_invalid");
        }
        Ok(value)
    }

    pub fn protocol_version(&self) -> u8 {
        self.protocol_version
    }
    pub fn build_id(&self) -> &str {
        &self.build_id
    }
    pub fn broker_sha256(&self) -> &str {
        &self.broker_sha256
    }
    pub fn core_sha256(&self) -> &str {
        &self.core_sha256
    }
    pub fn engine_sha256(&self) -> &str {
        &self.engine_sha256
    }
}

#[derive(Clone, Debug)]
pub struct BindingEvidence {
    pub broker_sha256: String,
    pub broker_static_unique: Vec<u8>,
    pub broker_live_unique: Vec<u8>,
    pub authority_static_unique: Vec<u8>,
    pub authority_live_unique: Vec<u8>,
    pub core_sha256: String,
    pub core_static_unique: Vec<u8>,
    pub core_live_unique: Vec<u8>,
    pub engine_sha256: String,
    pub engine_static_unique: Vec<u8>,
    pub engine_live_unique: Vec<u8>,
}

pub fn validate_binding_evidence(
    claims: &ReleaseAuthorizationV1,
    evidence: &BindingEvidence,
) -> Result<(), &'static str> {
    let identities = [
        (&evidence.broker_static_unique, &evidence.broker_live_unique),
        (
            &evidence.authority_static_unique,
            &evidence.authority_live_unique,
        ),
        (&evidence.core_static_unique, &evidence.core_live_unique),
        (&evidence.engine_static_unique, &evidence.engine_live_unique),
    ];
    if evidence.broker_sha256 != claims.broker_sha256
        || evidence.core_sha256 != claims.core_sha256
        || evidence.engine_sha256 != claims.engine_sha256
        || identities
            .into_iter()
            .any(|(static_id, live_id)| static_id.is_empty() || static_id != live_id)
    {
        return Err("release_binding_invalid");
    }
    Ok(())
}

#[cfg(target_os = "macos")]
pub struct MacBindingVerifier {
    broker: StableExecutable,
    broker_requirement: security_framework::os::macos::code_signing::SecRequirement,
    broker_static_unique: Vec<u8>,
    broker_live_unique: Vec<u8>,
    authority_requirement: security_framework::os::macos::code_signing::SecRequirement,
    core_requirement: security_framework::os::macos::code_signing::SecRequirement,
    engine_requirement: security_framework::os::macos::code_signing::SecRequirement,
}

#[cfg(target_os = "macos")]
pub struct MacBootstrapState {
    claims: ReleaseAuthorizationV1,
    authority_static_unique: Vec<u8>,
    authority_live_unique: Vec<u8>,
}

#[cfg(target_os = "macos")]
#[derive(Clone, Debug, Eq, PartialEq)]
struct ProcessIdentity {
    pid: i32,
    parent_pid: i32,
    user_id: u32,
    start_seconds: u64,
    start_microseconds: u64,
}

#[cfg(target_os = "macos")]
pub struct MacBoundState {
    parent: ProcessIdentity,
    engine: ProcessIdentity,
    core: StableExecutable,
    engine_file: StableExecutable,
    core_live_unique: Vec<u8>,
    engine_live_unique: Vec<u8>,
}

#[cfg(target_os = "macos")]
impl MacBindingVerifier {
    pub fn new(broker_path: PathBuf, signer_anchor_sha1: String) -> Result<Self, &'static str> {
        if !broker_path.is_absolute() || !lower_hex(&signer_anchor_sha1, 40) {
            return Err("binding_configuration_invalid");
        }
        let broker_requirement =
            role_requirement(&signer_anchor_sha1, "top.gamja.slack-credential-broker.v1")?;
        let authority_requirement = role_requirement(
            &signer_anchor_sha1,
            "top.gamja.slack-credential-release-authority.v1",
        )?;
        let core_requirement = role_requirement(&signer_anchor_sha1, "top.gamja.credential-core")?;
        let engine_requirement = role_requirement(&signer_anchor_sha1, "top.gamja.engine")?;
        let broker = StableExecutable::open(&broker_path)?;
        let broker_static = static_code_for(&broker, &broker_requirement)?;
        let broker_live = self_code_for(&broker, &broker_requirement)?;
        let broker_static_unique =
            code_unique_identity(broker_static.as_concrete_TypeRef() as *const libc::c_void)?;
        let broker_live_unique =
            code_unique_identity(broker_live.as_concrete_TypeRef() as *const libc::c_void)?;
        if broker_static_unique != broker_live_unique {
            return Err("broker_live_identity_invalid");
        }
        broker.revalidate_path()?;
        Ok(Self {
            broker,
            broker_requirement,
            broker_static_unique,
            broker_live_unique,
            authority_requirement,
            core_requirement,
            engine_requirement,
        })
    }

    fn revalidate_self(&self) -> Result<(), &'static str> {
        self.broker.revalidate_path()?;
        let live = self_code_for(&self.broker, &self.broker_requirement)?;
        let unique = code_unique_identity(live.as_concrete_TypeRef() as *const libc::c_void)?;
        if unique != self.broker_live_unique || unique != self.broker_static_unique {
            return Err("broker_live_identity_invalid");
        }
        Ok(())
    }
}

#[cfg(target_os = "macos")]
fn role_requirement(
    signer_anchor_sha1: &str,
    identifier: &'static str,
) -> Result<security_framework::os::macos::code_signing::SecRequirement, &'static str> {
    if !lower_hex(signer_anchor_sha1, 40)
        || !matches!(
            identifier,
            "top.gamja.slack-credential-broker.v1"
                | "top.gamja.slack-credential-release-authority.v1"
                | "top.gamja.credential-core"
                | "top.gamja.engine"
        )
    {
        return Err("code_requirement_invalid");
    }
    parse_requirement(&format!(
        "anchor = H\"{signer_anchor_sha1}\" and identifier \"{identifier}\""
    ))
}

#[cfg(target_os = "macos")]
impl BindingVerifier for MacBindingVerifier {
    type Bootstrap = MacBootstrapState;
    type Bound = MacBoundState;

    fn bootstrap(&mut self, request: &BootstrapRequestV1) -> Result<Self::Bootstrap, &'static str> {
        let authority = StableExecutable::open(Path::new(request.authority_path()))?;
        if authority.sha256() != request.authority_sha256() {
            return Err("authority_hash_mismatch");
        }
        self.revalidate_self()?;
        let authority_static = static_code_for(&authority, &self.authority_requirement)?;
        let authority_static_unique =
            code_unique_identity(authority_static.as_concrete_TypeRef() as *const libc::c_void)?;
        let (authority_live_unique, bytes) = spawn_and_verify_authority(
            &authority,
            &self.authority_requirement,
            Duration::from_secs(2),
        )?;
        if authority_static_unique != authority_live_unique {
            return Err("authority_live_identity_invalid");
        }
        authority.revalidate_path()?;
        let claims = ReleaseAuthorizationV1::parse(&bytes)?;
        if self.broker.sha256() != claims.broker_sha256() {
            return Err("broker_hash_mismatch");
        }
        self.revalidate_self()?;
        Ok(MacBootstrapState {
            claims,
            authority_static_unique,
            authority_live_unique,
        })
    }

    fn bind(
        &mut self,
        bootstrap: &Self::Bootstrap,
        request: &BindRequestV1,
    ) -> Result<Self::Bound, &'static str> {
        if request.parent_pid() != unsafe { libc::getppid() } {
            return Err("parent_pid_invalid");
        }
        let core = StableExecutable::open(Path::new(request.parent_path()))?;
        let engine = StableExecutable::open(Path::new(request.engine_path()))?;
        if core.sha256() != bootstrap.claims.core_sha256()
            || engine.sha256() != bootstrap.claims.engine_sha256()
        {
            return Err("release_hash_mismatch");
        }
        let parent_identity = process_identity(request.parent_pid())?;
        let engine_identity = process_identity(request.engine_pid())?;
        if parent_identity.user_id != unsafe { libc::geteuid() }
            || engine_identity.user_id != parent_identity.user_id
            || engine_identity.parent_pid != parent_identity.pid
        {
            return Err("engine_ownership_invalid");
        }
        let core_static = static_code_for(&core, &self.core_requirement)?;
        let engine_static = static_code_for(&engine, &self.engine_requirement)?;
        let core_live = live_code_for(request.parent_pid(), &self.core_requirement)?;
        let engine_live = live_code_for(request.engine_pid(), &self.engine_requirement)?;
        live_path_matches(&core_live, &core)?;
        live_path_matches(&engine_live, &engine)?;
        let evidence = BindingEvidence {
            broker_sha256: bootstrap.claims.broker_sha256().into(),
            broker_static_unique: self.broker_static_unique.clone(),
            broker_live_unique: self.broker_live_unique.clone(),
            authority_static_unique: bootstrap.authority_static_unique.clone(),
            authority_live_unique: bootstrap.authority_live_unique.clone(),
            core_sha256: core.sha256().into(),
            core_static_unique: code_unique_identity(
                core_static.as_concrete_TypeRef() as *const libc::c_void
            )?,
            core_live_unique: code_unique_identity(
                core_live.as_concrete_TypeRef() as *const libc::c_void
            )?,
            engine_sha256: engine.sha256().into(),
            engine_static_unique: code_unique_identity(
                engine_static.as_concrete_TypeRef() as *const libc::c_void
            )?,
            engine_live_unique: code_unique_identity(
                engine_live.as_concrete_TypeRef() as *const libc::c_void
            )?,
        };
        validate_binding_evidence(&bootstrap.claims, &evidence)?;
        core.revalidate_path()?;
        engine.revalidate_path()?;
        Ok(MacBoundState {
            parent: parent_identity,
            engine: engine_identity,
            core,
            engine_file: engine,
            core_live_unique: evidence.core_live_unique,
            engine_live_unique: evidence.engine_live_unique,
        })
    }

    fn check_bound(&mut self, bound: &Self::Bound) -> Result<(), &'static str> {
        self.revalidate_self()?;
        let current_core = process_identity(bound.parent.pid)?;
        let current_engine = process_identity(bound.engine.pid)?;
        let broker_current_ppid = unsafe { libc::getppid() };
        if !bound_processes_match(
            &current_core,
            &bound.parent,
            &current_engine,
            &bound.engine,
            broker_current_ppid,
        ) {
            return Err("bound_process_changed");
        }
        bound.core.revalidate_path()?;
        bound.engine_file.revalidate_path()?;
        let core_live = live_code_for(bound.parent.pid, &self.core_requirement)?;
        let engine_live = live_code_for(bound.engine.pid, &self.engine_requirement)?;
        live_path_matches(&core_live, &bound.core)?;
        live_path_matches(&engine_live, &bound.engine_file)?;
        if code_unique_identity(core_live.as_concrete_TypeRef() as *const libc::c_void)?
            != bound.core_live_unique
            || code_unique_identity(engine_live.as_concrete_TypeRef() as *const libc::c_void)?
                != bound.engine_live_unique
        {
            return Err("bound_process_changed");
        }
        Ok(())
    }
}

#[cfg(target_os = "macos")]
use core_foundation::base::TCFType;

#[cfg(target_os = "macos")]
fn parse_requirement(
    value: &str,
) -> Result<security_framework::os::macos::code_signing::SecRequirement, &'static str> {
    value.parse().map_err(|_| "code_requirement_invalid")
}

#[cfg(target_os = "macos")]
fn validation_flags() -> security_framework::os::macos::code_signing::Flags {
    use security_framework::os::macos::code_signing::Flags;
    Flags::STRICT_VALIDATE | Flags::NO_NETWORK_ACCESS
}

#[cfg(target_os = "macos")]
fn static_code_for(
    executable: &StableExecutable,
    requirement: &security_framework::os::macos::code_signing::SecRequirement,
) -> Result<security_framework::os::macos::code_signing::SecStaticCode, &'static str> {
    use core_foundation::url::CFURL;
    use security_framework::os::macos::code_signing::{Flags, SecStaticCode};
    let url = CFURL::from_path(executable.path(), false).ok_or("static_code_invalid")?;
    let code = SecStaticCode::from_path(&url, Flags::NONE).map_err(|_| "static_code_invalid")?;
    code.check_validity(validation_flags(), requirement)
        .map_err(|_| "static_code_invalid")?;
    executable.revalidate_path()?;
    Ok(code)
}

#[cfg(target_os = "macos")]
fn live_code_for(
    pid: i32,
    requirement: &security_framework::os::macos::code_signing::SecRequirement,
) -> Result<security_framework::os::macos::code_signing::SecCode, &'static str> {
    use security_framework::os::macos::code_signing::{Flags, GuestAttributes, SecCode};
    if pid <= 1 {
        return Err("live_code_invalid");
    }
    let mut attributes = GuestAttributes::new();
    attributes.set_pid(pid);
    let code = SecCode::copy_guest_with_attribues(None, &attributes, Flags::NONE)
        .map_err(|_| "live_code_invalid")?;
    code.check_validity(validation_flags(), requirement)
        .map_err(|_| "live_code_invalid")?;
    Ok(code)
}

#[cfg(target_os = "macos")]
fn self_code_for(
    executable: &StableExecutable,
    requirement: &security_framework::os::macos::code_signing::SecRequirement,
) -> Result<security_framework::os::macos::code_signing::SecCode, &'static str> {
    use security_framework::os::macos::code_signing::{Flags, SecCode};
    let code = SecCode::for_self(Flags::NONE).map_err(|_| "broker_live_identity_invalid")?;
    code.check_validity(validation_flags(), requirement)
        .map_err(|_| "broker_live_identity_invalid")?;
    live_path_matches(&code, executable)?;
    Ok(code)
}

#[cfg(target_os = "macos")]
fn process_identity(pid: i32) -> Result<ProcessIdentity, &'static str> {
    if pid <= 1 {
        return Err("process_identity_invalid");
    }
    let mut info = unsafe { std::mem::zeroed::<libc::proc_bsdinfo>() };
    let expected = std::mem::size_of::<libc::proc_bsdinfo>();
    let received = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            &mut info as *mut _ as *mut libc::c_void,
            expected as libc::c_int,
        )
    };
    if received != expected as libc::c_int || info.pbi_pid != pid as u32 {
        return Err("process_identity_invalid");
    }
    Ok(ProcessIdentity {
        pid,
        parent_pid: i32::try_from(info.pbi_ppid).map_err(|_| "process_identity_invalid")?,
        user_id: info.pbi_uid,
        start_seconds: info.pbi_start_tvsec,
        start_microseconds: info.pbi_start_tvusec,
    })
}

/// Checks if process identities match for broker lifetime verification.
/// Core lifetime excludes parent_pid (grandparent transitions allowed).
/// Engine identity is full ProcessIdentity equality (parent_pid included).
/// Broker's current PPID must equal Core's current PID.
#[cfg(target_os = "macos")]
fn bound_processes_match(
    current_core: &ProcessIdentity,
    recorded_core: &ProcessIdentity,
    current_engine: &ProcessIdentity,
    recorded_engine: &ProcessIdentity,
    broker_current_ppid: i32,
) -> bool {
    // Core lifetime: PID + UID + start_seconds + start_microseconds (excluding parent_pid)
    let core_lifetime_matches = current_core.pid == recorded_core.pid
        && current_core.user_id == recorded_core.user_id
        && current_core.start_seconds == recorded_core.start_seconds
        && current_core.start_microseconds == recorded_core.start_microseconds;

    // Engine identity: full ProcessIdentity equality (including parent_pid)
    let engine_identity_matches = current_engine == recorded_engine;

    // Ownership: engine parent must be core PID
    let engine_ownership_valid = current_engine.parent_pid == current_core.pid;

    // Broker PPID must equal Core PID
    let broker_parent_matches = broker_current_ppid == current_core.pid;

    core_lifetime_matches
        && engine_identity_matches
        && engine_ownership_valid
        && broker_parent_matches
}

#[cfg(target_os = "macos")]
fn live_path_matches(
    code: &security_framework::os::macos::code_signing::SecCode,
    executable: &StableExecutable,
) -> Result<(), &'static str> {
    use security_framework::os::macos::code_signing::Flags;
    let path = code
        .path(Flags::NONE)
        .map_err(|_| "live_code_path_invalid")?
        .to_path()
        .ok_or("live_code_path_invalid")?;
    let metadata = std::fs::symlink_metadata(path).map_err(|_| "live_code_path_invalid")?;
    if metadata.dev() != executable.device || metadata.ino() != executable.inode {
        return Err("live_code_path_invalid");
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn code_unique_identity(code: *const libc::c_void) -> Result<Vec<u8>, &'static str> {
    use core_foundation::base::{CFGetTypeID, CFRelease, CFTypeRef};
    use core_foundation::data::{CFDataGetBytePtr, CFDataGetLength, CFDataGetTypeID};
    use core_foundation::dictionary::{CFDictionaryGetValue, CFDictionaryRef};
    extern "C" {
        static kSecCodeInfoUnique: CFTypeRef;
        fn SecCodeCopySigningInformation(
            code: *const libc::c_void,
            flags: u32,
            information: *mut CFDictionaryRef,
        ) -> i32;
    }
    const SEC_CS_SIGNING_INFORMATION: u32 = 1 << 1;
    unsafe {
        let mut information = std::ptr::null();
        if SecCodeCopySigningInformation(code, SEC_CS_SIGNING_INFORMATION, &mut information) != 0
            || information.is_null()
        {
            return Err("code_identity_invalid");
        }
        struct Release(CFTypeRef);
        impl Drop for Release {
            fn drop(&mut self) {
                unsafe { CFRelease(self.0) }
            }
        }
        let _release = Release(information as CFTypeRef);
        let value = CFDictionaryGetValue(information, kSecCodeInfoUnique);
        if value.is_null() || CFGetTypeID(value) != CFDataGetTypeID() {
            return Err("code_identity_invalid");
        }
        let size = CFDataGetLength(value as _);
        if !(16..=128).contains(&size) {
            return Err("code_identity_invalid");
        }
        let bytes = CFDataGetBytePtr(value as _);
        if bytes.is_null() {
            return Err("code_identity_invalid");
        }
        Ok(std::slice::from_raw_parts(bytes, size as usize).to_vec())
    }
}

#[cfg(target_os = "macos")]
struct OwnedChild {
    pid: Option<i32>,
}

#[cfg(target_os = "macos")]
impl OwnedChild {
    fn new(pid: i32) -> Self {
        Self { pid: Some(pid) }
    }
    fn pid(&self) -> Result<i32, &'static str> {
        self.pid.ok_or("authority_failed")
    }

    fn reap_until(&mut self, deadline: Instant) -> Result<i32, &'static str> {
        let pid = self.pid()?;
        let mut status = 0;
        loop {
            let result = unsafe { libc::waitpid(pid, &mut status, libc::WNOHANG) };
            if result == pid {
                self.pid = None;
                return Ok(status);
            }
            if result < 0 {
                return Err("authority_failed");
            }
            if Instant::now() >= deadline {
                return Err("authority_timeout");
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    }
}

#[cfg(target_os = "macos")]
impl Drop for OwnedChild {
    fn drop(&mut self) {
        if let Some(pid) = self.pid.take() {
            unsafe {
                libc::kill(pid, libc::SIGKILL);
                let mut status = 0;
                while libc::waitpid(pid, &mut status, 0) < 0
                    && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted
                {
                }
            }
        }
    }
}

#[cfg(target_os = "macos")]
struct OwnedFd(RawFd);

#[cfg(target_os = "macos")]
impl Drop for OwnedFd {
    fn drop(&mut self) {
        unsafe {
            libc::close(self.0);
        }
    }
}

#[cfg(target_os = "macos")]
fn pipe_cloexec() -> Result<[RawFd; 2], &'static str> {
    let mut pipe = [-1; 2];
    if unsafe { libc::pipe(pipe.as_mut_ptr()) } != 0 {
        return Err("authority_spawn_failed");
    }
    for fd in pipe {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } != 0 {
            unsafe {
                libc::close(pipe[0]);
                libc::close(pipe[1]);
            }
            return Err("authority_spawn_failed");
        }
    }
    Ok(pipe)
}

#[cfg(target_os = "macos")]
fn drain_authority_output(
    stdout: RawFd,
    stderr: RawFd,
    child: &mut OwnedChild,
    deadline: Instant,
) -> Result<Vec<u8>, &'static str> {
    for fd in [stdout, stderr] {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } != 0 {
            return Err("authority_output_invalid");
        }
    }
    let mut output = Vec::new();
    let mut stderr_bytes = 0usize;
    let mut open = [true, true];
    while open[0] || open[1] {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("authority_timeout")?;
        let mut pollfds = [
            libc::pollfd {
                fd: stdout,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: stderr,
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        let ready = unsafe {
            libc::poll(
                pollfds.as_mut_ptr(),
                pollfds.len() as _,
                i32::try_from(remaining.as_millis().max(1)).unwrap_or(i32::MAX),
            )
        };
        if ready == 0 {
            return Err("authority_timeout");
        }
        if ready < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err("authority_output_invalid");
        }
        for index in 0..2 {
            if !open[index] {
                continue;
            }
            if pollfds[index].revents & (libc::POLLERR | libc::POLLNVAL) != 0 {
                return Err("authority_output_invalid");
            }
            if pollfds[index].revents & (libc::POLLIN | libc::POLLHUP) == 0 {
                continue;
            }
            loop {
                let mut chunk = [0u8; 1024];
                let count =
                    unsafe { libc::read(pollfds[index].fd, chunk.as_mut_ptr() as _, chunk.len()) };
                if count == 0 {
                    open[index] = false;
                    break;
                }
                if count < 0 {
                    let kind = std::io::Error::last_os_error().kind();
                    if kind == std::io::ErrorKind::Interrupted {
                        continue;
                    }
                    if kind == std::io::ErrorKind::WouldBlock {
                        break;
                    }
                    return Err("authority_output_invalid");
                }
                if index == 0 {
                    output.extend_from_slice(&chunk[..count as usize]);
                    if output.len() > MAX_AUTHORITY_OUTPUT {
                        return Err("authority_output_invalid");
                    }
                } else {
                    stderr_bytes = stderr_bytes.saturating_add(count as usize);
                    if stderr_bytes > MAX_AUTHORITY_OUTPUT {
                        return Err("authority_stderr_nonempty");
                    }
                }
            }
        }
    }
    let status = child.reap_until(deadline)?;
    if !libc::WIFEXITED(status) || libc::WEXITSTATUS(status) != 0 {
        return Err("authority_failed");
    }
    if stderr_bytes != 0 {
        return Err("authority_stderr_nonempty");
    }
    Ok(output)
}

#[cfg(target_os = "macos")]
fn spawn_and_verify_authority(
    authority: &StableExecutable,
    requirement: &security_framework::os::macos::code_signing::SecRequirement,
    timeout: Duration,
) -> Result<(Vec<u8>, Vec<u8>), &'static str> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let path = CString::new(authority.path().as_os_str().as_bytes())
        .map_err(|_| "authority_spawn_failed")?;
    let argv = [path.as_ptr() as *mut libc::c_char, std::ptr::null_mut()];
    let envp = [std::ptr::null_mut()];
    let stdout_pipe = pipe_cloexec()?;
    let stdout_read = OwnedFd(stdout_pipe[0]);
    let stdout_write = OwnedFd(stdout_pipe[1]);
    let stderr_pipe = pipe_cloexec()?;
    let stderr_read = OwnedFd(stderr_pipe[0]);
    let stderr_write = OwnedFd(stderr_pipe[1]);
    unsafe {
        let mut actions = std::mem::zeroed();
        let mut attrs = std::mem::zeroed();
        if libc::posix_spawn_file_actions_init(&mut actions) != 0
            || libc::posix_spawnattr_init(&mut attrs) != 0
        {
            return Err("authority_spawn_failed");
        }
        struct SpawnCleanup(libc::posix_spawn_file_actions_t, libc::posix_spawnattr_t);
        impl Drop for SpawnCleanup {
            fn drop(&mut self) {
                unsafe {
                    libc::posix_spawn_file_actions_destroy(&mut self.0);
                    libc::posix_spawnattr_destroy(&mut self.1);
                }
            }
        }
        let mut cleanup = SpawnCleanup(actions, attrs);
        let dev_null = b"/dev/null\0";
        if libc::posix_spawn_file_actions_adddup2(&mut cleanup.0, stdout_write.0, 1) != 0
            || libc::posix_spawn_file_actions_adddup2(&mut cleanup.0, stderr_write.0, 2) != 0
            || libc::posix_spawn_file_actions_addopen(
                &mut cleanup.0,
                0,
                dev_null.as_ptr() as _,
                libc::O_RDONLY,
                0,
            ) != 0
        {
            return Err("authority_spawn_failed");
        }
        const CLOEXEC_DEFAULT: libc::c_short = 0x4000;
        const START_SUSPENDED: libc::c_short = 0x0080;
        if libc::posix_spawnattr_setflags(&mut cleanup.1, CLOEXEC_DEFAULT | START_SUSPENDED) != 0 {
            return Err("authority_spawn_failed");
        }
        let mut pid = 0;
        if libc::posix_spawn(
            &mut pid,
            path.as_ptr(),
            &cleanup.0,
            &cleanup.1,
            argv.as_ptr(),
            envp.as_ptr(),
        ) != 0
        {
            return Err("authority_spawn_failed");
        }
        let mut child = OwnedChild::new(pid);
        drop(stdout_write);
        drop(stderr_write);
        let authority_live = live_code_for(pid, requirement)?;
        let authority_live_unique =
            code_unique_identity(authority_live.as_concrete_TypeRef() as *const libc::c_void)?;
        authority.revalidate_path()?;
        if libc::kill(pid, libc::SIGCONT) != 0 {
            return Err("authority_spawn_failed");
        }
        let bytes = drain_authority_output(
            stdout_read.0,
            stderr_read.0,
            &mut child,
            Instant::now() + timeout,
        )?;
        Ok((authority_live_unique, bytes))
    }
}

#[cfg(all(test, target_os = "macos"))]
mod authority_lifecycle_tests {
    use super::*;

    #[test]
    fn reaped_child_is_disarmed_before_exit_status_is_interpreted() {
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            unsafe { libc::_exit(7) }
        }
        let mut child = OwnedChild::new(pid);
        let status = child
            .reap_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(child.pid, None);
        assert!(libc::WIFEXITED(status));
        assert_eq!(libc::WEXITSTATUS(status), 7);
    }

    #[test]
    fn bounded_stderr_is_rejected_after_child_is_reaped_without_exposing_bytes() {
        let stdout_pipe = pipe_cloexec().unwrap();
        let stderr_pipe = pipe_cloexec().unwrap();
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            unsafe {
                libc::close(stdout_pipe[0]);
                libc::close(stderr_pipe[0]);
                libc::close(stdout_pipe[1]);
                let marker = b"synthetic-stderr";
                libc::write(stderr_pipe[1], marker.as_ptr() as _, marker.len());
                libc::close(stderr_pipe[1]);
                libc::_exit(0);
            }
        }
        unsafe {
            libc::close(stdout_pipe[1]);
            libc::close(stderr_pipe[1]);
        }
        let stdout = OwnedFd(stdout_pipe[0]);
        let stderr = OwnedFd(stderr_pipe[0]);
        let mut child = OwnedChild::new(pid);
        assert_eq!(
            drain_authority_output(
                stdout.0,
                stderr.0,
                &mut child,
                Instant::now() + Duration::from_secs(1),
            ),
            Err("authority_stderr_nonempty")
        );
        assert_eq!(child.pid, None);
    }
}

pub struct StableExecutable {
    path: PathBuf,
    file: File,
    device: u64,
    inode: u64,
    owner: u32,
    mode: u32,
    size: u64,
    sha256: String,
}

impl StableExecutable {
    pub fn open(path: &Path) -> Result<Self, &'static str> {
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "public_executable_invalid")?;
        let metadata = file.metadata().map_err(|_| "public_executable_invalid")?;
        let effective_user = unsafe { libc::geteuid() };
        if !metadata.is_file()
            || !(metadata.uid() == effective_user || metadata.uid() == 0)
            || metadata.mode() & 0o022 != 0
            || metadata.mode() & 0o111 == 0
            || metadata.nlink() != 1
            || metadata.len() == 0
        {
            return Err("public_executable_invalid");
        }
        let sha256 = hash_reader(&mut file)?;
        Ok(Self {
            path: path.to_path_buf(),
            file,
            device: metadata.dev(),
            inode: metadata.ino(),
            owner: metadata.uid(),
            mode: metadata.mode(),
            size: metadata.len(),
            sha256,
        })
    }

    pub fn sha256(&self) -> &str {
        &self.sha256
    }
    pub fn inode(&self) -> u64 {
        self.inode
    }
    pub fn raw_fd(&self) -> RawFd {
        self.file.as_raw_fd()
    }
    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn revalidate_path(&self) -> Result<(), &'static str> {
        let metadata =
            std::fs::symlink_metadata(&self.path).map_err(|_| "public_executable_changed")?;
        if metadata.dev() != self.device
            || metadata.ino() != self.inode
            || metadata.uid() != self.owner
            || metadata.mode() != self.mode
            || metadata.len() != self.size
            || !metadata.is_file()
            || metadata.nlink() != 1
        {
            return Err("public_executable_changed");
        }
        let current = self
            .file
            .metadata()
            .map_err(|_| "public_executable_changed")?;
        if current.dev() != self.device || current.ino() != self.inode || current.len() != self.size
        {
            return Err("public_executable_changed");
        }
        Ok(())
    }
}

fn hash_reader(file: &mut File) -> Result<String, &'static str> {
    use std::io::Seek;
    file.rewind().map_err(|_| "public_executable_invalid")?;
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|_| "public_executable_invalid")?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    file.rewind().map_err(|_| "public_executable_invalid")?;
    Ok(hex(digest.finalize().as_slice()))
}

fn hex(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(output, "{byte:02x}");
    }
    output
}

#[derive(Clone, Copy, Debug)]
pub struct AllowlistedFd {
    pub source: RawFd,
    pub target: RawFd,
}

#[cfg(target_os = "macos")]
pub fn spawn_allowlisted(
    path: &Path,
    argv: &[&str],
    mappings: &[AllowlistedFd],
) -> Result<std::process::ExitStatus, &'static str> {
    use std::ffi::CString;
    use std::os::unix::process::ExitStatusExt;
    if !path.is_absolute()
        || argv.is_empty()
        || mappings.iter().any(|m| m.source < 0 || m.target < 3)
    {
        return Err("spawn_invalid");
    }
    let mut targets = std::collections::BTreeSet::new();
    if mappings.iter().any(|m| !targets.insert(m.target)) {
        return Err("spawn_invalid");
    }
    let path = CString::new(path.as_os_str().as_encoded_bytes()).map_err(|_| "spawn_invalid")?;
    let args = argv
        .iter()
        .map(|v| CString::new(*v).map_err(|_| "spawn_invalid"))
        .collect::<Result<Vec<_>, _>>()?;
    let mut pointers = args
        .iter()
        .map(|v| v.as_ptr() as *mut libc::c_char)
        .collect::<Vec<_>>();
    pointers.push(std::ptr::null_mut());
    unsafe {
        let mut actions = std::mem::zeroed();
        let mut attrs = std::mem::zeroed();
        if libc::posix_spawn_file_actions_init(&mut actions) != 0
            || libc::posix_spawnattr_init(&mut attrs) != 0
        {
            return Err("spawn_failed");
        }
        struct Cleanup(libc::posix_spawn_file_actions_t, libc::posix_spawnattr_t);
        impl Drop for Cleanup {
            fn drop(&mut self) {
                unsafe {
                    libc::posix_spawn_file_actions_destroy(&mut self.0);
                    libc::posix_spawnattr_destroy(&mut self.1);
                }
            }
        }
        let mut cleanup = Cleanup(actions, attrs);
        for mapping in mappings {
            if libc::posix_spawn_file_actions_adddup2(
                &mut cleanup.0,
                mapping.source,
                mapping.target,
            ) != 0
            {
                return Err("spawn_failed");
            }
        }
        const POSIX_SPAWN_CLOEXEC_DEFAULT_DARWIN: libc::c_short = 0x4000;
        if libc::posix_spawnattr_setflags(&mut cleanup.1, POSIX_SPAWN_CLOEXEC_DEFAULT_DARWIN) != 0 {
            return Err("spawn_failed");
        }
        let mut pid = 0;
        let status = libc::posix_spawn(
            &mut pid,
            path.as_ptr(),
            &cleanup.0,
            &cleanup.1,
            pointers.as_ptr(),
            [std::ptr::null_mut::<libc::c_char>()].as_ptr(),
        );
        if status != 0 {
            return Err("spawn_failed");
        }
        let mut wait_status = 0;
        loop {
            let result = libc::waitpid(pid, &mut wait_status, 0);
            if result == pid {
                break;
            }
            if result < 0
                && std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted
            {
                return Err("spawn_failed");
            }
        }
        Ok(std::process::ExitStatus::from_raw(wait_status))
    }
}

#[derive(Debug, Serialize)]
pub struct FirstWriteProofReceipt {
    pub version: u8,
    pub status: &'static str,
    pub initial_missing: Option<bool>,
    pub write_succeeded: Option<bool>,
    pub readback_exact: Option<bool>,
    pub delete_succeeded: Option<bool>,
    pub final_missing: Option<bool>,
    pub cleanup_confirmed: bool,
    pub query_count: u8,
    pub write_count: u8,
    pub delete_count: u8,
    pub ui_allowed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure_stage: Option<ProofFailureStage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub operation: Option<CredentialOperation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub os_status: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_class: Option<&'static str>,
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProofFailureStage {
    Random,
    InitialQuery,
    InitialCollision,
    Write,
    Readback,
    Delete,
    FinalQuery,
}

impl FirstWriteProofReceipt {
    pub fn passed(&self) -> bool {
        self.status == "passed"
    }
}

fn proof_receipt(
    status: &'static str,
    initial_missing: Option<bool>,
    write_succeeded: Option<bool>,
    readback_exact: Option<bool>,
    delete_succeeded: Option<bool>,
    final_missing: Option<bool>,
    query_count: u8,
    write_count: u8,
    delete_count: u8,
    failure: Option<(ProofFailureStage, CredentialBackendError)>,
    stage: Option<ProofFailureStage>,
) -> FirstWriteProofReceipt {
    FirstWriteProofReceipt {
        version: 1,
        status,
        initial_missing,
        write_succeeded,
        readback_exact,
        delete_succeeded,
        final_missing,
        cleanup_confirmed: delete_succeeded == Some(true) && final_missing == Some(true),
        query_count,
        write_count,
        delete_count,
        ui_allowed: false,
        failure_stage: failure.map(|value| value.0).or(stage),
        operation: failure.map(|value| value.1.operation),
        os_status: failure.map(|value| value.1.os_status),
        error_class: failure.map(|value| {
            if value.1.interaction_required {
                "interaction_required"
            } else {
                "native_os_status"
            }
        }),
    }
}

pub fn run_isolated_first_write_proof(
    backend: &mut impl CredentialBackend,
) -> FirstWriteProofReceipt {
    let mut random = Zeroizing::new([0u8; 32]);
    if getrandom::fill(random.as_mut()).is_err() {
        return proof_receipt(
            "failed",
            None,
            None,
            None,
            None,
            None,
            0,
            0,
            0,
            None,
            Some(ProofFailureStage::Random),
        );
    }
    let suffix = hex(&random[..16]);
    let service = format!("{PROOF_SERVICE_PREFIX}{suffix}");
    let account = format!("proof:{suffix}");
    let value = Zeroizing::new(format!("broker-proof-{}", hex(&random[16..])));
    let mut query_count = 0;
    query_count += 1;
    let initial_missing = match backend.get(&service, &account) {
        Ok(value) => value.is_none(),
        Err(error) => {
            return proof_receipt(
                if error.interaction_required {
                    "interaction_required"
                } else {
                    "failed"
                },
                None,
                None,
                None,
                None,
                None,
                query_count,
                0,
                0,
                Some((ProofFailureStage::InitialQuery, error)),
                None,
            )
        }
    };
    if !initial_missing {
        return proof_receipt(
            "failed",
            Some(false),
            None,
            None,
            None,
            None,
            query_count,
            0,
            0,
            None,
            Some(ProofFailureStage::InitialCollision),
        );
    }
    let write_count = 1;
    let write_result = backend.put(&service, &account, value.as_bytes());
    let write_succeeded = write_result.is_ok();
    let mut primary_failure = write_result
        .err()
        .map(|error| (ProofFailureStage::Write, error));
    let readback_exact = if write_succeeded {
        query_count += 1;
        match backend.get(&service, &account) {
            Ok(candidate) => candidate.is_some_and(|candidate| candidate == value.as_bytes()),
            Err(error) => {
                primary_failure = Some((ProofFailureStage::Readback, error));
                false
            }
        }
    } else {
        false
    };
    let delete_count = 1;
    let delete_result = backend.delete(&service, &account);
    let delete_succeeded = delete_result.is_ok();
    let cleanup_failure = delete_result
        .err()
        .map(|error| (ProofFailureStage::Delete, error));
    query_count += 1;
    let final_result = backend.get(&service, &account);
    let final_missing = final_result.as_ref().is_ok_and(|value| value.is_none());
    let cleanup_failure = cleanup_failure.or_else(|| {
        final_result
            .err()
            .map(|error| (ProofFailureStage::FinalQuery, error))
    });
    let cleanup_confirmed = delete_succeeded && final_missing;
    let readback_failure =
        (!readback_exact && primary_failure.is_none()).then_some(ProofFailureStage::Readback);
    let status = if !cleanup_confirmed {
        "cleanup_uncertain"
    } else if primary_failure
        .as_ref()
        .is_some_and(|failure| failure.1.interaction_required)
    {
        "interaction_required"
    } else if write_succeeded && readback_exact {
        "passed"
    } else {
        "failed"
    };
    proof_receipt(
        status,
        Some(initial_missing),
        Some(write_succeeded),
        Some(readback_exact),
        Some(delete_succeeded),
        Some(final_missing),
        query_count,
        write_count,
        delete_count,
        cleanup_failure.or(primary_failure),
        readback_failure,
    )
}

#[cfg(all(test, target_os = "macos"))]
mod broker_lifetime_tests {
    use super::*;

    fn make_identity(pid: i32, parent_pid: i32, uid: u32, sec: u64, usec: u64) -> ProcessIdentity {
        ProcessIdentity {
            pid,
            parent_pid,
            user_id: uid,
            start_seconds: sec,
            start_microseconds: usec,
        }
    }

    #[test]
    fn launcher_ppid_transition_to_one_allowed() {
        // Core's grandparent PPID transition (launcher→1) should be allowed
        // Old predicate would fail; new should pass
        let recorded_core = make_identity(100, 50, 1000, 100, 200);
        let current_core = make_identity(100, 1, 1000, 100, 200); // parent_pid changed from 50 to 1
        let recorded_engine = make_identity(200, 100, 1000, 200, 300);
        let current_engine = make_identity(200, 100, 1000, 200, 300);
        let broker_ppid = 100;

        // This should now pass with new Core lifetime check
        assert!(bound_processes_match(
            &current_core,
            &recorded_core,
            &current_engine,
            &recorded_engine,
            broker_ppid,
        ));
    }

    #[test]
    fn core_pid_change_rejected() {
        let recorded_core = make_identity(100, 50, 1000, 100, 200);
        let current_core = make_identity(101, 50, 1000, 100, 200); // PID changed
        let recorded_engine = make_identity(200, 100, 1000, 200, 300);
        let current_engine = make_identity(200, 100, 1000, 200, 300);
        let broker_ppid = 101;

        assert!(!bound_processes_match(
            &current_core,
            &recorded_core,
            &current_engine,
            &recorded_engine,
            broker_ppid,
        ));
    }

    #[test]
    fn core_uid_change_rejected() {
        let recorded_core = make_identity(100, 50, 1000, 100, 200);
        let current_core = make_identity(100, 50, 1001, 100, 200); // UID changed
        let recorded_engine = make_identity(200, 100, 1000, 200, 300);
        let current_engine = make_identity(200, 100, 1000, 200, 300);
        let broker_ppid = 100;

        assert!(!bound_processes_match(
            &current_core,
            &recorded_core,
            &current_engine,
            &recorded_engine,
            broker_ppid,
        ));
    }

    #[test]
    fn core_start_time_change_rejected() {
        let recorded_core = make_identity(100, 50, 1000, 100, 200);
        let current_core = make_identity(100, 50, 1000, 101, 200); // start_seconds changed
        let recorded_engine = make_identity(200, 100, 1000, 200, 300);
        let current_engine = make_identity(200, 100, 1000, 200, 300);
        let broker_ppid = 100;

        assert!(!bound_processes_match(
            &current_core,
            &recorded_core,
            &current_engine,
            &recorded_engine,
            broker_ppid,
        ));
    }

    #[test]
    fn engine_parent_pid_change_rejected() {
        let recorded_core = make_identity(100, 50, 1000, 100, 200);
        let current_core = make_identity(100, 50, 1000, 100, 200);
        let recorded_engine = make_identity(200, 100, 1000, 200, 300);
        let current_engine = make_identity(200, 99, 1000, 200, 300); // parent_pid changed
        let broker_ppid = 100;

        assert!(!bound_processes_match(
            &current_core,
            &recorded_core,
            &current_engine,
            &recorded_engine,
            broker_ppid,
        ));
    }

    #[test]
    fn engine_pid_change_rejected() {
        let recorded_core = make_identity(100, 50, 1000, 100, 200);
        let current_core = make_identity(100, 50, 1000, 100, 200);
        let recorded_engine = make_identity(200, 100, 1000, 200, 300);
        let current_engine = make_identity(201, 100, 1000, 200, 300); // PID changed
        let broker_ppid = 100;

        assert!(!bound_processes_match(
            &current_core,
            &recorded_core,
            &current_engine,
            &recorded_engine,
            broker_ppid,
        ));
    }

    #[test]
    fn broker_ppid_wrong_rejected() {
        let recorded_core = make_identity(100, 50, 1000, 100, 200);
        let current_core = make_identity(100, 50, 1000, 100, 200);
        let recorded_engine = make_identity(200, 100, 1000, 200, 300);
        let current_engine = make_identity(200, 100, 1000, 200, 300);
        let broker_ppid = 99; // Wrong PPID

        assert!(!bound_processes_match(
            &current_core,
            &recorded_core,
            &current_engine,
            &recorded_engine,
            broker_ppid,
        ));
    }

    #[test]
    fn unchanged_process_passes() {
        let recorded_core = make_identity(100, 50, 1000, 100, 200);
        let current_core = make_identity(100, 50, 1000, 100, 200); // Unchanged
        let recorded_engine = make_identity(200, 100, 1000, 200, 300);
        let current_engine = make_identity(200, 100, 1000, 200, 300); // Unchanged
        let broker_ppid = 100;

        assert!(bound_processes_match(
            &current_core,
            &recorded_core,
            &current_engine,
            &recorded_engine,
            broker_ppid,
        ));
    }
}

