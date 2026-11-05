//! Credential-only inherited channel. The Python engine has no Keychain access.

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
#[cfg(target_os = "macos")]
use std::path::Path;
use std::time::{Duration, Instant};

pub(crate) const MAX_FRAME: usize = 65_536;
pub(crate) const MAX_VALUE: usize = 32_768;
pub const SLACK_SERVICE: &str = "cloud.gamja.credential-core.v1.slack";
pub const MCP_SERVICE: &str = "cloud.gamja.credential-core.v1.mcp";
pub const PROVIDER_SERVICE: &str = "cloud.gamja.credential-core.v1.provider";
pub const TEMPLATE_PUBLISHER_SERVICE: &str = "cloud.gamja.credential-core.v1.template-publisher";
pub const RECOVERY_SERVICE: &str = "cloud.gamja.credential-core.v1.provider-recovery";
pub const PREPARED_SERVICE: &str = "cloud.gamja.credential-core.v1.provider-prepared";
pub const COMPLETION_SERVICE: &str = "cloud.gamja.credential-core.v1.provider-completion";
const LOGIN_SERVICE: &str = "cloud.gamja.credential-core.v1.remembered-login";

/// Library load constraints are enforced for third-party apps starting with
/// macOS 14. Keep this parser pure so the fail-closed boundary is testable on
/// every build host.
pub fn library_constraints_supported_version(version: &str) -> bool {
    let mut components = version.split('.');
    let Some(major) = components.next() else {
        return false;
    };
    if major.is_empty() || !major.bytes().all(|byte| byte.is_ascii_digit()) {
        return false;
    }
    if components.clone().any(|component| {
        component.is_empty() || !component.bytes().all(|byte| byte.is_ascii_digit())
    }) {
        return false;
    }
    major.parse::<u32>().is_ok_and(|value| value >= 14)
}

#[cfg(target_os = "macos")]
pub fn require_library_constraints_os() -> crate::Result<()> {
    const NAME: &[u8] = b"kern.osproductversion\0";
    let mut value = [0u8; 32];
    let mut size = value.len();
    let status = unsafe {
        libc::sysctlbyname(
            NAME.as_ptr().cast(),
            value.as_mut_ptr().cast(),
            &mut size,
            std::ptr::null_mut(),
            0,
        )
    };
    if status != 0 || size == 0 || size > value.len() {
        return Err("engine_requires_macos_14");
    }
    let bytes = value[..size].strip_suffix(&[0]).unwrap_or(&value[..size]);
    let version = std::str::from_utf8(bytes).map_err(|_| "engine_requires_macos_14")?;
    if library_constraints_supported_version(version) {
        Ok(())
    } else {
        Err("engine_requires_macos_14")
    }
}

#[cfg(target_os = "macos")]
pub fn verify_engine(path: &Path, expected: &str) -> crate::Result<()> {
    use sha2::{Digest, Sha256};
    use std::fs::OpenOptions;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

    if expected.len() != 64 || !expected.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("credential_engine_untrusted");
    }
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .map_err(|_| "credential_engine_untrusted")?;
    let metadata = file.metadata().map_err(|_| "credential_engine_untrusted")?;
    if !trusted_engine_metadata(
        metadata.is_file(),
        metadata.uid(),
        unsafe { libc::geteuid() },
        metadata.mode(),
        metadata.nlink(),
    ) {
        return Err("credential_engine_untrusted");
    }
    let mut digest = Sha256::new();
    let mut buffer = [0; 64 * 1024];
    loop {
        let size = file
            .read(&mut buffer)
            .map_err(|_| "credential_engine_untrusted")?;
        if size == 0 {
            break;
        }
        digest.update(&buffer[..size]);
    }
    let actual = digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if actual != expected.to_ascii_lowercase() {
        return Err("credential_engine_untrusted");
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn trusted_engine_metadata(
    is_file: bool,
    owner: u32,
    effective_user: u32,
    mode: u32,
    links: u64,
) -> bool {
    is_file && (owner == effective_user || owner == 0) && mode & 0o022 == 0 && links == 1
}

#[cfg(target_os = "macos")]
pub fn verify_live_engine(pid: u32, requirement: &str) -> crate::Result<()> {
    use security_framework::os::macos::code_signing::{
        Flags, GuestAttributes, SecCode, SecRequirement,
    };
    use std::str::FromStr;

    let pid = i32::try_from(pid).map_err(|_| "credential_engine_untrusted")?;
    let expression = requirement
        .strip_prefix("designated => ")
        .unwrap_or(requirement);
    let requirement =
        SecRequirement::from_str(expression).map_err(|_| "credential_engine_untrusted")?;
    let mut attributes = GuestAttributes::new();
    attributes.set_pid(pid);
    let code = SecCode::copy_guest_with_attribues(None, &attributes, Flags::NONE)
        .map_err(|_| "credential_engine_untrusted")?;
    code.check_validity(Flags::NONE, &requirement)
        .map_err(|_| "credential_engine_untrusted")
}

#[cfg(target_os = "macos")]
pub fn inherit_descriptor_in_child(
    command: &mut std::process::Command,
    descriptor: std::os::fd::RawFd,
) -> crate::Result<()> {
    use std::os::unix::process::CommandExt;

    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    if flags < 0 || flags & libc::FD_CLOEXEC == 0 {
        return Err("credential_channel_unavailable");
    }
    // fcntl is async-signal-safe. The mutation occurs after fork in the one
    // intended child, never in the multithreaded parent.
    unsafe {
        command.pre_exec(move || {
            let child_flags = libc::fcntl(descriptor, libc::F_GETFD);
            if child_flags < 0
                || libc::fcntl(descriptor, libc::F_SETFD, child_flags & !libc::FD_CLOEXEC) < 0
            {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
pub struct CredentialBackendError {
    interaction_required: bool,
    operation: Option<CredentialOperation>,
    os_status: Option<i32>,
    error_class: CredentialFailureClass,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CredentialFailureClass {
    BridgeTransport,
    BridgeProtocol,
    RequestValidation,
    StoreUnavailable,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
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
            operation: Some(operation),
            os_status: Some(os_status),
            error_class: CredentialFailureClass::StoreUnavailable, // Will be ignored
        }
    }

    pub fn failed(operation: CredentialOperation, os_status: i32) -> Self {
        Self {
            interaction_required: false,
            operation: Some(operation),
            os_status: Some(os_status),
            error_class: CredentialFailureClass::StoreUnavailable, // Will be ignored
        }
    }

    pub fn unavailable(error_class: CredentialFailureClass) -> Self {
        Self {
            interaction_required: false,
            operation: None,
            os_status: None,
            error_class,
        }
    }

    pub fn error_class(&self) -> &'static str {
        if self.interaction_required {
            "interaction_required"
        } else if self.os_status.is_some() && self.operation.is_some() {
            "native_os_status"
        } else {
            match self.error_class {
                CredentialFailureClass::BridgeTransport => "bridge_transport",
                CredentialFailureClass::BridgeProtocol => "bridge_protocol",
                CredentialFailureClass::RequestValidation => "request_validation",
                CredentialFailureClass::StoreUnavailable => "store_unavailable",
            }
        }
    }

    pub fn os_status(&self) -> Option<i32> {
        self.os_status
    }

    pub fn operation(&self) -> Option<CredentialOperation> {
        self.operation
    }

    fn from_os_status(operation: CredentialOperation, os_status: i32) -> Self {
        // errSecNotAvailable means the file Keychain is unavailable, commonly
        // because it is locked. Both cases require an in-app human recovery.
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
        // This process is a headless credential boundary. It never permits
        // Keychain Services to display UI; the product recovers in-app.
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
struct Request {
    version: u8,
    operation: Operation,
    namespace: Namespace,
    reference: String,
    value: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Operation {
    Get,
    Put,
    Delete,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Namespace {
    Provider,
    RememberedLogin,
    TemplatePublisher,
}

impl Namespace {
    fn service(&self) -> &'static str {
        match self {
            Self::Provider => PROVIDER_SERVICE,
            Self::RememberedLogin => LOGIN_SERVICE,
            Self::TemplatePublisher => TEMPLATE_PUBLISHER_SERVICE,
        }
    }

    fn accepts_reference(&self, reference: &str) -> bool {
        match self {
            Self::RememberedLogin => reference == "current" || lower_hex(reference, 32),
            Self::TemplatePublisher => {
                let mut fields = reference.split(':');
                fields.next() == Some("template-publisher")
                    && decimal_identifier(fields.next().unwrap_or_default())
                    && decimal_identifier(fields.next().unwrap_or_default())
                    && lower_hex(fields.next().unwrap_or_default(), 32)
                    && fields.next().is_none()
            }
            Self::Provider => {
                let mut fields = reference.split(':');
                let owner = fields.next().unwrap_or_default();
                let connection = fields.next().unwrap_or_default();
                let opaque = fields.next().unwrap_or_default();
                fields.next().is_none()
                    && decimal_identifier(owner)
                    && decimal_identifier(connection)
                    && (lower_hex(opaque, 32) || legacy_uuid(opaque))
            }
        }
    }
}

fn decimal_identifier(value: &str) -> bool {
    !value.is_empty() && !value.starts_with('0') && value.bytes().all(|byte| byte.is_ascii_digit())
}

fn lower_hex(value: &str, size: usize) -> bool {
    value.len() == size
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn legacy_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte),
        })
}

#[derive(Serialize)]
struct Response {
    status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    os_status: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    operation: Option<CredentialOperation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error_class: Option<&'static str>,
}

impl Response {
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
        // For bridge errors (no os_status/operation), emit error_class only
        if error.os_status.is_none() && error.operation.is_none() {
            Self {
                status: "failed",
                value: None,
                os_status: None,
                operation: None,
                error_class: Some(match error.error_class {
                    CredentialFailureClass::BridgeTransport => "bridge_transport",
                    CredentialFailureClass::BridgeProtocol => "bridge_protocol",
                    CredentialFailureClass::RequestValidation => "request_validation",
                    CredentialFailureClass::StoreUnavailable => "store_unavailable",
                }),
            }
        } else {
            // For native errors (with os_status/operation), emit all fields
            Self {
                status: if error.interaction_required {
                    "interaction_required"
                } else {
                    "failed"
                },
                value: None,
                os_status: error.os_status,
                operation: error.operation,
                error_class: Some(if error.interaction_required {
                    "interaction_required"
                } else {
                    "native_os_status"
                }),
            }
        }
    }
}

fn dispatch(backend: &mut impl CredentialBackend, bytes: &[u8]) -> Response {
    let Ok(request) = serde_json::from_slice::<Request>(bytes) else {
        return Response::plain("failed");
    };
    if request.version != 1
        || !request.namespace.accepts_reference(&request.reference)
        || request
            .value
            .as_ref()
            .is_some_and(|value| value.is_empty() || value.len() > MAX_VALUE)
        || !matches!(
            (&request.operation, &request.value),
            (Operation::Put, Some(_)) | (Operation::Get | Operation::Delete, None)
        )
    {
        return Response::plain("failed");
    }
    let service = request.namespace.service();
    match request.operation {
        Operation::Get => match backend.get(service, &request.reference) {
            Ok(Some(value)) if !value.is_empty() && value.len() <= MAX_VALUE => {
                match String::from_utf8(value) {
                    Ok(value) => Response {
                        status: "ok",
                        value: Some(value),
                        os_status: None,
                        operation: None,
                        error_class: None,
                    },
                    Err(_) => Response::plain("failed"),
                }
            }
            Ok(None) => Response::plain("missing"),
            Ok(Some(_)) => Response::plain("failed"),
            Err(error) => Response::error(error),
        },
        Operation::Put => match backend.put(
            service,
            &request.reference,
            request.value.as_deref().unwrap().as_bytes(),
        ) {
            Ok(()) => Response::plain("ok"),
            Err(error) => Response::error(error),
        },
        Operation::Delete => match backend.delete(service, &request.reference) {
            Ok(()) => Response::plain("ok"),
            Err(error) => Response::error(error),
        },
    }
}

pub fn serve_channel(stream: UnixStream, backend: impl CredentialBackend) -> Result<(), ()> {
    serve_channel_with_timeout(stream, backend, Duration::from_secs(5))
}

#[doc(hidden)]
pub fn serve_channel_with_timeout(
    mut stream: UnixStream,
    mut backend: impl CredentialBackend,
    timeout: Duration,
) -> Result<(), ()> {
    loop {
        let mut header = [0; 4];
        stream.set_read_timeout(None).map_err(|_| ())?;
        match stream.read(&mut header[..1]) {
            Ok(0) => return Ok(()),
            Ok(1) => {}
            Ok(_) => unreachable!(),
            Err(_) => return Err(()),
        }
        let deadline = Instant::now() + timeout;
        read_exact_until(&mut stream, &mut header[1..], deadline)?;
        let size = u32::from_be_bytes(header) as usize;
        if size == 0 || size > MAX_FRAME {
            return Err(());
        }
        let mut request = vec![0; size];
        read_exact_until(&mut stream, &mut request, deadline)?;
        let response = dispatch(&mut backend, &request);
        let response = serde_json::to_vec(&response).map_err(|_| ())?;
        write_all_until(
            &mut stream,
            &(response.len() as u32).to_be_bytes(),
            deadline,
        )?;
        write_all_until(&mut stream, &response, deadline)?;
    }
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

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::trusted_engine_metadata;

    #[test]
    fn installed_engine_accepts_only_root_or_current_owner_and_nonwritable_single_link() {
        assert!(trusted_engine_metadata(true, 501, 501, 0o100755, 1));
        assert!(trusted_engine_metadata(true, 0, 501, 0o100755, 1));
        assert!(!trusted_engine_metadata(true, 502, 501, 0o100755, 1));
        assert!(!trusted_engine_metadata(true, 0, 501, 0o100775, 1));
        assert!(!trusted_engine_metadata(true, 0, 501, 0o100755, 2));
        assert!(!trusted_engine_metadata(false, 0, 501, 0o100755, 1));
    }
}
