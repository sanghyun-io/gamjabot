//! Fixed-protocol D2 provider transport boundary.
//!
//! This module does not own auth generations or credential commit state.  It
//! consumes a binding supplied by the existing Core auth lifecycle, keeps SDK
//! clients inside that opaque binding, and returns only bounded public results.

use async_trait::async_trait;
use base64::Engine as _;
use azure_core::credentials::{TokenCredential, TokenRequestOptions};
use azure_identity::{AzureCliCredential, AzureCliCredentialOptions, Executor};
use google_cloud_aiplatform_v1::model::{
    Content, GenerateContentRequest, GenerateContentResponse, GenerationConfig, Part,
};
use google_cloud_auth::credentials::{
    CacheableResource, Credentials, CredentialsProvider, EntityTag,
};
use google_cloud_auth::errors::CredentialsError;
use http::{Extensions, HeaderMap, HeaderValue};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha1::Sha1;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::ffi::OsStr;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Output;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use std::process::Stdio;
use std::sync::{
    atomic::{AtomicBool, AtomicU32, Ordering},
    Arc, Condvar, Mutex,
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use tokio::io::AsyncReadExt;
use tokio::sync::Notify;
use zeroize::{Zeroize, Zeroizing};

const AZURE_SCOPE: &str = "https://cognitiveservices.azure.com/.default";
const MAX_FILE_BYTES: u64 = 65_536;
const MAX_INPUT_BYTES: usize = 65_536;
const MAX_OUTPUT_TOKENS: u32 = 8_192;
const MAX_PROCESS_OUTPUT: u64 = 65_536;
const MAX_HTTP_BODY: usize = 1_048_576;
const MAX_PROVIDER_FRAME_BYTES: usize = 14_000_000;
const MAX_PROVIDER_IMAGE_BYTES: usize = 10_000_000;
const MAX_PROVIDER_ITEMS: usize = 64;
const MAX_SCHEMA_BYTES: usize = 65_536;
const MAX_RESPONSE_TEXT_BYTES: usize = 1_000_000;
const MAX_COMMITTED_PROVIDER_VALUE: usize = 32_768;
const GOOGLE_CLOUD_SCOPE: &str = "https://www.googleapis.com/auth/cloud-platform";
const GOOGLE_OAUTH_TOKEN_ENDPOINT: &str = "https://oauth2.googleapis.com/token";
const GOOGLE_IAM_ORIGIN: &str = "https://iamcredentials.googleapis.com";
const VERTEX_X_GOOG_API_CLIENT: &str =
    "gl-rust/1.98.1 gax/0.7.18 rest/0.7.18-reqwest gapic/1.16.0";
// Matches the closed provider/public-config wire version in the Python catalog DTO.
pub(crate) const COMPILED_PROVIDER_DESCRIPTOR_REVISION: u64 = 1;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderProtocol {
    #[serde(rename = "azure_openai_responses_v1")]
    AzureOpenAiResponsesV1,
    #[serde(rename = "aws_bedrock_converse")]
    AwsBedrockConverse,
    #[serde(rename = "vertex_generate_content_v1")]
    VertexGenerateContentV1,
}

pub const PROVIDER_PRIVATE_FRAME_BYTES: usize = MAX_PROVIDER_FRAME_BYTES;
pub const PROVIDER_PRIVATE_MAX_IN_FLIGHT: usize = 4;
pub const MAX_TEST_PHYSICAL_REQUESTS: u32 = 7;

fn deserialize_physical_request_limit<'de, D>(deserializer: D) -> Result<u32, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let value = u32::deserialize(deserializer)?;
    if value <= MAX_TEST_PHYSICAL_REQUESTS {
        Ok(value)
    } else {
        Err(serde::de::Error::custom("physical request limit exceeds maximum"))
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderHandshakeV1 {
    pub version: u8,
    pub current_generation_fingerprint: String,
    pub max_frame_bytes: usize,
    pub max_in_flight: usize,
}

impl ProviderHandshakeV1 {
    pub fn new(current_generation_fingerprint: String) -> Self {
        Self {
            version: 1,
            current_generation_fingerprint,
            max_frame_bytes: PROVIDER_PRIVATE_FRAME_BYTES,
            max_in_flight: PROVIDER_PRIVATE_MAX_IN_FLIGHT,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderExecuteCallV1 {
    pub descriptor_revision: u64,
    pub protocol: ProviderProtocol,
    pub model_id: String,
    pub public_fields: Value,
    #[serde(deserialize_with = "deserialize_physical_request_limit")]
    pub physical_model_request_budget: u32,
    pub request: BoundedProviderRequest,
}

impl ProviderExecuteCallV1 {
    pub fn into_protocol_call(self, handle: String) -> Result<ProtocolCall, &'static str> {
        let request = BoundedProviderRequest::from_wire_value(
            serde_json::to_value(self.request).map_err(|_| "invalid_bounded_request")?,
        )?;
        Ok(ProtocolCall::with_physical_model_request_budget(
            handle,
            self.descriptor_revision,
            self.protocol,
            self.model_id,
            self.public_fields,
            request,
            self.physical_model_request_budget,
        ))
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(tag = "type", deny_unknown_fields)]
pub enum ProviderWireRequest {
    #[serde(rename = "provider_bind_v1")]
    Bind {
        version: u8,
        request_id: u64,
        current_generation_fingerprint: String,
        binding: crate::auth_broker::ProviderBindingRequest,
    },
    #[serde(rename = "provider_execute_v1")]
    Execute {
        version: u8,
        request_id: u64,
        handle: String,
        call: ProviderExecuteCallV1,
    },
    #[serde(rename = "provider_cancel_v1")]
    Cancel {
        version: u8,
        request_id: u64,
        handle: String,
        target_request_id: u64,
    },
    #[serde(rename = "provider_release_v1")]
    Release {
        version: u8,
        request_id: u64,
        handle: String,
    },
}

impl ProviderWireRequest {
    pub fn version_and_request_id(&self) -> (u8, u64) {
        match self {
            Self::Bind { version, request_id, .. }
            | Self::Execute { version, request_id, .. }
            | Self::Cancel { version, request_id, .. }
            | Self::Release { version, request_id, .. } => (*version, *request_id),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderOperationV1 {
    Bind,
    Execute,
    Cancel,
    Release,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "type", deny_unknown_fields)]
pub enum ProviderWireReply {
    #[serde(rename = "provider_bound_v1")]
    Bound { version: u8, request_id: u64, handle: String },
    #[serde(rename = "provider_result_v1")]
    Result {
        version: u8,
        request_id: u64,
        handle: String,
        result: SafeProviderResult,
    },
    #[serde(rename = "provider_cancelled_v1")]
    Cancelled {
        version: u8,
        request_id: u64,
        handle: String,
        target_request_id: u64,
    },
    #[serde(rename = "provider_released_v1")]
    Released { version: u8, request_id: u64, handle: String },
    #[serde(rename = "provider_error_v1")]
    Error {
        version: u8,
        request_id: u64,
        operation: ProviderOperationV1,
        error_code: &'static str,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AvailabilityReason {
    Available,
    NativeSourceUnavailable,
    NativeSourceDisallowed,
    NativeSourceUnsupported,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SafeAvailability {
    pub available: bool,
    pub finite_reason: AvailabilityReason,
    pub source_generation_fingerprint: String,
}

impl SafeAvailability {
    fn available(fingerprint: String) -> Self {
        Self {
            available: true,
            finite_reason: AvailabilityReason::Available,
            source_generation_fingerprint: fingerprint,
        }
    }

    fn unavailable(reason: AvailabilityReason) -> Self {
        Self {
            available: false,
            finite_reason: reason,
            source_generation_fingerprint: String::new(),
        }
    }
}

pub struct NativeChainInspector;

impl NativeChainInspector {
    pub fn azure_cli(path: &Path) -> SafeAvailability {
        let metadata = match safe_file_metadata(path, false) {
            Ok(metadata) => metadata,
            Err(reason) => return SafeAvailability::unavailable(reason),
        };
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if metadata.permissions().mode() & 0o111 == 0 {
                return SafeAvailability::unavailable(AvailabilityReason::NativeSourceDisallowed);
            }
        }
        SafeAvailability::available(metadata_fingerprint("azure_cli", path, &metadata, "az"))
    }

    pub fn aws_profile(config: &Path, credentials: &Path, profile: &str) -> SafeAvailability {
        match inspect_aws_profile_snapshot(config, credentials, profile) {
            Ok(snapshot) => snapshot.availability,
            Err(availability) => availability,
        }
    }

    pub fn vertex_adc(path: &Path) -> SafeAvailability {
        match canonical_vertex_adc(path) {
            Ok((_document, availability)) => availability,
            Err(reason) => SafeAvailability::unavailable(reason),
        }
    }
}

/// Core-internal enrollment transport. It inspects only caller-selected native
/// sources and stores an immutable, reduced snapshot for a later authorized
/// Test or inference request. It never performs a login or provider request.
pub struct NativeChainEnrollmentTransport {
    azure_cli_path: PathBuf,
    aws_config_path: PathBuf,
    aws_credentials_path: PathBuf,
    aws_sso_cache_path: PathBuf,
    vertex_adc_path: PathBuf,
    now_epoch_seconds: Option<i64>,
}

impl NativeChainEnrollmentTransport {
    #[doc(hidden)]
    pub fn from_paths(
        azure_cli_path: PathBuf,
        aws_config_path: PathBuf,
        aws_credentials_path: PathBuf,
        aws_sso_cache_path: PathBuf,
        vertex_adc_path: PathBuf,
    ) -> Self {
        Self {
            azure_cli_path,
            aws_config_path,
            aws_credentials_path,
            aws_sso_cache_path,
            vertex_adc_path,
            now_epoch_seconds: None,
        }
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn from_paths_with_test_time(
        azure_cli_path: PathBuf,
        aws_config_path: PathBuf,
        aws_credentials_path: PathBuf,
        aws_sso_cache_path: PathBuf,
        vertex_adc_path: PathBuf,
        now_epoch_seconds: i64,
    ) -> Self {
        let mut value = Self::from_paths(
            azure_cli_path,
            aws_config_path,
            aws_credentials_path,
            aws_sso_cache_path,
            vertex_adc_path,
        );
        value.now_epoch_seconds = Some(now_epoch_seconds);
        value
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct AwsRoleStep {
    role_arn: String,
    external_id: Option<String>,
    role_session_name: String,
    duration_seconds: Option<i32>,
}

#[derive(Clone, Debug)]
struct InspectedLegacySso {
    start_url: String,
    region: String,
    account_id: String,
    role_name: String,
    role_steps: Vec<AwsRoleStep>,
}

#[derive(Serialize)]
#[serde(tag = "source", rename_all = "snake_case")]
enum CanonicalNativeEnrollment<'a> {
    AzureCli { executable_path: &'a str },
    AwsProfile {
        config_path: &'a str,
        credentials_path: &'a str,
        config_contents: &'a str,
        credentials_contents: &'a str,
    },
    AwsLegacySso {
        config_path: &'a str,
        credentials_path: &'a str,
        cache_path: &'a str,
        config_contents: &'a str,
        credentials_contents: &'a str,
        start_url: &'a str,
        sso_region: &'a str,
        account_id: &'a str,
        role_name: &'a str,
        access_token: &'a str,
        expires_at_epoch_seconds: i64,
        role_steps: &'a [AwsRoleStep],
    },
    VertexAdc { document_path: &'a str, document: &'a Value },
}

#[derive(Deserialize)]
#[serde(tag = "source", rename_all = "snake_case", deny_unknown_fields)]
enum StoredNativeEnrollment {
    AzureCli { executable_path: String },
    AwsProfile {
        config_path: String,
        credentials_path: String,
        config_contents: String,
        credentials_contents: String,
    },
    AwsLegacySso {
        config_path: String,
        credentials_path: String,
        cache_path: String,
        config_contents: String,
        credentials_contents: String,
        start_url: String,
        sso_region: String,
        account_id: String,
        role_name: String,
        access_token: Zeroizing<String>,
        expires_at_epoch_seconds: i64,
        role_steps: Vec<AwsRoleStep>,
    },
    VertexAdc { document_path: String, document: Value },
}

impl crate::auth_broker::SyntheticAuthTransport for NativeChainEnrollmentTransport {
    fn start(
        &mut self,
        _binding: &crate::auth_broker::StableBinding,
        native_enrollment: Option<&crate::auth_broker::NativeEnrollmentSpecV1>,
    ) -> crate::auth_broker::TransportResult {
        use crate::auth_broker::{NativeEnrollmentSpecV1, TransportResult};
        let Some(spec) = native_enrollment else { return TransportResult::Unavailable; };
        let result = match spec {
            NativeEnrollmentSpecV1::AzureCli { resource_endpoint, api_version } => {
                if api_version != "v1" || validate_azure_root(resource_endpoint).is_err() {
                    return TransportResult::Unavailable;
                }
                let availability = NativeChainInspector::azure_cli(&self.azure_cli_path);
                if !availability.available { return TransportResult::Unavailable; }
                let Some(path) = self.azure_cli_path.to_str() else {
                    return TransportResult::Unavailable;
                };
                serde_json::to_vec(&CanonicalNativeEnrollment::AzureCli {
                    executable_path: path,
                }).map(|payload| (availability, Zeroizing::new(payload)))
            }
            NativeEnrollmentSpecV1::AwsProfile { profile_name, region } => {
                if !valid_aws_region(region) { return TransportResult::Unavailable; }
                let snapshot = match inspect_aws_profile_snapshot(
                    &self.aws_config_path,
                    &self.aws_credentials_path,
                    profile_name,
                ) {
                    Ok(value) => value,
                    Err(_) => return TransportResult::Unavailable,
                };
                let (Some(config_path), Some(credentials_path)) = (
                    self.aws_config_path.to_str(), self.aws_credentials_path.to_str()
                ) else { return TransportResult::Unavailable; };
                if let Some(legacy) = snapshot.legacy_sso.as_ref() {
                    let now = self.now_epoch_seconds.unwrap_or_else(current_epoch_seconds);
                    let cache = match inspect_legacy_sso_cache(
                        &self.aws_sso_cache_path,
                        &legacy.start_url,
                        now,
                    ) {
                        Ok(value) => value,
                        Err(_) => return TransportResult::Unavailable,
                    };
                    let Some(cache_path) = cache.path.to_str() else {
                        return TransportResult::Unavailable;
                    };
                    let availability = SafeAvailability::available(digest_safe(
                        format!(
                            "{}:{}",
                            snapshot.availability.source_generation_fingerprint,
                            cache.metadata_fingerprint,
                        ).as_bytes(),
                    ));
                    serde_json::to_vec(&CanonicalNativeEnrollment::AwsLegacySso {
                        config_path,
                        credentials_path,
                        cache_path,
                        config_contents: snapshot.config_contents.as_str(),
                        credentials_contents: snapshot.credentials_contents.as_str(),
                        start_url: &legacy.start_url,
                        sso_region: &legacy.region,
                        account_id: &legacy.account_id,
                        role_name: &legacy.role_name,
                        access_token: cache.access_token.as_str(),
                        expires_at_epoch_seconds: cache.expires_at_epoch_seconds,
                        role_steps: &legacy.role_steps,
                    }).and_then(|payload| {
                        if payload.len() > MAX_COMMITTED_PROVIDER_VALUE {
                            Err(serde_json::Error::io(io::Error::new(
                                io::ErrorKind::InvalidData,
                                "provider value too large",
                            )))
                        } else {
                            Ok((availability, Zeroizing::new(payload)))
                        }
                    })
                } else {
                    serde_json::to_vec(&CanonicalNativeEnrollment::AwsProfile {
                        config_path,
                        credentials_path,
                        config_contents: snapshot.config_contents.as_str(),
                        credentials_contents: snapshot.credentials_contents.as_str(),
                    }).map(|payload| (snapshot.availability, Zeroizing::new(payload)))
                }
            }
            NativeEnrollmentSpecV1::VertexAdc { project_id, location } => {
                if validate_vertex_ids(project_id, location, "enrollment-model").is_err() {
                    return TransportResult::Unavailable;
                }
                let (document, availability) = match canonical_vertex_adc(&self.vertex_adc_path) {
                    Ok(value) => value,
                    Err(_) => return TransportResult::Unavailable,
                };
                let Some(document_path) = self.vertex_adc_path.to_str() else {
                    return TransportResult::Unavailable;
                };
                serde_json::to_vec(&CanonicalNativeEnrollment::VertexAdc {
                    document_path,
                    document: &document.0,
                }).map(|payload| (availability, Zeroizing::new(payload)))
            }
        };
        match result {
            Ok((availability, canonical_payload)) => TransportResult::NativeReady {
                source_generation_fingerprint: availability.source_generation_fingerprint,
                canonical_payload,
            },
            Err(_) => TransportResult::Unavailable,
        }
    }

    fn status(
        &mut self,
        _binding: &crate::auth_broker::StableBinding,
    ) -> crate::auth_broker::TransportResult {
        crate::auth_broker::TransportResult::Unavailable
    }

    fn cancel(&mut self, _binding: &crate::auth_broker::StableBinding) {}
}

struct InspectedAwsProfile {
    config_contents: Zeroizing<String>,
    credentials_contents: Zeroizing<String>,
    availability: SafeAvailability,
    legacy_sso: Option<InspectedLegacySso>,
}

struct InspectedLegacySsoCache {
    path: PathBuf,
    access_token: Zeroizing<String>,
    expires_at_epoch_seconds: i64,
    metadata_fingerprint: String,
}

const AWS_ROLE_KEYS: &[&str] = &[
    "role_arn", "source_profile", "external_id", "role_session_name", "duration_seconds",
];
const AWS_STATIC_KEYS: &[&str] = &[
    "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
];
const AWS_LEGACY_SSO_KEYS: &[&str] = &[
    "sso_start_url", "sso_region", "sso_account_id", "sso_role_name",
];

fn inspect_aws_profile_snapshot(
    config: &Path,
    credentials: &Path,
    profile: &str,
) -> Result<InspectedAwsProfile, SafeAvailability> {
    if !bounded_identifier(profile) {
        return Err(SafeAvailability::unavailable(
            AvailabilityReason::NativeSourceDisallowed,
        ));
    }
    let (config_bytes, config_meta) = match read_bounded_file(config, false) {
        Ok(value) => value,
        Err(reason) => return Err(SafeAvailability::unavailable(reason)),
    };
    let (credentials_bytes, credentials_meta) = match read_bounded_file(credentials, false) {
        Ok(value) => value,
        Err(reason) => return Err(SafeAvailability::unavailable(reason)),
    };
    let config_contents = match zeroizing_string(config_bytes) {
        Ok(value) => value,
        Err(()) => {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceDisallowed,
            ))
        }
    };
    let credentials_contents = match zeroizing_string(credentials_bytes) {
        Ok(value) => value,
        Err(()) => {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceDisallowed,
            ))
        }
    };
    let config_sections = parse_profile_sections(config_contents.as_str(), true);
    let credential_sections = parse_profile_sections(credentials_contents.as_str(), false);
    let mut sections = config_sections.clone();
    for (name, values) in credential_sections {
        sections.entry(name).or_default().extend(values);
    }
    let mut current = profile.to_owned();
    let mut visited = HashSet::new();
    let mut safe_chain = Vec::new();
    let uses_legacy_sso = loop {
        if !visited.insert(current.clone()) || visited.len() > 16 {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceDisallowed,
            ));
        }
        let Some(values) = sections.get(&current) else {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceUnavailable,
            ));
        };
        if values.keys().any(|key| {
            matches!(
                key.as_str(),
                "credential_process"
                    | "credential_source"
                    | "web_identity_token_file"
                    | "login_session"
                    | "endpoint_url"
                    | "services"
            )
        }) {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceDisallowed,
            ));
        }
        if values.contains_key("sso_session") {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceUnsupported,
            ));
        }
        if values.keys().any(|key| {
            !AWS_ROLE_KEYS.contains(&key.as_str())
                && !AWS_STATIC_KEYS.contains(&key.as_str())
                && !AWS_LEGACY_SSO_KEYS.contains(&key.as_str())
                && !matches!(key.as_str(), "region" | "output")
        }) {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceDisallowed,
            ));
        }
        safe_chain.push(current.clone());
        if values.contains_key("role_arn") {
            if AWS_STATIC_KEYS.iter().any(|key| values.contains_key(*key))
                || AWS_LEGACY_SSO_KEYS.iter().any(|key| values.contains_key(*key))
            {
                return Err(SafeAvailability::unavailable(
                    AvailabilityReason::NativeSourceDisallowed,
                ));
            }
            let Some(source) = values.get("source_profile") else {
                return Err(SafeAvailability::unavailable(
                    AvailabilityReason::NativeSourceDisallowed,
                ));
            };
            if !bounded_identifier(source) {
                return Err(SafeAvailability::unavailable(
                    AvailabilityReason::NativeSourceDisallowed,
                ));
            }
            current = source.clone();
            continue;
        }
        let static_leaf = values.contains_key("aws_access_key_id")
            && values.contains_key("aws_secret_access_key");
        let legacy_sso = ["sso_start_url", "sso_region", "sso_account_id", "sso_role_name"]
            .iter()
            .all(|key| values.contains_key(*key));
        let any_static = AWS_STATIC_KEYS.iter().any(|key| values.contains_key(*key));
        let any_legacy_sso = AWS_LEGACY_SSO_KEYS.iter().any(|key| values.contains_key(*key));
        if static_leaf == legacy_sso
            || any_static != static_leaf
            || any_legacy_sso != legacy_sso
        {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceDisallowed,
            ));
        }
        break legacy_sso;
    };
    let provenance = format!(
        "{}:{}:{}",
        metadata_fingerprint("aws_config", config, &config_meta, profile),
        metadata_fingerprint("aws_credentials", credentials, &credentials_meta, profile),
        safe_chain.join("/")
    );
    let mut canonical_config = Zeroizing::new(String::new());
    let mut canonical_credentials = Zeroizing::new(String::new());
    for name in &safe_chain {
        let values = sections.get(name).expect("selected profile exists");
        let config_values = values.iter().filter(|(key, _)| {
            AWS_ROLE_KEYS.contains(&key.as_str()) || AWS_LEGACY_SSO_KEYS.contains(&key.as_str())
        }).collect::<Vec<_>>();
        if !config_values.is_empty() {
            canonical_config.push_str(&format!("[profile {name}]\n"));
            for (key, value) in config_values {
                canonical_config.push_str(key);
                canonical_config.push('=');
                canonical_config.push_str(value);
                canonical_config.push('\n');
            }
        }
        let credential_values = values.iter().filter(|(key, _)| {
            AWS_STATIC_KEYS.contains(&key.as_str())
        }).collect::<Vec<_>>();
        if !credential_values.is_empty() {
            canonical_credentials.push_str(&format!("[{name}]\n"));
            for (key, value) in credential_values {
                canonical_credentials.push_str(key);
                canonical_credentials.push('=');
                canonical_credentials.push_str(value);
                canonical_credentials.push('\n');
            }
        }
    }
    if canonical_config.is_empty() { canonical_config.push('\n'); }
    if canonical_credentials.is_empty() { canonical_credentials.push('\n'); }
    let legacy_sso = if uses_legacy_sso {
        let leaf_name = safe_chain.last().expect("selected AWS leaf exists");
        let leaf = sections.get(leaf_name).expect("selected AWS leaf exists");
        let start_url = leaf.get("sso_start_url").cloned().ok_or_else(|| {
            SafeAvailability::unavailable(AvailabilityReason::NativeSourceDisallowed)
        })?;
        let region = leaf.get("sso_region").cloned().ok_or_else(|| {
            SafeAvailability::unavailable(AvailabilityReason::NativeSourceDisallowed)
        })?;
        let account_id = leaf.get("sso_account_id").cloned().ok_or_else(|| {
            SafeAvailability::unavailable(AvailabilityReason::NativeSourceDisallowed)
        })?;
        let role_name = leaf.get("sso_role_name").cloned().ok_or_else(|| {
            SafeAvailability::unavailable(AvailabilityReason::NativeSourceDisallowed)
        })?;
        if !valid_sso_start_url(&start_url)
            || !valid_aws_region(&region)
            || !valid_aws_account_id(&account_id)
            || !valid_aws_role_name(&role_name)
            || AWS_STATIC_KEYS.iter().any(|key| leaf.contains_key(*key))
        {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceDisallowed,
            ));
        }
        let mut role_steps = Vec::new();
        for name in safe_chain[..safe_chain.len() - 1].iter().rev() {
            let values = sections.get(name).expect("selected AWS role exists");
            let role_arn = values.get("role_arn").cloned().ok_or_else(|| {
                SafeAvailability::unavailable(AvailabilityReason::NativeSourceDisallowed)
            })?;
            let external_id = values.get("external_id").cloned();
            let role_session_name = values.get("role_session_name")
                .cloned()
                .unwrap_or_else(|| "gamja-provider".to_owned());
            let duration_seconds = values.get("duration_seconds")
                .map(|value| value.parse::<i32>())
                .transpose()
                .map_err(|_| SafeAvailability::unavailable(
                    AvailabilityReason::NativeSourceDisallowed,
                ))?;
            if !valid_role_arn(&role_arn)
                || external_id.as_deref().is_some_and(|value| !valid_external_id(value))
                || !valid_role_session_name(&role_session_name)
                || duration_seconds.is_some_and(|value| !(900..=43_200).contains(&value))
            {
                return Err(SafeAvailability::unavailable(
                    AvailabilityReason::NativeSourceDisallowed,
                ));
            }
            role_steps.push(AwsRoleStep {
                role_arn,
                external_id,
                role_session_name,
                duration_seconds,
            });
        }
        Some(InspectedLegacySso {
            start_url,
            region,
            account_id,
            role_name,
            role_steps,
        })
    } else {
        None
    };
    Ok(InspectedAwsProfile {
        config_contents: canonical_config,
        credentials_contents: canonical_credentials,
        availability: SafeAvailability::available(digest_safe(provenance.as_bytes())),
        legacy_sso,
    })
}

fn zeroizing_string(mut bytes: Zeroizing<Vec<u8>>) -> Result<Zeroizing<String>, ()> {
    match String::from_utf8(std::mem::take(&mut *bytes)) {
        Ok(value) => Ok(Zeroizing::new(value)),
        Err(error) => {
            let mut bytes = error.into_bytes();
            bytes.zeroize();
            Err(())
        }
    }
}

fn current_epoch_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|value| i64::try_from(value.as_secs()).ok())
        .unwrap_or(i64::MAX)
}

fn validate_owned_private_directory(path: &Path) -> Result<(), AvailabilityReason> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| AvailabilityReason::NativeSourceUnavailable)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(AvailabilityReason::NativeSourceDisallowed);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if metadata.uid() != unsafe { libc::geteuid() }
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err(AvailabilityReason::NativeSourceDisallowed);
        }
    }
    Ok(())
}

fn legacy_sso_cache_filename(start_url: &str) -> String {
    let digest = Sha1::digest(start_url.as_bytes());
    let mut name = String::with_capacity(45);
    for byte in digest {
        use std::fmt::Write;
        let _ = write!(name, "{byte:02x}");
    }
    name.push_str(".json");
    name
}

fn inspect_legacy_sso_cache(
    cache_directory: &Path,
    start_url: &str,
    now_epoch_seconds: i64,
) -> Result<InspectedLegacySsoCache, SafeAvailability> {
    validate_owned_private_directory(cache_directory)
        .map_err(SafeAvailability::unavailable)?;
    let path = cache_directory.join(legacy_sso_cache_filename(start_url));
    let (bytes, metadata) = read_bounded_file(&path, true)
        .map_err(SafeAvailability::unavailable)?;
    let mut document = SecretJson::parse(bytes.as_slice()).map_err(|_| {
        SafeAvailability::unavailable(AvailabilityReason::NativeSourceDisallowed)
    })?;
    let object = document.0.as_object_mut().ok_or_else(|| {
        SafeAvailability::unavailable(AvailabilityReason::NativeSourceDisallowed)
    })?;
    let access_token = match object.remove("accessToken") {
        Some(Value::String(value)) => Zeroizing::new(value),
        _ => return Err(SafeAvailability::unavailable(
            AvailabilityReason::NativeSourceDisallowed,
        )),
    };
    if access_token.is_empty() || access_token.len() > MAX_FILE_BYTES as usize {
        return Err(SafeAvailability::unavailable(
            AvailabilityReason::NativeSourceDisallowed,
        ));
    }
    let expires_at = object.remove("expiresAt")
        .and_then(|value| value.as_str().map(ToOwned::to_owned))
        .filter(|value| value.len() <= 64 && value.ends_with('Z'))
        .ok_or_else(|| SafeAvailability::unavailable(
            AvailabilityReason::NativeSourceDisallowed,
        ))?;
    if let Some(cache_start_url) = object.remove("startUrl") {
        if cache_start_url.as_str() != Some(start_url) {
            return Err(SafeAvailability::unavailable(
                AvailabilityReason::NativeSourceDisallowed,
            ));
        }
    }
    let expires_at_epoch_seconds = chrono::DateTime::parse_from_rfc3339(&expires_at)
        .ok()
        .filter(|value| value.offset().local_minus_utc() == 0)
        .map(|value| value.timestamp())
        .filter(|value| *value > now_epoch_seconds)
        .ok_or_else(|| SafeAvailability::unavailable(
            AvailabilityReason::NativeSourceUnavailable,
        ))?;
    zeroize_json_value(&mut document.0);
    Ok(InspectedLegacySsoCache {
        metadata_fingerprint: metadata_fingerprint(
            "aws_sso_cache",
            &path,
            &metadata,
            start_url,
        ),
        path,
        access_token,
        expires_at_epoch_seconds,
    })
}

fn valid_sso_start_url(value: &str) -> bool {
    if value.len() > 2_048 { return false; }
    let Ok(url) = url::Url::parse(value) else { return false; };
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && url.host_str().is_some_and(|host| {
            host == "awsapps.com" || host.ends_with(".awsapps.com")
        })
        && url.path() == "/start"
}

fn valid_aws_account_id(value: &str) -> bool {
    value.len() == 12 && value.bytes().all(|byte| byte.is_ascii_digit())
}

fn valid_aws_role_name(value: &str) -> bool {
    !value.is_empty() && value.len() <= 64 && value.bytes().all(|byte| {
        byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'+' | b'=' | b',' | b'.' | b'@' | b'-')
    })
}

fn valid_role_session_name(value: &str) -> bool {
    value.len() >= 2 && value.len() <= 64 && value.bytes().all(|byte| {
        byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'+' | b'=' | b',' | b'.' | b'@' | b'-')
    })
}

fn valid_external_id(value: &str) -> bool {
    value.len() >= 2 && value.len() <= 1_224 && value.bytes().all(|byte| {
        byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'+' | b'=' | b',' | b'.' | b'@' | b':' | b'/' | b'-')
    })
}

fn valid_role_arn(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("arn:aws:iam::") else { return false; };
    let Some((account, role)) = rest.split_once(":role/") else { return false; };
    valid_aws_account_id(account)
        && !role.is_empty()
        && role.len() <= 512
        && role.split('/').all(|segment| valid_aws_role_name(segment))
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderRequestScope {
    pub connection_id: u64,
    pub provider_kind: String,
    pub base_url: Option<String>,
    pub room_id: u64,
    pub agent_id: u64,
    pub watermark_seq: u64,
    pub descriptor_revision: u64,
    pub model_id: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderMessageRole { User, Assistant }

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderMessage { pub role: ProviderMessageRole, pub content: String }

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderToolDefinition {
    pub family: String,
    pub wire_name: String,
    pub description: String,
    pub parameters: Value,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderImage {
    pub mime_type: String,
    pub data_base64: String,
    pub width: Option<u32>,
    pub height: Option<u32>,
}

impl ProviderImage {
    fn decoded(&self) -> Result<Zeroizing<Vec<u8>>, &'static str> {
        if !matches!(self.mime_type.as_str(), "image/png" | "image/jpeg") {
            return Err("unsupported_option");
        }
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(self.data_base64.as_bytes())
            .map_err(|_| "invalid_bounded_request")?;
        if decoded.is_empty() || decoded.len() > MAX_PROVIDER_IMAGE_BYTES {
            return Err("invalid_bounded_request");
        }
        if self.width.is_some_and(|value| value == 0 || value > 100_000)
            || self.height.is_some_and(|value| value == 0 || value > 100_000)
        {
            return Err("invalid_bounded_request");
        }
        Ok(Zeroizing::new(decoded))
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderVisionInput { pub prompt: String, pub image: ProviderImage }

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderToolObservation {
    pub call_id: String,
    pub state: String,
    pub text: String,
    pub screenshot_ref: Option<String>,
    pub image: Option<ProviderImage>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderStateToolCall {
    pub call_id: String,
    pub name: String,
    pub arguments: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thought_signature: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ProviderContinuationState {
    AzureResponses { previous_response_id: String },
    BedrockConverse { assistant_text: String, tool_calls: Vec<ProviderStateToolCall> },
    VertexGenerateContent { assistant_text: String, tool_calls: Vec<ProviderStateToolCall> },
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderContinuation {
    pub provider_kind: String,
    pub protocol: ProviderProtocol,
    pub connection_id: u64,
    pub model_id: String,
    pub base_url: Option<String>,
    pub room_id: u64,
    pub agent_id: u64,
    pub watermark_seq: u64,
    pub descriptor_revision: u64,
    pub response_id: String,
    pub pending_call_ids: Vec<String>,
    pub provider_state: ProviderContinuationState,
}

#[derive(Clone, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BoundedProviderRequest {
    pub scope: ProviderRequestScope,
    pub protocol: ProviderProtocol,
    pub system: String,
    pub messages: Vec<ProviderMessage>,
    pub tools: Vec<ProviderToolDefinition>,
    pub observations: Vec<ProviderToolObservation>,
    pub vision: Option<ProviderVisionInput>,
    pub continuation: Option<ProviderContinuation>,
    pub temperature: Option<f64>,
    pub max_output_tokens: u32,
}

impl BoundedProviderRequest {
    pub fn new(input: impl Into<String>, max_output_tokens: u32) -> Result<Self, &'static str> {
        let input = input.into();
        Self::from_wire_value(serde_json::json!({
            "scope":{"connection_id":1,"provider_kind":"fixture","base_url":null,
                     "room_id":1,"agent_id":1,"watermark_seq":0,
                     "descriptor_revision":1,"model_id":"fixture-model"},
            "protocol":"azure_openai_responses_v1","system":"",
            "messages":[{"role":"user","content":input}],"tools":[],"observations":[],
            "vision":null,"continuation":null,"temperature":null,
            "max_output_tokens":max_output_tokens
        }))
    }

    pub fn from_wire_value(value: Value) -> Result<Self, &'static str> {
        let encoded = serde_json::to_vec(&value).map_err(|_| "invalid_bounded_request")?;
        if encoded.len() > MAX_PROVIDER_FRAME_BYTES { return Err("invalid_bounded_request"); }
        let request: Self = serde_json::from_value(value).map_err(|_| "invalid_bounded_request")?;
        request.validate()?;
        Ok(request)
    }

    fn validate(&self) -> Result<(), &'static str> {
        if self.scope.connection_id == 0 || !bounded_identifier(&self.scope.provider_kind)
            || self.scope.base_url.as_ref().is_some_and(|value| value.len() > 2_048)
            || self.scope.room_id == 0 || self.scope.agent_id == 0
            || self.scope.descriptor_revision == 0
            || !valid_provider_model_id(self.protocol, &self.scope.model_id)
            || self.system.len() > MAX_INPUT_BYTES || self.messages.is_empty()
            || self.messages.len() > MAX_PROVIDER_ITEMS || self.tools.len() > MAX_PROVIDER_ITEMS
            || self.observations.len() > MAX_PROVIDER_ITEMS
            || self.max_output_tokens == 0 || self.max_output_tokens > MAX_OUTPUT_TOKENS
            || self.temperature.is_some_and(|value| !value.is_finite() || !(0.0..=2.0).contains(&value))
        { return Err("invalid_bounded_request"); }
        for message in &self.messages {
            if message.content.is_empty() || message.content.len() > MAX_INPUT_BYTES {
                return Err("invalid_bounded_request");
            }
        }
        for tool in &self.tools {
            if !bounded_identifier(&tool.family) || !valid_wire_tool_name(&tool.wire_name)
                || !valid_persistence_binding(&tool.family, &tool.wire_name)
                || tool.description.len() > 4_096 || !tool.parameters.is_object()
                || serde_json::to_vec(&tool.parameters).map_or(true, |bytes| bytes.len() > MAX_SCHEMA_BYTES)
                || (if valid_mcp_family(&tool.family) {!crate::mcp_schema::safe_mcp_schema(&tool.parameters)} else {contains_remote_ref(&tool.parameters)})
            { return Err("invalid_bounded_request"); }
        }
        let mut image_bytes = 0usize;
        if let Some(vision) = &self.vision {
            if vision.prompt.is_empty() || vision.prompt.len() > MAX_INPUT_BYTES { return Err("invalid_bounded_request"); }
            image_bytes = image_bytes.saturating_add(vision.image.decoded()?.len());
        }
        for item in &self.observations {
            if !bounded_identifier(&item.call_id) || !bounded_identifier(&item.state)
                || item.text.len() > MAX_INPUT_BYTES || item.screenshot_ref.as_ref().is_some_and(|v| v.len() > 4_096)
            { return Err("invalid_bounded_request"); }
            if let Some(image) = &item.image { image_bytes = image_bytes.saturating_add(image.decoded()?.len()); }
        }
        if image_bytes > MAX_PROVIDER_IMAGE_BYTES { return Err("invalid_bounded_request"); }
        self.validate_continuation()?;
        Ok(())
    }

    fn validate_continuation(&self) -> Result<(), &'static str> {
        let Some(state) = &self.continuation else {
            return if self.observations.is_empty() { Ok(()) } else { Err("invalid_continuation") };
        };
        if state.protocol != self.protocol || state.connection_id != self.scope.connection_id
            || state.provider_kind != self.scope.provider_kind
            || state.base_url != self.scope.base_url
            || state.room_id != self.scope.room_id || state.agent_id != self.scope.agent_id
            || state.watermark_seq != self.scope.watermark_seq
            || state.descriptor_revision != self.scope.descriptor_revision
            || state.model_id != self.scope.model_id || !bounded_identifier(&state.response_id)
            || state.pending_call_ids.len() > MAX_PROVIDER_ITEMS
            || state.pending_call_ids.iter().collect::<HashSet<_>>().len() != state.pending_call_ids.len()
            || state.pending_call_ids.iter().any(|id| !bounded_identifier(id))
            || self.observations.iter().map(|v| v.call_id.as_str()).ne(state.pending_call_ids.iter().map(String::as_str))
        { return Err("invalid_continuation"); }
        let protocol_matches = matches!(
            (self.protocol, &state.provider_state),
            (ProviderProtocol::AzureOpenAiResponsesV1, ProviderContinuationState::AzureResponses { .. })
            | (ProviderProtocol::AwsBedrockConverse, ProviderContinuationState::BedrockConverse { .. })
            | (ProviderProtocol::VertexGenerateContentV1, ProviderContinuationState::VertexGenerateContent { .. })
        );
        if !protocol_matches { return Err("invalid_continuation"); }
        match &state.provider_state {
            ProviderContinuationState::AzureResponses { previous_response_id } => {
                if previous_response_id != &state.response_id { return Err("invalid_continuation"); }
            }
            ProviderContinuationState::BedrockConverse { assistant_text, tool_calls }
            | ProviderContinuationState::VertexGenerateContent { assistant_text, tool_calls } => {
                if assistant_text.len() > MAX_RESPONSE_TEXT_BYTES || tool_calls.len() != state.pending_call_ids.len()
                    || tool_calls.iter().map(|call| call.call_id.as_str())
                        .ne(state.pending_call_ids.iter().map(String::as_str))
                { return Err("invalid_continuation"); }
                for call in tool_calls {
                    if !valid_wire_tool_name(&call.name) || !call.arguments.is_object()
                        || serde_json::to_vec(&call.arguments).map_or(true, |value| value.len() > 32_000)
                    { return Err("invalid_continuation"); }
                    if let Some(signature) = &call.thought_signature {
                        if signature.len() > 65_536 || base64::engine::general_purpose::STANDARD.decode(signature).is_err() {
                            return Err("invalid_continuation");
                        }
                    }
                }
            }
        }
        Ok(())
    }

    fn primary_input(&self) -> &str {
        self.messages.iter().rev().find(|message| message.role == ProviderMessageRole::User)
            .map(|message| message.content.as_str()).unwrap_or("")
    }
}

impl std::fmt::Debug for BoundedProviderRequest {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("BoundedProviderRequest")
            .field("scope", &self.scope)
            .field("content", &"<private>")
            .field("max_output_tokens", &self.max_output_tokens)
            .finish()
    }
}

#[derive(Clone)]
pub struct ProtocolCall {
    committed_handle_id: Zeroizing<String>,
    descriptor_revision: u64,
    protocol: ProviderProtocol,
    model_id: String,
    public_fields: Value,
    request: BoundedProviderRequest,
    accounting: Arc<PhysicalRequestAccounting>,
}

#[derive(Debug)]
struct PhysicalRequestAccounting {
    budget: u32,
    count: AtomicU32,
    exhausted: AtomicBool,
}

impl PhysicalRequestAccounting {
    fn new(budget: u32) -> Self {
        Self { budget, count: AtomicU32::new(0), exhausted: AtomicBool::new(false) }
    }

    fn try_admit(&self) -> bool {
        loop {
            let current = self.count.load(Ordering::Acquire);
            if current >= self.budget {
                self.exhausted.store(true, Ordering::Release);
                return false;
            }
            if self.count.compare_exchange_weak(
                current,
                current + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ).is_ok() {
                return true;
            }
        }
    }

    fn count(&self) -> u32 { self.count.load(Ordering::Acquire) }
    fn exhausted(&self) -> bool { self.exhausted.load(Ordering::Acquire) }
}

impl ProtocolCall {
    pub fn new(
        committed_handle_id: impl Into<String>,
        descriptor_revision: u64,
        protocol: ProviderProtocol,
        model_id: impl Into<String>,
        public_fields: Value,
        request: BoundedProviderRequest,
    ) -> Self {
        Self::with_physical_model_request_budget(
            committed_handle_id,
            descriptor_revision,
            protocol,
            model_id,
            public_fields,
            request,
            1,
        )
    }

    pub fn with_physical_model_request_budget(
        committed_handle_id: impl Into<String>,
        descriptor_revision: u64,
        protocol: ProviderProtocol,
        model_id: impl Into<String>,
        public_fields: Value,
        mut request: BoundedProviderRequest,
        physical_model_request_budget: u32,
    ) -> Self {
        let model_id = model_id.into();
        if request.scope.provider_kind == "fixture" {
            request.scope.descriptor_revision = descriptor_revision;
            request.scope.model_id = model_id.clone();
            request.protocol = protocol;
        }
        Self {
            committed_handle_id: Zeroizing::new(committed_handle_id.into()),
            descriptor_revision,
            protocol,
            model_id,
            public_fields,
            request,
            accounting: Arc::new(PhysicalRequestAccounting::new(
                physical_model_request_budget.min(MAX_TEST_PHYSICAL_REQUESTS),
            )),
        }
    }

    pub fn model_id(&self) -> &str {
        &self.model_id
    }

    fn try_admit_model_request(&self) -> bool { self.accounting.try_admit() }
    fn physical_model_request_count(&self) -> u32 { self.accounting.count() }
    fn physical_model_request_budget_exhausted(&self) -> bool { self.accounting.exhausted() }
}

impl std::fmt::Debug for ProtocolCall {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ProtocolCall")
            .field("committed_handle_id", &"<private>")
            .field("descriptor_revision", &self.descriptor_revision)
            .field("protocol", &self.protocol)
            .field("model_id", &self.model_id)
            .field("public_fields", &self.public_fields)
            .field("request", &self.request)
            .field("physical_model_request_budget", &self.accounting.budget)
            .finish()
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SafeProviderToolCall {
    pub call_id: String,
    pub name: String,
    pub arguments: Value,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SafeProviderResult {
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub input_tokens: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub output_tokens: Option<u32>,
    #[serde(deserialize_with = "deserialize_physical_request_limit")]
    pub physical_model_request_count: u32,
    pub text: String,
    pub tool_calls: Vec<SafeProviderToolCall>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub continuation: Option<ProviderContinuation>,
}

impl SafeProviderResult {
    pub fn success(
        response_id: impl Into<String>,
        model: impl Into<String>,
        input_tokens: Option<u32>,
        output_tokens: Option<u32>,
    ) -> Self {
        let response_id = response_id.into();
        let model = model.into();
        if !bounded_identifier(&response_id) || !bounded_model_id(&model) {
            return Self::failure("malformed_provider_response");
        }
        Self {
            ok: true,
            error_code: None,
            response_id: Some(response_id),
            model: Some(model),
            input_tokens,
            output_tokens,
            physical_model_request_count: 0,
            text: String::new(),
            tool_calls: Vec::new(),
            continuation: None,
        }
    }

    fn success_contract(
        response_id: String,
        model: String,
        input_tokens: Option<u32>,
        output_tokens: Option<u32>,
        text: String,
        tool_calls: Vec<SafeProviderToolCall>,
        continuation: Option<ProviderContinuation>,
    ) -> Self {
        if !bounded_identifier(&response_id) || !bounded_model_id(&model)
            || text.len() > MAX_RESPONSE_TEXT_BYTES || tool_calls.len() > MAX_PROVIDER_ITEMS
            || tool_calls.iter().any(|call| !bounded_identifier(&call.call_id)
                || !valid_wire_tool_name(&call.name) || !call.arguments.is_object()
                || serde_json::to_vec(&call.arguments).map_or(true, |value| value.len() > 32_000))
        { return Self::failure("malformed_provider_response"); }
        Self { ok: true, error_code: None, response_id: Some(response_id), model: Some(model),
            input_tokens, output_tokens, physical_model_request_count: 0, text, tool_calls, continuation }
    }

    pub fn failure(code: &'static str) -> Self {
        Self {
            ok: false,
            error_code: Some(code),
            response_id: None,
            model: None,
            input_tokens: None,
            output_tokens: None,
            physical_model_request_count: 0,
            text: String::new(),
            tool_calls: Vec::new(),
            continuation: None,
        }
    }

    fn with_physical_model_request_count(mut self, count: u32) -> Self {
        self.physical_model_request_count = count.min(MAX_TEST_PHYSICAL_REQUESTS);
        self
    }
}

#[async_trait]
pub trait SdkAdapter: Send + Sync + std::fmt::Debug {
    fn protocol(&self) -> ProviderProtocol;
    async fn execute(&self, call: &ProtocolCall) -> SafeProviderResult;
}

pub struct CoreProviderTransport {
    opaque_id: Zeroizing<String>,
    descriptor_revision: u64,
    protocol: ProviderProtocol,
    model_id: String,
    public_fields: Value,
    adapter: Arc<dyn SdkAdapter>,
    authority_cancel: BoundedCancellation,
    _authority_lease: Option<crate::auth_broker::AuthorizedProviderPermit>,
}

impl std::fmt::Debug for CoreProviderTransport {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("CoreProviderTransport")
            .field("opaque_id", &"<private>")
            .field("descriptor_revision", &self.descriptor_revision)
            .field("protocol", &self.protocol)
            .field("model_id", &self.model_id)
            .finish_non_exhaustive()
    }
}

impl CoreProviderTransport {
    pub fn from_authorized_permit(
        permit: crate::auth_broker::AuthorizedProviderPermit,
        adapter: Arc<dyn SdkAdapter>,
    ) -> Result<Self, &'static str> {
        let valid_authority = if let Some((credential_ref, _, source_generation_fingerprint)) =
            permit.native_source()
        {
            !credential_ref.is_empty()
                && source_generation_fingerprint.len() == 64
                && source_generation_fingerprint
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        } else {
            permit.is_azure_api_key()
        };
        if !valid_authority {
            return Err("invalid_committed_binding");
        }
        let opaque_id = permit.handle().to_owned();
        let descriptor_revision = permit.descriptor_revision;
        let protocol = permit.protocol;
        let model_id = permit.model_id.clone();
        let public_fields = permit.public_fields.clone();
        let authority_cancel = permit.cancel.clone();
        Self::bind_authorized(
            opaque_id,
            descriptor_revision,
            protocol,
            model_id,
            public_fields,
            adapter,
            authority_cancel,
            Some(permit),
        )
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn bind_test_fixture(
        opaque_id: impl Into<String>,
        descriptor_revision: u64,
        protocol: ProviderProtocol,
        model_id: impl Into<String>,
        public_fields: Value,
        adapter: Arc<dyn SdkAdapter>,
    ) -> Result<Self, &'static str> {
        Self::bind_authorized(
            opaque_id.into(), descriptor_revision, protocol, model_id.into(),
            public_fields, adapter, BoundedCancellation::new(),
            None,
        )
    }

    fn bind_authorized(
        opaque_id: String,
        descriptor_revision: u64,
        protocol: ProviderProtocol,
        model_id: String,
        public_fields: Value,
        adapter: Arc<dyn SdkAdapter>,
        authority_cancel: BoundedCancellation,
        authority_lease: Option<crate::auth_broker::AuthorizedProviderPermit>,
    ) -> Result<Self, &'static str> {
        if !bounded_identifier(&opaque_id)
            || descriptor_revision == 0
            || !valid_provider_model_id(protocol, &model_id)
            || !public_fields.is_object()
            || serde_json::to_vec(&public_fields)
                .map_or(true, |encoded| encoded.len() > 4_096)
            || adapter.protocol() != protocol
        {
            return Err("invalid_committed_binding");
        }
        validate_public_binding(protocol, &model_id, &public_fields)?;
        Ok(Self {
            opaque_id: Zeroizing::new(opaque_id),
            descriptor_revision,
            protocol,
            model_id,
            public_fields,
            adapter,
            authority_cancel,
            _authority_lease: authority_lease,
        })
    }

    pub async fn execute(
        &self,
        call: ProtocolCall,
        cancel: &BoundedCancellation,
    ) -> SafeProviderResult {
        if !constant_binding_match(self, &call) {
            return SafeProviderResult::failure("configuration_changed")
                .with_physical_model_request_count(call.physical_model_request_count());
        }
        let result = tokio::select! {
            _ = cancel.cancelled() => SafeProviderResult::failure("cancelled"),
            _ = self.authority_cancel.cancelled() => SafeProviderResult::failure("cancelled"),
            result = self.adapter.execute(&call) => result,
        };
        result.with_physical_model_request_count(call.physical_model_request_count())
    }

    pub fn cancel(&self, cancel: &BoundedCancellation) {
        cancel.cancel();
    }
}

/// Builds the provider SDK client from the broker-reread immutable snapshot.
/// The secure broker lock must already be released before this function runs.
pub async fn transport_from_resolved_permit(
    resolved: crate::auth_broker::ResolvedProviderPermit,
) -> Result<CoreProviderTransport, &'static str> {
    let (permit, payload) = resolved.into_parts();
    if permit.is_azure_api_key() {
        let resource_endpoint = permit
            .public_fields
            .get("resource_endpoint")
            .and_then(Value::as_str)
            .ok_or("invalid_committed_binding")?;
        let adapter: Arc<dyn SdkAdapter> = Arc::new(
            AzureOpenAiSdkAdapter::with_api_key(resource_endpoint, payload)?,
        );
        return CoreProviderTransport::from_authorized_permit(permit, adapter);
    }
    let stored: StoredNativeEnrollment = serde_json::from_slice(payload.as_slice())
        .map_err(|_| "credential_recovery_required")?;
    let (_, native_enrollment, source_generation_fingerprint) = permit
        .native_source()
        .ok_or("invalid_committed_binding")?;
    let adapter: Arc<dyn SdkAdapter> = match (native_enrollment, stored) {
        (
            crate::auth_broker::NativeEnrollmentSpecV1::AzureCli {
                resource_endpoint,
                api_version,
            },
            StoredNativeEnrollment::AzureCli { executable_path },
        ) if api_version == "v1" => {
            let path = PathBuf::from(executable_path);
            let availability = NativeChainInspector::azure_cli(&path);
            if !availability.available
                || availability.source_generation_fingerprint
                    != source_generation_fingerprint
            {
                return Err("native_source_stale");
            }
            Arc::new(AzureOpenAiSdkAdapter::from_cli_program(resource_endpoint, path)?)
        }
        (
            crate::auth_broker::NativeEnrollmentSpecV1::AwsProfile {
                profile_name,
                region,
            },
            StoredNativeEnrollment::AwsLegacySso {
                config_path,
                credentials_path,
                cache_path,
                config_contents,
                credentials_contents,
                start_url,
                sso_region,
                account_id,
                role_name,
                access_token,
                expires_at_epoch_seconds,
                role_steps,
            },
        ) => {
            let snapshot = inspect_aws_profile_snapshot(
                Path::new(&config_path),
                Path::new(&credentials_path),
                profile_name,
            ).map_err(|_| "native_source_stale")?;
            let cache_parent = Path::new(&cache_path).parent().ok_or("native_source_stale")?;
            let cache = inspect_legacy_sso_cache(
                cache_parent,
                &start_url,
                current_epoch_seconds(),
            ).map_err(|_| "native_source_stale")?;
            let combined_fingerprint = digest_safe(format!(
                "{}:{}",
                snapshot.availability.source_generation_fingerprint,
                cache.metadata_fingerprint,
            ).as_bytes());
            let exact_legacy = snapshot.legacy_sso.as_ref().is_some_and(|legacy| {
                legacy.start_url == start_url
                    && legacy.region == sso_region
                    && legacy.account_id == account_id
                    && legacy.role_name == role_name
                    && legacy.role_steps == role_steps
            });
            if combined_fingerprint != source_generation_fingerprint
                || cache.path != PathBuf::from(&cache_path)
                || cache.access_token.as_str() != access_token.as_str()
                || cache.expires_at_epoch_seconds != expires_at_epoch_seconds
                || snapshot.config_contents.as_str() != config_contents
                || snapshot.credentials_contents.as_str() != credentials_contents
                || !exact_legacy
            {
                return Err("native_source_stale");
            }
            Arc::new(AwsLegacySsoSdkAdapter::from_stored(
                profile_name,
                region,
                access_token,
                expires_at_epoch_seconds,
                &sso_region,
                &account_id,
                &role_name,
                role_steps,
                None,
            )?)
        }
        (
            crate::auth_broker::NativeEnrollmentSpecV1::AwsProfile {
                profile_name,
                region,
            },
            StoredNativeEnrollment::AwsProfile {
                config_path,
                credentials_path,
                config_contents,
                credentials_contents,
            },
        ) => {
            let availability = NativeChainInspector::aws_profile(
                Path::new(&config_path),
                Path::new(&credentials_path),
                profile_name,
            );
            if !availability.available
                || availability.source_generation_fingerprint
                    != source_generation_fingerprint
            {
                return Err("native_source_stale");
            }
            Arc::new(AwsBedrockSdkAdapter::from_profile_contents_with_endpoint(
                Zeroizing::new(config_contents),
                Zeroizing::new(credentials_contents),
                profile_name,
                region,
                None,
            )?)
        }
        (
            crate::auth_broker::NativeEnrollmentSpecV1::VertexAdc {
                project_id,
                location,
            },
            StoredNativeEnrollment::VertexAdc { document_path, document },
        ) => {
            let availability = NativeChainInspector::vertex_adc(Path::new(&document_path));
            if !availability.available
                || availability.source_generation_fingerprint
                    != source_generation_fingerprint
            {
                return Err("native_source_stale");
            }
            let document = SecretJson(document);
            Arc::new(VertexSdkAdapter::from_adc_document_with_endpoints(
                document,
                project_id,
                location,
                &permit.model_id,
                None,
                GOOGLE_OAUTH_TOKEN_ENDPOINT,
                GOOGLE_IAM_ORIGIN,
            ).await?)
        }
        _ => return Err("native_source_unsupported"),
    };
    CoreProviderTransport::from_authorized_permit(permit, adapter)
}

#[cfg(unix)]
struct ProviderInFlight {
    handle: String,
    cancel: BoundedCancellation,
    terminal_queued: Arc<(Mutex<bool>, Condvar)>,
}

#[cfg(unix)]
#[derive(Default)]
struct ProviderChannelState {
    handles: HashMap<String, Arc<CoreProviderTransport>>,
    in_flight: HashMap<u64, ProviderInFlight>,
}

#[cfg(unix)]
fn provider_error_code(code: crate::auth_broker::FiniteCode) -> &'static str {
    use crate::auth_broker::FiniteCode;
    match code {
        FiniteCode::OwnerEpochUnavailable | FiniteCode::OwnerEpochChanged => "owner_epoch_unavailable",
        FiniteCode::ConfigurationChanged | FiniteCode::OwnerMismatch => "configuration_changed",
        FiniteCode::CredentialStoreUnavailable => "credential_store_unavailable",
        FiniteCode::CredentialRecoveryRequired => "credential_recovery_required",
        FiniteCode::OperationInterrupted | FiniteCode::OperationCancelled => "operation_interrupted",
        _ => "internal_error",
    }
}

#[cfg(unix)]
fn write_provider_frame(
    stream: &mut std::os::unix::net::UnixStream,
    value: &impl Serialize,
) -> Result<(), ()> {
    use std::io::Write as _;
    let bytes = Zeroizing::new(serde_json::to_vec(value).map_err(|_| ())?);
    if bytes.is_empty() || bytes.len() > PROVIDER_PRIVATE_FRAME_BYTES {
        return Err(());
    }
    let size = u32::try_from(bytes.len()).map_err(|_| ())?;
    stream.write_all(&size.to_be_bytes()).and_then(|_| stream.write_all(&bytes)).map_err(|_| ())
}

#[cfg(unix)]
fn read_provider_request(
    stream: &mut std::os::unix::net::UnixStream,
) -> Result<Option<ProviderWireRequest>, ()> {
    use std::io::Read as _;
    let mut header = [0u8; 4];
    match stream.read(&mut header[..1]) {
        Ok(0) => return Ok(None),
        Ok(1) => {}
        _ => return Err(()),
    }
    stream.read_exact(&mut header[1..]).map_err(|_| ())?;
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > PROVIDER_PRIVATE_FRAME_BYTES { return Err(()); }
    let mut bytes = Zeroizing::new(vec![0u8; size]);
    stream.read_exact(&mut bytes).map_err(|_| ())?;
    serde_json::from_slice(bytes.as_slice()).map(Some).map_err(|_| ())
}

#[cfg(unix)]
fn wait_terminal(terminal: &Arc<(Mutex<bool>, Condvar)>) -> Result<(), ()> {
    let (lock, wake) = &**terminal;
    let queued = lock.lock().map_err(|_| ())?;
    let (queued, _) = wake.wait_timeout_while(
        queued,
        Duration::from_secs(2),
        |queued| !*queued,
    ).map_err(|_| ())?;
    if *queued { Ok(()) } else { Err(()) }
}

/// Closed provider-only inherited channel. The broker is shared with the auth
/// channel; its mutex is held only for permit issue and secure GPC1 reread.
#[cfg(unix)]
pub fn serve_provider_channel<B, T>(
    stream: std::os::unix::net::UnixStream,
    broker: Arc<Mutex<crate::auth_broker::AuthBroker<B, T>>>,
    generation: crate::auth_broker::BackendGeneration,
) -> Result<(), ()>
where
    B: crate::credential::CredentialBackend,
    T: crate::auth_broker::SyntheticAuthTransport,
{
    serve_provider_channel_inner(stream, broker, generation, Vec::new())
}

#[cfg(all(unix, debug_assertions))]
#[doc(hidden)]
pub fn serve_provider_channel_with_test_handles<B, T>(
    stream: std::os::unix::net::UnixStream,
    broker: Arc<Mutex<crate::auth_broker::AuthBroker<B, T>>>,
    generation: crate::auth_broker::BackendGeneration,
    handles: Vec<(String, Arc<CoreProviderTransport>)>,
) -> Result<(), ()>
where
    B: crate::credential::CredentialBackend,
    T: crate::auth_broker::SyntheticAuthTransport,
{
    serve_provider_channel_inner(stream, broker, generation, handles)
}

#[cfg(unix)]
fn serve_provider_channel_inner<B, T>(
    mut stream: std::os::unix::net::UnixStream,
    broker: Arc<Mutex<crate::auth_broker::AuthBroker<B, T>>>,
    generation: crate::auth_broker::BackendGeneration,
    initial_handles: Vec<(String, Arc<CoreProviderTransport>)>,
) -> Result<(), ()>
where
    B: crate::credential::CredentialBackend,
    T: crate::auth_broker::SyntheticAuthTransport,
{
    use std::net::Shutdown;
    use std::sync::mpsc;

    write_provider_frame(&mut stream, &ProviderHandshakeV1::new(
        generation.fingerprint().to_owned(),
    ))?;
    let mut writer = stream.try_clone().map_err(|_| ())?;
    let poison = Arc::new(AtomicBool::new(false));
    let writer_poison = poison.clone();
    let (reply_tx, reply_rx) = mpsc::channel::<ProviderWireReply>();
    let writer_thread = std::thread::spawn(move || {
        for reply in reply_rx {
            if write_provider_frame(&mut writer, &reply).is_err() {
                writer_poison.store(true, Ordering::Release);
                let _ = writer.shutdown(Shutdown::Both);
                break;
            }
        }
    });
    let state = Arc::new(Mutex::new(ProviderChannelState {
        handles: initial_handles.into_iter().collect(),
        in_flight: HashMap::new(),
    }));
    let mut last_request_id = 0u64;
    let result = (|| -> Result<(), ()> {
        loop {
            if poison.load(Ordering::Acquire) { return Err(()); }
            let Some(request) = read_provider_request(&mut stream)? else { return Ok(()); };
            let (version, request_id) = request.version_and_request_id();
            if version != 1 || request_id == 0 || request_id <= last_request_id { return Err(()); }
            last_request_id = request_id;
            match request {
                ProviderWireRequest::Bind {
                    current_generation_fingerprint,
                    binding,
                    ..
                } => {
                    if state.lock().map_err(|_| ())?.handles.len() >= PROVIDER_PRIVATE_MAX_IN_FLIGHT {
                        reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                            operation: ProviderOperationV1::Bind,
                            error_code: "provider_capacity_exceeded" }).map_err(|_| ())?;
                        continue;
                    }
                    let resolved = (|| -> Result<crate::auth_broker::ResolvedProviderPermit, crate::auth_broker::FiniteCode> {
                        let mut authority = broker.lock()
                            .map_err(|_| crate::auth_broker::FiniteCode::InternalError)?;
                        if authority.current_generation_fingerprint()
                            != Some(current_generation_fingerprint.as_str())
                            || current_generation_fingerprint != generation.fingerprint()
                        {
                            Err(crate::auth_broker::FiniteCode::OperationInterrupted)
                        } else {
                            let generation_binding = authority.current_generation_binding()
                                .ok_or(crate::auth_broker::FiniteCode::OperationInterrupted)?;
                            let handle = authority.issue_provider_binding(binding, &generation_binding)?;
                            authority.authorize_provider_source(&handle, &current_generation_fingerprint)
                        }
                    })();
                    let resolved = match resolved {
                        Ok(value) => value,
                        Err(code) => {
                            reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                                operation: ProviderOperationV1::Bind,
                                error_code: provider_error_code(code) }).map_err(|_| ())?;
                            continue;
                        }
                    };
                    let runtime = tokio::runtime::Builder::new_current_thread().enable_all()
                        .build().map_err(|_| ())?;
                    let transport = match runtime.block_on(transport_from_resolved_permit(resolved)) {
                        Ok(value) => Arc::new(value),
                        Err(code) => {
                            let error_code = match code {
                                "native_source_stale" => "native_source_stale",
                                "native_source_unsupported" => "native_source_unsupported",
                                "credential_recovery_required" => "credential_recovery_required",
                                _ => "internal_error",
                            };
                            reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                                operation: ProviderOperationV1::Bind, error_code }).map_err(|_| ())?;
                            continue;
                        }
                    };
                    let handle = transport.opaque_id.as_str().to_owned();
                    state.lock().map_err(|_| ())?.handles.insert(handle.clone(), transport);
                    reply_tx.send(ProviderWireReply::Bound { version: 1, request_id, handle })
                        .map_err(|_| ())?;
                }
                ProviderWireRequest::Execute { handle, call, .. } => {
                    let (transport, call, cancel, terminal) = {
                        let mut state = state.lock().map_err(|_| ())?;
                        let Some(transport) = state.handles.get(&handle).cloned() else {
                            reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                                operation: ProviderOperationV1::Execute,
                                error_code: "provider_handle_not_found" }).map_err(|_| ())?;
                            continue;
                        };
                        if state.in_flight.len() >= PROVIDER_PRIVATE_MAX_IN_FLIGHT {
                            reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                                operation: ProviderOperationV1::Execute,
                                error_code: "provider_capacity_exceeded" }).map_err(|_| ())?;
                            continue;
                        }
                        let call = match call.into_protocol_call(handle.clone()) {
                            Ok(call) => call,
                            Err(_) => {
                                reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                                    operation: ProviderOperationV1::Execute,
                                    error_code: "invalid_bounded_request" }).map_err(|_| ())?;
                                continue;
                            }
                        };
                        let cancel = BoundedCancellation::new();
                        let terminal = Arc::new((Mutex::new(false), Condvar::new()));
                        state.in_flight.insert(request_id, ProviderInFlight {
                            handle: handle.clone(), cancel: cancel.clone(), terminal_queued: terminal.clone(),
                        });
                        (transport, call, cancel, terminal)
                    };
                    let worker_state = state.clone();
                    let worker_tx = reply_tx.clone();
                    std::thread::spawn(move || {
                        let result = tokio::runtime::Builder::new_current_thread().enable_all().build()
                            .map(|runtime| runtime.block_on(transport.execute(call, &cancel)))
                            .unwrap_or_else(|_| SafeProviderResult::failure("provider_error"));
                        let _ = worker_tx.send(ProviderWireReply::Result {
                            version: 1, request_id, handle, result,
                        });
                        if let Ok(mut current) = worker_state.lock() { current.in_flight.remove(&request_id); }
                        let (queued, wake) = &*terminal;
                        if let Ok(mut queued) = queued.lock() { *queued = true; wake.notify_all(); }
                    });
                }
                ProviderWireRequest::Cancel { handle, target_request_id, .. } => {
                    let terminal = {
                        let state = state.lock().map_err(|_| ())?;
                        let Some(target) = state.in_flight.get(&target_request_id) else {
                            reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                                operation: ProviderOperationV1::Cancel,
                                error_code: "provider_request_not_found" }).map_err(|_| ())?;
                            continue;
                        };
                        if target.handle != handle {
                            reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                                operation: ProviderOperationV1::Cancel,
                                error_code: "provider_request_not_found" }).map_err(|_| ())?;
                            continue;
                        }
                        target.cancel.cancel();
                        target.terminal_queued.clone()
                    };
                    wait_terminal(&terminal)?;
                    reply_tx.send(ProviderWireReply::Cancelled { version: 1, request_id,
                        handle, target_request_id }).map_err(|_| ())?;
                }
                ProviderWireRequest::Release { handle, .. } => {
                    let terminals = {
                        let state = state.lock().map_err(|_| ())?;
                        if !state.handles.contains_key(&handle) {
                            reply_tx.send(ProviderWireReply::Error { version: 1, request_id,
                                operation: ProviderOperationV1::Release,
                                error_code: "provider_handle_not_found" }).map_err(|_| ())?;
                            continue;
                        }
                        state.in_flight.values().filter(|item| item.handle == handle).map(|item| {
                            item.cancel.cancel();
                            item.terminal_queued.clone()
                        }).collect::<Vec<_>>()
                    };
                    for terminal in terminals { wait_terminal(&terminal)?; }
                    state.lock().map_err(|_| ())?.handles.remove(&handle);
                    reply_tx.send(ProviderWireReply::Released { version: 1, request_id, handle })
                        .map_err(|_| ())?;
                }
            }
        }
    })();
    let terminals = {
        let state = state.lock().map_err(|_| ())?;
        state.in_flight.values().map(|item| {
            item.cancel.cancel();
            item.terminal_queued.clone()
        }).collect::<Vec<_>>()
    };
    for terminal in terminals { let _ = wait_terminal(&terminal); }
    if let Ok(mut state) = state.lock() { state.handles.clear(); state.in_flight.clear(); }
    drop(reply_tx);
    let _ = stream.shutdown(Shutdown::Both);
    let _ = writer_thread.join();
    result
}

#[derive(Clone, Debug, Default)]
pub struct BoundedCancellation {
    cancelled: Arc<AtomicBool>,
    notify: Arc<Notify>,
}

impl BoundedCancellation {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
        self.notify.notify_waiters();
    }

    async fn cancelled(&self) {
        loop {
            let notified = self.notify.notified();
            if self.cancelled.load(Ordering::Acquire) {
                return;
            }
            notified.await;
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FixedRequestSpec {
    pub method: &'static str,
    pub endpoint: String,
    pub protocol: ProviderProtocol,
}

pub fn fixed_request_spec(
    protocol: ProviderProtocol,
    model_id: &str,
    public_fields: &Value,
    request: &BoundedProviderRequest,
) -> Result<FixedRequestSpec, &'static str> {
    if !valid_provider_model_id(protocol, model_id)
        || request.primary_input().is_empty()
        || request.primary_input().len() > MAX_INPUT_BYTES
        || request.max_output_tokens == 0
        || request.max_output_tokens > MAX_OUTPUT_TOKENS
    {
        return Err("invalid_bounded_request");
    }
    let fields = public_fields.as_object().ok_or("invalid_public_fields")?;
    let endpoint = match protocol {
        ProviderProtocol::AzureOpenAiResponsesV1 => {
            exact_keys(fields, &["api_version", "resource_endpoint"])?;
            if fields.get("api_version").and_then(Value::as_str) != Some("v1") {
                return Err("invalid_public_fields");
            }
            let root = fields
                .get("resource_endpoint")
                .and_then(Value::as_str)
                .ok_or("invalid_public_fields")?;
            validate_azure_root(root)?;
            let root = root.trim_end_matches('/');
            format!("{root}/openai/v1/responses?api-version=v1")
        }
        ProviderProtocol::AwsBedrockConverse => {
            exact_keys(fields, &["profile_name", "region"])?;
            let region = fields.get("region").and_then(Value::as_str).ok_or("invalid_public_fields")?;
            let profile = fields.get("profile_name").and_then(Value::as_str).ok_or("invalid_public_fields")?;
            if !valid_aws_region(region) || !bounded_identifier(profile) {
                return Err("invalid_public_fields");
            }
            let model_label = url::form_urlencoded::byte_serialize(model_id.as_bytes())
                .collect::<String>();
            format!("https://bedrock-runtime.{region}.amazonaws.com/model/{model_label}/converse")
        }
        ProviderProtocol::VertexGenerateContentV1 => {
            exact_keys(fields, &["location", "project_id"])?;
            let project = fields.get("project_id").and_then(Value::as_str).ok_or("invalid_public_fields")?;
            let location = fields.get("location").and_then(Value::as_str).ok_or("invalid_public_fields")?;
            validate_vertex_ids(project, location, model_id)?;
            let host = if location == "global" {
                "aiplatform.googleapis.com".to_owned()
            } else {
                format!("{location}-aiplatform.googleapis.com")
            };
            format!("https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model_id}:generateContent")
        }
    };
    Ok(FixedRequestSpec {
        method: "POST",
        endpoint,
        protocol,
    })
}

pub fn bedrock_converse_input(
    model_id: &str,
    request: &BoundedProviderRequest,
) -> Result<aws_sdk_bedrockruntime::operation::converse::ConverseInput, &'static str> {
    if !bounded_model_id(model_id) || request.max_output_tokens > i32::MAX as u32 {
        return Err("invalid_bounded_request");
    }
    use aws_sdk_bedrockruntime::types::{ContentBlock, ConversationRole, ImageBlock,
        ImageFormat, ImageSource, InferenceConfiguration, Message, SystemContentBlock,
        Tool, ToolConfiguration, ToolInputSchema, ToolResultBlock, ToolResultContentBlock,
        ToolSpecification, ToolUseBlock};
    let mut messages = Vec::new();
    for projected in &request.messages {
        let role = match projected.role { ProviderMessageRole::User => ConversationRole::User,
            ProviderMessageRole::Assistant => ConversationRole::Assistant };
        messages.push(Message::builder().role(role)
            .content(ContentBlock::Text(projected.content.clone())).build()
            .map_err(|_| "invalid_bounded_request")?);
    }
    if let Some(state) = &request.continuation {
        if let ProviderContinuationState::BedrockConverse { assistant_text, tool_calls } = &state.provider_state {
            let mut builder = Message::builder().role(ConversationRole::Assistant);
            if !assistant_text.is_empty() { builder = builder.content(ContentBlock::Text(assistant_text.clone())); }
            for call in tool_calls {
                let block = ToolUseBlock::builder().tool_use_id(&call.call_id).name(&call.name)
                    .input(json_to_document(&call.arguments)?).build().map_err(|_| "invalid_continuation")?;
                builder = builder.content(ContentBlock::ToolUse(block));
            }
            messages.push(builder.build().map_err(|_| "invalid_continuation")?);
            let mut results = Message::builder().role(ConversationRole::User);
            for observation in &request.observations {
                let mut block = ToolResultBlock::builder().tool_use_id(&observation.call_id)
                    .content(ToolResultContentBlock::Text(observation.text.clone()));
                if let Some(image) = &observation.image {
                    let bytes = image.decoded()?;
                    if bytes.len() > 3_750_000 { return Err("unsupported_option"); }
                    let format = match image.mime_type.as_str() {
                        "image/png" => ImageFormat::Png, "image/jpeg" => ImageFormat::Jpeg,
                        _ => return Err("unsupported_option"),
                    };
                    let image = ImageBlock::builder().format(format)
                        .source(ImageSource::Bytes(aws_sdk_bedrockruntime::primitives::Blob::new(bytes.to_vec())))
                        .build().map_err(|_| "invalid_continuation")?;
                    block = block.content(ToolResultContentBlock::Image(image));
                }
                let block = block.build().map_err(|_| "invalid_continuation")?;
                results = results.content(ContentBlock::ToolResult(block));
            }
            messages.push(results.build().map_err(|_| "invalid_continuation")?);
        }
    }
    if let Some(vision) = &request.vision {
        let bytes = vision.image.decoded()?;
        if bytes.len() > 3_750_000 { return Err("unsupported_option"); }
        let format = match vision.image.mime_type.as_str() {
            "image/png" => ImageFormat::Png, "image/jpeg" => ImageFormat::Jpeg,
            _ => return Err("unsupported_option"),
        };
        let image = ImageBlock::builder().format(format)
            .source(ImageSource::Bytes(aws_sdk_bedrockruntime::primitives::Blob::new(bytes.to_vec())))
            .build().map_err(|_| "invalid_bounded_request")?;
        messages.push(Message::builder().role(ConversationRole::User)
            .content(ContentBlock::Text(vision.prompt.clone())).content(ContentBlock::Image(image))
            .build().map_err(|_| "invalid_bounded_request")?);
    }
    let mut inference = InferenceConfiguration::builder().max_tokens(request.max_output_tokens as i32);
    if let Some(temperature) = request.temperature { inference = inference.temperature(temperature as f32); }
    let inference = inference.build();
    let mut builder = aws_sdk_bedrockruntime::operation::converse::ConverseInput::builder()
        .model_id(model_id)
        .set_messages(Some(messages)).inference_config(inference);
    if !request.system.is_empty() { builder = builder.system(SystemContentBlock::Text(request.system.clone())); }
    if !request.tools.is_empty() {
        let mut config = ToolConfiguration::builder();
        for definition in &request.tools {
            let specification = ToolSpecification::builder().name(&definition.wire_name)
                .description(&definition.description)
                .input_schema(ToolInputSchema::Json(json_to_document(&definition.parameters)?))
                .build().map_err(|_| "invalid_bounded_request")?;
            config = config.tools(Tool::ToolSpec(specification));
        }
        builder = builder.tool_config(config.build().map_err(|_| "invalid_bounded_request")?);
    }
    builder.build().map_err(|_| "invalid_bounded_request")
}

pub fn vertex_generate_content_request(
    project: &str,
    location: &str,
    model_id: &str,
    request: &BoundedProviderRequest,
) -> Result<GenerateContentRequest, &'static str> {
    validate_vertex_ids(project, location, model_id)?;
    use google_cloud_aiplatform_v1::model::{Blob, FunctionCall, FunctionDeclaration,
        FunctionResponse, FunctionResponseBlob, FunctionResponsePart, Tool};
    let mut contents = request.messages.iter().map(|message| Content::new()
        .set_role(match message.role { ProviderMessageRole::User => "user", ProviderMessageRole::Assistant => "model" })
        .set_parts([Part::new().set_text(message.content.clone())])).collect::<Vec<_>>();
    if let Some(state) = &request.continuation {
        if let ProviderContinuationState::VertexGenerateContent { assistant_text, tool_calls } = &state.provider_state {
            let mut parts = Vec::new();
            if !assistant_text.is_empty() { parts.push(Part::new().set_text(assistant_text.clone())); }
            for call in tool_calls {
                let mut part = Part::new().set_function_call(serde_json::from_value::<FunctionCall>(
                    serde_json::json!({"name":call.name,"args":call.arguments})
                ).map_err(|_| "invalid_continuation")?);
                if let Some(signature) = &call.thought_signature {
                    let decoded = base64::engine::general_purpose::STANDARD.decode(signature)
                        .map_err(|_| "invalid_continuation")?;
                    part = part.set_thought_signature(decoded);
                }
                parts.push(part);
            }
            contents.push(Content::new().set_role("model").set_parts(parts));
            let mut response_parts = Vec::new();
            for observation in &request.observations {
                let mut response = serde_json::from_value::<FunctionResponse>(serde_json::json!({
                    "name": state.pending_call_ids.iter().position(|id| id == &observation.call_id)
                        .and_then(|index| tool_calls.get(index)).map(|call| call.name.as_str()).unwrap_or("unknown"),
                    "response":{"state":observation.state,"text":observation.text}
                })).map_err(|_| "invalid_continuation")?;
                if let Some(image) = &observation.image {
                    let bytes = image.decoded()?;
                    response = response.set_parts([FunctionResponsePart::new().set_inline_data(
                        FunctionResponseBlob::new().set_mime_type(&image.mime_type).set_data(bytes.to_vec())
                    )]);
                }
                response_parts.push(Part::new().set_function_response(response));
            }
            contents.push(Content::new().set_role("user").set_parts(response_parts));
        }
    }
    if let Some(vision) = &request.vision {
        let bytes = vision.image.decoded()?;
        contents.push(Content::new().set_role("user").set_parts([
            Part::new().set_text(vision.prompt.clone()),
            Part::new().set_inline_data(Blob::new().set_mime_type(&vision.image.mime_type).set_data(bytes.to_vec())),
        ]));
    }
    let mut config = GenerationConfig::new().set_max_output_tokens(request.max_output_tokens as i32);
    if let Some(temperature) = request.temperature { config = config.set_temperature(temperature as f32); }
    let mut result = GenerateContentRequest::new()
        .set_model(format!(
            "projects/{project}/locations/{location}/publishers/google/models/{model_id}"
        ))
        .set_contents(contents).set_generation_config(config);
    if !request.system.is_empty() {
        result = result.set_system_instruction(Content::new().set_role("system")
            .set_parts([Part::new().set_text(request.system.clone())]));
    }
    if !request.tools.is_empty() {
        let declarations = request.tools.iter().map(|definition| {
            serde_json::from_value::<FunctionDeclaration>(serde_json::json!({
                "name":definition.wire_name,"description":definition.description,
                "parametersJsonSchema":definition.parameters
            })).map_err(|_| "invalid_bounded_request")
        }).collect::<Result<Vec<_>, _>>()?;
        result = result.set_tools([Tool::new().set_function_declarations(declarations)]);
    }
    Ok(result)
}

fn json_to_document(value: &Value) -> Result<aws_smithy_types::Document, &'static str> {
    use aws_smithy_types::{Document, Number};
    Ok(match value {
        Value::Null => Document::Null,
        Value::Bool(value) => Document::Bool(*value),
        Value::String(value) => Document::String(value.clone()),
        Value::Number(value) if value.as_i64().is_some() => Document::Number(Number::NegInt(value.as_i64().unwrap())),
        Value::Number(value) if value.as_u64().is_some() => Document::Number(Number::PosInt(value.as_u64().unwrap())),
        Value::Number(value) if value.as_f64().is_some() => Document::Number(Number::Float(value.as_f64().unwrap())),
        Value::Array(values) => Document::Array(values.iter().map(json_to_document).collect::<Result<_, _>>()?),
        Value::Object(values) => Document::Object(values.iter().map(|(key, value)|
            Ok((key.clone(), json_to_document(value)?))).collect::<Result<_, &'static str>>()?),
        _ => return Err("invalid_bounded_request"),
    })
}

fn document_to_json(value: &aws_smithy_types::Document) -> Result<Value, &'static str> {
    use aws_smithy_types::{Document, Number};
    Ok(match value {
        Document::Null => Value::Null,
        Document::Bool(value) => Value::Bool(*value),
        Document::String(value) => Value::String(value.clone()),
        Document::Number(Number::NegInt(value)) => serde_json::json!(*value),
        Document::Number(Number::PosInt(value)) => serde_json::json!(*value),
        Document::Number(Number::Float(value)) if value.is_finite() => serde_json::json!(*value),
        Document::Number(_) => return Err("malformed_provider_response"),
        Document::Array(values) => Value::Array(values.iter().map(document_to_json).collect::<Result<_, _>>()?),
        Document::Object(values) => Value::Object(values.iter().map(|(key, value)|
            Ok((key.clone(), document_to_json(value)?))).collect::<Result<_, &'static str>>()?),
    })
}

fn continuation_for(
    request: &BoundedProviderRequest,
    response_id: &str,
    calls: &[SafeProviderToolCall],
    provider_state: ProviderContinuationState,
) -> Option<ProviderContinuation> {
    if calls.is_empty() { return None; }
    Some(ProviderContinuation {
        provider_kind: request.scope.provider_kind.clone(), protocol: request.protocol,
        connection_id: request.scope.connection_id, model_id: request.scope.model_id.clone(),
        base_url: request.scope.base_url.clone(), room_id: request.scope.room_id,
        agent_id: request.scope.agent_id, watermark_seq: request.scope.watermark_seq,
        descriptor_revision: request.scope.descriptor_revision, response_id: response_id.to_owned(),
        pending_call_ids: calls.iter().map(|call| call.call_id.clone()).collect(), provider_state,
    })
}

fn declared_tool(request: &BoundedProviderRequest, name: &str) -> bool {
    request.tools.iter().any(|tool| tool.wire_name == name)
}

fn azure_responses_body(call: &ProtocolCall) -> Result<Value, &'static str> {
    let request = &call.request;
    let mut input = request.messages.iter().map(|message| serde_json::json!({
        "role": match message.role { ProviderMessageRole::User => "user", ProviderMessageRole::Assistant => "assistant" },
        "content":[{"type":"input_text","text":message.content}]
    })).collect::<Vec<_>>();
    if let Some(vision) = &request.vision {
        vision.image.decoded()?;
        input.push(serde_json::json!({"role":"user","content":[
            {"type":"input_text","text":vision.prompt},
            {"type":"input_image","image_url":format!("data:{};base64,{}", vision.image.mime_type, vision.image.data_base64)}
        ]}));
    }
    let mut body = serde_json::json!({"model":call.model_id,"input":input,
        "max_output_tokens":request.max_output_tokens,"stream":false});
    if !request.system.is_empty() { body["instructions"] = Value::String(request.system.clone()); }
    if let Some(temperature) = request.temperature { body["temperature"] = serde_json::json!(temperature); }
    if !request.tools.is_empty() {
        body["tools"] = Value::Array(request.tools.iter().map(|tool| serde_json::json!({
            "type":"function","name":tool.wire_name,"description":tool.description,
            "parameters":tool.parameters,"strict":false
        })).collect());
    }
    if let Some(continuation) = &request.continuation {
        let ProviderContinuationState::AzureResponses { previous_response_id } = &continuation.provider_state
            else { return Err("invalid_continuation"); };
        body["previous_response_id"] = Value::String(previous_response_id.clone());
        if request.observations.iter().any(|observation| observation.image.is_some()) {
            return Err("unsupported_option");
        }
        body["input"] = Value::Array(request.observations.iter().map(|observation| serde_json::json!({
            "type":"function_call_output","call_id":observation.call_id,"output":observation.text
        })).collect());
    }
    Ok(body)
}

fn parse_azure_result(request: &BoundedProviderRequest, model: &str, value: &Value) -> SafeProviderResult {
    let Some(response_id) = value.get("id").and_then(Value::as_str) else {
        return SafeProviderResult::failure("malformed_provider_response");
    };
    let mut text = String::new();
    let mut calls = Vec::new();
    let Some(output) = value.get("output").and_then(Value::as_array) else {
        return SafeProviderResult::failure("malformed_provider_response");
    };
    for item in output {
        match item.get("type").and_then(Value::as_str) {
            Some("message") => {
                if let Some(parts) = item.get("content").and_then(Value::as_array) {
                    for part in parts {
                        if part.get("type").and_then(Value::as_str) == Some("output_text") {
                            if let Some(value) = part.get("text").and_then(Value::as_str) { text.push_str(value); }
                        }
                    }
                }
            }
            Some("function_call") => {
                let Some(call_id) = item.get("call_id").and_then(Value::as_str) else { return SafeProviderResult::failure("malformed_provider_response"); };
                let Some(name) = item.get("name").and_then(Value::as_str) else { return SafeProviderResult::failure("malformed_provider_response"); };
                if !declared_tool(request, name) { return SafeProviderResult::failure("malformed_provider_response"); }
                let Some(arguments) = item.get("arguments").and_then(Value::as_str)
                    .and_then(|raw| serde_json::from_str::<Value>(raw).ok()) else { return SafeProviderResult::failure("malformed_provider_response"); };
                calls.push(SafeProviderToolCall { call_id: call_id.to_owned(), name: name.to_owned(), arguments });
            }
            _ => {}
        }
    }
    let usage = value.get("usage");
    let continuation = continuation_for(request, response_id, &calls,
        ProviderContinuationState::AzureResponses { previous_response_id: response_id.to_owned() });
    SafeProviderResult::success_contract(response_id.to_owned(), model.to_owned(),
        usage.and_then(|v| v.get("input_tokens")).and_then(Value::as_u64).and_then(|v| u32::try_from(v).ok()),
        usage.and_then(|v| v.get("output_tokens")).and_then(Value::as_u64).and_then(|v| u32::try_from(v).ok()),
        text, calls, continuation)
}

pub fn azure_cli_credential(
    executor: Arc<dyn Executor>,
) -> azure_core::Result<Arc<AzureCliCredential>> {
    AzureCliCredential::new(Some(AzureCliCredentialOptions {
        subscription: None,
        tenant_id: None,
        executor: Some(executor),
    }))
}

enum AzureOpenAiAuth {
    Cli(Arc<dyn TokenCredential>),
    ApiKey(Zeroizing<Vec<u8>>),
}

pub struct AzureOpenAiSdkAdapter {
    auth: AzureOpenAiAuth,
    client: reqwest::Client,
    resource_endpoint: String,
    request_origin_override: Option<String>,
}

impl std::fmt::Debug for AzureOpenAiSdkAdapter {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AzureOpenAiSdkAdapter")
            .field("auth", &"<private>")
            .field("resource_endpoint", &self.resource_endpoint)
            .finish_non_exhaustive()
    }
}

impl AzureOpenAiSdkAdapter {
    pub fn from_cli(resource_endpoint: &str) -> Result<Self, &'static str> {
        let credential = azure_cli_credential(Arc::new(BoundedAzureCliExecutor::default()))
            .map_err(|_| "native_source_unavailable")?;
        Self::with_credential(resource_endpoint, credential)
    }

    fn from_cli_program(
        resource_endpoint: &str,
        program: PathBuf,
    ) -> Result<Self, &'static str> {
        let credential = azure_cli_credential(Arc::new(
            BoundedAzureCliExecutor::with_program(Duration::from_secs(30), program)?,
        )).map_err(|_| "native_source_unavailable")?;
        Self::with_credential(resource_endpoint, credential)
    }

    pub fn with_credential(
        resource_endpoint: &str,
        credential: Arc<dyn TokenCredential>,
    ) -> Result<Self, &'static str> {
        Self::with_auth(resource_endpoint, AzureOpenAiAuth::Cli(credential))
    }

    pub fn with_api_key(
        resource_endpoint: &str,
        api_key: Zeroizing<Vec<u8>>,
    ) -> Result<Self, &'static str> {
        if api_key.is_empty() || !api_key.iter().all(|byte| matches!(byte, 0x21..=0x7e)) {
            return Err("invalid_committed_binding");
        }
        Self::with_auth(resource_endpoint, AzureOpenAiAuth::ApiKey(api_key))
    }

    fn with_auth(
        resource_endpoint: &str,
        auth: AzureOpenAiAuth,
    ) -> Result<Self, &'static str> {
        validate_azure_root(resource_endpoint)?;
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| "provider_unavailable")?;
        Ok(Self {
            auth,
            client,
            resource_endpoint: resource_endpoint.trim_end_matches('/').to_owned(),
            request_origin_override: None,
        })
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn with_test_endpoint(
        resource_endpoint: &str,
        credential: Arc<dyn TokenCredential>,
        request_origin: &str,
    ) -> Result<Self, &'static str> {
        let mut value = Self::with_credential(resource_endpoint, credential)?;
        value.request_origin_override = Some(request_origin.trim_end_matches('/').to_owned());
        Ok(value)
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn with_test_api_key_endpoint(
        resource_endpoint: &str,
        api_key: Zeroizing<Vec<u8>>,
        request_origin: &str,
    ) -> Result<Self, &'static str> {
        let mut value = Self::with_api_key(resource_endpoint, api_key)?;
        value.request_origin_override = Some(request_origin.trim_end_matches('/').to_owned());
        Ok(value)
    }

    pub fn endpoint(&self) -> &str {
        &self.resource_endpoint
    }
}

#[async_trait]
impl SdkAdapter for AzureOpenAiSdkAdapter {
    fn protocol(&self) -> ProviderProtocol {
        ProviderProtocol::AzureOpenAiResponsesV1
    }

    async fn execute(&self, call: &ProtocolCall) -> SafeProviderResult {
        let spec = match fixed_request_spec(
            ProviderProtocol::AzureOpenAiResponsesV1,
            &call.model_id,
            &call.public_fields,
            &call.request,
        ) {
            Ok(value) if value.endpoint.starts_with(&self.resource_endpoint) => value,
            _ => return SafeProviderResult::failure("configuration_changed"),
        };
        let request_endpoint = self
            .request_origin_override
            .as_ref()
            .and_then(|origin| {
                spec.endpoint
                    .strip_prefix(&self.resource_endpoint)
                    .map(|suffix| format!("{origin}{suffix}"))
            })
            .unwrap_or(spec.endpoint);
        let request_body = match azure_responses_body(call) {
            Ok(value) => value,
            Err(code) => return SafeProviderResult::failure(code),
        };
        let request = self.client.post(request_endpoint).json(&request_body);
        let request = match &self.auth {
            AzureOpenAiAuth::Cli(credential) => {
                let token = match credential
                    .get_token(&[AZURE_SCOPE], None::<TokenRequestOptions<'_>>)
                    .await
                {
                    Ok(value) => value,
                    Err(_) => {
                        return SafeProviderResult::failure("auth_expired_or_revoked")
                    }
                };
                request.bearer_auth(token.token.secret())
            }
            AzureOpenAiAuth::ApiKey(api_key) => request.header("api-key", api_key.as_slice()),
        };
        if !call.try_admit_model_request() {
            return SafeProviderResult::failure("provider_request_budget_exhausted");
        }
        let mut response = match request.send().await {
            Ok(value) => value,
            Err(error) if error.is_timeout() => {
                return SafeProviderResult::failure("provider_unavailable")
            }
            Err(_) => return SafeProviderResult::failure("provider_error"),
        };
        if !response.status().is_success() {
            return SafeProviderResult::failure(match response.status().as_u16() {
                401 => "auth_expired_or_revoked",
                403 => "permission_denied",
                404 => "resource_not_found",
                429 => "rate_limited",
                500..=599 => "provider_unavailable",
                _ => "provider_error",
            });
        }
        let mut body = Zeroizing::new(Vec::new());
        loop {
            match response.chunk().await {
                Ok(Some(chunk)) if body.len() + chunk.len() <= 1_048_576 => {
                    body.extend_from_slice(&chunk)
                }
                Ok(Some(_)) => return SafeProviderResult::failure("malformed_provider_response"),
                Ok(None) => break,
                Err(_) => return SafeProviderResult::failure("malformed_provider_response"),
            }
        }
        let value = match SecretJson::parse(body.as_slice()) {
            Ok(value) => value,
            Err(_) => return SafeProviderResult::failure("malformed_provider_response"),
        };
        parse_azure_result(&call.request, &call.model_id, &value.0)
    }
}

#[derive(Clone, Default)]
struct AwsLegacyTestEndpoints {
    sso: Option<String>,
    sts: Option<String>,
    bedrock: Option<String>,
}

pub struct AwsLegacySsoSdkAdapter {
    profile_name: String,
    region: String,
    sso_region: String,
    account_id: String,
    role_name: String,
    access_token: Zeroizing<String>,
    expires_at_epoch_seconds: i64,
    role_steps: Vec<AwsRoleStep>,
    endpoints: AwsLegacyTestEndpoints,
}

impl std::fmt::Debug for AwsLegacySsoSdkAdapter {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AwsLegacySsoSdkAdapter")
            .field("profile_name", &self.profile_name)
            .field("region", &self.region)
            .field("sso_region", &self.sso_region)
            .field("role_steps", &self.role_steps.len())
            .finish_non_exhaustive()
    }
}

impl AwsLegacySsoSdkAdapter {
    fn from_stored(
        profile_name: &str,
        region: &str,
        access_token: Zeroizing<String>,
        expires_at_epoch_seconds: i64,
        sso_region: &str,
        account_id: &str,
        role_name: &str,
        role_steps: Vec<AwsRoleStep>,
        endpoints: Option<AwsLegacyTestEndpoints>,
    ) -> Result<Self, &'static str> {
        if !bounded_identifier(profile_name)
            || !valid_aws_region(region)
            || !valid_aws_region(sso_region)
            || !valid_aws_account_id(account_id)
            || !valid_aws_role_name(role_name)
            || access_token.is_empty()
            || expires_at_epoch_seconds <= current_epoch_seconds()
            || role_steps.iter().any(|step| {
                !valid_role_arn(&step.role_arn)
                    || step.external_id.as_deref().is_some_and(|value| !valid_external_id(value))
                    || !valid_role_session_name(&step.role_session_name)
                    || step.duration_seconds.is_some_and(|value| !(900..=43_200).contains(&value))
            })
        {
            return Err("native_source_unsupported");
        }
        Ok(Self {
            profile_name: profile_name.to_owned(),
            region: region.to_owned(),
            sso_region: sso_region.to_owned(),
            account_id: account_id.to_owned(),
            role_name: role_name.to_owned(),
            access_token,
            expires_at_epoch_seconds,
            role_steps,
            endpoints: endpoints.unwrap_or_default(),
        })
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn from_profile_files_with_test_endpoints(
        config_path: &Path,
        credentials_path: &Path,
        cache_directory: &Path,
        profile_name: &str,
        region: &str,
        sso_endpoint: &str,
        sts_endpoint: &str,
        bedrock_endpoint: &str,
    ) -> Result<Self, &'static str> {
        let snapshot = inspect_aws_profile_snapshot(config_path, credentials_path, profile_name)
            .map_err(|_| "native_source_unsupported")?;
        let legacy = snapshot.legacy_sso.ok_or("native_source_unsupported")?;
        let cache = inspect_legacy_sso_cache(
            cache_directory,
            &legacy.start_url,
            current_epoch_seconds(),
        ).map_err(|_| "native_source_unavailable")?;
        Self::from_stored(
            profile_name,
            region,
            cache.access_token,
            cache.expires_at_epoch_seconds,
            &legacy.region,
            &legacy.account_id,
            &legacy.role_name,
            legacy.role_steps,
            Some(AwsLegacyTestEndpoints {
                sso: Some(sso_endpoint.to_owned()),
                sts: Some(sts_endpoint.to_owned()),
                bedrock: Some(bedrock_endpoint.to_owned()),
            }),
        )
    }

    async fn selected_credentials(&self) -> Result<aws_credential_types::Credentials, &'static str> {
        if self.expires_at_epoch_seconds <= current_epoch_seconds() {
            return Err("native_source_unavailable");
        }
        let timeout = aws_sdk_sso::config::timeout::TimeoutConfig::builder()
            .operation_timeout(Duration::from_secs(20))
            .operation_attempt_timeout(Duration::from_secs(15))
            .build();
        let retry = aws_sdk_sso::config::retry::RetryConfig::standard().with_max_attempts(1);
        let mut config = aws_sdk_sso::Config::builder()
            .behavior_version_latest()
            .region(aws_sdk_sso::config::Region::new(self.sso_region.clone()))
            .timeout_config(timeout)
            .retry_config(retry);
        if let Some(endpoint) = self.endpoints.sso.as_deref() {
            config = config.endpoint_url(endpoint);
        }
        let client = aws_sdk_sso::Client::from_conf(config.build());
        let result = tokio::time::timeout(
            Duration::from_secs(20),
            client.get_role_credentials()
                .role_name(self.role_name.clone())
                .account_id(self.account_id.clone())
                .access_token(self.access_token.as_str())
                .send(),
        ).await.map_err(|_| "provider_unavailable")?
            .map_err(|_| "provider_error")?;
        let role = result.role_credentials().ok_or("provider_error")?;
        let access_key = role.access_key_id().filter(|value| !value.is_empty())
            .ok_or("provider_error")?;
        let secret_key = role.secret_access_key().filter(|value| !value.is_empty())
            .ok_or("provider_error")?;
        let session_token = role.session_token().filter(|value| !value.is_empty())
            .ok_or("provider_error")?;
        let expiration_millis = role.expiration();
        if expiration_millis <= current_epoch_seconds().saturating_mul(1_000) {
            return Err("provider_error");
        }
        let expiration = UNIX_EPOCH.checked_add(Duration::from_millis(
            u64::try_from(expiration_millis).map_err(|_| "provider_error")?,
        )).ok_or("provider_error")?;
        let mut credentials = aws_credential_types::Credentials::new(
            access_key.to_owned(),
            secret_key.to_owned(),
            Some(session_token.to_owned()),
            Some(expiration),
            "GamjaSelectedLegacySso",
        );
        for step in &self.role_steps {
            let timeout = aws_sdk_sts::config::timeout::TimeoutConfig::builder()
                .operation_timeout(Duration::from_secs(20))
                .operation_attempt_timeout(Duration::from_secs(15))
                .build();
            let retry = aws_sdk_sts::config::retry::RetryConfig::standard().with_max_attempts(1);
            let mut config = aws_sdk_sts::Config::builder()
                .behavior_version_latest()
                .region(aws_sdk_sts::config::Region::new(self.region.clone()))
                .credentials_provider(credentials)
                .timeout_config(timeout)
                .retry_config(retry);
            if let Some(endpoint) = self.endpoints.sts.as_deref() {
                config = config.endpoint_url(endpoint);
            }
            let client = aws_sdk_sts::Client::from_conf(config.build());
            let request = client.assume_role()
                .role_arn(step.role_arn.clone())
                .role_session_name(step.role_session_name.clone())
                .set_external_id(step.external_id.clone())
                .set_duration_seconds(step.duration_seconds);
            let result = tokio::time::timeout(Duration::from_secs(20), request.send())
                .await.map_err(|_| "provider_unavailable")?
                .map_err(|_| "provider_error")?;
            let assumed = result.credentials().ok_or("provider_error")?;
            let expiration = SystemTime::try_from(assumed.expiration().clone())
                .map_err(|_| "provider_error")?;
            credentials = aws_credential_types::Credentials::new(
                assumed.access_key_id().to_owned(),
                assumed.secret_access_key().to_owned(),
                Some(assumed.session_token().to_owned()),
                Some(expiration),
                "GamjaSelectedAssumeRole",
            );
        }
        Ok(credentials)
    }
}

#[async_trait]
impl SdkAdapter for AwsLegacySsoSdkAdapter {
    fn protocol(&self) -> ProviderProtocol { ProviderProtocol::AwsBedrockConverse }

    async fn execute(&self, call: &ProtocolCall) -> SafeProviderResult {
        if call.public_fields != serde_json::json!({
            "profile_name": self.profile_name,
            "region": self.region,
        }) {
            return SafeProviderResult::failure("configuration_changed");
        }
        let credentials = match self.selected_credentials().await {
            Ok(value) => value,
            Err(code) => return SafeProviderResult::failure(code),
        };
        let adapter = match AwsBedrockSdkAdapter::from_explicit_credentials(
            credentials,
            &self.profile_name,
            &self.region,
            self.endpoints.bedrock.as_deref(),
        ) {
            Ok(value) => value,
            Err(code) => return SafeProviderResult::failure(code),
        };
        adapter.execute(call).await
    }
}

pub struct AwsBedrockSdkAdapter {
    client: aws_sdk_bedrockruntime::Client,
    profile_name: String,
    region: String,
}

impl std::fmt::Debug for AwsBedrockSdkAdapter {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AwsBedrockSdkAdapter")
            .field("profile_name", &self.profile_name)
            .field("region", &self.region)
            .finish_non_exhaustive()
    }
}

impl AwsBedrockSdkAdapter {
    fn from_explicit_credentials(
        credentials: aws_credential_types::Credentials,
        profile_name: &str,
        region: &str,
        endpoint: Option<&str>,
    ) -> Result<Self, &'static str> {
        if !valid_aws_region(region) || !bounded_identifier(profile_name) {
            return Err("native_source_unsupported");
        }
        let timeout = aws_sdk_bedrockruntime::config::timeout::TimeoutConfig::builder()
            .operation_timeout(Duration::from_secs(30))
            .operation_attempt_timeout(Duration::from_secs(20))
            .build();
        let retry = aws_sdk_bedrockruntime::config::retry::RetryConfig::standard()
            .with_max_attempts(2);
        let mut config = aws_sdk_bedrockruntime::Config::builder()
            .behavior_version_latest()
            .region(aws_sdk_bedrockruntime::config::Region::new(region.to_owned()))
            .credentials_provider(credentials)
            .timeout_config(timeout)
            .retry_config(retry);
        if let Some(endpoint) = endpoint {
            config = config.endpoint_url(endpoint);
        }
        Ok(Self {
            client: aws_sdk_bedrockruntime::Client::from_conf(config.build()),
            profile_name: profile_name.to_owned(),
            region: region.to_owned(),
        })
    }

    #[allow(deprecated)]
    pub fn from_profile_files(
        config_path: &Path,
        credentials_path: &Path,
        profile_name: &str,
        region: &str,
    ) -> Result<Self, &'static str> {
        Self::from_profile_files_with_endpoint(
            config_path,
            credentials_path,
            profile_name,
            region,
            None,
        )
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn from_profile_files_with_test_endpoint(
        config_path: &Path,
        credentials_path: &Path,
        profile_name: &str,
        region: &str,
        endpoint: &str,
    ) -> Result<Self, &'static str> {
        Self::from_profile_files_with_endpoint(
            config_path,
            credentials_path,
            profile_name,
            region,
            Some(endpoint),
        )
    }

    #[allow(deprecated)]
    fn from_profile_files_with_endpoint(
        config_path: &Path,
        credentials_path: &Path,
        profile_name: &str,
        region: &str,
        endpoint: Option<&str>,
    ) -> Result<Self, &'static str> {
        let snapshot = match inspect_aws_profile_snapshot(
            config_path,
            credentials_path,
            profile_name,
        ) {
            Ok(snapshot) if valid_aws_region(region) => snapshot,
            Ok(snapshot) => {
                return Err(match snapshot.availability.finite_reason {
                    AvailabilityReason::NativeSourceUnsupported => "native_source_unsupported",
                    AvailabilityReason::NativeSourceDisallowed => "native_source_disallowed",
                    _ => "native_source_unavailable",
                })
            }
            Err(availability) => return Err(match availability.finite_reason {
                AvailabilityReason::NativeSourceUnsupported => "native_source_unsupported",
                AvailabilityReason::NativeSourceDisallowed => "native_source_disallowed",
                _ => "native_source_unavailable",
            }),
        };
        Self::from_profile_contents_with_endpoint(
            snapshot.config_contents,
            snapshot.credentials_contents,
            profile_name,
            region,
            endpoint,
        )
    }

    #[allow(deprecated)]
    fn from_profile_contents_with_endpoint(
        mut config_contents: Zeroizing<String>,
        mut credentials_contents: Zeroizing<String>,
        profile_name: &str,
        region: &str,
        endpoint: Option<&str>,
    ) -> Result<Self, &'static str> {
        if !valid_aws_region(region) || !bounded_identifier(profile_name) {
            return Err("native_source_unsupported");
        }
        use aws_config::profile::profile_file::{
            Builder as ProfileFilesBuilder, ProfileFileKind,
        };
        let files = ProfileFilesBuilder::new()
            .include_default_config_file(false)
            .include_default_credentials_file(false)
            .with_contents(
                ProfileFileKind::Config,
                std::mem::take(&mut *config_contents),
            )
            .with_contents(
                ProfileFileKind::Credentials,
                std::mem::take(&mut *credentials_contents),
            )
            .build();
        let provider_config = aws_config::provider_config::ProviderConfig::without_region()
            .with_region(Some(aws_sdk_bedrockruntime::config::Region::new(
                region.to_owned(),
            )))
            .with_use_fips(Some(false))
            .with_use_dual_stack(Some(false));
        let credentials = aws_config::profile::ProfileFileCredentialsProvider::builder()
            .configure(&provider_config)
            .profile_files(files)
            .profile_name(profile_name)
            .build();
        let timeout = aws_sdk_bedrockruntime::config::timeout::TimeoutConfig::builder()
            .operation_timeout(Duration::from_secs(30))
            .operation_attempt_timeout(Duration::from_secs(20))
            .build();
        let retry = aws_sdk_bedrockruntime::config::retry::RetryConfig::standard()
            .with_max_attempts(2);
        let mut config = aws_sdk_bedrockruntime::Config::builder()
            .behavior_version_latest()
            .region(aws_sdk_bedrockruntime::config::Region::new(region.to_owned()))
            .credentials_provider(credentials)
            .timeout_config(timeout)
            .retry_config(retry);
        if let Some(endpoint) = endpoint {
            config = config.endpoint_url(endpoint);
        }
        let config = config.build();
        Ok(Self {
            client: aws_sdk_bedrockruntime::Client::from_conf(config),
            profile_name: profile_name.to_owned(),
            region: region.to_owned(),
        })
    }

    pub fn signing_name(&self) -> &'static str {
        "bedrock"
    }

    pub fn region(&self) -> &str {
        &self.region
    }
}

#[derive(Clone, Debug)]
struct AwsPhysicalRequestInterceptor {
    accounting: Arc<PhysicalRequestAccounting>,
}

#[derive(Debug)]
struct PhysicalRequestBudgetExceeded;

impl std::fmt::Display for PhysicalRequestBudgetExceeded {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("provider_request_budget_exhausted")
    }
}

impl std::error::Error for PhysicalRequestBudgetExceeded {}

impl aws_sdk_bedrockruntime::config::Intercept for AwsPhysicalRequestInterceptor {
    fn name(&self) -> &'static str { "GamjaPhysicalRequestBudget" }

    fn read_before_transmit(
        &self,
        _context: &aws_sdk_bedrockruntime::config::interceptors::BeforeTransmitInterceptorContextRef<'_>,
        _runtime_components: &aws_sdk_bedrockruntime::config::RuntimeComponents,
        _cfg: &mut aws_sdk_bedrockruntime::config::ConfigBag,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync + 'static>> {
        if self.accounting.try_admit() {
            Ok(())
        } else {
            Err(Box::new(PhysicalRequestBudgetExceeded))
        }
    }
}

#[async_trait]
impl SdkAdapter for AwsBedrockSdkAdapter {
    fn protocol(&self) -> ProviderProtocol {
        ProviderProtocol::AwsBedrockConverse
    }

    async fn execute(&self, call: &ProtocolCall) -> SafeProviderResult {
        if call.public_fields
            != serde_json::json!({
                "profile_name": self.profile_name,
                "region": self.region
            })
        {
            return SafeProviderResult::failure("configuration_changed");
        }
        let input = match bedrock_converse_input(&call.model_id, &call.request) {
            Ok(value) => value,
            Err(code) => return SafeProviderResult::failure(code),
        };
        let operation = self.client
            .converse()
            .model_id(call.model_id.clone())
            .set_messages(Some(input.messages().to_vec()))
            .set_system(Some(input.system().to_vec()))
            .set_tool_config(input.tool_config().cloned())
            .set_inference_config(input.inference_config().cloned())
            .customize()
            .interceptor(AwsPhysicalRequestInterceptor {
                accounting: call.accounting.clone(),
            });
        let result = match tokio::time::timeout(
            Duration::from_secs(30),
            operation.send(),
        )
        .await
        {
            Ok(Ok(value)) => value,
            Ok(Err(_)) if call.physical_model_request_budget_exhausted() => {
                return SafeProviderResult::failure("provider_request_budget_exhausted")
            }
            Ok(Err(_)) => return SafeProviderResult::failure("provider_error"),
            Err(_) => return SafeProviderResult::failure("provider_unavailable"),
        };
        use aws_sdk_bedrockruntime::types::{ContentBlock, ConverseOutput};
        let mut text = String::new();
        let mut calls = Vec::new();
        let Some(ConverseOutput::Message(message)) = result.output() else {
            return SafeProviderResult::failure("malformed_provider_response");
        };
        for block in message.content() {
            match block {
                ContentBlock::Text(value) => text.push_str(value),
                ContentBlock::ToolUse(value) => {
                    if !declared_tool(&call.request, value.name()) {
                        return SafeProviderResult::failure("malformed_provider_response");
                    }
                    let arguments = match document_to_json(value.input()) {
                        Ok(value) => value,
                        Err(code) => return SafeProviderResult::failure(code),
                    };
                    calls.push(SafeProviderToolCall { call_id: value.tool_use_id().to_owned(),
                        name: value.name().to_owned(), arguments });
                }
                _ => {}
            }
        }
        let response_id = "bedrock-converse";
        let state_calls = calls.iter().map(|call| ProviderStateToolCall { call_id: call.call_id.clone(),
            name: call.name.clone(), arguments: call.arguments.clone(), thought_signature: None }).collect();
        let continuation = continuation_for(&call.request, response_id, &calls,
            ProviderContinuationState::BedrockConverse { assistant_text: text.clone(), tool_calls: state_calls });
        let usage = result.usage();
        SafeProviderResult::success_contract(response_id.to_owned(), call.model_id.clone(),
            usage.and_then(|value| u32::try_from(value.input_tokens()).ok()),
            usage.and_then(|value| u32::try_from(value.output_tokens()).ok()), text, calls, continuation)
    }
}

pub struct VertexSdkAdapter {
    client: reqwest::Client,
    credentials: Credentials,
    endpoint: String,
    project: String,
    location: String,
    model_id: String,
}

struct SecretJson(Value);

impl SecretJson {
    fn parse(bytes: &[u8]) -> Result<Self, &'static str> {
        serde_json::from_slice(bytes)
            .map(Self)
            .map_err(|_| "native_source_disallowed")
    }

    fn take(&mut self) -> Value {
        std::mem::replace(&mut self.0, Value::Null)
    }
}

impl Drop for SecretJson {
    fn drop(&mut self) {
        zeroize_json_value(&mut self.0);
    }
}

struct BoundedUserCredential {
    client_id: Zeroizing<String>,
    client_secret: Zeroizing<String>,
    refresh_token: Zeroizing<String>,
}

impl std::fmt::Debug for BoundedUserCredential {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("BoundedUserCredential(<private>)")
    }
}

struct BoundedImpersonation {
    path: String,
    delegates: Vec<String>,
}

struct BoundedVertexCredentials {
    client: reqwest::Client,
    user: BoundedUserCredential,
    impersonation: Option<BoundedImpersonation>,
    quota_project: Option<String>,
    oauth_endpoint: String,
    iam_origin: String,
}

impl std::fmt::Debug for BoundedVertexCredentials {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("BoundedVertexCredentials")
            .field("user", &self.user)
            .field("impersonation", &self.impersonation.is_some())
            .field("quota_project", &self.quota_project)
            .finish_non_exhaustive()
    }
}

impl BoundedVertexCredentials {
    fn from_document(
        mut document: SecretJson,
        oauth_endpoint: &str,
        iam_origin: &str,
    ) -> Result<Self, &'static str> {
        let document = document.take();
        let mut root = SecretJson(document);
        let kind = root
            .0
            .get("type")
            .and_then(Value::as_str)
            .ok_or("native_source_disallowed")?
            .to_owned();
        let (user, impersonation, quota_project) = match kind.as_str() {
            "authorized_user" => {
                let quota = take_optional_public_string(&mut root.0, "quota_project_id")?;
                (take_user_credential(&mut root.0)?, None, quota)
            }
            "impersonated_service_account" => {
                validate_fixed_scopes(&root.0)?;
                let quota = take_optional_public_string(&mut root.0, "quota_project_id")?;
                let url = take_secret_string(&mut root.0, "service_account_impersonation_url")?;
                let path = validate_impersonation_url(url.as_str())?;
                let delegates = take_delegates(&mut root.0)?;
                let source = root
                    .0
                    .as_object_mut()
                    .and_then(|object| object.remove("source_credentials"))
                    .ok_or("native_source_disallowed")?;
                let mut source = SecretJson(source);
                let user = take_user_credential(&mut source.0)?;
                (user, Some(BoundedImpersonation { path, delegates }), quota)
            }
            _ => return Err("native_source_disallowed"),
        };
        if quota_project.as_deref().is_some_and(|value| !bounded_dns_label(value)) {
            return Err("native_source_disallowed");
        }
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .build()
            .map_err(|_| "provider_unavailable")?;
        Ok(Self {
            client,
            user,
            impersonation,
            quota_project,
            oauth_endpoint: oauth_endpoint.to_owned(),
            iam_origin: iam_origin.to_owned(),
        })
    }

    async fn source_access_token(&self) -> Result<Zeroizing<String>, CredentialsError> {
        let response = self
            .client
            .post(&self.oauth_endpoint)
            .form(&[
                ("client_id", self.user.client_id.as_str()),
                ("client_secret", self.user.client_secret.as_str()),
                ("refresh_token", self.user.refresh_token.as_str()),
                ("grant_type", "refresh_token"),
                ("scope", GOOGLE_CLOUD_SCOPE),
            ])
            .send()
            .await
            .map_err(|_| finite_credentials_error(true, "credential_exchange_unavailable"))?;
        if !response.status().is_success() {
            return Err(finite_credentials_error(false, "credential_exchange_rejected"));
        }
        let body = read_bounded_response(response).await?;
        let mut parsed = SecretJson::parse(body.as_slice())
            .map_err(|_| finite_credentials_error(false, "credential_exchange_malformed"))?;
        let valid = parsed.0.get("token_type").and_then(Value::as_str) == Some("Bearer")
            && parsed
                .0
                .get("expires_in")
                .and_then(Value::as_u64)
                .is_some_and(|value| value > 0)
            && parsed
                .0
                .get("access_token")
                .and_then(Value::as_str)
                .is_some_and(|value| !value.is_empty() && value.len() <= MAX_FILE_BYTES as usize);
        if !valid
        {
            return Err(finite_credentials_error(false, "credential_exchange_malformed"));
        }
        take_secret_string(&mut parsed.0, "access_token")
            .map_err(|_| finite_credentials_error(false, "credential_exchange_malformed"))
    }

    async fn impersonated_access_token(
        &self,
        source_token: &str,
        config: &BoundedImpersonation,
    ) -> Result<Zeroizing<String>, CredentialsError> {
        let mut authorization = Zeroizing::new(format!("Bearer {source_token}"));
        let mut header = HeaderValue::from_str(authorization.as_str())
            .map_err(|_| finite_credentials_error(false, "credential_exchange_malformed"))?;
        header.set_sensitive(true);
        let response = self
            .client
            .post(format!("{}{}", self.iam_origin, config.path))
            .header(http::header::AUTHORIZATION, header)
            .json(&serde_json::json!({
                "delegates": config.delegates,
                "scope": [GOOGLE_CLOUD_SCOPE],
                "lifetime": "3600s"
            }))
            .send()
            .await
            .map_err(|_| finite_credentials_error(true, "impersonation_unavailable"))?;
        authorization.zeroize();
        if !response.status().is_success() {
            return Err(finite_credentials_error(false, "impersonation_rejected"));
        }
        let body = read_bounded_response(response).await?;
        let mut parsed = SecretJson::parse(body.as_slice())
            .map_err(|_| finite_credentials_error(false, "impersonation_malformed"))?;
        let valid = parsed
            .0
            .get("accessToken")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty() && value.len() <= MAX_FILE_BYTES as usize)
            && parsed
                .0
                .get("expireTime")
                .and_then(Value::as_str)
                .is_some_and(|value| (20..=64).contains(&value.len()));
        if !valid
        {
            return Err(finite_credentials_error(false, "impersonation_malformed"));
        }
        take_secret_string(&mut parsed.0, "accessToken")
            .map_err(|_| finite_credentials_error(false, "impersonation_malformed"))
    }
}

impl CredentialsProvider for BoundedVertexCredentials {
    fn headers(
        &self,
        _extensions: Extensions,
    ) -> impl std::future::Future<
        Output = Result<CacheableResource<HeaderMap>, CredentialsError>,
    > + Send {
        async move {
            let source_token = self.source_access_token().await?;
            let access_token = match self.impersonation.as_ref() {
                Some(config) => self
                    .impersonated_access_token(source_token.as_str(), config)
                    .await?,
                None => source_token,
            };
            let mut authorization = Zeroizing::new(format!("Bearer {}", access_token.as_str()));
            let mut header = HeaderValue::from_str(authorization.as_str())
                .map_err(|_| finite_credentials_error(false, "credential_exchange_malformed"))?;
            header.set_sensitive(true);
            let mut headers = HeaderMap::new();
            headers.insert(http::header::AUTHORIZATION, header);
            authorization.zeroize();
            if let Some(quota_project) = self.quota_project.as_deref() {
                headers.insert(
                    http::header::HeaderName::from_static("x-goog-user-project"),
                    HeaderValue::from_str(quota_project)
                        .map_err(|_| finite_credentials_error(false, "invalid_quota_project"))?,
                );
            }
            Ok(CacheableResource::New {
                entity_tag: EntityTag::new(),
                data: headers,
            })
        }
    }

    fn universe_domain(&self) -> impl std::future::Future<Output = Option<String>> + Send {
        async { Some("googleapis.com".to_owned()) }
    }
}

impl std::fmt::Debug for VertexSdkAdapter {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("VertexSdkAdapter")
            .field("endpoint", &self.endpoint)
            .field("project", &self.project)
            .field("location", &self.location)
            .field("model_id", &self.model_id)
            .finish_non_exhaustive()
    }
}

impl VertexSdkAdapter {
    pub async fn from_adc_file(
        path: &Path,
        project: &str,
        location: &str,
        model_id: &str,
    ) -> Result<Self, &'static str> {
        Self::from_adc_file_with_endpoints(
            path,
            project,
            location,
            model_id,
            None,
            GOOGLE_OAUTH_TOKEN_ENDPOINT,
            GOOGLE_IAM_ORIGIN,
        )
        .await
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub async fn from_adc_file_with_test_endpoints(
        path: &Path,
        project: &str,
        location: &str,
        model_id: &str,
        prediction_endpoint: &str,
        oauth_endpoint: &str,
        iam_origin: &str,
    ) -> Result<Self, &'static str> {
        Self::from_adc_file_with_endpoints(
            path,
            project,
            location,
            model_id,
            Some(prediction_endpoint),
            oauth_endpoint,
            iam_origin,
        )
        .await
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn with_test_credentials(
        project: &str,
        location: &str,
        model_id: &str,
        prediction_endpoint: &str,
        credentials: Credentials,
    ) -> Result<Self, &'static str> {
        validate_vertex_ids(project, location, model_id)?;
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| "provider_unavailable")?;
        Ok(Self {
            client,
            credentials,
            endpoint: prediction_endpoint.trim_end_matches('/').to_owned(),
            project: project.to_owned(),
            location: location.to_owned(),
            model_id: model_id.to_owned(),
        })
    }

    async fn from_adc_file_with_endpoints(
        path: &Path,
        project: &str,
        location: &str,
        model_id: &str,
        prediction_endpoint: Option<&str>,
        oauth_endpoint: &str,
        iam_origin: &str,
    ) -> Result<Self, &'static str> {
        validate_vertex_ids(project, location, model_id)?;
        let availability = NativeChainInspector::vertex_adc(path);
        if !availability.available {
            return Err("native_source_disallowed");
        }
        let (bytes, _) = read_bounded_file(path, true).map_err(|reason| match reason {
            AvailabilityReason::NativeSourceUnavailable => "native_source_unavailable",
            _ => "native_source_disallowed",
        })?;
        let document = SecretJson::parse(bytes.as_slice())?;
        Self::from_adc_document_with_endpoints(
            document,
            project,
            location,
            model_id,
            prediction_endpoint,
            oauth_endpoint,
            iam_origin,
        ).await
    }

    async fn from_adc_document_with_endpoints(
        document: SecretJson,
        project: &str,
        location: &str,
        model_id: &str,
        prediction_endpoint: Option<&str>,
        oauth_endpoint: &str,
        iam_origin: &str,
    ) -> Result<Self, &'static str> {
        validate_vertex_ids(project, location, model_id)?;
        let credentials = Credentials::from(BoundedVertexCredentials::from_document(
            document,
            oauth_endpoint,
            iam_origin,
        )?);
        let endpoint = prediction_endpoint.map(str::to_owned).unwrap_or_else(|| {
            if location == "global" {
                "https://aiplatform.googleapis.com".to_owned()
            } else {
                format!("https://{location}-aiplatform.googleapis.com")
            }
        });
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| "provider_unavailable")?;
        Ok(Self {
            client,
            credentials,
            endpoint: endpoint.trim_end_matches('/').to_owned(),
            project: project.to_owned(),
            location: location.to_owned(),
            model_id: model_id.to_owned(),
        })
    }

    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }
}

#[async_trait]
impl SdkAdapter for VertexSdkAdapter {
    fn protocol(&self) -> ProviderProtocol {
        ProviderProtocol::VertexGenerateContentV1
    }

    async fn execute(&self, call: &ProtocolCall) -> SafeProviderResult {
        if call.model_id != self.model_id
            || call.public_fields
                != serde_json::json!({
                    "project_id": self.project,
                    "location": self.location
                })
        {
            return SafeProviderResult::failure("configuration_changed");
        }
        let mut request = match vertex_generate_content_request(
            &self.project,
            &self.location,
            &self.model_id,
            &call.request,
        ) {
            Ok(value) => value,
            Err(code) => return SafeProviderResult::failure(code),
        };
        request.model.clear();
        let body = match serde_json::to_vec(&request) {
            Ok(value) if value.len() <= MAX_HTTP_BODY => Zeroizing::new(value),
            _ => return SafeProviderResult::failure("invalid_bounded_request"),
        };
        let mut headers = match self.credentials.headers(Extensions::new()).await {
            Ok(CacheableResource::New { data, .. }) => data,
            Ok(CacheableResource::NotModified) | Err(_) => {
                return SafeProviderResult::failure("auth_expired_or_revoked")
            }
        };
        let Some(authorization) = headers.remove(http::header::AUTHORIZATION) else {
            return SafeProviderResult::failure("auth_expired_or_revoked");
        };
        let quota = headers.remove(http::header::HeaderName::from_static("x-goog-user-project"));
        if !headers.is_empty() {
            return SafeProviderResult::failure("auth_expired_or_revoked");
        }
        let endpoint = format!(
            "{}/v1/projects/{}/locations/{}/publishers/google/models/{}:generateContent?%24alt=json%3Benum-encoding%3Dint",
            self.endpoint, self.project, self.location, self.model_id,
        );
        let mut builder = self.client.post(endpoint)
            .header(http::header::AUTHORIZATION, authorization)
            .header(http::header::CONTENT_TYPE, "application/json")
            .header("x-goog-api-client", VERTEX_X_GOOG_API_CLIENT)
            .body(body.to_vec());
        if let Some(quota) = quota {
            builder = builder.header("x-goog-user-project", quota);
        }
        if !call.try_admit_model_request() {
            return SafeProviderResult::failure("provider_request_budget_exhausted");
        }
        let mut response = match builder.send().await {
            Ok(value) => value,
            Err(error) if error.is_timeout() => {
                return SafeProviderResult::failure("provider_unavailable")
            }
            Err(_) => return SafeProviderResult::failure("provider_error"),
        };
        if !response.status().is_success() {
            return SafeProviderResult::failure(match response.status().as_u16() {
                401 => "auth_expired_or_revoked",
                403 => "permission_denied",
                404 => "resource_not_found",
                429 => "rate_limited",
                500..=599 => "provider_unavailable",
                _ => "provider_error",
            });
        }
        let mut response_body = Zeroizing::new(Vec::new());
        loop {
            match response.chunk().await {
                Ok(Some(chunk)) if response_body.len() + chunk.len() <= MAX_HTTP_BODY => {
                    response_body.extend_from_slice(&chunk);
                }
                Ok(Some(_)) => return SafeProviderResult::failure("malformed_provider_response"),
                Ok(None) => break,
                Err(_) => return SafeProviderResult::failure("malformed_provider_response"),
            }
        }
        let response: GenerateContentResponse = match serde_json::from_slice(response_body.as_slice()) {
            Ok(value) => value,
            Err(_) => return SafeProviderResult::failure("malformed_provider_response"),
        };
        use google_cloud_aiplatform_v1::model::part::Data;
        let Some(content) = response.candidates.first().and_then(|candidate| candidate.content.as_ref()) else {
            return SafeProviderResult::failure("malformed_provider_response");
        };
        let mut text = String::new();
        let mut calls = Vec::new();
        let mut state_calls = Vec::new();
        for part in &content.parts {
            match part.data.as_ref() {
                Some(Data::Text(value)) if !part.thought => text.push_str(value),
                Some(Data::FunctionCall(value)) => {
                    if !declared_tool(&call.request, &value.name) {
                        return SafeProviderResult::failure("malformed_provider_response");
                    }
                    let arguments = match value.args.as_ref().map(serde_json::to_value).transpose() {
                        Ok(Some(value)) => value,
                        Ok(None) => serde_json::json!({}),
                        Err(_) => return SafeProviderResult::failure("malformed_provider_response"),
                    };
                    let call_id = format!("vertex-{}", calls.len());
                    let signature = if part.thought_signature.is_empty() { None } else {
                        Some(base64::engine::general_purpose::STANDARD.encode(&part.thought_signature))
                    };
                    calls.push(SafeProviderToolCall { call_id: call_id.clone(), name: value.name.clone(),
                        arguments: arguments.clone() });
                    state_calls.push(ProviderStateToolCall { call_id, name: value.name.clone(), arguments,
                        thought_signature: signature });
                }
                _ => {}
            }
        }
        let response_id = response.response_id;
        let continuation = continuation_for(&call.request, &response_id, &calls,
            ProviderContinuationState::VertexGenerateContent { assistant_text: text.clone(), tool_calls: state_calls });
        let usage = response.usage_metadata.as_ref();
        SafeProviderResult::success_contract(response_id, call.model_id.clone(),
            usage.and_then(|value| u32::try_from(value.prompt_token_count).ok()),
            usage.and_then(|value| u32::try_from(value.candidates_token_count).ok()), text, calls, continuation)
    }
}

#[derive(Debug)]
pub struct BoundedAzureCliExecutor {
    deadline: Duration,
    program: std::path::PathBuf,
}

impl Default for BoundedAzureCliExecutor {
    fn default() -> Self {
        Self {
            deadline: Duration::from_secs(30),
            program: std::path::PathBuf::from("az"),
        }
    }
}

impl BoundedAzureCliExecutor {
    pub fn with_deadline(deadline: Duration) -> Result<Self, &'static str> {
        if deadline < Duration::from_millis(10) || deadline > Duration::from_secs(60) {
            return Err("invalid_process_deadline");
        }
        Ok(Self {
            deadline,
            program: std::path::PathBuf::from("az"),
        })
    }

    fn with_program(
        deadline: Duration,
        program: PathBuf,
    ) -> Result<Self, &'static str> {
        let mut value = Self::with_deadline(deadline)?;
        if !program.is_absolute() {
            return Err("native_source_disallowed");
        }
        value.program = program;
        Ok(value)
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn with_test_program(
        deadline: Duration,
        program: impl Into<std::path::PathBuf>,
    ) -> Result<Self, &'static str> {
        let program = program.into();
        if program.is_absolute() {
            Self::with_program(deadline, program)
        } else {
            let mut value = Self::with_deadline(deadline)?;
            value.program = program;
            Ok(value)
        }
    }
}

#[async_trait]
impl Executor for BoundedAzureCliExecutor {
    async fn run(&self, program: &OsStr, args: &[&OsStr]) -> io::Result<Output> {
        validate_azure_sdk_command(program, args)?;
        let (cancel_tx, cancel_rx) = tokio::sync::oneshot::channel();
        let mut cleanup = AzureCleanupSignal(Some(cancel_tx));
        let task = tokio::spawn(run_azure_process(
            self.program.clone(),
            self.deadline,
            cancel_rx,
        ));
        let result = task.await.map_err(|_| io::Error::other("azure cli supervisor"))?;
        cleanup.0.take();
        result
    }
}

struct AzureCleanupSignal(Option<tokio::sync::oneshot::Sender<()>>);

impl Drop for AzureCleanupSignal {
    fn drop(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}

async fn run_azure_process(
    program: std::path::PathBuf,
    deadline: Duration,
    cancel: tokio::sync::oneshot::Receiver<()>,
) -> io::Result<Output> {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return run_azure_native(program, deadline, cancel).await;
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    {
    let mut cancel = cancel;
    let mut command = tokio::process::Command::new(program);
    command
        .args([
            "account",
            "get-access-token",
            "-o",
            "json",
            "--scope",
            AZURE_SCOPE,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    #[cfg(unix)]
    command.process_group(0);
    let mut child = command.spawn()?;
    let process_group = child.id();
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| io::Error::other("missing stdout"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| io::Error::other("missing stderr"))?;
    let mut stdout_task = tokio::spawn(read_bounded_output(stdout));
    let mut stderr_task = tokio::spawn(read_bounded_output(stderr));
    let absolute_deadline = tokio::time::sleep(deadline);
    tokio::pin!(absolute_deadline);
    let status = tokio::select! {
        status = child.wait() => status,
        _ = &mut absolute_deadline => {
            stop_azure_process(
                &mut child,
                process_group,
                &mut stdout_task,
                &mut stderr_task,
            ).await;
            return Err(io::Error::new(io::ErrorKind::TimedOut, "azure cli deadline"));
        },
        _ = &mut cancel => {
            stop_azure_process(
                &mut child,
                process_group,
                &mut stdout_task,
                &mut stderr_task,
            ).await;
            return Err(io::Error::new(io::ErrorKind::Interrupted, "azure cli cancelled"));
        },
    };
    let status = match status {
        Ok(status) => status,
        Err(error) => {
            stop_azure_process(
                &mut child,
                process_group,
                &mut stdout_task,
                &mut stderr_task,
            )
            .await;
            return Err(error);
        }
    };
    let (mut stdout, mut stderr) = tokio::select! {
        output = join_azure_outputs(&mut stdout_task, &mut stderr_task) => output?,
        _ = &mut absolute_deadline => {
            stop_azure_process(
                &mut child,
                process_group,
                &mut stdout_task,
                &mut stderr_task,
            ).await;
            return Err(io::Error::new(io::ErrorKind::TimedOut, "azure cli deadline"));
        },
        _ = &mut cancel => {
            stop_azure_process(
                &mut child,
                process_group,
                &mut stdout_task,
                &mut stderr_task,
            ).await;
            return Err(io::Error::new(io::ErrorKind::Interrupted, "azure cli cancelled"));
        },
    };
    Ok(Output {
        status,
        stdout: std::mem::take(&mut *stdout),
        stderr: std::mem::take(&mut *stderr),
    })
    }
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
async fn join_azure_outputs(
    stdout_task: &mut tokio::task::JoinHandle<io::Result<Zeroizing<Vec<u8>>>>,
    stderr_task: &mut tokio::task::JoinHandle<io::Result<Zeroizing<Vec<u8>>>>,
) -> io::Result<(Zeroizing<Vec<u8>>, Zeroizing<Vec<u8>>)> {
    let (stdout, stderr) = tokio::join!(stdout_task, stderr_task);
    Ok((
        stdout.map_err(|_| io::Error::other("stdout task"))??,
        stderr.map_err(|_| io::Error::other("stderr task"))??,
    ))
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
async fn stop_azure_process(
    child: &mut tokio::process::Child,
    process_group: Option<u32>,
    stdout_task: &mut tokio::task::JoinHandle<io::Result<Zeroizing<Vec<u8>>>>,
    stderr_task: &mut tokio::task::JoinHandle<io::Result<Zeroizing<Vec<u8>>>>,
) {
    #[cfg(unix)]
    if let Some(pid) = process_group {
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
    }
    #[cfg(not(unix))]
    let _ = child.kill().await;
    let _ = child.wait().await;
    stdout_task.abort();
    stderr_task.abort();
    let _ = stdout_task.await;
    let _ = stderr_task.await;
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
async fn read_bounded_output<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
) -> io::Result<Zeroizing<Vec<u8>>> {
    let mut bytes = Zeroizing::new(Vec::new());
    reader.take(MAX_PROCESS_OUTPUT + 1).read_to_end(&mut bytes).await?;
    if bytes.len() as u64 > MAX_PROCESS_OUTPUT {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "process output too large"));
    }
    Ok(bytes)
}

fn validate_azure_sdk_command(program: &OsStr, args: &[&OsStr]) -> io::Result<()> {
    #[cfg(unix)]
    let expected = (
        OsStr::new("/bin/sh"),
        OsStr::new("-c"),
        OsStr::new("cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default"),
    );
    #[cfg(windows)]
    let expected = (
        program,
        OsStr::new("/C"),
        OsStr::new("cd \\ && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default"),
    );
    if program != expected.0 || args != [expected.1, expected.2] {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "disallowed azure cli command"));
    }
    Ok(())
}

fn constant_binding_match(handle: &CoreProviderTransport, call: &ProtocolCall) -> bool {
    use subtle::ConstantTimeEq;
    handle.opaque_id.as_bytes().ct_eq(call.committed_handle_id.as_bytes()).into()
        && handle.descriptor_revision == call.descriptor_revision
        && handle.protocol == call.protocol
        && handle.model_id == call.model_id
        && handle.public_fields == call.public_fields
        && call.request.protocol == call.protocol
        && call.request.scope.descriptor_revision == call.descriptor_revision
        && call.request.scope.model_id == call.model_id
}

fn validate_public_binding(
    protocol: ProviderProtocol,
    model_id: &str,
    public_fields: &Value,
) -> Result<(), &'static str> {
    let request = BoundedProviderRequest::new("binding", 1)?;
    fixed_request_spec(protocol, model_id, public_fields, &request).map(|_| ())
}

pub(crate) fn compiled_provider_binding(
    provider_id: &str,
    auth_method_id: &str,
    requested_protocol: ProviderProtocol,
    requested_descriptor_revision: u64,
    model_id: &str,
    public_fields: &Value,
) -> Option<(ProviderProtocol, u64)> {
    let protocol = match (provider_id, auth_method_id) {
        ("azure-openai", "native_chain" | "api_key") => {
            ProviderProtocol::AzureOpenAiResponsesV1
        }
        ("amazon-bedrock", "native_chain") => ProviderProtocol::AwsBedrockConverse,
        ("google-vertex", "native_chain") => ProviderProtocol::VertexGenerateContentV1,
        _ => return None,
    };
    (requested_protocol == protocol
        && requested_descriptor_revision == COMPILED_PROVIDER_DESCRIPTOR_REVISION
        && valid_provider_model_id(protocol, model_id)
        && validate_public_binding(protocol, model_id, public_fields).is_ok())
        .then_some((protocol, COMPILED_PROVIDER_DESCRIPTOR_REVISION))
}

fn exact_keys(
    fields: &serde_json::Map<String, Value>,
    expected: &[&str],
) -> Result<(), &'static str> {
    if fields.len() != expected.len() || expected.iter().any(|key| !fields.contains_key(*key)) {
        return Err("invalid_public_fields");
    }
    Ok(())
}

fn validate_azure_root(root: &str) -> Result<(), &'static str> {
    let parsed = url::Url::parse(root).map_err(|_| "invalid_public_fields")?;
    let host = parsed.host_str().ok_or("invalid_public_fields")?;
    let resource = host
        .strip_suffix(".openai.azure.com")
        .ok_or("invalid_public_fields")?;
    if parsed.scheme() != "https"
        || parsed.username() != ""
        || parsed.password().is_some()
        || parsed.port().is_some()
        || !matches!(parsed.path(), "" | "/")
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !bounded_dns_label(resource)
    {
        return Err("invalid_public_fields");
    }
    Ok(())
}

fn validate_vertex_ids(project: &str, location: &str, model: &str) -> Result<(), &'static str> {
    if !bounded_dns_label(project)
        || !(location == "global" || valid_location(location))
        || !valid_provider_model_id(ProviderProtocol::VertexGenerateContentV1, model)
    {
        return Err("invalid_public_fields");
    }
    Ok(())
}

fn valid_authorized_user(document: &Value) -> bool {
    document.get("type").and_then(Value::as_str) == Some("authorized_user")
        && ["client_id", "client_secret", "refresh_token"]
            .iter()
            .all(|key| {
                document
                    .get(*key)
                    .and_then(Value::as_str)
                    .is_some_and(|value| !value.is_empty() && value.len() <= 16_384)
            })
        && document
            .get("token_uri")
            .and_then(Value::as_str)
            .is_none_or(|value| value == "https://oauth2.googleapis.com/token")
        && document
            .get("universe_domain")
            .and_then(Value::as_str)
            .is_none_or(|value| value == "googleapis.com")
}

fn canonical_vertex_adc(
    path: &Path,
) -> Result<(SecretJson, SafeAvailability), AvailabilityReason> {
    let (bytes, metadata) = read_bounded_file(path, true)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if metadata.uid() != unsafe { libc::geteuid() }
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err(AvailabilityReason::NativeSourceDisallowed);
        }
    }
    let mut original = SecretJson::parse(bytes.as_slice())
        .map_err(|_| AvailabilityReason::NativeSourceDisallowed)?;
    let kind = original.0.get("type").and_then(Value::as_str)
        .ok_or(AvailabilityReason::NativeSourceDisallowed)?.to_owned();
    let allowed = match kind.as_str() {
        "authorized_user" => valid_authorized_user(&original.0)
            && valid_optional_quota_project(&original.0),
        "impersonated_service_account" => original.0
            .get("source_credentials").is_some_and(valid_authorized_user)
            && original.0.get("service_account_impersonation_url")
                .and_then(Value::as_str)
                .is_some_and(|value| validate_impersonation_url(value).is_ok())
            && validate_delegate_values(&original.0)
            && validate_fixed_scopes(&original.0).is_ok()
            && valid_optional_quota_project(&original.0),
        _ => false,
    };
    if !allowed { return Err(AvailabilityReason::NativeSourceDisallowed); }

    let mut selected = serde_json::Map::new();
    selected.insert("type".into(), Value::String(kind.clone()));
    match kind.as_str() {
        "authorized_user" => move_selected_fields(
            &mut original.0,
            &mut selected,
            &["client_id", "client_secret", "refresh_token", "quota_project_id"],
        ),
        "impersonated_service_account" => {
            let source = original.0.as_object_mut()
                .and_then(|object| object.remove("source_credentials"))
                .ok_or(AvailabilityReason::NativeSourceDisallowed)?;
            let mut source = SecretJson(source);
            let mut selected_source = serde_json::Map::new();
            selected_source.insert("type".into(), Value::String("authorized_user".into()));
            move_selected_fields(
                &mut source.0,
                &mut selected_source,
                &["client_id", "client_secret", "refresh_token", "quota_project_id"],
            );
            selected.insert("source_credentials".into(), Value::Object(selected_source));
            move_selected_fields(
                &mut original.0,
                &mut selected,
                &["service_account_impersonation_url", "delegates", "scopes", "quota_project_id"],
            );
        }
        _ => unreachable!("kind validated above"),
    }
    let availability = SafeAvailability::available(metadata_fingerprint(
        "vertex_adc", path, &metadata, &kind,
    ));
    Ok((SecretJson(Value::Object(selected)), availability))
}

fn valid_optional_quota_project(document: &Value) -> bool {
    document.get("quota_project_id").is_none_or(|value| {
        value.as_str().is_some_and(bounded_dns_label)
    })
}

fn move_selected_fields(
    source: &mut Value,
    destination: &mut serde_json::Map<String, Value>,
    keys: &[&str],
) {
    let Some(source) = source.as_object_mut() else { return; };
    for key in keys {
        if let Some(value) = source.remove(*key) {
            destination.insert((*key).to_owned(), value);
        }
    }
}

fn take_secret_string(
    document: &mut Value,
    key: &str,
) -> Result<Zeroizing<String>, &'static str> {
    let slot = document
        .as_object_mut()
        .and_then(|object| object.get_mut(key))
        .ok_or("native_source_disallowed")?;
    let Value::String(value) = slot else {
        return Err("native_source_disallowed");
    };
    if value.is_empty() || value.len() > MAX_FILE_BYTES as usize {
        return Err("native_source_disallowed");
    }
    let value = std::mem::take(value);
    *slot = Value::Null;
    Ok(Zeroizing::new(value))
}

fn take_optional_public_string(
    document: &mut Value,
    key: &str,
) -> Result<Option<String>, &'static str> {
    let Some(value) = document
        .as_object_mut()
        .and_then(|object| object.remove(key))
    else {
        return Ok(None);
    };
    let value = value.as_str().ok_or("native_source_disallowed")?;
    if value.is_empty() || value.len() > 128 {
        return Err("native_source_disallowed");
    }
    Ok(Some(value.to_owned()))
}

fn take_user_credential(document: &mut Value) -> Result<BoundedUserCredential, &'static str> {
    if !valid_authorized_user(document) {
        return Err("native_source_disallowed");
    }
    Ok(BoundedUserCredential {
        client_id: take_secret_string(document, "client_id")?,
        client_secret: take_secret_string(document, "client_secret")?,
        refresh_token: take_secret_string(document, "refresh_token")?,
    })
}

fn validate_fixed_scopes(document: &Value) -> Result<(), &'static str> {
    let Some(scopes) = document.get("scopes") else {
        return Ok(());
    };
    let scopes = scopes.as_array().ok_or("native_source_disallowed")?;
    if scopes.len() != 1 || scopes[0].as_str() != Some(GOOGLE_CLOUD_SCOPE) {
        return Err("native_source_disallowed");
    }
    Ok(())
}

fn validate_impersonation_url(value: &str) -> Result<String, &'static str> {
    let parsed = url::Url::parse(value).map_err(|_| "native_source_disallowed")?;
    if parsed.scheme() != "https"
        || parsed.host_str() != Some("iamcredentials.googleapis.com")
        || parsed.username() != ""
        || parsed.password().is_some()
        || parsed.port().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err("native_source_disallowed");
    }
    validate_impersonation_path(parsed.path())?;
    Ok(parsed.path().to_owned())
}

fn validate_impersonation_path(value: &str) -> Result<(), &'static str> {
    let prefix = "/v1/projects/-/serviceAccounts/";
    let suffix = ":generateAccessToken";
    let principal = value
        .strip_prefix(prefix)
        .and_then(|value| value.strip_suffix(suffix))
        .ok_or("native_source_disallowed")?;
    if !valid_service_account_email(principal) {
        return Err("native_source_disallowed");
    }
    Ok(())
}

fn valid_service_account_email(value: &str) -> bool {
    if value.len() > 320 || value.contains('%') || value.contains('/') {
        return false;
    }
    let Some((local, domain)) = value.split_once('@') else {
        return false;
    };
    !local.is_empty()
        && local.len() <= 64
        && local.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.')
        })
        && domain.ends_with(".iam.gserviceaccount.com")
        && bounded_dns_label(domain.trim_end_matches(".iam.gserviceaccount.com"))
}

fn valid_delegate(value: &str) -> bool {
    value
        .strip_prefix("projects/-/serviceAccounts/")
        .is_some_and(valid_service_account_email)
}

fn validate_delegate_values(document: &Value) -> bool {
    document.get("delegates").is_none_or(|delegates| {
        delegates.as_array().is_some_and(|values| {
            values.len() <= 16
                && values
                    .iter()
                    .all(|value| value.as_str().is_some_and(valid_delegate))
        })
    })
}

fn take_delegates(document: &mut Value) -> Result<Vec<String>, &'static str> {
    if !validate_delegate_values(document) {
        return Err("native_source_disallowed");
    }
    let Some(value) = document
        .as_object_mut()
        .and_then(|object| object.remove("delegates"))
    else {
        return Ok(Vec::new());
    };
    value
        .as_array()
        .ok_or("native_source_disallowed")?
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .ok_or("native_source_disallowed")
        })
        .collect()
}

async fn read_bounded_response(
    mut response: reqwest::Response,
) -> Result<Zeroizing<Vec<u8>>, CredentialsError> {
    let mut body = Zeroizing::new(Vec::new());
    loop {
        match response.chunk().await {
            Ok(Some(chunk)) if body.len() + chunk.len() <= MAX_HTTP_BODY => {
                body.extend_from_slice(&chunk)
            }
            Ok(Some(_)) => {
                return Err(finite_credentials_error(false, "credential_response_too_large"))
            }
            Ok(None) => return Ok(body),
            Err(_) => return Err(finite_credentials_error(true, "credential_response_unavailable")),
        }
    }
}

fn finite_credentials_error(transient: bool, code: &'static str) -> CredentialsError {
    CredentialsError::from_msg(transient, code)
}

fn zeroize_json_value(value: &mut Value) {
    match value {
        Value::String(value) => value.zeroize(),
        Value::Array(values) => values.iter_mut().for_each(zeroize_json_value),
        Value::Object(values) => values.values_mut().for_each(zeroize_json_value),
        _ => {}
    }
}

fn valid_aws_region(value: &str) -> bool {
    matches!(
        value,
        "us-east-1"
            | "us-east-2"
            | "us-west-2"
            | "ca-central-1"
            | "sa-east-1"
            | "eu-central-1"
            | "eu-north-1"
            | "eu-south-1"
            | "eu-west-1"
            | "eu-west-2"
            | "eu-west-3"
            | "ap-northeast-1"
            | "ap-northeast-2"
            | "ap-northeast-3"
            | "ap-south-1"
            | "ap-southeast-1"
            | "ap-southeast-2"
    )
}

fn valid_location(value: &str) -> bool {
    matches!(
        value,
        "us-central1"
            | "us-east1"
            | "us-east4"
            | "us-west1"
            | "europe-west1"
            | "europe-west4"
            | "asia-northeast1"
            | "asia-southeast1"
    )
}

fn bounded_dns_label(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && !value.starts_with('-')
        && !value.ends_with('-')
}

fn bounded_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'/' | b':'))
}

fn plain_persistence_tool(value: &str) -> bool {
    matches!(value, "save_bot_memory" | "edit_bot_memory" | "forget_bot_memory" | "change_bot_role"
        | "set_bot_memory_scope" | "set_memory_group_access" | "save_owner_fact"
        | "edit_owner_fact" | "forget_owner_fact" | "save_owner_skill" | "edit_owner_skill" | "create_bot_routine" | "edit_bot_routine" | "set_bot_routine" | "test_bot_routine" | "read_bot_routine"
        | "synthesize_voice_memo")
}

fn valid_persistence_binding(family: &str, wire_name: &str) -> bool {
    !plain_persistence_tool(wire_name) || family == wire_name
}

fn valid_wire_tool_name(value: &str) -> bool {
    let suffix = value.strip_prefix("gamja_").unwrap_or("");
    plain_persistence_tool(value) || (!suffix.is_empty() && suffix.len() <= 58 && suffix.len() % 2 == 0
        && suffix.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()))
}

fn valid_mcp_family(value:&str)->bool {
    value.strip_prefix("mcp_").is_some_and(|v|v.len()==20 && v.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
}

fn contains_remote_ref(value: &Value) -> bool {
    match value {
        Value::Object(values) => values.iter().any(|(key, value)| {
            matches!(key.as_str(), "$ref" | "$dynamicRef" | "$recursiveRef")
                || contains_remote_ref(value)
        }),
        Value::Array(values) => values.iter().any(contains_remote_ref),
        _ => false,
    }
}

fn bounded_model_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 200
        && value.split('/').all(|segment| {
            let mut bytes = segment.bytes();
            bytes.next().is_some_and(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
                && bytes.all(|byte| {
                    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.' | b':')
                })
        })
}

fn valid_provider_model_id(protocol: ProviderProtocol, value: &str) -> bool {
    if !bounded_model_id(value) {
        return false;
    }
    match protocol {
        ProviderProtocol::VertexGenerateContentV1 => {
            !value.contains('/') && !value.contains(':')
        }
        ProviderProtocol::AzureOpenAiResponsesV1 | ProviderProtocol::AwsBedrockConverse => true,
    }
}

fn parse_profile_sections(text: &str, config: bool) -> BTreeMap<String, BTreeMap<String, String>> {
    let mut sections = BTreeMap::new();
    let mut current: Option<String> = None;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if let Some(section) = line.strip_prefix('[').and_then(|v| v.strip_suffix(']')) {
            let name = if config {
                section.trim().strip_prefix("profile ").unwrap_or(section.trim())
            } else {
                section.trim()
            };
            current = bounded_identifier(name).then(|| name.to_owned());
            continue;
        }
        let Some(name) = current.as_ref() else { continue };
        let Some((key, value)) = line.split_once('=') else { continue };
        let key = key.trim().to_ascii_lowercase();
        if !bounded_identifier(&key) {
            continue;
        }
        let retained_value = value.trim().to_owned();
        sections
            .entry(name.clone())
            .or_insert_with(BTreeMap::new)
            .insert(key, retained_value);
    }
    sections
}

fn safe_file_metadata(path: &Path, strict_permissions: bool) -> Result<fs::Metadata, AvailabilityReason> {
    let link_metadata = fs::symlink_metadata(path)
        .map_err(|_| AvailabilityReason::NativeSourceUnavailable)?;
    if link_metadata.file_type().is_symlink() || !link_metadata.is_file() {
        return Err(AvailabilityReason::NativeSourceDisallowed);
    }
    if link_metadata.len() > MAX_FILE_BYTES {
        return Err(AvailabilityReason::NativeSourceDisallowed);
    }
    let _ = strict_permissions;
    Ok(link_metadata)
}

fn read_bounded_file(
    path: &Path,
    strict_permissions: bool,
) -> Result<(Zeroizing<Vec<u8>>, fs::Metadata), AvailabilityReason> {
    safe_file_metadata(path, strict_permissions)?;
    #[cfg(unix)]
    let mut file = {
        use std::os::unix::fs::OpenOptionsExt;
        fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(path)
            .map_err(|_| AvailabilityReason::NativeSourceUnavailable)?
    };
    #[cfg(not(unix))]
    let mut file = {
        safe_file_metadata(path, strict_permissions)?;
        fs::File::open(path).map_err(|_| AvailabilityReason::NativeSourceUnavailable)?
    };
    let metadata = file
        .metadata()
        .map_err(|_| AvailabilityReason::NativeSourceUnavailable)?;
    if !metadata.is_file() || metadata.len() > MAX_FILE_BYTES {
        return Err(AvailabilityReason::NativeSourceDisallowed);
    }
    #[cfg(unix)]
    if strict_permissions {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if metadata.uid() != unsafe { libc::geteuid() }
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err(AvailabilityReason::NativeSourceDisallowed);
        }
    }
    let mut bytes = Zeroizing::new(Vec::with_capacity(metadata.len() as usize));
    let mut limited = std::io::Read::take(&mut file, MAX_FILE_BYTES + 1);
    std::io::Read::read_to_end(&mut limited, &mut bytes)
        .map_err(|_| AvailabilityReason::NativeSourceUnavailable)?;
    if bytes.len() as u64 != metadata.len() || bytes.len() as u64 > MAX_FILE_BYTES {
        return Err(AvailabilityReason::NativeSourceDisallowed);
    }
    Ok((bytes, metadata))
}

fn metadata_fingerprint(kind: &str, path: &Path, metadata: &fs::Metadata, selector: &str) -> String {
    let modified = metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_nanos())
        .unwrap_or_default();
    let mut safe = format!("{kind}\0{}\0{}\0{modified}\0{selector}", path.display(), metadata.len());
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        safe.push_str(&format!("\0{}\0{}\0{}", metadata.dev(), metadata.ino(), metadata.ctime()));
    }
    let digest = digest_safe(safe.as_bytes());
    safe.zeroize();
    digest
}

fn digest_safe(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write;
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

/// Native ARM CLI supervision uses the same FD boundary as Core children. Pipes
/// are polled without reader tasks, so cancellation cannot leave a blocked read.
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
async fn run_azure_native(
    program: PathBuf,
    deadline: Duration,
    mut cancel: tokio::sync::oneshot::Receiver<()>,
) -> io::Result<Output> {
    use crate::macos_spawn::{cloexec_pipe, inherited_environment, OwnedChild};
    use std::{fs::File, os::fd::AsRawFd};
    let absolute_deadline = tokio::time::Instant::now() + deadline;
    let program = azure_native_program_path(program)?;
    let input = File::open("/dev/null")?;
    let (mut stdout_pipe, stdout_child) =
        cloexec_pipe().map_err(|_| io::Error::other("azure cli pipe unavailable"))?;
    let (mut stderr_pipe, stderr_child) =
        cloexec_pipe().map_err(|_| io::Error::other("azure cli pipe unavailable"))?;
    let mut child = OwnedChild::spawn(
        &program,
        &[
            "account",
            "get-access-token",
            "-o",
            "json",
            "--scope",
            AZURE_SCOPE,
        ],
        inherited_environment()
            .map_err(|_| io::Error::other("azure cli environment unavailable"))?,
        &[
            (input.as_raw_fd(), 0),
            (stdout_child.as_raw_fd(), 1),
            (stderr_child.as_raw_fd(), 2),
        ],
        deadline,
    )
    .map_err(|_| io::Error::other("azure cli spawn failed"))?;
    drop(stdout_child);
    drop(stderr_child);
    drop(input);
    let mut stdout = Zeroizing::new(Vec::new());
    let mut stderr = Zeroizing::new(Vec::new());
    let result=async {
        for fd in [stdout_pipe.as_raw_fd(),stderr_pipe.as_raw_fd()] {
            let flags=unsafe {libc::fcntl(fd,libc::F_GETFL)};
            if flags<0 || unsafe {libc::fcntl(fd,libc::F_SETFL,flags|libc::O_NONBLOCK)}<0 {
                return Err(io::Error::other("azure cli pipe configuration failed"));
            }
        }
        let mut stdout_eof=false;let mut stderr_eof=false;
        loop {
            if tokio::time::Instant::now()>=absolute_deadline {
                return Err(io::Error::new(io::ErrorKind::TimedOut,"azure cli deadline"));
            }
            if !stdout_eof {stdout_eof=read_azure_pipe_once(&mut stdout_pipe,&mut stdout)?;}
            if !stderr_eof {stderr_eof=read_azure_pipe_once(&mut stderr_pipe,&mut stderr)?;}
            let status=child.try_wait().map_err(|_| io::Error::other("azure cli monitor failed"))?;
            if let Some(status)=status {
                if stdout_eof && stderr_eof {return Ok(status);}
            }
            tokio::select! {
                _ = &mut cancel => return Err(io::Error::new(io::ErrorKind::Interrupted,"azure cli cancelled")),
                _ = tokio::time::sleep_until(absolute_deadline) => return Err(io::Error::new(io::ErrorKind::TimedOut,"azure cli deadline")),
                _ = tokio::time::sleep(Duration::from_millis(5)) => {},
            }
        }
    }.await;
    // Keep the leader waitable through every group signal, even after its exit.
    // Failed cleanup is never replaced with a successful SDK output.
    child
        .shutdown(Duration::from_secs(1))
        .map_err(|_| io::Error::other("azure cli cleanup uncertain"))?;
    let status = result?;
    Ok(Output {
        status,
        stdout: std::mem::take(&mut *stdout),
        stderr: std::mem::take(&mut *stderr),
    })
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn read_azure_pipe_once(
    pipe: &mut std::fs::File,
    bytes: &mut Zeroizing<Vec<u8>>,
) -> io::Result<bool> {
    use std::io::Read;
    let mut chunk = Zeroizing::new([0; 4096]);
    match pipe.read(&mut *chunk) {
        Ok(0) => Ok(true),
        Ok(n) => {
            if bytes.len() + n > MAX_PROCESS_OUTPUT as usize {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "process output too large",
                ));
            }
            bytes.extend_from_slice(&chunk[..n]);
            Ok(false)
        }
        Err(e)
            if matches!(
                e.kind(),
                io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
            ) =>
        {
            Ok(false)
        }
        Err(_) => Err(io::Error::other("azure cli pipe read failed")),
    }
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn azure_native_program_path(program: PathBuf) -> io::Result<PathBuf> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};
    if program.is_absolute() {
        return Ok(program);
    }
    if program.components().count() > 1 {
        return Ok(std::env::current_dir()?.join(program));
    }
    let path = std::env::var_os("PATH").unwrap_or_else(|| "/usr/bin:/bin".into());
    for base in std::env::split_paths(&path) {
        let candidate = base.join(&program);
        if !candidate.is_file() {
            continue;
        }
        if let Ok(bytes) = CString::new(candidate.as_os_str().as_bytes()) {
            if unsafe { libc::access(bytes.as_ptr(), libc::X_OK) } == 0 {
                return if candidate.is_absolute() {
                    Ok(candidate)
                } else {
                    Ok(std::env::current_dir()?.join(candidate))
                };
            }
        }
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        "azure cli unavailable",
    ))
}

#[cfg(test)]
mod persistence_wire_name_tests {
    use super::{plain_persistence_tool, valid_persistence_binding, valid_wire_tool_name};

    #[test]
    fn mcp_reference_policy_is_scoped_to_mcp_families() {
        let schema=serde_json::json!({"type":"object","$defs":{"x":{"type":"string"}},"properties":{"v":{"$ref":"#/$defs/x"}}});
        assert!(super::valid_mcp_family("mcp_aaaaaaaaaaaaaaaaaaaa"));
        assert!(!super::valid_mcp_family("other"));
        assert!(crate::mcp_schema::safe_mcp_schema(&schema));
        assert!(super::contains_remote_ref(&schema));
    }

    #[test]
    fn fixed_names_preserve_family_and_opaque_names() {
        for name in ["save_bot_memory", "edit_bot_memory", "forget_bot_memory", "change_bot_role",
            "set_bot_memory_scope", "set_memory_group_access", "save_owner_fact",
            "edit_owner_fact", "forget_owner_fact", "save_owner_skill", "edit_owner_skill", "create_bot_routine", "edit_bot_routine", "set_bot_routine", "test_bot_routine", "read_bot_routine"] {
            assert!(valid_wire_tool_name(name));
            assert!(valid_persistence_binding(name, name));
            assert!(!valid_persistence_binding("other", name));
        }
        assert!(valid_wire_tool_name("gamja_726561645f66696c65"));
        assert!(!plain_persistence_tool("read_file"));
        assert!(!valid_wire_tool_name("save_bot_memory_extra"));
        assert!(!valid_wire_tool_name("gamja_abc"));
    }
}
