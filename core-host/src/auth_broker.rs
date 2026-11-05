//! Core-owned provider-auth state machine and private recovery channel.
//!
//! Raw credentials remain inside this module and the credential backend.
//! Core-private replies may carry an opaque ordinary credential reference and
//! commit handle; `AuthPublicState` contains only public finite state.

use crate::credential::{
    CredentialBackend, COMPLETION_SERVICE, PREPARED_SERVICE, PROVIDER_SERVICE, RECOVERY_SERVICE,
};
use getrandom::fill;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};
use subtle::ConstantTimeEq;
use zeroize::{Zeroize,Zeroizing};

const MAX_ENVELOPE: usize = 65_536;
const MAX_PROVIDER_VALUE: usize = 32_768;
const ENVELOPE_MAGIC: &[u8; 4] = b"GAR1";
const PROVIDER_VALUE_MAGIC: &[u8; 4] = b"GPC1";

#[derive(Clone, Eq, PartialEq)]
pub struct PrivateString(Zeroizing<String>);

impl PrivateString {
    fn new(value: String) -> Self {
        Self(Zeroizing::new(value))
    }

    fn as_str(&self) -> &str {
        self.0.as_str()
    }
}

impl std::fmt::Debug for PrivateString {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("<private>")
    }
}

impl Serialize for PrivateString {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for PrivateString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer).map(Self::new)
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StableBinding {
    pub version: u8,
    pub operation_id: String,
    pub owner_id: u64,
    pub connection_id: u64,
    pub kind: String,
    pub intent_fingerprint: String,
    pub before_state_fingerprint: String,
    pub revoke_requested: bool,
    pub expires_at: i64,
    pub owner_epoch: String,
    pub provider_id: String,
    pub auth_method_id: String,
    pub config_revision: u64,
    pub auth_grant_revision: u64,
    pub public_config_fingerprint: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "provider", rename_all = "snake_case", deny_unknown_fields)]
pub enum NativeEnrollmentSpecV1 {
    AzureCli {
        resource_endpoint: String,
        api_version: String,
    },
    AwsProfile {
        profile_name: String,
        region: String,
    },
    VertexAdc {
        project_id: String,
        location: String,
    },
}

impl NativeEnrollmentSpecV1 {
    fn provider_id(&self) -> &'static str {
        match self {
            Self::AzureCli { .. } => "azure-openai",
            Self::AwsProfile { .. } => "amazon-bedrock",
            Self::VertexAdc { .. } => "google-vertex",
        }
    }

    fn execution_fingerprint(&self) -> Option<String> {
        let encoded = serde_json::to_vec(self).ok()?;
        Some(hex(Sha256::digest([
            b"gamja-native-execution-fields\0v1\0".as_slice(),
            encoded.as_slice(),
        ].concat()).as_ref()))
    }
}

pub const RECOVERY_NO_USER_EXPIRY: i64 = i64::MAX;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveryBindingSummary {
    pub provider_id: String,
    pub auth_method_id: String,
    pub config_revision: u64,
    pub auth_grant_revision: u64,
    pub public_config_fingerprint: String,
}

impl RecoveryBindingSummary {
    fn from_binding(binding: &StableBinding) -> Self {
        Self {
            provider_id: binding.provider_id.clone(),
            auth_method_id: binding.auth_method_id.clone(),
            config_revision: binding.config_revision,
            auth_grant_revision: binding.auth_grant_revision,
            public_config_fingerprint: binding.public_config_fingerprint.clone(),
        }
    }

    fn valid(&self) -> bool {
        bounded_identifier(&self.provider_id)
            && bounded_identifier(&self.auth_method_id)
            && self.config_revision > 0
            && lower_hex(&self.public_config_fingerprint, 64)
    }
}

impl StableBinding {
    fn valid(&self) -> bool {
        self.version == 1
            && lower_hex(&self.operation_id, 64)
            && lower_hex(&self.intent_fingerprint, 64)
            && lower_hex(&self.before_state_fingerprint, 64)
            && lower_hex(&self.owner_epoch, 64)
            && lower_hex(&self.public_config_fingerprint, 64)
            && self.owner_id > 0
            && self.connection_id > 0
            && matches!(
                self.kind.as_str(),
                "auth" | "disconnect" | "grant_replace" | "same_grant_refresh"
            )
            && bounded_identifier(&self.provider_id)
            && bounded_identifier(&self.auth_method_id)
            && self.config_revision > 0
            && (self.expires_at != RECOVERY_NO_USER_EXPIRY || self.kind == "disconnect")
    }
}

pub fn stable_binding_fingerprint(binding: &StableBinding) -> Option<String> {
    if !binding.valid() {
        return None;
    }
    let mut encoded = Zeroizing::new(Vec::with_capacity(512));
    encoded.extend_from_slice(b"gamja-auth-stable-binding-fingerprint\0v1\0");
    encoded.push(binding.version);
    push_fingerprint_string(&mut encoded, &binding.operation_id)?;
    encoded.extend_from_slice(&binding.owner_id.to_be_bytes());
    encoded.extend_from_slice(&binding.connection_id.to_be_bytes());
    push_fingerprint_string(&mut encoded, &binding.kind)?;
    push_fingerprint_string(&mut encoded, &binding.intent_fingerprint)?;
    push_fingerprint_string(&mut encoded, &binding.before_state_fingerprint)?;
    encoded.push(u8::from(binding.revoke_requested));
    encoded.extend_from_slice(&binding.expires_at.to_be_bytes());
    push_fingerprint_string(&mut encoded, &binding.owner_epoch)?;
    push_fingerprint_string(&mut encoded, &binding.provider_id)?;
    push_fingerprint_string(&mut encoded, &binding.auth_method_id)?;
    encoded.extend_from_slice(&binding.config_revision.to_be_bytes());
    encoded.extend_from_slice(&binding.auth_grant_revision.to_be_bytes());
    push_fingerprint_string(&mut encoded, &binding.public_config_fingerprint)?;
    Some(hex(Sha256::digest(encoded.as_slice()).as_ref()))
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecoverySelector {
    pub operation_id: String,
    pub owner_id: u64,
    pub connection_id: u64,
    pub kind: String,
    pub intent_fingerprint: String,
    pub before_state_fingerprint: String,
    pub revoke_requested: bool,
}

impl RecoverySelector {
    fn valid(&self) -> bool {
        lower_hex(&self.operation_id, 64)
            && self.owner_id > 0
            && self.connection_id > 0
            && matches!(
                self.kind.as_str(),
                "auth" | "disconnect" | "grant_replace" | "same_grant_refresh"
            )
            && lower_hex(&self.intent_fingerprint, 64)
            && lower_hex(&self.before_state_fingerprint, 64)
    }

    fn matches_binding(&self, binding: &StableBinding) -> bool {
        self.operation_id == binding.operation_id
            && self.owner_id == binding.owner_id
            && self.connection_id == binding.connection_id
            && self.kind == binding.kind
            && self.intent_fingerprint == binding.intent_fingerprint
            && self.before_state_fingerprint == binding.before_state_fingerprint
            && self.revoke_requested == binding.revoke_requested
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GenerationBinding {
    nonce: PrivateString,
    current_generation_fingerprint: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialDiscardAction {
    Inspect,
    RestoreOld,
    DiscardPrepared,
    CleanupCommitted,
    AcknowledgeTerminal,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExactDiscardTarget {
    pub exact_binding: StableBinding,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveryDiscardTarget {
    pub recovery_selector: RecoverySelector,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding_fingerprint: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(untagged)]
pub enum CredentialDiscardTarget {
    Exact(ExactDiscardTarget),
    Recovery(RecoveryDiscardTarget),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum AuthFrame {
    AuthStart {
        binding: StableBinding,
        generation: GenerationBinding,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        native_enrollment: Option<NativeEnrollmentSpecV1>,
    },
    AuthStatus {
        binding: StableBinding,
        generation: GenerationBinding,
    },
    AuthCancel {
        binding: StableBinding,
        generation: GenerationBinding,
    },
    CredentialPrepare {
        binding: StableBinding,
        generation: GenerationBinding,
        #[serde(default)]
        exact_old_reference: Option<PrivateString>,
        #[serde(default)]
        direct_secret: Option<PrivateString>,
    },
    CredentialCommit {
        binding: StableBinding,
        generation: GenerationBinding,
    },
    CredentialDiscard {
        target: CredentialDiscardTarget,
        generation: GenerationBinding,
        action: CredentialDiscardAction,
        #[serde(default)]
        commit_handle: Option<PrivateString>,
        #[serde(default)]
        adopted_prepared_ref: Option<PrivateString>,
    },
    CredentialRefresh {
        binding: StableBinding,
        generation: GenerationBinding,
    },
}

impl AuthFrame {
    fn binding(&self) -> Option<&StableBinding> {
        match self {
            Self::AuthStart { binding, .. }
            | Self::AuthStatus { binding, .. }
            | Self::AuthCancel { binding, .. }
            | Self::CredentialPrepare { binding, .. }
            | Self::CredentialCommit { binding, .. }
            | Self::CredentialRefresh { binding, .. } => Some(binding),
            Self::CredentialDiscard {
                target: CredentialDiscardTarget::Exact(target),
                ..
            } => Some(&target.exact_binding),
            Self::CredentialDiscard {
                target: CredentialDiscardTarget::Recovery(_),
                ..
            } => None,
        }
    }

    fn recovery_target(&self) -> Option<&RecoveryDiscardTarget> {
        match self {
            Self::CredentialDiscard {
                target: CredentialDiscardTarget::Recovery(target),
                ..
            } => Some(target),
            _ => None,
        }
    }

    fn generation(&self) -> &GenerationBinding {
        match self {
            Self::AuthStart { generation, .. }
            | Self::AuthStatus { generation, .. }
            | Self::AuthCancel { generation, .. }
            | Self::CredentialPrepare { generation, .. }
            | Self::CredentialCommit { generation, .. }
            | Self::CredentialDiscard { generation, .. }
            | Self::CredentialRefresh { generation, .. } => generation,
        }
    }

    pub fn discard_action(&self) -> Option<CredentialDiscardAction> {
        match self {
            Self::CredentialDiscard { action, .. } => Some(*action),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AuthState {
    Prepared,
    AwaitingUser,
    Polling,
    Committing,
    Recovering,
    Restored,
    Succeeded,
    Cancelled,
    Expired,
    Interrupted,
    Failed,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FiniteCode {
    OwnerMismatch,
    OwnerEpochUnavailable,
    OwnerEpochChanged,
    ConfigurationChanged,
    AuthMethodUnavailable,
    CallbackMismatch,
    DeviceSlowDown,
    AuthorizationExpired,
    CredentialStoreUnavailable,
    CredentialRecoveryRequired,
    CredentialCleanupUnconfirmed,
    NativeEnrollmentTooLarge,
    OperationCancelled,
    OperationInterrupted,
    InternalError,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OwnerControlState {
    Bound,
    Unbound,
    AlreadyUnbound,
    MismatchPreserved,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationAllocationState {
    Allocated,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OperationAllocationReply {
    pub version: u8,
    pub state: OperationAllocationState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub operation_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<FiniteCode>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OwnerControlReply {
    pub version: u8,
    pub state: OwnerControlState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub owner_id: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub current_owner_epoch: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum OwnerControl {
    OwnerBind {
        version: u8,
        owner_id: u64,
    },
    OwnerUnbind {
        version: u8,
        expected_owner_id: u64,
        expected_owner_epoch: String,
    },
    VaultRealmBind {
        version: u8,
        owner_id: u64,
        current_owner_epoch: String,
        vault_account_id: String,
    },
    VaultCsvBind {
        version:u8,owner_id:u64,current_owner_epoch:String,draft_id:String,row:u64,item_id:String,website:String,username:String,reservation_command:String,
    },
    VaultProfilePrepare {
        version:u8,owner_id:u64,current_owner_epoch:String,grant:serde_json::Value,
        profile_metadata_sha256:String,record_sha256:String,proxy_port:u16,proxy_secret:PrivateString,
    },
    VaultCustodySeal {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,profile_id:String,profile_revision:u64,profile_metadata_sha256:String,record_sha256:String},
    VaultCustodyStatus {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String},
    VaultCustodyBorrow {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,profile_id:String,profile_revision:u64,profile_metadata_sha256:String,record_sha256:String,custody_id:String,borrow_id:String,review_context_id:String,workflow_id:Option<String>,room_id:u64,agent_id:u64,action_id:String,review_id:String,root_request_event_id:String,run_generation:u64,execution_binding_sha256:String,execution_binding:Option<crate::mcp_transport::ExecutionBinding>,origin:String,scope:String,context_mode:String,operation:String,selector:Option<PrivateString>,url:PrivateString,url_sha256:String,payload_digest:String,call_id:Option<String>,expires_at:String},
    VaultCustodyEnd {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,custody_id:String,borrow_id:String,context_id:String},
    VaultCustodyPark {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,custody_id:String,context_id:String,room_id:u64,agent_id:u64,root_request_event_id:String,run_generation:u64,workflow_id:Option<String>},
    VaultCustodyCallStart {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,custody_id:String,borrow_id:String,action_id:String,operation:String,selector:Option<PrivateString>,url:PrivateString,url_sha256:String},
    VaultCustodyCallPoll {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,custody_id:String,borrow_id:String,action_id:String},
    VaultCustodyCaptureChunk {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,custody_id:String,borrow_id:String,action_id:String,context_id:String,capture_id:String,index:u64},
    VaultCustodyCaptureRelease {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,custody_id:String,borrow_id:String,action_id:String,context_id:String,capture_id:String},
    VaultProfileRecover {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String,profile_id:String,profile_metadata_sha256:String,record_sha256:String,pendingjournal_sha256:Option<String>},
    VaultProfileStatus {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String},
    VaultProfileRevoke {version:u8,owner_id:u64,current_owner_epoch:String,handoff_id:String},
    VaultCardMetadataStatus {
        version:u8, owner_id:u64, current_owner_epoch:String, item_id:String,
        cardholder:String, expiry_month:u8, expiry_year:u16, billing:serde_json::Value, last4:Option<String>,
    },
    VaultMetadataStatus {
        version:u8, owner_id:u64, current_owner_epoch:String, item_id:String, website:String, username:String,
    },
    OperationAllocate {
        version: u8,
        owner_id: u64,
        current_owner_epoch: String,
    },
}

#[derive(Deserialize)]
#[serde(untagged)]
enum PrivateRequest {
    Control(OwnerControl),
    Frame(AuthFrame),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AuthPublicState {
    pub version: u8,
    pub operation_id: String,
    pub state: AuthState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<FiniteCode>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub authorization_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verification_uri: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub user_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub poll_interval: Option<u64>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AuthReply {
    #[serde(flatten)]
    public: AuthPublicState,
    #[serde(skip_serializing_if = "Option::is_none")]
    prepared_ref: Option<PrivateString>,
    #[serde(skip_serializing_if = "Option::is_none")]
    commit_handle: Option<PrivateString>,
}

impl std::ops::Deref for AuthReply {
    type Target = AuthPublicState;

    fn deref(&self) -> &Self::Target {
        &self.public
    }
}

impl std::ops::DerefMut for AuthReply {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.public
    }
}

impl AuthReply {
    fn state(binding: &StableBinding, state: AuthState) -> Self {
        Self {
            public: AuthPublicState {
                version: 1,
                operation_id: binding.operation_id.clone(),
                state,
                code: None,
                authorization_url: None,
                verification_uri: None,
                user_code: None,
                poll_interval: None,
            },
            prepared_ref: None,
            commit_handle: None,
        }
    }

    fn error(binding: &StableBinding, state: AuthState, code: FiniteCode) -> Self {
        let mut reply = Self::state(binding, state);
        reply.code = Some(code);
        reply
    }

    fn selector_error(operation_id: &str, code: FiniteCode) -> Self {
        Self {
            public: AuthPublicState {
                version: 1,
                operation_id: operation_id.to_owned(),
                state: AuthState::Failed,
                code: Some(code),
                authorization_url: None,
                verification_uri: None,
                user_code: None,
                poll_interval: None,
            },
            prepared_ref: None,
            commit_handle: None,
        }
    }

    fn with_receipt(mut self, receipt: &CommitReceipt) -> Self {
        self.prepared_ref = Some(receipt.prepared_ref.clone());
        self.commit_handle = Some(receipt.commit_handle.clone());
        self
    }

    pub fn public_state(&self) -> AuthPublicState {
        self.public.clone()
    }

    pub fn prepared_ref(&self) -> Option<&str> {
        self.prepared_ref.as_ref().map(PrivateString::as_str)
    }

    pub fn commit_handle(&self) -> Option<&str> {
        self.commit_handle.as_ref().map(PrivateString::as_str)
    }
}

pub enum TransportResult {
    AwaitingUser { authorization_url: String },
    DeviceInstruction {
        verification_uri: String,
        user_code: String,
        poll_interval: u64,
    },
    SlowDown { poll_interval: u64 },
    Pending,
    Ready(Zeroizing<Vec<u8>>),
    NativeReady {
        source_generation_fingerprint: String,
        canonical_payload: Zeroizing<Vec<u8>>,
    },
    Denied,
    Unavailable,
}

/// Injectable only inside Core. Implementations never expose a credential to
/// an IPC response; a Ready value is consumed into the prepared secure item.
pub trait SyntheticAuthTransport: Send + 'static {
    fn start(
        &mut self,
        binding: &StableBinding,
        native_enrollment: Option<&NativeEnrollmentSpecV1>,
    ) -> TransportResult;
    fn status(&mut self, binding: &StableBinding) -> TransportResult;
    fn cancel(&mut self, binding: &StableBinding);
}

pub struct UnavailableAuthTransport;

impl SyntheticAuthTransport for UnavailableAuthTransport {
    fn start(
        &mut self,
        _binding: &StableBinding,
        _native_enrollment: Option<&NativeEnrollmentSpecV1>,
    ) -> TransportResult {
        TransportResult::Unavailable
    }

    fn status(&mut self, _binding: &StableBinding) -> TransportResult {
        TransportResult::Unavailable
    }

    fn cancel(&mut self, _binding: &StableBinding) {}
}

pub struct BrowserFixture {
    pub authorization_url: String,
    pub callback_path: String,
    pub callback_port: u16,
    pub state: String,
    pub pkce_verifier: Zeroizing<String>,
    pub issuer: String,
    pub audience: String,
    pub prepared_secret: Zeroizing<Vec<u8>>,
}

pub struct BrowserCallback {
    pub path: String,
    pub port: u16,
    pub state: String,
    pub pkce_verifier: Zeroizing<String>,
    pub issuer: String,
    pub audience: String,
    pub authorization_code: Zeroizing<String>,
}

pub struct DeviceFixture {
    pub verification_uri: String,
    pub user_code: String,
    pub poll_interval: u64,
    pub slow_downs: u8,
    pub prepared_secret: Zeroizing<Vec<u8>>,
}

enum SyntheticFlow {
    Browser {
        fixture: BrowserFixture,
        started: bool,
        callback_consumed: bool,
        ready: bool,
    },
    Device {
        fixture: DeviceFixture,
        started: bool,
    },
}

/// Deterministic Core-internal transport used by the closed broker fixtures.
/// It has no provider origins, client IDs, SDKs, or network access.
#[derive(Default)]
pub struct SyntheticFixtureTransport {
    flows: HashMap<String, SyntheticFlow>,
}

impl SyntheticFixtureTransport {
    pub fn register_browser(
        &mut self,
        binding: &StableBinding,
        fixture: BrowserFixture,
    ) -> Result<(), FiniteCode> {
        if !binding.valid()
            || !valid_browser_fixture(&fixture)
            || self.flows.contains_key(&binding.operation_id)
        {
            return Err(FiniteCode::ConfigurationChanged);
        }
        self.flows.insert(
            binding.operation_id.clone(),
            SyntheticFlow::Browser {
                fixture,
                started: false,
                callback_consumed: false,
                ready: false,
            },
        );
        Ok(())
    }

    pub fn register_device(
        &mut self,
        binding: &StableBinding,
        fixture: DeviceFixture,
    ) -> Result<(), FiniteCode> {
        if !binding.valid()
            || !valid_device_fixture(&fixture)
            || self.flows.contains_key(&binding.operation_id)
        {
            return Err(FiniteCode::ConfigurationChanged);
        }
        self.flows.insert(
            binding.operation_id.clone(),
            SyntheticFlow::Device {
                fixture,
                started: false,
            },
        );
        Ok(())
    }

    pub fn submit_browser_callback(
        &mut self,
        operation_id: &str,
        callback: BrowserCallback,
    ) -> Result<(), FiniteCode> {
        let Some(SyntheticFlow::Browser {
            fixture,
            started,
            callback_consumed,
            ready,
        }) = self.flows.get_mut(operation_id)
        else {
            return Err(FiniteCode::CallbackMismatch);
        };
        let matches = *started
            && !*callback_consumed
            && callback.path == fixture.callback_path
            && callback.port == fixture.callback_port
            && constant_eq(&callback.state, &fixture.state)
            && constant_eq(&callback.pkce_verifier, &fixture.pkce_verifier)
            && callback.issuer == fixture.issuer
            && callback.audience == fixture.audience
            && !callback.authorization_code.is_empty()
            && callback.authorization_code.len() <= 4096;
        if !matches {
            return Err(FiniteCode::CallbackMismatch);
        }
        *callback_consumed = true;
        *ready = true;
        Ok(())
    }
}

impl SyntheticAuthTransport for SyntheticFixtureTransport {
    fn start(
        &mut self,
        binding: &StableBinding,
        native_enrollment: Option<&NativeEnrollmentSpecV1>,
    ) -> TransportResult {
        if native_enrollment.is_some() {
            return TransportResult::Unavailable;
        }
        match self.flows.get_mut(&binding.operation_id) {
            Some(SyntheticFlow::Browser {
                fixture, started, ..
            }) => {
                *started = true;
                TransportResult::AwaitingUser {
                    authorization_url: fixture.authorization_url.clone(),
                }
            }
            Some(SyntheticFlow::Device { fixture, started }) => {
                *started = true;
                TransportResult::DeviceInstruction {
                    verification_uri: fixture.verification_uri.clone(),
                    user_code: fixture.user_code.clone(),
                    poll_interval: fixture.poll_interval,
                }
            }
            None => TransportResult::Unavailable,
        }
    }

    fn status(&mut self, binding: &StableBinding) -> TransportResult {
        match self.flows.get_mut(&binding.operation_id) {
            Some(SyntheticFlow::Browser {
                fixture,
                started,
                ready,
                ..
            }) if *started && *ready => fixture
                .prepared_secret
                .is_empty()
                .then_some(TransportResult::Unavailable)
                .unwrap_or_else(|| {
                    TransportResult::Ready(std::mem::take(&mut fixture.prepared_secret))
                }),
            Some(SyntheticFlow::Browser { started: true, .. }) => TransportResult::Pending,
            Some(SyntheticFlow::Device { fixture, started }) if *started => {
                if fixture.slow_downs > 0 {
                    fixture.slow_downs -= 1;
                    TransportResult::SlowDown {
                        poll_interval: fixture.poll_interval.saturating_add(5).min(300),
                    }
                } else if fixture.prepared_secret.is_empty() {
                    TransportResult::Unavailable
                } else {
                    TransportResult::Ready(std::mem::take(&mut fixture.prepared_secret))
                }
            }
            _ => TransportResult::Unavailable,
        }
    }

    fn cancel(&mut self, binding: &StableBinding) {
        self.flows.remove(&binding.operation_id);
    }
}

#[derive(Clone)]
pub struct BackendGeneration {
    _nonce: Zeroizing<[u8; 32]>,
    nonce_hex: Zeroizing<String>,
    fingerprint: String,
}

impl BackendGeneration {
    pub fn nonce_hex(&self) -> &str {
        &self.nonce_hex
    }

    pub fn fingerprint(&self) -> &str {
        &self.fingerprint
    }
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct PrivateHandshake<'a> {
    version: u8,
    nonce: &'a str,
    current_generation_fingerprint: &'a str,
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct SecureEnvelopeHeader {
    binding: StableBinding,
    originating_generation_fingerprint: String,
    exact_old_reference: Option<PrivateString>,
    prepared_destination: Option<PrivateString>,
    private_grant_identity: Option<PrivateString>,
    #[serde(default)]
    commit_handle: Option<PrivateString>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum CompletionOutcome {
    Restored,
    Cleaned,
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum PostconditionTargetV1 {
    RestoredValue {
        target_service: String,
        target_handle: PrivateString,
        value_verifier_version: u8,
        value_verifier: PrivateString,
    },
    DisconnectOldAbsent {
        target_service: String,
        target_handle: PrivateString,
    },
    GrantReplacementOldAbsent {
        target_service: String,
        target_handle: PrivateString,
    },
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct CompletionReceipt {
    version: u8,
    locator: RecoverySelector,
    fingerprint_version: u8,
    binding_fingerprint: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    binding_summary: Option<RecoveryBindingSummary>,
    outcome: CompletionOutcome,
    completed_at: i64,
    postcondition: PostconditionTargetV1,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryInspectState {
    Recovering,
    Restored,
    Cleaned,
    Absent,
    Failed,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryMaterialState {
    Recovery,
    Prepared,
    RecoveryAndPrepared,
    None,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryReceiptState {
    Present,
    None,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveryInspectReply {
    pub operation_id: String,
    pub state: RecoveryInspectState,
    pub material_state: RecoveryMaterialState,
    pub receipt_state: RecoveryReceiptState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding_fingerprint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding_summary: Option<RecoveryBindingSummary>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<FiniteCode>,
}

struct RecoverySnapshot {
    binding: Option<StableBinding>,
    fingerprint: Option<String>,
    binding_summary: Option<RecoveryBindingSummary>,
    material_state: RecoveryMaterialState,
    receipt: Option<CompletionReceipt>,
}

#[derive(Clone)]
struct CommitReceipt {
    prepared_ref: PrivateString,
    commit_handle: PrivateString,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum OperationPhase {
    AwaitingUser,
    Polling,
    RecoveryStaged,
    Prepared,
    Committing,
    Recovering,
    Interrupted,
    Terminal,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum DirectPrepareMaterialState {
    Absent,
    RecoveryOnly,
    Prepared,
    Mismatch,
}

#[derive(Clone)]
struct ActiveOperation {
    binding: StableBinding,
    originating_generation_fingerprint: String,
    phase: OperationPhase,
    next_poll_at: Option<i64>,
    poll_interval: Option<u64>,
    native_enrollment: Option<NativeEnrollmentSpecV1>,
}

#[derive(Clone)]
struct BoundOwner {
    owner_id: u64,
    epoch: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct CommittedProviderHeaderV1 {
    version: u8,
    owner_id: u64,
    connection_id: u64,
    provider_id: String,
    auth_method_id: String,
    config_revision: u64,
    auth_grant_revision: u64,
    public_config_fingerprint: String,
    execution_fields_fingerprint: String,
    source_generation_fingerprint: String,
    native_enrollment: NativeEnrollmentSpecV1,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum CommittedCredentialKindV1 {
    AzureApiKey,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct CommittedAzureApiKeyHeaderV1 {
    version: u8,
    credential_kind: CommittedCredentialKindV1,
    owner_id: u64,
    connection_id: u64,
    provider_id: String,
    auth_method_id: String,
    config_revision: u64,
    auth_grant_revision: u64,
    public_config_fingerprint: String,
}

#[derive(Clone)]
enum CommittedProviderAuthority {
    Native {
        execution_fields_fingerprint: String,
        source_generation_fingerprint: String,
        native_enrollment: NativeEnrollmentSpecV1,
    },
    AzureApiKey,
}

struct DecodedProviderValue {
    version: u8,
    owner_id: u64,
    connection_id: u64,
    provider_id: String,
    auth_method_id: String,
    config_revision: u64,
    auth_grant_revision: u64,
    public_config_fingerprint: String,
    authority: CommittedProviderAuthority,
    payload: Zeroizing<Vec<u8>>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderBindingRequest {
    pub credential_ref: String,
    pub owner_id: u64,
    pub owner_epoch: String,
    pub connection_id: u64,
    pub config_revision: u64,
    pub auth_grant_revision: u64,
    pub provider_id: String,
    pub auth_method_id: String,
    pub public_config_fingerprint: String,
    pub descriptor_revision: u64,
    pub protocol: crate::provider_transport::ProviderProtocol,
    pub model_id: String,
    pub public_fields: serde_json::Value,
}

pub struct AuthorizedProviderPermit {
    handle: Zeroizing<String>,
    pub(crate) generation_fingerprint: String,
    pub(crate) owner_id: u64,
    pub(crate) owner_epoch: String,
    pub(crate) connection_id: u64,
    pub(crate) config_revision: u64,
    pub(crate) auth_grant_revision: u64,
    pub(crate) provider_id: String,
    pub(crate) auth_method_id: String,
    pub(crate) public_config_fingerprint: String,
    pub(crate) descriptor_revision: u64,
    pub(crate) protocol: crate::provider_transport::ProviderProtocol,
    pub(crate) model_id: String,
    pub(crate) public_fields: serde_json::Value,
    pub(crate) credential_ref: PrivateString,
    authority: CommittedProviderAuthority,
    pub(crate) cancel: crate::provider_transport::BoundedCancellation,
    lease_registry: Option<ProviderPermitRegistry>,
}

impl std::fmt::Debug for AuthorizedProviderPermit {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AuthorizedProviderPermit")
            .field("handle", &"<private>")
            .field("connection_id", &self.connection_id)
            .field("descriptor_revision", &self.descriptor_revision)
            .field("protocol", &self.protocol)
            .field("model_id", &self.model_id)
            .finish_non_exhaustive()
    }
}

impl AuthorizedProviderPermit {
    pub(crate) fn handle(&self) -> &str { self.handle.as_str() }

    pub(crate) fn native_source(
        &self,
    ) -> Option<(&str, &NativeEnrollmentSpecV1, &str)> {
        match &self.authority {
            CommittedProviderAuthority::Native {
                native_enrollment,
                source_generation_fingerprint,
                ..
            } => Some((
                self.credential_ref.as_str(),
                native_enrollment,
                source_generation_fingerprint,
            )),
            CommittedProviderAuthority::AzureApiKey => None,
        }
    }

    pub(crate) fn is_azure_api_key(&self) -> bool {
        matches!(self.authority, CommittedProviderAuthority::AzureApiKey)
    }
}

impl Drop for AuthorizedProviderPermit {
    fn drop(&mut self) {
        if let Some(registry) = self.lease_registry.take() {
            registry.release(self.handle.as_str());
        }
    }
}

pub struct ResolvedProviderPermit {
    permit: AuthorizedProviderPermit,
    payload: Zeroizing<Vec<u8>>,
}

impl ResolvedProviderPermit {
    pub(crate) fn into_parts(self) -> (AuthorizedProviderPermit, Zeroizing<Vec<u8>>) {
        (self.permit, self.payload)
    }
}

struct ProviderPermitEntry {
    permit: AuthorizedProviderPermit,
    claimed: bool,
}

#[derive(Clone, Default)]
pub struct ProviderPermitRegistry {
    inner: Arc<Mutex<HashMap<String, ProviderPermitEntry>>>,
}

impl ProviderPermitRegistry {
    fn insert(&self, permit: AuthorizedProviderPermit) {
        self.inner.lock().expect("provider permit registry").insert(
            permit.handle().to_owned(), ProviderPermitEntry { permit, claimed: false },
        );
    }

    pub fn authorize(
        &self,
        handle: &str,
        generation_fingerprint: &str,
    ) -> Option<AuthorizedProviderPermit> {
        let mut values = self.inner.lock().ok()?;
        let entry = values.get_mut(handle)?;
        if entry.claimed
            || !constant_eq(&entry.permit.generation_fingerprint, generation_fingerprint)
        {
            return None;
        }
        entry.claimed = true;
        let permit = &entry.permit;
        Some(AuthorizedProviderPermit {
            handle: Zeroizing::new(permit.handle().to_owned()),
            generation_fingerprint: permit.generation_fingerprint.clone(),
            owner_id: permit.owner_id,
            owner_epoch: permit.owner_epoch.clone(),
            connection_id: permit.connection_id,
            config_revision: permit.config_revision,
            auth_grant_revision: permit.auth_grant_revision,
            provider_id: permit.provider_id.clone(),
            auth_method_id: permit.auth_method_id.clone(),
            public_config_fingerprint: permit.public_config_fingerprint.clone(),
            descriptor_revision: permit.descriptor_revision,
            protocol: permit.protocol,
            model_id: permit.model_id.clone(),
            public_fields: permit.public_fields.clone(),
            credential_ref: PrivateString::new(permit.credential_ref.as_str().to_owned()),
            authority: permit.authority.clone(),
            cancel: permit.cancel.clone(),
            lease_registry: Some(self.clone()),
        })
    }

    pub fn release(&self, handle: &str) {
        if let Some(entry) = self.inner.lock().ok().and_then(|mut values| values.remove(handle)) {
            entry.permit.cancel.cancel();
        }
    }

    fn invalidate_connection(&self, owner_id: u64, connection_id: u64) {
        let Ok(mut values) = self.inner.lock() else { return; };
        let handles = values.iter().filter_map(|(handle, permit)| {
            (permit.permit.owner_id == owner_id && permit.permit.connection_id == connection_id)
                .then_some(handle.clone())
        }).collect::<Vec<_>>();
        for handle in handles {
            if let Some(entry) = values.remove(&handle) { entry.permit.cancel.cancel(); }
        }
    }

    fn invalidate_all(&self) {
        if let Ok(mut values) = self.inner.lock() {
            for (_, entry) in values.drain() { entry.permit.cancel.cancel(); }
        }
    }
}

// Slack's local slot is Core-only. Tokens never enter the Engine channel.
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct SlackSlot { version:u8, account_id:String, revision:u64, operation_id:String, state:String, app_token:Option<crate::OwnerSecret>, bot_token:Option<crate::OwnerSecret> }
fn slack_uuid(s:&str)->bool { s.len()==36 && s.bytes().enumerate().all(|(i,b)| if matches!(i,8|13|18|23) {b==b'-'} else {b.is_ascii_digit() || (b'a'..=b'f').contains(&b)}) }
pub(crate) trait SlackOwner: Send + Sync {
    fn status(&self, owner:u64)->Result<serde_json::Value,&'static str>;
    fn change(&self, owner:u64, epoch:&str, core:&str, account:&str, revision:u64, operation:&str, tokens:Option<(crate::OwnerSecret,crate::OwnerSecret)>)->Result<serde_json::Value,&'static str>;
}
struct SlackOwnerHandle<B:CredentialBackend,T:SyntheticAuthTransport>(std::sync::Arc<std::sync::Mutex<AuthBroker<B,T>>>,std::path::PathBuf);
pub(crate) fn slack_owner<B:CredentialBackend+'static,T:SyntheticAuthTransport>(broker:std::sync::Arc<std::sync::Mutex<AuthBroker<B,T>>>,path:std::path::PathBuf)->std::sync::Arc<dyn SlackOwner> {std::sync::Arc::new(SlackOwnerHandle(broker,path))}
impl<B:CredentialBackend+'static,T:SyntheticAuthTransport> SlackOwner for SlackOwnerHandle<B,T> {
    fn status(&self, owner:u64)->Result<serde_json::Value,&'static str> {
        let mut b=self.0.lock().map_err(|_|"slack_credential_unavailable")?;
        let (epoch,core)=b.slack_context(owner)?;
        let slot=b.slack_read(owner)?;
        let pending=slack_pending_read(&self.1,owner)?;
        if let Some(ref pending)=pending {
            if slot.as_ref().is_some_and(|s|s.account_id==pending.account_id && s.operation_id==pending.operation_id && s.revision==pending.expected_revision.saturating_add(1) && s.state==pending.target_state) {slack_pending_remove(&self.1,owner)?;}
            else {let mut out=slack_public(owner,&epoch,&core,slot.as_ref());out["state"]=serde_json::json!("uncertain");out["pending"]=serde_json::to_value(pending).map_err(|_|"slack_credential_unavailable")?;return Ok(out);}
        }
        Ok(slack_public(owner,&epoch,&core,slot.as_ref()))
    }
    fn change(&self, owner:u64, epoch:&str, core:&str, account:&str, revision:u64, operation:&str, tokens:Option<(crate::OwnerSecret,crate::OwnerSecret)>)->Result<serde_json::Value,&'static str> {
        if !slack_uuid(account) || !slack_uuid(operation) {return Err("slack_credential_invalid");}
        if let Some((a,b))=&tokens { if !a.as_str().starts_with("xapp-") || !b.as_str().starts_with("xoxb-") || a.as_str().len()>4096 || b.as_str().len()>4096 || a.as_str().bytes().any(|x| !x.is_ascii_graphic()) || b.as_str().bytes().any(|x| !x.is_ascii_graphic()) || a.as_str().len()<=5 || b.as_str().len()<=5 {return Err("slack_credential_invalid");} }
        let mut b=self.0.lock().map_err(|_|"slack_credential_unavailable")?;
        let (current_epoch,current_core)=b.slack_context(owner)?;
        if !constant_eq(epoch,&current_epoch) || !constant_eq(core,&current_core) {return Err("slack_credential_changed");}
        if slack_pending_read(&self.1,owner)?.is_some() {return Err("slack_credential_changed");}
        let prior=b.slack_read(owner)?;
        if prior.as_ref().map_or(0,|x|x.revision)!=revision || prior.as_ref().is_some_and(|x|x.account_id!=account || x.operation_id==operation) {return Err("slack_credential_changed");}
        if tokens.is_none() && prior.as_ref().is_none_or(|x|x.state!="stored") {return Err("slack_credential_changed");}
        let (state,app_token,bot_token)=match tokens {Some((a,b))=>("stored",Some(a),Some(b)),None=>("deleted",None,None)};
        let slot=SlackSlot{version:1,account_id:account.to_owned(),revision:revision.checked_add(1).ok_or("slack_credential_changed")?,operation_id:operation.to_owned(),state:state.to_owned(),app_token,bot_token};
        let data=zeroize::Zeroizing::new(serde_json::to_vec(&slot).map_err(|_|"slack_credential_unavailable")?);
        let pending=SlackPending{version:1,owner_id:owner,owner_epoch:epoch.to_owned(),core_epoch:core.to_owned(),account_id:account.to_owned(),expected_revision:revision,operation_id:operation.to_owned(),target_state:state.to_owned()};
        slack_pending_create(&self.1,&pending)?;
        b.slack_mutation_generation=b.slack_mutation_generation.wrapping_add(1);
        b.backend.put(crate::credential::SLACK_SERVICE,&format!("slack:{owner}"),&data).map_err(|_|"slack_credential_unavailable")?;
        slack_pending_remove(&self.1,owner)?;
        Ok(slack_public(owner,&current_epoch,&current_core,Some(&slot)))
    }
}
fn slack_public(owner:u64,epoch:&str,core:&str,slot:Option<&SlackSlot>)->serde_json::Value {
    serde_json::json!({"version":1,"owner_id":owner,"owner_epoch":epoch,"core_epoch":core,"state":slot.map_or("missing",|s|s.state.as_str()),"account_id":slot.map(|s|s.account_id.as_str()),"revision":slot.map_or(0,|s|s.revision),"operation_id":slot.map(|s|s.operation_id.as_str()),"pending":null})
}
#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct SlackPending {version:u8,owner_id:u64,owner_epoch:String,core_epoch:String,account_id:String,expected_revision:u64,operation_id:String,target_state:String}
#[cfg(unix)]
fn slack_pending_dir(path:&std::path::Path)->Result<std::fs::File,&'static str> {
    use std::os::unix::fs::{OpenOptionsExt,MetadataExt};
    let d=std::fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(path).map_err(|_|"slack_credential_unavailable")?;
    let m=d.metadata().map_err(|_|"slack_credential_unavailable")?;
    if m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o077!=0 {return Err("slack_credential_unavailable");} Ok(d)
}
#[cfg(unix)]
fn slack_pending_read(path:&std::path::Path,owner:u64)->Result<Option<SlackPending>,&'static str> {
    use std::os::fd::{AsRawFd,FromRawFd}; use std::os::unix::fs::MetadataExt; use std::io::Read;
    let dir=slack_pending_dir(path)?; let name=std::ffi::CString::new(format!("slack-pending-{owner}.json")).map_err(|_|"slack_credential_unavailable")?;
    let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
    if fd<0 {return if std::io::Error::last_os_error().kind()==std::io::ErrorKind::NotFound {Ok(None)} else {Err("slack_credential_unavailable")};}
    let f=unsafe{std::fs::File::from_raw_fd(fd)}; let m=f.metadata().map_err(|_|"slack_credential_unavailable")?;
    if !m.is_file() || m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o777!=0o600 || m.nlink()!=1 || m.len()>4096 {return Err("slack_credential_unavailable");}
    let mut data=Vec::new(); f.take(4097).read_to_end(&mut data).map_err(|_|"slack_credential_unavailable")?;
    let p:SlackPending=serde_json::from_slice(&data).map_err(|_|"slack_credential_unavailable")?;
    if p.version!=1 || p.owner_id!=owner || !slack_uuid(&p.account_id) || !slack_uuid(&p.operation_id) || !matches!(p.target_state.as_str(),"stored"|"deleted") {return Err("slack_credential_unavailable");} Ok(Some(p))
}
#[cfg(unix)]
fn slack_pending_create(path:&std::path::Path,p:&SlackPending)->Result<(),&'static str> {
    use std::os::fd::{AsRawFd,FromRawFd}; use std::io::Write;
    let dir=slack_pending_dir(path)?;let target=std::ffi::CString::new(format!("slack-pending-{}.json",p.owner_id)).map_err(|_|"slack_credential_unavailable")?;
    let temp=std::ffi::CString::new(format!(".slack-pending-{}-{}",p.owner_id,p.operation_id)).map_err(|_|"slack_credential_unavailable")?;
    let fd=unsafe{libc::openat(dir.as_raw_fd(),temp.as_ptr(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_NOFOLLOW|libc::O_CLOEXEC,0o600)};
    if fd<0 {return Err("slack_credential_unavailable");} let mut file=unsafe{std::fs::File::from_raw_fd(fd)};
    let result=(|| {let data=serde_json::to_vec(p).map_err(|_|"slack_credential_unavailable")?;file.write_all(&data).and_then(|_|file.sync_all()).map_err(|_|"slack_credential_unavailable")?;
        if unsafe{libc::linkat(dir.as_raw_fd(),temp.as_ptr(),dir.as_raw_fd(),target.as_ptr(),0)}!=0 {return Err("slack_credential_unavailable");} Ok(())})();
    unsafe{libc::unlinkat(dir.as_raw_fd(),temp.as_ptr(),0)};dir.sync_all().map_err(|_|"slack_credential_unavailable")?;result
}
#[cfg(unix)]
fn slack_pending_remove(path:&std::path::Path,owner:u64)->Result<(),&'static str> {
    use std::os::fd::AsRawFd;let dir=slack_pending_dir(path)?;let name=std::ffi::CString::new(format!("slack-pending-{owner}.json")).map_err(|_|"slack_credential_unavailable")?;
    if unsafe{libc::unlinkat(dir.as_raw_fd(),name.as_ptr(),0)}!=0 {return Err("slack_credential_unavailable");}dir.sync_all().map_err(|_|"slack_credential_unavailable")
}
pub struct AuthBroker<B: CredentialBackend, T: SyntheticAuthTransport> {
    backend: B,
    transport: T,
    slack_mutation_generation: u64,
    generation: Option<BackendGeneration>,
    owner: Option<BoundOwner>,
    vault_attestation_allowed: bool,
    vault_realm: Option<(u64, String, String, String)>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_session: Option<crate::vault::VaultSession>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_browser: Option<crate::vault_browser::Browser>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_closed: HashMap<(String,String),String>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_pending: Option<std::os::unix::net::UnixStream>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_csv_draft: Option<crate::vault_csv_import::Draft>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_profile: Option<crate::vault_profile::Handoff>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_custody_task: Option<std::sync::Arc<std::sync::Mutex<crate::vault_profile::CustodyTask>>>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_profile_cancel: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_profile_id: Option<String>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_config: Option<crate::vault::VaultConfig>,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    vault_engine_busy: bool,
    custody_revoked_scopes: HashMap<(u64,String),u64>,
    custody_scope_closed: bool,
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    custody_scope: Option<(String,Option<crate::mcp_transport::ExecutionBinding>)>,
    active: HashMap<String, ActiveOperation>,
    permits: ProviderPermitRegistry,
}

impl<B: CredentialBackend, T: SyntheticAuthTransport> AuthBroker<B, T> {
    pub fn new(backend: B, transport: T) -> Self {
        Self::with_permit_registry(backend, transport, ProviderPermitRegistry::default())
    }

    pub fn with_permit_registry(
        backend: B,
        transport: T,
        permits: ProviderPermitRegistry,
    ) -> Self {
        Self {
            backend,
            transport,
            slack_mutation_generation: 0,
            generation: None,
            owner: None,
            vault_attestation_allowed: false,
            vault_realm: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_session: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_browser: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_closed: HashMap::new(),
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_pending: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_csv_draft: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_profile: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_custody_task: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_profile_cancel: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_profile_id: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_config: None,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            vault_engine_busy: false,
            custody_revoked_scopes: HashMap::new(),
            custody_scope_closed: false,
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            custody_scope: None,
            active: HashMap::new(),
            permits,
        }
    }

    // Only the verified signed Engine private channel may attest the DB identity.
    pub(crate) fn allow_vault_attestation(&mut self) { self.vault_attestation_allowed = true; }

    fn bind_vault_realm(&mut self, owner_id: u64, epoch: &str, account: &str) -> bool {
        if !self.vault_attestation_allowed || !lower_hex(epoch, 64) || !lower_hex(account, 32) {
            return false;
        }
        let (Some(owner), Some(generation)) = (&self.owner, &self.generation) else { return false; };
        if owner.owner_id != owner_id || !constant_eq(&owner.epoch, epoch) { return false; }
        let realm = format!("{:x}", Sha256::digest(format!("gamja-vault-account-realm:v1:{account}").as_bytes()));
        // A second distinct account assertion within an epoch is not a rebind.
        if self.vault_realm.as_ref().is_some_and(|(_, _, _, prior)| prior != &realm) {
            self.vault_realm = None;
            #[cfg(all(target_os="macos",target_arch="aarch64"))] { self.vault_cancel(); }
            return false;
        }
        self.vault_realm = Some((owner_id, epoch.to_owned(), generation.fingerprint().to_owned(), realm));
        true
    }

    pub(crate) fn vault_realm(&self, owner_id: u64, epoch: &str) -> Option<&str> {
        let (id, bound_epoch, fingerprint, realm) = self.vault_realm.as_ref()?;
        if *id != owner_id || !constant_eq(bound_epoch, epoch)
            || !self.mcp_owner_valid(owner_id, epoch, fingerprint) { return None; }
        Some(realm)
    }

    pub(crate) fn custody_revoke_scopes(&mut self, owner:u64, owner_epoch:&str, core_epoch:&str, scopes:&[crate::mcp_transport::ScopeEpoch]) -> Result<(), &'static str> {
        if !self.mcp_owner_valid(owner,owner_epoch,core_epoch) || scopes.is_empty() || scopes.len()>1024 {return Err("mcp_owner_changed");}
        let new_keys=scopes.iter().map(|scope|scope.scope_id.as_str())
            .filter(|id|!self.custody_revoked_scopes.contains_key(&(owner,(*id).to_owned())))
            .collect::<std::collections::HashSet<_>>().len();
        if self.custody_revoked_scopes.len().saturating_add(new_keys)>16384 {
            self.custody_scope_closed=true;
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            if let Some(cancel)=&self.vault_profile_cancel {cancel.store(true,std::sync::atomic::Ordering::SeqCst);}
            return Err("mcp_authority_unavailable");
        }
        for scope in scopes {self.custody_revoked_scopes.entry((owner,scope.scope_id.clone())).and_modify(|v|*v=(*v).max(scope.epoch)).or_insert(scope.epoch);}
        #[cfg(all(target_os="macos",target_arch="aarch64"))]
        if self.custody_scope.as_ref().and_then(|(_,binding)|binding.as_ref()).is_some_and(|binding| binding.owner_id==owner as i64 && binding.lineage.iter().any(|item|scopes.iter().any(|s|s.scope_id==item.scope_id && item.epoch<=s.epoch))) {
            if let Some(cancel)=&self.vault_profile_cancel {cancel.store(true,std::sync::atomic::Ordering::SeqCst);}
        }
        Ok(())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn custody_scope_allowed(&self, owner:u64, handoff:&str, binding:&Option<crate::mcp_transport::ExecutionBinding>) -> bool {
        !self.custody_scope_closed && self.custody_scope.as_ref().is_some_and(|(id,current)|id==handoff && current==binding)
            && binding.as_ref().is_none_or(|b|b.owner_id==owner as i64 && b.valid(owner as i64,b.agent_id,&b.root_request_event_id,b.run_generation)
                && b.lineage.iter().all(|item|self.custody_revoked_scopes.get(&(owner,item.scope_id.clone())).is_none_or(|epoch|item.epoch>*epoch)))
    }
    #[cfg(all(test,target_os="macos",target_arch="aarch64"))]
    pub(crate) fn test_custody_scope(&mut self, handoff:&str, binding:Option<crate::mcp_transport::ExecutionBinding>) -> std::sync::Arc<std::sync::atomic::AtomicBool> {
        let cancel=std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        self.vault_profile_cancel=Some(cancel.clone());
        self.custody_scope=Some((handoff.to_owned(),binding));
        cancel
    }
    #[cfg(all(test,target_os="macos",target_arch="aarch64"))]
    pub(crate) fn test_custody_park_commit(&mut self, owner:u64, handoff:&str, binding:&Option<crate::mcp_transport::ExecutionBinding>) -> bool {
        if !self.custody_scope_allowed(owner,handoff,binding) {return false;}
        self.custody_scope=None;true
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_cancel(&mut self) {
        if let Some(socket)=self.vault_pending.take() {let _=socket.shutdown(std::net::Shutdown::Both);}
        self.vault_browser.take();
        self.vault_csv_draft.take();
        self.vault_profile_id=None;
        self.vault_custody_task.take();
        self.custody_scope=None;
        if let Some(cancel)=self.vault_profile_cancel.take(){cancel.store(true,std::sync::atomic::Ordering::SeqCst);}
        if let Some(profile)=self.vault_profile.take() {profile.cancel.store(true,std::sync::atomic::Ordering::SeqCst);drop(profile);}
        self.vault_closed.clear();
        self.vault_session.take();
        self.vault_engine_busy=false;
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_register(&mut self, socket:&std::os::unix::net::UnixStream) -> Result<(), &'static str> {
        if self.vault_pending.is_some() || self.vault_engine_busy {return Err("vault_unavailable");}
        self.vault_pending=Some(socket.try_clone().map_err(|_| "vault_unavailable")?); Ok(())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_unregister(&mut self) { self.vault_pending.take(); }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_stop(&mut self) {self.vault_cancel();}

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_snapshot(&mut self, peer:&std::os::unix::net::UnixStream, request:&crate::vault_control::Request) -> Result<(String,Option<String>), &'static str> {
        if !request.valid() {return Err("vault_unavailable");}
        let realm=self.vault_realm(request.owner_id,&request.owner_epoch).ok_or("vault_unavailable")?.to_owned();
        let manifest=match self.vault_session.as_mut() {
            None=>None,
            Some(session)=>match session.reusable_manifest() {
                Ok(value)=>Some(value),
                Err(_)=>{self.vault_session.take();return Err("vault_unavailable");}
            },
        };
        self.vault_register(peer)?;
        Ok((realm,manifest))
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_validate_existing(&mut self, request:&crate::vault_control::Request, realm:&str, expected_manifest:&str)->Result<(), &'static str> {
        if self.vault_pending.is_none() || self.vault_realm(request.owner_id,&request.owner_epoch)!=Some(realm) {return Err("vault_unavailable");}
        let current=self.vault_session.as_mut().ok_or("vault_unavailable")?.reusable_manifest();
        match current {
            Ok(value) if value==expected_manifest=>Ok(()),
            _=>{self.vault_session.take();Err("vault_unavailable")}
        }
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_install(&mut self, request:&crate::vault_control::Request, realm:&str, session:crate::vault::VaultSession)->Result<(), &'static str> {
        if self.vault_pending.is_none() || self.vault_realm(request.owner_id,&request.owner_epoch)!=Some(realm) || self.vault_session.is_some() {return Err("vault_unavailable");}
        self.vault_session=Some(session); Ok(())
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_browser_prepare(&mut self, peer:&std::os::unix::net::UnixStream, request:&crate::vault_control::Request)->Result<(String,crate::vault::VaultConfig,String,Option<crate::vault_browser::Browser>),&'static str> {
        if !request.valid() || !matches!(request.operation.as_str(),"open_and_fill"|"card_open_and_fill") || self.vault_pending.is_none() {return Err("vault_unavailable");}
        if self.vault_browser.as_ref().is_some_and(|browser|browser.window_item!=request.item_id || browser.card!=(request.operation=="card_open_and_fill") || browser.needs_close) {return Err("vault_unavailable");}
        let realm=self.vault_realm(request.owner_id,&request.owner_epoch).ok_or("vault_unavailable")?.to_owned();
        let config=self.vault_config.clone().ok_or("vault_unavailable")?;
        let session=self.vault_session.as_mut().ok_or("vault_unavailable")?;
        let manifest=session.reusable_manifest()?;session.authorize_peer(peer)?;
        Ok((realm,config,manifest,self.vault_browser.take()))
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_browser_preserve(&mut self,peer:&std::os::unix::net::UnixStream,request:&crate::vault_control::Request,mut browser:crate::vault_browser::Browser)->Result<crate::vault::Status,&'static str> {
        if !request.valid() || self.vault_pending.is_none() || self.vault_browser.is_some() || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() {return Err("vault_unavailable");}
        self.vault_session.as_ref().ok_or("vault_unavailable")?.authorize_peer(peer)?;
        if !crate::vault_control::active(peer) {return Err("vault_unavailable");}
        browser.disarm();self.vault_browser=Some(browser);
        Ok(crate::vault::Status{status:"not_filled".into(),revision:request.expected_revision,command_id:request.command_id.clone()})
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_browser_fill(&mut self, peer:&std::os::unix::net::UnixStream, stop:&std::sync::atomic::AtomicBool, request:&crate::vault_control::Request, mut browser:crate::vault_browser::Browser)->Result<crate::vault::Status,&'static str> {
        if !request.valid() || self.vault_pending.is_none() || self.vault_browser.is_some() || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() || stop.load(std::sync::atomic::Ordering::SeqCst) {return Err("vault_unavailable");}
        let mut session=self.vault_session.take().ok_or("vault_unavailable")?;
        let result=(|| {
            session.authorize_peer(peer)?;
            let origin=crate::vault::login_origin(request.website.as_deref().ok_or("vault_unavailable")?)?;
            let revision=request.expected_revision.ok_or("vault_unavailable")?;
            let password=session.fill_secret(self,request.owner_id,&request.owner_epoch,&request.item_id,revision,&origin,request.username.as_deref().ok_or("vault_unavailable")?)?;
            session.authorize_peer(peer)?;
            if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() {return Err("vault_unavailable");}
            let state=browser.fill(request.username.as_deref().ok_or("vault_unavailable")?,&password);drop(password);
            Ok(crate::vault::Status{status:state.into(),revision:Some(revision),command_id:request.command_id.clone()})
        })();
        if result.is_ok() {self.vault_session=Some(session);} else {drop(session);}
        if result.as_ref().is_ok_and(|s|s.status=="unknown") {browser.mark_unknown();}
        if result.as_ref().is_ok_and(|s|matches!(s.status.as_str(),"filled"|"filled_password_only"|"not_filled"|"unknown")) {self.vault_browser=Some(browser);} else {drop(browser);}
        result
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_browser_fill_card(&mut self,peer:&std::os::unix::net::UnixStream,stop:&std::sync::atomic::AtomicBool,request:&crate::vault_control::Request,mut browser:crate::vault_browser::Browser)->Result<crate::vault::Status,&'static str> {
        if !request.valid() || self.vault_pending.is_none() || self.vault_browser.is_some() || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() || stop.load(std::sync::atomic::Ordering::SeqCst) {return Err("vault_unavailable");}
        let mut session=self.vault_session.take().ok_or("vault_unavailable")?;
        let result=(|| {
            session.authorize_peer(peer)?;
            let revision=request.expected_revision.ok_or("vault_unavailable")?;
            let card=session.card_fill_secret(self,request.owner_id,&request.owner_epoch,&request.item_id,revision,request.card_metadata.as_ref().ok_or("vault_unavailable")?).map_err(|e|{browser.record_card_read_failure();e})?;
            session.authorize_peer(peer)?;
            if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() {return Err("vault_unavailable");}
            let state=browser.fill_card(&card);drop(card);
            Ok(crate::vault::Status{status:state.into(),revision:Some(revision),command_id:request.command_id.clone()})
        })();
        if result.is_ok(){self.vault_session=Some(session);}else{drop(session);}
        if result.as_ref().is_ok_and(|s|s.status=="unknown"){browser.mark_unknown();}
        if result.as_ref().is_ok_and(|s|matches!(s.status.as_str(),"filled"|"not_filled"|"unknown")){self.vault_browser=Some(browser);}else{drop(browser);}result
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_browser_delivery_unknown(&mut self,request:&crate::vault_control::Request) {
        if self.vault_realm(request.owner_id,&request.owner_epoch).is_none() {return;}
        if let Some(browser)=self.vault_browser.as_mut() {
            if browser.item==request.item_id && Some(browser.command.as_str())==request.command_id.as_deref() {browser.mark_unknown();}
        }
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_verify_closed_peer(&self,peer:&std::os::unix::net::UnixStream,manifest:&str)->Result<(),&'static str> {
        let config=self.vault_config.as_ref().ok_or("vault_unavailable")?;
        let path=config.core.parent().and_then(std::path::Path::parent).ok_or("vault_unavailable")?.join("core-manifest.json");
        let pin=crate::broker_runtime::SourcePin::open(&path,Some(manifest)).map_err(|_| "vault_unavailable")?;
        crate::vault_control::verify_peer(peer,manifest)?;pin.revalidate().map_err(|_| "vault_unavailable")
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_close_prepare(&mut self,peer:&std::os::unix::net::UnixStream,request:&crate::vault_control::Request)->Result<(),&'static str> {
        if !request.valid() || request.operation!="close_browser" || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() {return Err("vault_unavailable");}
        let command=request.command_id.as_ref().ok_or("vault_unavailable")?;
        if let Some(browser)=self.vault_browser.as_ref() {
            if browser.window_item!=request.item_id || &browser.window_command!=command {return Err("vault_unavailable");}
            browser.authorize_peer(peer)?;
        } else {
            let manifest=self.vault_closed.get(&(request.item_id.clone(),command.clone())).ok_or("vault_unavailable")?;
            self.vault_verify_closed_peer(peer,manifest)?;
        }
        if self.vault_pending.is_some() {return Err("vault_unavailable");}
        self.vault_pending=Some(peer.try_clone().map_err(|_| "vault_unavailable")?);Ok(())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_browser_close(&mut self, peer:&std::os::unix::net::UnixStream, request:&crate::vault_control::Request)->Result<crate::vault::Status,&'static str> {
        if !request.valid() || request.operation!="close_browser" || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() {return Err("vault_unavailable");}
        let command=request.command_id.as_ref().ok_or("vault_unavailable")?;
        if let Some(browser)=self.vault_browser.as_ref() {
            browser.authorize_peer(peer)?;
            if browser.window_item!=request.item_id || &browser.window_command!=command {return Err("vault_unavailable");}
            let manifest=browser.checked_manifest()?;
            let mut browser=self.vault_browser.take().ok_or("vault_unavailable")?;
            browser.shutdown_confirmed()?;
            self.vault_closed.insert((request.item_id.clone(),command.clone()),manifest);
        } else {
            let manifest=self.vault_closed.get(&(request.item_id.clone(),command.clone())).ok_or("vault_unavailable")?;
            self.vault_verify_closed_peer(peer,manifest)?;
        }
        Ok(crate::vault::Status{status:"closed".into(),revision:None,command_id:request.command_id.clone()})
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_card_preflight(&mut self,peer:&std::os::unix::net::UnixStream,stop:&std::sync::atomic::AtomicBool,request:&crate::vault_control::Request)->Result<(),&'static str> {
        if !request.valid() || self.vault_realm(request.owner_id,&request.owner_epoch).is_none(){return Err("vault_unavailable");}
        let mut session=self.vault_session.take().ok_or("vault_unavailable")?;
        let result=(||{session.authorize_peer(peer)?;if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer){return Err("vault_unavailable");}session.card_preflight(self,request.owner_id,&request.owner_epoch,&request.item_id)?;
            if request.operation=="card_open_and_fill" {let status=session.card_metadata_status(self,request.owner_id,&request.owner_epoch,&request.item_id,request.card_metadata.as_ref().ok_or("vault_unavailable")?)?;if status.status!="present" || status.revision!=request.expected_revision {return Err("vault_unavailable");}}Ok(())})();
        if result.is_ok(){self.vault_session=Some(session);}else{drop(session);}result
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_card_native(&mut self,peer:&std::os::unix::net::UnixStream,stop:&std::sync::atomic::AtomicBool,request:&crate::vault_control::Request,input:Option<&crate::vault_card::CardInput>)->Result<serde_json::Value,&'static str> {
        if !request.valid() || self.vault_realm(request.owner_id,&request.owner_epoch).is_none(){return Err("vault_unavailable");}
        let mut session=self.vault_session.take().ok_or("vault_unavailable")?;
        let outcome=(|| {
            session.authorize_peer(peer)?;
            if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() {return Err("vault_unavailable");}
            match request.operation.as_str() {
                "card_create"|"card_update"=>serde_json::to_value(session.card_store(peer,stop,self,request.owner_id,&request.owner_epoch,&request.item_id,request.expected_revision.ok_or("vault_unavailable")?,request.command_id.as_deref().ok_or("vault_unavailable")?,request.card_metadata.as_ref().ok_or("vault_unavailable")?,input.ok_or("vault_unavailable")?)?).map_err(|_| "vault_unavailable"),
                "card_delete" if input.is_none()=>serde_json::to_value(session.card_delete(peer,stop,self,request.owner_id,&request.owner_epoch,&request.item_id,request.expected_revision.ok_or("vault_unavailable")?,request.command_id.as_deref().ok_or("vault_unavailable")?)?).map_err(|_| "vault_unavailable"),
                "card_status" if input.is_none()=>serde_json::to_value(session.card_metadata_status(self,request.owner_id,&request.owner_epoch,&request.item_id,request.card_metadata.as_ref().ok_or("vault_unavailable")?)?).map_err(|_| "vault_unavailable"),
                _=>Err("vault_unavailable"),
            }
        })();
        if outcome.is_ok(){self.vault_session=Some(session);}else{drop(session);}outcome
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_native_execute(&mut self, peer:&std::os::unix::net::UnixStream, stop:&std::sync::atomic::AtomicBool, request:&crate::vault_control::Request, secret:Option<&Zeroizing<String>>) -> Result<crate::vault::Status,&'static str> {
        if !request.valid() || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() {return Err("vault_unavailable");}
        let mut session=self.vault_session.take().ok_or("vault_unavailable")?;
        let result=match request.operation.as_str() {
            "status" if secret.is_none()=>session.status(peer,stop,self,request.owner_id,&request.owner_epoch,&request.item_id),
            "replace" if secret.is_some()=>session.replace(peer,stop,self,request.owner_id,&request.owner_epoch,&request.item_id,request.expected_revision.ok_or("vault_unavailable")?,request.command_id.as_deref().ok_or("vault_unavailable")?,request.website.as_deref().ok_or("vault_unavailable")?,request.username.as_deref().ok_or("vault_unavailable")?,secret.ok_or("vault_unavailable")?),
            "delete" if secret.is_none()=>session.delete(peer,stop,self,request.owner_id,&request.owner_epoch,&request.item_id,request.expected_revision.ok_or("vault_unavailable")?,request.command_id.as_deref().ok_or("vault_unavailable")?),
            _=>Err("vault_unavailable"),
        };
        if result.is_ok() {self.vault_session=Some(session);} else {drop(session);}
        result
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn set_vault_config(&mut self, config:crate::vault::VaultConfig) {self.vault_config=Some(config);}

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_profile_prepare(&mut self,version:u8,owner:u64,epoch:&str,record:crate::vault_profile::Record,metadata_sha256:String,record_sha256:String,proxy_port:u16,proxy_secret:PrivateString)->bool {
        if version!=1 || !self.vault_attestation_allowed || self.vault_pending.is_some() || self.vault_engine_busy {return false;}
        if self.vault_profile.as_ref().is_some_and(|v|v.state!="closed" || v.browser.is_some()) {return false;}
        let Some(realm)=self.vault_realm(owner,epoch).map(str::to_owned) else{return false;};
        let Some(config)=self.vault_config.as_ref() else{return false;};let Some(app_data)=config.directory.parent() else{return false;};
        let grant=crate::vault_profile::Grant{record,metadata_sha256,record_sha256,proxy_port,proxy_secret:Zeroizing::new(proxy_secret.as_str().to_owned())};
        if grant.validate(app_data,owner,epoch).is_err(){return false;}
        self.vault_profile.take();self.vault_profile_cancel.take();self.vault_profile_id.take();
        let cancel=std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));self.vault_profile_cancel=Some(cancel.clone());self.vault_profile_id=Some(grant.record.handoff_id.clone());
        self.vault_profile=Some(crate::vault_profile::Handoff{grant,realm,state:"prepared",fill_status:"not_started",browser:None,attempted:false,custody:None,cancel});true
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_custody_seal(&mut self,version:u8,owner:u64,epoch:&str,handoff:&str,profile_id:&str,revision:u64,metadata_sha:&str,record_sha:&str)->serde_json::Value {
        let unavailable=||serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff,"custody_id":null});
        if version!=1 || !self.vault_attestation_allowed || self.vault_pending.is_some() || self.vault_engine_busy {return unavailable();}
        let Some(realm)=self.vault_realm(owner,epoch).map(str::to_owned) else{return unavailable();};
        let Some(config)=self.vault_config.as_ref() else{return unavailable();};
        let Some(app_data)=config.directory.parent() else{return unavailable();};
        let Some(profile)=self.vault_profile.as_mut() else{return unavailable();};
        if profile.realm!=realm || profile.grant.record.handoff_id!=handoff || profile.grant.record.profile_id!=profile_id || profile.grant.record.profile_revision!=revision
            || profile.grant.metadata_sha256!=metadata_sha || profile.grant.record_sha256!=record_sha || profile.state!="active" || profile.cancel.load(std::sync::atomic::Ordering::SeqCst)
            || profile.grant.validate(app_data,owner,epoch).is_err() {return unavailable();}
        let Some(browser)=profile.browser.as_ref() else{return unavailable();};
        let Some(lease)=browser.profile_lease() else{return unavailable();};
        if lease.lock().map_or(true,|v|v.check().is_err()) {return unavailable();}
        if let Some(custody)=profile.custody.as_ref() {return if custody.phase=="sealed" {serde_json::json!({"version":1,"status":"sealed","handoff_id":handoff,"custody_id":custody.id})}else{unavailable()};}
        let mut random=[0u8;16];if getrandom::fill(&mut random).is_err(){return unavailable();}
        let id=random.iter().map(|b|format!("{b:02x}")).collect::<String>();
        profile.custody=Some(crate::vault_profile::Custody{id:id.clone(),phase:"sealed",borrow:None,workflow:None,context_id:None,used_actions:std::collections::HashSet::new(),used_borrows:std::collections::HashSet::new()});
        serde_json::json!({"version":1,"status":"sealed","handoff_id":handoff,"custody_id":id})
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn custody_take(&mut self,owner:u64,epoch:&str,handoff:&str,custody_id:&str)->Result<(crate::vault_profile::Handoff,crate::vault::VaultSession,crate::vault::VaultConfig),&'static str> {
        if !self.vault_attestation_allowed || self.vault_pending.is_some() || self.vault_engine_busy {return Err("vault_unavailable");}
        let realm=self.vault_realm(owner,epoch).ok_or("vault_unavailable")?.to_owned();
        let config=self.vault_config.clone().ok_or("vault_unavailable")?;
        let profile=self.vault_profile.as_ref().ok_or("vault_unavailable")?;
        if profile.realm!=realm || profile.grant.record.handoff_id!=handoff || profile.cancel.load(std::sync::atomic::Ordering::SeqCst)
            || profile.custody.as_ref().is_none_or(|c|c.id!=custody_id) || profile.grant.validate(config.directory.parent().ok_or("vault_unavailable")?,owner,epoch).is_err() {return Err("vault_unavailable");}
        let lease=profile.browser.as_ref().and_then(|b|b.profile_lease()).ok_or("vault_unavailable")?;
        {let locked=lease.lock().map_err(|_|"vault_unavailable")?;crate::vault_profile::verify_parked_receipt(&profile.grant.proof(),config.directory.parent().ok_or("vault_unavailable")?,owner,epoch,custody_id,&locked)?;}
        let profile=self.vault_profile.take().ok_or("vault_unavailable")?;
        let session=match self.vault_session.take(){Some(v)=>v,None=>{self.vault_profile=Some(profile);return Err("vault_unavailable");}};
        self.vault_engine_busy=true;Ok((profile,session,config))
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn custody_restore(&mut self,owner:u64,epoch:&str,mut profile:crate::vault_profile::Handoff,mut session:crate::vault::VaultSession)->Result<(),&'static str> {
        // Retain only the exact detached slot; an Owner transition or a newer
        // handoff must never be overwritten by the old worker.
        let same_slot=self.vault_engine_busy && self.vault_pending.is_none() && self.vault_profile.is_none() && self.vault_session.is_none()
            && self.vault_attestation_allowed && self.vault_realm(owner,epoch)==Some(profile.realm.as_str())
            && self.vault_profile_id.as_deref()==Some(profile.grant.record.handoff_id.as_str())
            && self.vault_profile_cancel.as_ref().is_some_and(|cancel|std::sync::Arc::ptr_eq(cancel,&profile.cancel));
        if !same_slot {return Err("vault_unavailable");}
        let valid=!profile.cancel.load(std::sync::atomic::Ordering::SeqCst)
            && self.vault_config.as_ref().and_then(|c|c.directory.parent()).is_some_and(|path|profile.grant.validate(path,owner,epoch).is_ok())
            && session.reusable_manifest().is_ok();
        if !valid {
            // Keep ownership for explicit revoke/recovery, never for another
            // borrow. This is not proof that Chromium has stopped.
            profile.cancel.store(true,std::sync::atomic::Ordering::SeqCst);
            profile.state="unknown";profile.fill_status="unknown";
            if let Some(custody)=profile.custody.as_mut(){custody.phase="unknown";}
            self.vault_profile=Some(profile);self.vault_engine_busy=false;
            return Err("vault_unavailable");
        }
        self.vault_session=Some(session);self.vault_profile=Some(profile);self.vault_engine_busy=false;Ok(())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_custody_status(&mut self,version:u8,owner:u64,epoch:&str,handoff:&str)->serde_json::Value {
        let unavailable=||serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff,"profile_id":null,"profile_revision":null,"custody_id":null,"review_context_id":null});
        if version!=1 || !self.vault_attestation_allowed {return unavailable();}
        let Some(realm)=self.vault_realm(owner,epoch).map(str::to_owned) else{return unavailable();};
        let Some(app_data)=self.vault_config.as_ref().and_then(|c|c.directory.parent()).map(std::path::Path::to_owned) else{return unavailable();};
        let Some(profile)=self.vault_profile.as_mut() else{return unavailable();};
        if profile.realm!=realm || profile.grant.record.handoff_id!=handoff {return unavailable();}
        let Some(id)=profile.custody.as_ref().map(|c|c.id.clone()) else{return unavailable();};
        let proof=profile.grant.proof();
        let live=profile.grant.validate(&app_data,owner,epoch).is_ok() && !profile.cancel.load(std::sync::atomic::Ordering::SeqCst)
            && profile.browser.as_mut().is_some_and(|browser|browser.custody_live() && browser.profile_lease().is_some_and(|lease|lease.lock().is_ok_and(|locked|crate::vault_profile::verify_parked_receipt(&proof,&app_data,owner,epoch,&id,&locked).is_ok())));
        let Some(custody)=profile.custody.as_mut() else{return unavailable();};
        if !matches!(custody.phase,"parked"|"borrowed"|"unknown") {return unavailable();}
        if !live || custody.context_id.as_deref().is_none_or(|v|!lower_hex(v,32)) {custody.phase="unknown";}
        serde_json::json!({"version":1,"status":custody.phase,"handoff_id":handoff,"profile_id":profile.grant.record.profile_id,"profile_revision":profile.grant.record.profile_revision,"custody_id":custody.id,"review_context_id":if custody.phase=="unknown" {None} else {custody.context_id.as_deref()}})
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_profile_status(&mut self,version:u8,owner:u64,epoch:&str,handoff_id:&str,revoke:bool)->serde_json::Value {
        if version!=1 || !self.vault_attestation_allowed || self.vault_realm(owner,epoch).is_none(){return crate::vault_profile::unavailable(handoff_id);}
        let Some(current)=self.vault_profile.as_ref() else{
            if revoke && self.vault_profile_id.as_deref()==Some(handoff_id) {
                if let Some(cancel)=&self.vault_profile_cancel {cancel.store(true,std::sync::atomic::Ordering::SeqCst);}
                if let Some(socket)=self.vault_pending.as_ref(){let _=socket.shutdown(std::net::Shutdown::Both);}
            }
            return crate::vault_profile::unavailable(handoff_id);
        };
        if current.grant.record.handoff_id!=handoff_id || self.vault_realm(owner,epoch)!=Some(current.realm.as_str()) {return crate::vault_profile::unavailable(handoff_id);}
        let current=self.vault_profile.as_mut().expect("checked profile");
        if revoke && matches!(current.state,"prepared"|"unknown") && !current.attempted && current.browser.is_none() {current.cancel.store(true,std::sync::atomic::Ordering::SeqCst);current.state="closed";current.fill_status="not_started";}
        else if revoke && current.state!="closed" {current.cancel.store(true,std::sync::atomic::Ordering::SeqCst);current.state="unknown";current.fill_status="unknown";}
        else if matches!(current.state,"prepared"|"active") && !current.grant.record.valid() {current.cancel.store(true,std::sync::atomic::Ordering::SeqCst);current.state="unknown";current.fill_status="unknown";}
        if revoke {current.grant.proxy_secret.zeroize();}
        let value=current.public();
        if revoke {if let Some(socket)=self.vault_pending.as_ref(){let _=socket.shutdown(std::net::Shutdown::Both);}}
        value
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_profile_view(&mut self,peer:&std::os::unix::net::UnixStream,owner:u64,epoch:&str,handoff:&str,closing:bool)->Result<serde_json::Value,&'static str> {
        if !self.vault_attestation_allowed || self.vault_pending.is_none() || !crate::vault_control::active(peer) {return Err("vault_unavailable");}
        let profile=self.vault_profile.as_ref().ok_or("vault_unavailable")?;
        if profile.grant.record.handoff_id!=handoff || self.vault_realm(owner,epoch)!=Some(profile.realm.as_str()) || (!closing && profile.cancel.load(std::sync::atomic::Ordering::SeqCst)) || !matches!(profile.state,"prepared"|"active"|"unknown"|"closed") {return Err("vault_unavailable");}
        if !closing && (profile.custody.is_some() || !(profile.state=="prepared" || (profile.state=="active" && profile.fill_status=="not_filled"))) {return Err("vault_unavailable");}
        let config=self.vault_config.as_ref().ok_or("vault_unavailable")?;
        if closing {profile.grant.validate_for_close(config.directory.parent().ok_or("vault_unavailable")?,owner,epoch)?;} else {profile.grant.validate(config.directory.parent().ok_or("vault_unavailable")?,owner,epoch)?;}
        self.vault_session.as_mut().ok_or("vault_unavailable")?.authorize_peer(peer)?;
        Ok(serde_json::json!({"version":1,"state":"ready","handoff_id":profile.grant.record.handoff_id,"profile_id":profile.grant.record.profile_id,"profile_revision":profile.grant.record.profile_revision,"profile_status":profile.state,"fill_status":profile.fill_status,"origin":profile.grant.record.origin,"username":profile.grant.record.username,"profile_label":profile.grant.record.profile_label,"account_label":profile.grant.record.account_label}))
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_profile_take(&mut self,peer:&std::os::unix::net::UnixStream,owner:u64,epoch:&str,handoff:&str,closing:bool)->Result<(crate::vault_profile::Handoff,crate::vault::VaultSession,crate::vault::VaultConfig),&'static str> {
        self.vault_profile_view(peer,owner,epoch,handoff,closing)?;
        let config=self.vault_config.clone().ok_or("vault_unavailable")?;
        let profile=self.vault_profile.take().ok_or("vault_unavailable")?;
        match self.vault_session.take() {Some(session)=>Ok((profile,session,config)),None=>{self.vault_profile=Some(profile);Err("vault_unavailable")}}
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_profile_restore(&mut self,peer:&std::os::unix::net::UnixStream,owner:u64,epoch:&str,mut profile:crate::vault_profile::Handoff,mut session:crate::vault::VaultSession)->Result<serde_json::Value,&'static str> {
        // Only the exact detached handoff may return to this Owner's slot.
        // A replaced Owner or newer handoff must never be overwritten.
        let same_slot=self.vault_profile.is_none() && self.vault_session.is_none() && self.vault_attestation_allowed
            && self.vault_realm(owner,epoch)==Some(profile.realm.as_str())
            && self.vault_profile_id.as_deref()==Some(profile.grant.record.handoff_id.as_str())
            && self.vault_profile_cancel.as_ref().is_some_and(|cancel|std::sync::Arc::ptr_eq(cancel,&profile.cancel));
        if !same_slot {return Err("vault_unavailable");}
        let valid=self.vault_pending.is_some() && (profile.state=="closed" || !profile.cancel.load(std::sync::atomic::Ordering::SeqCst))
            && crate::vault_control::active(peer) && session.authorize_peer(peer).is_ok();
        if !valid {
            // Retain the owner for explicit recovery; do not claim close unless
            // this handoff already recorded a confirmed shutdown.
            profile.cancel.store(true,std::sync::atomic::Ordering::SeqCst);
            if profile.state!="closed" {profile.state="unknown";}
            profile.fill_status="unknown";
            if let Some(custody)=profile.custody.as_mut(){custody.phase="unknown";}
            self.vault_profile=Some(profile);return Err("vault_unavailable");
        }
        let result=profile.public();self.vault_session=Some(session);self.vault_profile=Some(profile);Ok(result)
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_profile_effect_guard(&self,peer:&std::os::unix::net::UnixStream,owner:u64,epoch:&str,realm:&str,manifest:&str,proof:&crate::vault_profile::ProfileProof,lease:&crate::vault_profile::ProfileLease)->Result<(),&'static str> {
        if !self.vault_attestation_allowed || self.vault_pending.is_none() || self.vault_realm(owner,epoch)!=Some(realm) || !crate::vault_control::active(peer) || self.vault_profile_cancel.as_ref().map_or(true,|v|v.load(std::sync::atomic::Ordering::SeqCst)) {return Err("vault_unavailable");}
        let config=self.vault_config.as_ref().ok_or("vault_unavailable")?;proof.validate(config.directory.parent().ok_or("vault_unavailable")?,owner,epoch)?;lease.check()?;
        let path=config.core.parent().and_then(std::path::Path::parent).ok_or("vault_unavailable")?.join("core-manifest.json");
        let pin=crate::broker_runtime::SourcePin::open(&path,Some(manifest)).map_err(|_|"vault_unavailable")?;
        crate::vault_control::verify_peer(peer,manifest)?;pin.revalidate().map_err(|_|"vault_unavailable")?;Ok(())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_engine_snapshot(&mut self, owner:u64, epoch:&str, item:&str, website:&str, username:&str)->Result<(String,crate::vault::VaultConfig,bool), &'static str> {
        if !self.vault_attestation_allowed || self.vault_engine_busy || self.vault_pending.is_some()
            || !lower_hex(item,32) || crate::vault::login_origin(website).is_err() || !crate::vault::valid_username(username) {return Err("vault_unavailable");}
        let realm=self.vault_realm(owner,epoch).ok_or("vault_unavailable")?.to_owned();
        let config=self.vault_config.clone().ok_or("vault_unavailable")?;
        let existing=match self.vault_session.as_mut() {
            None=>false,
            Some(session)=>match session.reusable_manifest() {
                Ok(_)=>true, Err(_)=>{self.vault_session.take();return Err("vault_unavailable");}
            },
        };
        self.vault_engine_busy=true; Ok((realm,config,existing))
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_engine_install(&mut self, owner:u64, epoch:&str, realm:&str, session:crate::vault::VaultSession)->Result<(), &'static str> {
        if !self.vault_engine_busy || !self.vault_attestation_allowed || self.vault_pending.is_some()
            || self.vault_realm(owner,epoch)!=Some(realm) || self.vault_session.is_some() {return Err("vault_unavailable");}
        self.vault_session=Some(session); Ok(())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_engine_read(&mut self, owner:u64, epoch:&str, realm:&str, item:&str, website:&str, username:&str)->Result<crate::vault::MetadataStatus,&'static str> {
        if !self.vault_engine_busy || !self.vault_attestation_allowed || self.vault_pending.is_some() || self.vault_realm(owner,epoch)!=Some(realm) {return Err("vault_unavailable");}
        let mut session=self.vault_session.take().ok_or("vault_unavailable")?;
        let result=session.metadata_status(self,owner,epoch,item,website,username);
        if result.is_ok() {self.vault_session=Some(session);} else {drop(session);}
        result
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_csv_expire(&mut self) {
        if self.vault_csv_draft.as_ref().is_some_and(|d|self.owner.as_ref().is_none_or(|o|!d.current(o.owner_id,&o.epoch))) {self.vault_csv_draft.take();}
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_csv_create(&mut self,peer:&std::os::unix::net::UnixStream,owner:u64,epoch:&str,raw:Zeroizing<Vec<u8>>)->Result<serde_json::Value,&'static str> {
        self.vault_csv_expire();
        if self.vault_csv_draft.is_some() || self.vault_realm(owner,epoch).is_none(){return Err("vault_unavailable");}
        self.vault_session.as_ref().ok_or("vault_unavailable")?.authorize_peer(peer)?;
        let parsed=crate::vault_csv::parse_owned(raw).map_err(|_| "vault_unavailable")?;
        let draft=crate::vault_csv_import::Draft::new(owner,epoch,parsed)?;
        let summary=serde_json::to_value(draft.summary()).map_err(|_| "vault_unavailable")?;
        self.vault_csv_draft=Some(draft);Ok(summary)
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_csv_page(&mut self,peer:&std::os::unix::net::UnixStream,owner:u64,epoch:&str,id:&str,offset:usize,limit:usize)->Result<serde_json::Value,&'static str> {
        self.vault_csv_expire();if self.vault_realm(owner,epoch).is_none(){return Err("vault_unavailable");}
        self.vault_session.as_ref().ok_or("vault_unavailable")?.authorize_peer(peer)?;
        let draft=self.vault_csv_draft.as_mut().filter(|d|d.matches(id,owner,epoch)).ok_or("vault_unavailable")?;
        let page=serde_json::to_value(draft.page(offset,limit)?).map_err(|_| "vault_unavailable")?;
        draft.touch();Ok(page)
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_csv_cancel(&mut self,peer:&std::os::unix::net::UnixStream,owner:u64,epoch:&str,id:&str)->Result<serde_json::Value,&'static str> {
        self.vault_csv_expire();if self.vault_realm(owner,epoch).is_none(){return Err("vault_unavailable");}
        self.vault_session.as_ref().ok_or("vault_unavailable")?.authorize_peer(peer)?;
        match self.vault_csv_draft.as_ref() {
            None=>{},
            Some(draft) if draft.matches(id,owner,epoch)=>{self.vault_csv_draft.take();},
            Some(_)=>return Err("vault_unavailable"),
        }
        Ok(serde_json::json!({"version":1,"status":"cancelled"}))
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub(crate) fn vault_csv_row(&mut self,peer:&std::os::unix::net::UnixStream,stop:&std::sync::atomic::AtomicBool,request:&crate::vault_control::Request)->Result<serde_json::Value,&'static str> {
        self.vault_csv_expire();
        if !request.valid() || self.vault_realm(request.owner_id,&request.owner_epoch).is_none(){return Err("vault_unavailable");}
        let id=request.draft_id.as_deref().ok_or("vault_unavailable")?;let row=usize::try_from(request.row.ok_or("vault_unavailable")?).map_err(|_| "vault_unavailable")?;
        if !self.vault_csv_draft.as_ref().is_some_and(|d|d.matches(id,request.owner_id,&request.owner_epoch)){return Err("vault_unavailable");}
        let mut session=self.vault_session.take().ok_or("vault_unavailable")?;
        let mut draft=self.vault_csv_draft.take().ok_or("vault_unavailable")?;
        let outcome=(|| {
            session.authorize_peer(peer)?;
            if stop.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(peer) || self.vault_realm(request.owner_id,&request.owner_epoch).is_none() || !draft.current(request.owner_id,&request.owner_epoch) {return Err("vault_unavailable");}
            if request.operation=="csv_skip_row" {draft.skip(row)}
            else if request.operation=="csv_store_row" {
                draft.store(&mut session,self,peer,stop,row,&request.item_id,request.command_id.as_deref().ok_or("vault_unavailable")?)
            } else {draft.reconcile(&mut session,self,row,&request.item_id,request.command_id.as_deref().ok_or("vault_unavailable")?)}
        })();
        if outcome.as_ref().is_ok_and(|s|s.status!="unknown") {self.vault_session=Some(session);} else {drop(session);}
        self.vault_csv_draft=Some(draft);
        serde_json::to_value(outcome?).map_err(|_| "vault_unavailable")
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_csv_bind(&mut self,version:u8,owner:u64,epoch:&str,id:&str,row:u64,item:&str,website:&str,username:&str,command:&str)->bool {
        self.vault_csv_expire();
        if version!=1 || !self.vault_attestation_allowed || self.vault_realm(owner,epoch).is_none() || !lower_hex(id,32) || !lower_hex(item,32) || !lower_hex(command,32) || !(2..=5001).contains(&row)
            || !crate::vault::valid_username(username) || crate::vault::login_origin(website).ok().as_deref()!=Some(website) {return false;}
        self.vault_csv_draft.as_mut().filter(|d|d.matches(id,owner,epoch)).is_some_and(|draft|{
            if !draft.bind(row as usize,item,website,username,command) {return false;}
            draft.touch();true
        })
    }

    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_engine_card_snapshot(&mut self,owner:u64,epoch:&str,item:&str,metadata:&crate::vault_card::CardMetadata)->Result<(String,crate::vault::VaultConfig,bool),&'static str> {
        if !self.vault_attestation_allowed || self.vault_engine_busy || self.vault_pending.is_some() || !lower_hex(item,32) || !metadata.valid(true) {return Err("vault_unavailable");}
        let realm=self.vault_realm(owner,epoch).ok_or("vault_unavailable")?.to_owned();
        let config=self.vault_config.clone().ok_or("vault_unavailable")?;
        let existing=match self.vault_session.as_mut() {None=>false,Some(session)=>match session.reusable_manifest(){Ok(_)=>true,Err(_)=>{self.vault_session.take();return Err("vault_unavailable");}}};
        self.vault_engine_busy=true;Ok((realm,config,existing))
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    fn vault_engine_card_read(&mut self,owner:u64,epoch:&str,realm:&str,item:&str,metadata:&crate::vault_card::CardMetadata)->Result<crate::vault_card::CardMetadataStatus,&'static str> {
        if !self.vault_engine_busy || !self.vault_attestation_allowed || self.vault_pending.is_some() || self.vault_realm(owner,epoch)!=Some(realm) {return Err("vault_unavailable");}
        let mut session=self.vault_session.take().ok_or("vault_unavailable")?;
        let result=session.card_metadata_status(self,owner,epoch,item,metadata);
        if result.is_ok() {self.vault_session=Some(session);} else {drop(session);}result
    }

    pub fn permit_registry(&self) -> ProviderPermitRegistry { self.permits.clone() }

    // Only Core's fixed Slack transport may request this pair; it never crosses Engine IPC.
    pub(crate) fn slack_generation(&self)->u64 {self.slack_mutation_generation}
    pub(crate) fn slack_pending(&self,path:&std::path::Path,owner:u64)->Result<bool,&'static str> {Ok(slack_pending_read(path,owner)?.is_some())}
    pub(crate) fn slack_tokens(&mut self,owner:u64,epoch:&str,core:&str,account:&str,revision:u64)->Result<(crate::OwnerSecret,crate::OwnerSecret),&'static str> {
        let (actual_epoch,actual_core)=self.slack_context(owner)?;
        if !constant_eq(epoch,&actual_epoch) || !constant_eq(core,&actual_core) {return Err("slack_credential_changed");}
        let slot=self.slack_read(owner)?.ok_or("slack_credential_unavailable")?;
        if slot.state!="stored" || slot.account_id!=account || slot.revision!=revision {return Err("slack_credential_changed");}
        Ok((slot.app_token.ok_or("slack_credential_unavailable")?,slot.bot_token.ok_or("slack_credential_unavailable")?))
    }

    fn slack_context(&self, owner:u64)->Result<(String,String),&'static str> {
        let current=self.owner.as_ref().filter(|o|owner>0 && o.owner_id==owner).ok_or("slack_credential_changed")?;
        let generation=self.generation.as_ref().ok_or("slack_credential_unavailable")?;
        Ok((current.epoch.clone(),generation.fingerprint().to_owned()))
    }
    fn slack_read(&mut self,owner:u64)->Result<Option<SlackSlot>,&'static str> {
        let value=self.backend.get(crate::credential::SLACK_SERVICE,&format!("slack:{owner}")).map_err(|_|"slack_credential_unavailable")?;
        let Some(value)=value else {return Ok(None)};
        let value=zeroize::Zeroizing::new(value);
        let slot:SlackSlot=serde_json::from_slice(&value).map_err(|_|"slack_credential_unavailable")?;
        let valid=slot.version==1 && slot.revision>0 && slack_uuid(&slot.account_id) && slack_uuid(&slot.operation_id) && match slot.state.as_str() {"stored"=>slot.app_token.is_some() && slot.bot_token.is_some(),"deleted"=>slot.app_token.is_none() && slot.bot_token.is_none(),_=>false};
        if !valid {return Err("slack_credential_unavailable");} Ok(Some(slot))
    }

    pub(crate) fn mcp_owner_valid(&self, owner_id:u64, owner_epoch:&str, core_epoch:&str) -> bool {
        self.owner.as_ref().is_some_and(|owner| owner.owner_id==owner_id && constant_eq(&owner.epoch,owner_epoch))
            && self.generation.as_ref().is_some_and(|generation| constant_eq(generation.fingerprint(),core_epoch))
    }

    pub(crate) fn publisher_owner_valid(&self, owner_id: u64) -> bool {
        self.generation.is_some() && self.owner.as_ref().is_some_and(|owner| owner.owner_id == owner_id)
    }

    /// Only a trusted native Owner input path may supply the zeroized value.
    pub(crate) fn mcp_secret_put(&mut self, owner_id:u64, owner_epoch:&str, server_id:&str, reference:&str, value:&Zeroizing<Vec<u8>>) -> Result<(), &'static str> {
        if !cfg!(any(test,all(target_os="macos",target_arch="aarch64"))) {return Err("mcp_secret_unavailable");}
        let owner=self.owner.as_ref().ok_or("mcp_owner_unavailable")?;
        if owner.owner_id!=owner_id || !constant_eq(&owner.epoch,owner_epoch) || !mcp_secret_reference(reference,owner_id,server_id) { return Err("mcp_owner_changed"); }
        if value.is_empty() || value.len()>32768 || std::str::from_utf8(value.as_slice()).is_err() { return Err("mcp_secret_invalid"); }
        self.backend.put(crate::credential::MCP_SERVICE,reference,value.as_slice()).map_err(|_| "mcp_secret_unavailable")
    }

    /// Core-only MCP bearer access. Never serialize the returned value to Engine.
    pub(crate) fn mcp_secret_get(&mut self, owner_id: u64, owner_epoch: &str, server_id: &str, reference: &str) -> Result<Option<Zeroizing<Vec<u8>>>, &'static str> {
        if !cfg!(any(test,all(target_os="macos",target_arch="aarch64"))) {return Err("mcp_secret_unavailable");}
        let owner = self.owner.as_ref().ok_or("mcp_owner_unavailable")?;
        if owner.owner_id != owner_id || !constant_eq(&owner.epoch, owner_epoch)
            || !mcp_secret_reference(reference, owner_id, server_id) { return Err("mcp_owner_changed"); }
        self.backend.get(crate::credential::MCP_SERVICE, reference)
            .map(|v| v.map(Zeroizing::new)).map_err(|_| "mcp_secret_unavailable")
    }

    pub fn issue_provider_binding(
        &mut self,
        request: ProviderBindingRequest,
        generation: &GenerationBinding,
    ) -> Result<String, FiniteCode> {
        if !self.generation_matches(generation) {
            return Err(FiniteCode::OperationInterrupted);
        }
        let owner = self.owner.as_ref().ok_or(FiniteCode::OwnerEpochUnavailable)?;
        let compiled = crate::provider_transport::compiled_provider_binding(
            &request.provider_id,
            &request.auth_method_id,
            request.protocol,
            request.descriptor_revision,
            &request.model_id,
            &request.public_fields,
        );
        if owner.owner_id != request.owner_id
            || !constant_eq(&owner.epoch, &request.owner_epoch)
            || request.connection_id == 0
            || request.descriptor_revision == 0
            || !ordinary_reference_parts_match(
                &request.credential_ref,
                request.owner_id,
                request.connection_id,
            )
            || compiled.is_none()
        {
            return Err(FiniteCode::ConfigurationChanged);
        }
        let (protocol, descriptor_revision) = compiled.expect("validated compiled descriptor");
        let value = self.backend.get(PROVIDER_SERVICE, &request.credential_ref)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
            .ok_or(FiniteCode::CredentialRecoveryRequired)?;
        let decoded = decode_provider_value(Zeroizing::new(value))
            .ok_or(FiniteCode::CredentialRecoveryRequired)?;
        if decoded.version != 1
            || decoded.owner_id != request.owner_id
            || decoded.connection_id != request.connection_id
            || decoded.config_revision != request.config_revision
            || decoded.auth_grant_revision != request.auth_grant_revision
            || decoded.provider_id != request.provider_id
            || decoded.auth_method_id != request.auth_method_id
            || !constant_eq(
                &decoded.public_config_fingerprint,
                &request.public_config_fingerprint,
            )
            || !authority_matches_request(&decoded.authority, &request)
        {
            return Err(FiniteCode::ConfigurationChanged);
        }
        let mut handle_bytes = Zeroizing::new([0u8; 32]);
        fill(handle_bytes.as_mut()).map_err(|_| FiniteCode::InternalError)?;
        let handle = hex(handle_bytes.as_ref());
        let generation_fingerprint = self.generation.as_ref()
            .ok_or(FiniteCode::OperationInterrupted)?.fingerprint.clone();
        self.permits.insert(AuthorizedProviderPermit {
            handle: Zeroizing::new(handle.clone()),
            generation_fingerprint,
            owner_id: request.owner_id,
            owner_epoch: request.owner_epoch,
            connection_id: request.connection_id,
            config_revision: request.config_revision,
            auth_grant_revision: request.auth_grant_revision,
            provider_id: request.provider_id,
            auth_method_id: request.auth_method_id,
            public_config_fingerprint: request.public_config_fingerprint,
            descriptor_revision,
            protocol,
            model_id: request.model_id,
            public_fields: request.public_fields,
            credential_ref: PrivateString::new(request.credential_ref),
            authority: decoded.authority,
            cancel: crate::provider_transport::BoundedCancellation::new(),
            lease_registry: None,
        });
        Ok(handle)
    }

    /// Claims a single-use permit and rereads the secure GPC1 value while the
    /// broker authority is locked. Callers must release the broker lock before
    /// constructing an SDK client or starting provider I/O.
    pub fn authorize_provider_source(
        &mut self,
        handle: &str,
        generation_fingerprint: &str,
    ) -> Result<ResolvedProviderPermit, FiniteCode> {
        let permit = self.permits.authorize(handle, generation_fingerprint)
            .ok_or(FiniteCode::OperationInterrupted)?;
        let value = self.backend.get(PROVIDER_SERVICE, permit.credential_ref.as_str())
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
            .ok_or(FiniteCode::CredentialRecoveryRequired)?;
        let decoded = decode_provider_value(Zeroizing::new(value))
            .ok_or(FiniteCode::CredentialRecoveryRequired)?;
        if decoded.version != 1
            || decoded.owner_id != permit.owner_id
            || decoded.connection_id != permit.connection_id
            || decoded.config_revision != permit.config_revision
            || decoded.auth_grant_revision != permit.auth_grant_revision
            || decoded.provider_id != permit.provider_id
            || decoded.auth_method_id != permit.auth_method_id
            || !constant_eq(&decoded.public_config_fingerprint, &permit.public_config_fingerprint)
            || !committed_authority_matches(&decoded.authority, &permit.authority)
        {
            return Err(FiniteCode::ConfigurationChanged);
        }
        Ok(ResolvedProviderPermit { permit, payload: decoded.payload })
    }

    pub fn current_generation_binding(&self) -> Option<GenerationBinding> {
        let generation = self.generation.as_ref()?;
        Some(GenerationBinding {
            nonce: PrivateString::new(generation.nonce_hex().to_owned()),
            current_generation_fingerprint: generation.fingerprint().to_owned(),
        })
    }

    pub fn current_generation_fingerprint(&self) -> Option<&str> {
        self.generation.as_ref().map(BackendGeneration::fingerprint)
    }

    #[doc(hidden)]
    pub fn transport_mut(&mut self) -> &mut T {
        &mut self.transport
    }

    /// Replaces the current backend generation and writes its nonce exactly
    /// once to the caller-provided private framed channel.
    pub fn attach_backend(
        &mut self,
        output: &mut impl Write,
    ) -> Result<BackendGeneration, &'static str> {
        if self.generation.is_some() {
            let interrupted = self
                .active
                .values()
                .filter(|operation| {
                    !matches!(operation.phase, OperationPhase::Terminal | OperationPhase::Interrupted)
                })
                .map(|operation| operation.binding.clone())
                .collect::<Vec<_>>();
            for binding in interrupted {
                self.transport.cancel(&binding);
                if let Some(operation) = self.active.get_mut(&binding.operation_id) {
                    if !matches!(
                        operation.phase,
                        OperationPhase::Committing | OperationPhase::Recovering
                    ) {
                        operation.phase = OperationPhase::Interrupted;
                    }
                }
            }
        }
        let mut nonce = Zeroizing::new([0u8; 32]);
        fill(nonce.as_mut()).map_err(|_| "auth_broker_unavailable")?;
        let nonce_hex = Zeroizing::new(hex(nonce.as_ref()));
        let fingerprint = hex(Sha256::digest(nonce.as_ref()).as_ref());
        let generation = BackendGeneration {
            _nonce: nonce,
            nonce_hex,
            fingerprint,
        };
        let handshake = Zeroizing::new(
            serde_json::to_vec(&PrivateHandshake {
                version: 1,
                nonce: generation.nonce_hex(),
                current_generation_fingerprint: generation.fingerprint(),
            })
            .map_err(|_| "auth_broker_unavailable")?,
        );
        let size = u32::try_from(handshake.len()).map_err(|_| "auth_broker_unavailable")?;
        output
            .write_all(&size.to_be_bytes())
            .and_then(|_| output.write_all(&handshake))
            .map_err(|_| "auth_broker_unavailable")?;
        self.generation = Some(generation.clone());
        self.owner = None;
        self.vault_realm = None;
        #[cfg(all(target_os="macos",target_arch="aarch64"))] { self.vault_cancel(); }
        self.vault_attestation_allowed = false;
        #[cfg(all(target_os="macos",target_arch="aarch64"))] { self.vault_config=None; }
        self.permits.invalidate_all();
        Ok(generation)
    }

    pub fn bind_owner(&mut self, owner_id: u64) -> Result<OwnerControlReply, &'static str> {
        self.vault_realm = None;
        #[cfg(all(target_os="macos",target_arch="aarch64"))] { self.vault_cancel(); }
        if owner_id == 0 || self.generation.is_none() {
            return Err("owner_epoch_unavailable");
        }
        let interrupted = self
            .active
            .values()
            .filter(|operation| {
                !matches!(operation.phase, OperationPhase::Terminal | OperationPhase::Interrupted)
            })
            .map(|operation| operation.binding.clone())
            .collect::<Vec<_>>();
        for binding in interrupted {
            self.transport.cancel(&binding);
            if let Some(operation) = self.active.get_mut(&binding.operation_id) {
                if !matches!(operation.phase, OperationPhase::Committing | OperationPhase::Recovering) {
                    operation.phase = OperationPhase::Interrupted;
                }
            }
        }
        self.permits.invalidate_all();
        let mut epoch = Zeroizing::new([0u8; 32]);
        fill(epoch.as_mut()).map_err(|_| "owner_epoch_unavailable")?;
        let epoch = hex(epoch.as_ref());
        self.owner = Some(BoundOwner {
            owner_id,
            epoch: epoch.clone(),
        });
        Ok(OwnerControlReply {
            version: 1,
            state: OwnerControlState::Bound,
            owner_id: Some(owner_id),
            current_owner_epoch: Some(epoch),
        })
    }

    pub fn unbind_owner(
        &mut self,
        expected_owner_id: u64,
        expected_owner_epoch: &str,
    ) -> OwnerControlReply {
        let state = match self.owner.as_ref() {
            None => OwnerControlState::AlreadyUnbound,
            Some(owner)
                if owner.owner_id == expected_owner_id
                    && lower_hex(expected_owner_epoch, 64)
                    && constant_eq(&owner.epoch, expected_owner_epoch) =>
            {
                self.shutdown();
                self.owner = None;
                self.vault_realm = None;
                #[cfg(all(target_os="macos",target_arch="aarch64"))] { self.vault_cancel(); }
                OwnerControlState::Unbound
            }
            Some(_) => OwnerControlState::MismatchPreserved,
        };
        OwnerControlReply {
            version: 1,
            state,
            owner_id: None,
            current_owner_epoch: None,
        }
    }

    pub fn allocate_operation(
        &self,
        owner_id: u64,
        current_owner_epoch: &str,
    ) -> OperationAllocationReply {
        let failure = |code| OperationAllocationReply {
            version: 1,
            state: OperationAllocationState::Failed,
            operation_id: None,
            code: Some(code),
        };
        let Some(owner) = self.owner.as_ref() else {
            return failure(FiniteCode::OwnerEpochUnavailable);
        };
        if owner_id == 0
            || !lower_hex(current_owner_epoch, 64)
            || owner.owner_id != owner_id
            || !constant_eq(&owner.epoch, current_owner_epoch)
        {
            return failure(FiniteCode::OwnerEpochChanged);
        }
        let mut operation_id = Zeroizing::new([0u8; 32]);
        if fill(operation_id.as_mut()).is_err() {
            return failure(FiniteCode::InternalError);
        }
        OperationAllocationReply {
            version: 1,
            state: OperationAllocationState::Allocated,
            operation_id: Some(hex(operation_id.as_ref())),
            code: None,
        }
    }

    fn owner_channel_lost(&mut self) {
        self.shutdown();
        self.owner = None;
        self.vault_realm = None;
        #[cfg(all(target_os="macos",target_arch="aarch64"))] { self.vault_cancel(); }
        self.vault_attestation_allowed = false;
        #[cfg(all(target_os="macos",target_arch="aarch64"))] { self.vault_config=None; }
    }

    #[doc(hidden)]
    pub fn handle_recovery_frame(
        &mut self,
        frame: AuthFrame,
        now: i64,
    ) -> Option<RecoveryInspectReply> {
        match frame {
            AuthFrame::CredentialDiscard {
                target: CredentialDiscardTarget::Recovery(target),
                generation,
                action,
                commit_handle,
                adopted_prepared_ref,
            } => Some(self.handle_recovery_selector(
                &target,
                &generation,
                action,
                commit_handle.as_ref().map(PrivateString::as_str),
                adopted_prepared_ref.as_ref().map(PrivateString::as_str),
                now,
            )),
            _ => None,
        }
    }

    pub fn handle(&mut self, frame: AuthFrame, now: i64) -> AuthReply {
        let Some(binding) = frame.binding().cloned() else {
            let operation_id = frame
                .recovery_target()
                .map(|target| target.recovery_selector.operation_id.as_str())
                .unwrap_or_default();
            return AuthReply::selector_error(operation_id, FiniteCode::ConfigurationChanged);
        };
        if let Some(reply) = self.reject_frame(&frame) {
            return reply;
        }
        let current_generation = self
            .generation
            .as_ref()
            .map(|generation| generation.fingerprint.clone())
            .expect("validated generation");
        if let Some(existing) = self.active.get(&binding.operation_id).cloned() {
            if existing.binding != binding {
                if matches!(
                    &frame,
                    AuthFrame::CredentialPrepare {
                        direct_secret: Some(_),
                        ..
                    }
                ) {
                    return AuthReply::error(
                        &binding,
                        AuthState::Recovering,
                        FiniteCode::CredentialRecoveryRequired,
                    );
                }
                self.transport.cancel(&existing.binding);
                let prepared_absent = if existing.phase == OperationPhase::Committing {
                    false
                } else {
                    self.discard_uncommitted_prepared(&existing.binding, None, false)
                        .is_ok()
                };
                let recovery_absent = matches!(
                    self.backend.get(
                        RECOVERY_SERVICE,
                        &recovery_account(&existing.binding.operation_id),
                    ),
                    Ok(None)
                );
                let terminal = prepared_absent && recovery_absent;
                if let Some(operation) = self.active.get_mut(&binding.operation_id) {
                    operation.phase = if terminal {
                        OperationPhase::Terminal
                    } else {
                        OperationPhase::Recovering
                    };
                }
                return AuthReply::error(
                    &binding,
                    if terminal {
                        AuthState::Failed
                    } else {
                        AuthState::Recovering
                    },
                    FiniteCode::ConfigurationChanged,
                );
            }
            if existing.originating_generation_fingerprint != current_generation
                && !matches!(frame, AuthFrame::CredentialDiscard { .. })
            {
                return AuthReply::error(
                    &binding,
                    AuthState::Interrupted,
                    FiniteCode::OperationInterrupted,
                );
            }
        }
        if !matches!(frame, AuthFrame::CredentialDiscard { .. }) && now > binding.expires_at {
            return self.expire_operation(&binding);
        }

        match frame {
            AuthFrame::AuthStart { native_enrollment, .. } => {
                if (binding.auth_method_id == "native_chain") != native_enrollment.is_some()
                    || native_enrollment.as_ref().is_some_and(|spec| {
                        spec.provider_id() != binding.provider_id
                    })
                {
                    return AuthReply::error(
                        &binding,
                        AuthState::Failed,
                        FiniteCode::ConfigurationChanged,
                    );
                }
                if let Some(operation) = self.active.get(&binding.operation_id) {
                    if operation.phase != OperationPhase::RecoveryStaged {
                        return self.phase_error(&binding);
                    }
                    if let Some(operation) = self.active.get_mut(&binding.operation_id) {
                        operation.native_enrollment = native_enrollment.clone();
                    }
                    self.set_phase(
                        &binding,
                        OperationPhase::AwaitingUser,
                        None,
                        None,
                    );
                    return self.transport_reply(&binding, true, now);
                }
                if self.secure_material_present(&binding.operation_id) {
                    return AuthReply::error(
                        &binding,
                        AuthState::Recovering,
                        FiniteCode::CredentialRecoveryRequired,
                    );
                }
                self.active.insert(
                    binding.operation_id.clone(),
                    ActiveOperation {
                        binding: binding.clone(),
                        originating_generation_fingerprint: current_generation,
                        phase: OperationPhase::AwaitingUser,
                        next_poll_at: None,
                        poll_interval: None,
                        native_enrollment: native_enrollment.clone(),
                    },
                );
                self.transport_reply(&binding, true, now)
            }
            AuthFrame::AuthStatus { .. } => {
                let Some(operation) = self.active.get(&binding.operation_id) else {
                    return self.phase_error(&binding);
                };
                if !matches!(
                    operation.phase,
                    OperationPhase::AwaitingUser | OperationPhase::Polling
                ) {
                    return self.phase_error(&binding);
                }
                if let Some(next_poll_at) = operation.next_poll_at {
                    if now < next_poll_at {
                        let mut reply = AuthReply::state(&binding, AuthState::Polling);
                        reply.poll_interval = Some((next_poll_at - now) as u64);
                        return reply;
                    }
                }
                self.transport_reply(&binding, false, now)
            }
            AuthFrame::AuthCancel { .. } => {
                let Some(operation) = self.active.get(&binding.operation_id) else {
                    return self.phase_error(&binding);
                };
                if operation.phase == OperationPhase::Terminal {
                    return self.phase_error(&binding);
                }
                self.finish_operation(
                    &binding,
                    AuthState::Cancelled,
                    FiniteCode::OperationCancelled,
                )
            }
            AuthFrame::CredentialPrepare {
                exact_old_reference,
                direct_secret,
                ..
            } => {
                if let Some(secret) = direct_secret {
                    return self.handle_direct_prepare(
                        &binding,
                        exact_old_reference.as_ref().map(PrivateString::as_str),
                        secret,
                        &current_generation,
                    );
                }
                if self.active.contains_key(&binding.operation_id) {
                    return self.phase_error(&binding);
                }
                match exact_old_reference {
                    Some(reference) => match self.stage_recovery(
                        &binding,
                        reference.as_str(),
                        &current_generation,
                    ) {
                    Ok(true) => {
                        self.active.insert(
                            binding.operation_id.clone(),
                            ActiveOperation {
                                binding: binding.clone(),
                                originating_generation_fingerprint: current_generation,
                                phase: OperationPhase::RecoveryStaged,
                                next_poll_at: None,
                                poll_interval: None,
                                native_enrollment: None,
                            },
                        );
                        AuthReply::state(&binding, AuthState::Prepared)
                    }
                    Ok(false) => AuthReply::error(
                        &binding,
                        AuthState::Recovering,
                        FiniteCode::CredentialRecoveryRequired,
                    ),
                    Err(()) => AuthReply::error(
                        &binding,
                        AuthState::Failed,
                        FiniteCode::CredentialStoreUnavailable,
                    ),
                },
                None => AuthReply::error(
                    &binding,
                    AuthState::Failed,
                    FiniteCode::ConfigurationChanged,
                ),
                }
            }
            AuthFrame::CredentialCommit { .. } => {
                let Some(operation) = self.active.get(&binding.operation_id) else {
                    return self.phase_error(&binding);
                };
                if !matches!(
                    operation.phase,
                    OperationPhase::Prepared | OperationPhase::Committing
                ) {
                    return self.phase_error(&binding);
                }
                match self.commit_prepared(&binding, &current_generation) {
                    Ok(receipt) => {
                        if let Some(operation) = self.active.get_mut(&binding.operation_id) {
                            operation.phase = OperationPhase::Committing;
                        }
                        AuthReply::state(&binding, AuthState::Committing)
                            .with_receipt(&receipt)
                    }
                    Err(code) => {
                        if let Some(operation) = self.active.get_mut(&binding.operation_id) {
                            operation.phase = OperationPhase::Recovering;
                        }
                        AuthReply::error(&binding, AuthState::Recovering, code)
                    }
                }
            }
            AuthFrame::CredentialDiscard {
                action,
                commit_handle,
                adopted_prepared_ref,
                ..
            } => {
                self.handle_recovery_action(
                    &binding,
                    action,
                    commit_handle.as_ref().map(PrivateString::as_str),
                    adopted_prepared_ref.as_ref().map(PrivateString::as_str),
                    now,
                )
            }
            AuthFrame::CredentialRefresh { .. } => self.reject_refresh(&binding),
        }
    }

    fn reject_frame(&self, frame: &AuthFrame) -> Option<AuthReply> {
        let binding = frame.binding()?;
        if !binding.valid() {
            return Some(AuthReply::error(
                binding,
                AuthState::Failed,
                FiniteCode::ConfigurationChanged,
            ));
        }
        if self.owner.as_ref().is_none_or(|owner| {
            owner.owner_id != binding.owner_id || !constant_eq(&owner.epoch, &binding.owner_epoch)
        }) {
            return Some(AuthReply::error(
                binding,
                AuthState::Failed,
                FiniteCode::OwnerMismatch,
            ));
        }
        let Some(current) = self.generation.as_ref() else {
            return Some(AuthReply::error(
                binding,
                AuthState::Interrupted,
                FiniteCode::OperationInterrupted,
            ));
        };
        let presented = frame.generation();
        if !lower_hex(presented.nonce.as_str(), 64)
            || !lower_hex(&presented.current_generation_fingerprint, 64)
            || !constant_eq(presented.nonce.as_str(), current.nonce_hex())
            || !constant_eq(
                &presented.current_generation_fingerprint,
                current.fingerprint(),
            )
        {
            return Some(AuthReply::error(
                binding,
                AuthState::Interrupted,
                FiniteCode::OperationInterrupted,
            ));
        }
        None
    }

    fn transport_reply(&mut self, binding: &StableBinding, start: bool, now: i64) -> AuthReply {
        let result = if start {
            let native_enrollment = self.active.get(&binding.operation_id)
                .and_then(|operation| operation.native_enrollment.as_ref());
            self.transport.start(binding, native_enrollment)
        } else {
            self.transport.status(binding)
        };
        match result {
            TransportResult::AwaitingUser { authorization_url } => {
                self.set_phase(binding, OperationPhase::AwaitingUser, None, None);
                let mut reply = AuthReply::state(binding, AuthState::AwaitingUser);
                reply.authorization_url = Some(authorization_url);
                reply
            }
            TransportResult::DeviceInstruction {
                verification_uri,
                user_code,
                poll_interval,
            } => {
                self.set_phase(
                    binding,
                    OperationPhase::Polling,
                    Some(now.saturating_add(poll_interval as i64)),
                    Some(poll_interval),
                );
                let mut reply = AuthReply::state(binding, AuthState::Polling);
                reply.verification_uri = Some(verification_uri);
                reply.user_code = Some(user_code);
                reply.poll_interval = Some(poll_interval);
                reply
            }
            TransportResult::SlowDown { poll_interval } => {
                self.set_phase(
                    binding,
                    OperationPhase::Polling,
                    Some(now.saturating_add(poll_interval as i64)),
                    Some(poll_interval),
                );
                let mut reply = AuthReply::error(
                    binding,
                    AuthState::Polling,
                    FiniteCode::DeviceSlowDown,
                );
                reply.poll_interval = Some(poll_interval);
                reply
            }
            TransportResult::Pending => {
                let interval = self
                    .active
                    .get(&binding.operation_id)
                    .and_then(|operation| operation.poll_interval);
                self.set_phase(
                    binding,
                    OperationPhase::Polling,
                    interval.map(|seconds| now.saturating_add(seconds as i64)),
                    interval,
                );
                AuthReply::state(binding, AuthState::Polling)
            }
            TransportResult::Ready(secret) => {
                if self.active.get(&binding.operation_id)
                    .and_then(|operation| operation.native_enrollment.as_ref()).is_some()
                {
                    return self.transport_failure(binding, FiniteCode::ConfigurationChanged);
                }
                let originating_generation = self
                    .active
                    .get(&binding.operation_id)
                    .map(|operation| operation.originating_generation_fingerprint.clone())
                    .unwrap_or_default();
                match self.stage_prepared(binding, secret, &originating_generation, None) {
                Ok(()) => {
                    self.set_phase(binding, OperationPhase::Prepared, None, None);
                    AuthReply::state(binding, AuthState::Prepared)
                }
                Err(code) => {
                    self.set_phase(binding, OperationPhase::Recovering, None, None);
                    AuthReply::error(
                        binding,
                        AuthState::Recovering,
                        code,
                    )
                }
                }
            }
            TransportResult::NativeReady {
                source_generation_fingerprint,
                canonical_payload,
            } => {
                let native_enrollment = self.active.get(&binding.operation_id)
                    .and_then(|operation| operation.native_enrollment.as_ref()).cloned();
                let Some(native_enrollment) = native_enrollment else {
                    return self.transport_failure(binding, FiniteCode::ConfigurationChanged);
                };
                let originating_generation = self.active.get(&binding.operation_id)
                    .map(|operation| operation.originating_generation_fingerprint.clone())
                    .unwrap_or_default();
                let committed = encode_native_provider_value(
                    binding,
                    &native_enrollment,
                    &source_generation_fingerprint,
                    canonical_payload.as_ref(),
                );
                match committed.and_then(|value| {
                    self.stage_prepared(binding, value, &originating_generation, None)
                }) {
                    Ok(()) => {
                        self.set_phase(binding, OperationPhase::Prepared, None, None);
                        AuthReply::state(binding, AuthState::Prepared)
                    }
                    Err(code) => {
                        self.set_phase(binding, OperationPhase::Recovering, None, None);
                        AuthReply::error(binding, AuthState::Recovering, code)
                    }
                }
            }
            TransportResult::Denied => {
                self.transport_failure(binding, FiniteCode::InternalError)
            }
            TransportResult::Unavailable => {
                self.transport_failure(binding, FiniteCode::AuthMethodUnavailable)
            }
        }
    }

    fn transport_failure(&mut self, binding: &StableBinding, code: FiniteCode) -> AuthReply {
        if self.secure_material_present(&binding.operation_id) {
            self.set_phase(binding, OperationPhase::Recovering, None, None);
            AuthReply::error(binding, AuthState::Recovering, code)
        } else {
            self.set_phase(binding, OperationPhase::Terminal, None, None);
            AuthReply::error(binding, AuthState::Failed, code)
        }
    }

    fn set_phase(
        &mut self,
        binding: &StableBinding,
        phase: OperationPhase,
        next_poll_at: Option<i64>,
        poll_interval: Option<u64>,
    ) {
        if let Some(operation) = self.active.get_mut(&binding.operation_id) {
            operation.phase = phase;
            operation.next_poll_at = next_poll_at;
            operation.poll_interval = poll_interval;
        }
    }

    fn phase_error(&self, binding: &StableBinding) -> AuthReply {
        AuthReply::error(
            binding,
            AuthState::Failed,
            FiniteCode::ConfigurationChanged,
        )
    }

    fn reject_refresh(&mut self, binding: &StableBinding) -> AuthReply {
        if let Some(operation) = self.active.get(&binding.operation_id) {
            if matches!(
                operation.phase,
                OperationPhase::Committing | OperationPhase::Recovering
            ) {
                return AuthReply::error(
                    binding,
                    AuthState::Recovering,
                    FiniteCode::CredentialRecoveryRequired,
                );
            }
            return self.finish_operation(
                binding,
                AuthState::Failed,
                FiniteCode::AuthMethodUnavailable,
            );
        }
        if self.secure_material_present(&binding.operation_id) {
            AuthReply::error(
                binding,
                AuthState::Recovering,
                FiniteCode::CredentialRecoveryRequired,
            )
        } else {
            AuthReply::error(
                binding,
                AuthState::Failed,
                FiniteCode::AuthMethodUnavailable,
            )
        }
    }

    fn finish_operation(
        &mut self,
        binding: &StableBinding,
        terminal_state: AuthState,
        terminal_code: FiniteCode,
    ) -> AuthReply {
        self.transport.cancel(binding);
        let commit_outcome_unknown = self
            .active
            .get(&binding.operation_id)
            .is_some_and(|operation| operation.phase == OperationPhase::Committing);
        let prepared_absent = !commit_outcome_unknown
            && self
                .discard_uncommitted_prepared(binding, None, false)
                .is_ok();
        let recovery_absent = matches!(
            self.backend
                .get(RECOVERY_SERVICE, &recovery_account(&binding.operation_id)),
            Ok(None)
        );
        if prepared_absent && recovery_absent {
            self.set_phase(binding, OperationPhase::Terminal, None, None);
            AuthReply::error(binding, terminal_state, terminal_code)
        } else {
            self.set_phase(binding, OperationPhase::Recovering, None, None);
            AuthReply::error(
                binding,
                AuthState::Recovering,
                if prepared_absent {
                    FiniteCode::CredentialRecoveryRequired
                } else {
                    FiniteCode::CredentialCleanupUnconfirmed
                },
            )
        }
    }

    fn expire_operation(&mut self, binding: &StableBinding) -> AuthReply {
        if self.active.contains_key(&binding.operation_id) {
            self.finish_operation(
                binding,
                AuthState::Expired,
                FiniteCode::AuthorizationExpired,
            )
        } else {
            AuthReply::error(
                binding,
                AuthState::Expired,
                FiniteCode::AuthorizationExpired,
            )
        }
    }

    pub fn shutdown(&mut self) -> Vec<AuthReply> {
        self.permits.invalidate_all();
        let bindings = self
            .active
            .values()
            .filter(|operation| operation.phase != OperationPhase::Terminal)
            .map(|operation| operation.binding.clone())
            .collect::<Vec<_>>();
        bindings
            .into_iter()
            .map(|binding| {
                self.finish_operation(
                    &binding,
                    AuthState::Interrupted,
                    FiniteCode::OperationInterrupted,
                )
            })
            .collect()
    }

    fn secure_material_present(&mut self, operation_id: &str) -> bool {
        for (service, account) in [
            (RECOVERY_SERVICE, recovery_account(operation_id)),
            (PREPARED_SERVICE, prepared_account(operation_id)),
            (COMPLETION_SERVICE, completion_account(operation_id)),
        ] {
            if !matches!(self.backend.get(service, &account), Ok(None)) {
                return true;
            }
        }
        false
    }

    fn handle_direct_prepare(
        &mut self,
        binding: &StableBinding,
        exact_old_reference: Option<&str>,
        direct_secret: PrivateString,
        originating_generation_fingerprint: &str,
    ) -> AuthReply {
        let valid_shape = binding.provider_id == "azure-openai"
            && binding.auth_method_id == "api_key"
            && match binding.kind.as_str() {
                "auth" => exact_old_reference.is_none(),
                "grant_replace" => exact_old_reference
                    .is_some_and(|reference| ordinary_reference_matches(binding, reference)),
                _ => false,
            }
            && valid_azure_api_key(direct_secret.as_str().as_bytes());
        if !valid_shape {
            return AuthReply::error(
                binding,
                AuthState::Failed,
                FiniteCode::ConfigurationChanged,
            );
        }

        let encoded = match encode_azure_api_key_provider_value(
            binding,
            direct_secret.as_str().as_bytes(),
        ) {
            Ok(value) => value,
            Err(code) => return AuthReply::error(binding, AuthState::Failed, code),
        };

        if self.active.contains_key(&binding.operation_id)
            || self.secure_material_present(&binding.operation_id)
        {
            return match self.direct_prepare_material_state(
                binding,
                exact_old_reference,
                originating_generation_fingerprint,
                encoded.as_ref(),
            ) {
                DirectPrepareMaterialState::Prepared => {
                    if !self.active.contains_key(&binding.operation_id) {
                        self.active.insert(
                            binding.operation_id.clone(),
                            ActiveOperation {
                                binding: binding.clone(),
                                originating_generation_fingerprint:
                                    originating_generation_fingerprint.to_owned(),
                                phase: OperationPhase::Prepared,
                                next_poll_at: None,
                                poll_interval: None,
                                native_enrollment: None,
                            },
                        );
                    } else {
                        self.set_phase(binding, OperationPhase::Prepared, None, None);
                    }
                    AuthReply::state(binding, AuthState::Prepared)
                }
                DirectPrepareMaterialState::RecoveryOnly => {
                    if let Some(operation) = self.active.get_mut(&binding.operation_id) {
                        operation.phase = OperationPhase::Recovering;
                    }
                    AuthReply::error(
                        binding,
                        AuthState::Recovering,
                        FiniteCode::CredentialRecoveryRequired,
                    )
                }
                DirectPrepareMaterialState::Absent
                | DirectPrepareMaterialState::Mismatch => {
                    if let Some(operation) = self.active.get_mut(&binding.operation_id) {
                        operation.phase = OperationPhase::Recovering;
                    }
                    AuthReply::error(
                        binding,
                        AuthState::Recovering,
                        FiniteCode::CredentialRecoveryRequired,
                    )
                }
            };
        }

        self.active.insert(
            binding.operation_id.clone(),
            ActiveOperation {
                binding: binding.clone(),
                originating_generation_fingerprint:
                    originating_generation_fingerprint.to_owned(),
                phase: OperationPhase::Recovering,
                next_poll_at: None,
                poll_interval: None,
                native_enrollment: None,
            },
        );

        if let Some(reference) = exact_old_reference {
            match self.stage_recovery(binding, reference, originating_generation_fingerprint) {
                Ok(true) => {}
                Ok(false) => {
                    return AuthReply::error(
                        binding,
                        AuthState::Recovering,
                        FiniteCode::CredentialRecoveryRequired,
                    )
                }
                Err(()) => {
                    return AuthReply::error(
                        binding,
                        AuthState::Recovering,
                        FiniteCode::CredentialStoreUnavailable,
                    )
                }
            }
        }

        match self.stage_prepared(
            binding,
            encoded,
            originating_generation_fingerprint,
            exact_old_reference,
        ) {
            Ok(()) => {
                self.set_phase(binding, OperationPhase::Prepared, None, None);
                AuthReply::state(binding, AuthState::Prepared)
            }
            Err(code) => AuthReply::error(binding, AuthState::Recovering, code),
        }
    }

    fn direct_prepare_material_state(
        &mut self,
        binding: &StableBinding,
        exact_old_reference: Option<&str>,
        originating_generation_fingerprint: &str,
        expected_provider_value: &[u8],
    ) -> DirectPrepareMaterialState {
        let recovery = match self
            .backend
            .get(RECOVERY_SERVICE, &recovery_account(&binding.operation_id))
        {
            Ok(value) => value,
            Err(_) => return DirectPrepareMaterialState::Mismatch,
        };
        let prepared = match self
            .backend
            .get(PREPARED_SERVICE, &prepared_account(&binding.operation_id))
        {
            Ok(value) => value,
            Err(_) => return DirectPrepareMaterialState::Mismatch,
        };

        let recovery_exact = match (exact_old_reference, recovery) {
            (None, None) => true,
            (Some(reference), Some(value)) => {
                let old = match self.backend.get(PROVIDER_SERVICE, reference) {
                    Ok(Some(value)) => Zeroizing::new(value),
                    _ => return DirectPrepareMaterialState::Mismatch,
                };
                decode_envelope(Zeroizing::new(value)).is_some_and(|(header, secret)| {
                    header.binding == *binding
                        && header.originating_generation_fingerprint
                            == originating_generation_fingerprint
                        && header
                            .exact_old_reference
                            .as_ref()
                            .map(PrivateString::as_str)
                            == Some(reference)
                        && header.prepared_destination.is_none()
                        && header.private_grant_identity.is_none()
                        && header.commit_handle.is_none()
                        && constant_eq_bytes(secret.as_ref(), old.as_ref())
                })
            }
            _ => false,
        };
        if !recovery_exact {
            return DirectPrepareMaterialState::Mismatch;
        }

        let Some(value) = prepared else {
            return if exact_old_reference.is_some() {
                DirectPrepareMaterialState::RecoveryOnly
            } else {
                DirectPrepareMaterialState::Absent
            };
        };
        let prepared_exact = decode_envelope(Zeroizing::new(value)).is_some_and(
            |(header, secret)| {
                header.binding == *binding
                    && header.originating_generation_fingerprint
                        == originating_generation_fingerprint
                    && header
                        .exact_old_reference
                        .as_ref()
                        .map(PrivateString::as_str)
                        == exact_old_reference
                    && receipt_from_header(binding, &header).is_ok()
                    && constant_eq_bytes(secret.as_ref(), expected_provider_value)
            },
        );
        if prepared_exact {
            DirectPrepareMaterialState::Prepared
        } else {
            DirectPrepareMaterialState::Mismatch
        }
    }

    fn stage_recovery(
        &mut self,
        binding: &StableBinding,
        exact_old_reference: &str,
        originating_generation_fingerprint: &str,
    ) -> Result<bool, ()> {
        if !ordinary_reference_matches(binding, exact_old_reference) {
            return Ok(false);
        }
        let account = recovery_account(&binding.operation_id);
        if let Some(existing) = self.backend.get(RECOVERY_SERVICE, &account).map_err(|_| ())? {
            let old = self
                .backend
                .get(PROVIDER_SERVICE, exact_old_reference)
                .map_err(|_| ())?
                .map(Zeroizing::new);
            return Ok(old.is_some_and(|old| {
                decode_envelope(Zeroizing::new(existing)).is_some_and(|(header, secret)| {
                    header.binding == *binding
                        && header.originating_generation_fingerprint
                            == originating_generation_fingerprint
                        && header
                            .exact_old_reference
                            .as_ref()
                            .map(PrivateString::as_str)
                            == Some(exact_old_reference)
                        && header.prepared_destination.is_none()
                        && header.private_grant_identity.is_none()
                        && header.commit_handle.is_none()
                        && constant_eq_bytes(secret.as_ref(), old.as_ref())
                })
            }));
        }
        let Some(old) = self
            .backend
            .get(PROVIDER_SERVICE, exact_old_reference)
            .map_err(|_| ())?
            .map(Zeroizing::new)
        else {
            return Ok(false);
        };
        let header = SecureEnvelopeHeader {
            binding: binding.clone(),
            originating_generation_fingerprint: originating_generation_fingerprint.to_owned(),
            exact_old_reference: Some(PrivateString::new(exact_old_reference.to_owned())),
            prepared_destination: None,
            private_grant_identity: None,
            commit_handle: None,
        };
        let envelope = encode_envelope(&header, old.as_ref()).ok_or(())?;
        self.backend
            .put(RECOVERY_SERVICE, &account, envelope.as_ref())
            .map_err(|_| ())?;
        let Some(readback) = self.backend.get(RECOVERY_SERVICE, &account).map_err(|_| ())? else {
            return Ok(false);
        };
        let readback = Zeroizing::new(readback);
        Ok(constant_eq_bytes(readback.as_ref(), envelope.as_ref())
            && envelope_matches(
                readback,
                &header,
                old.as_ref(),
            ))
    }

    fn stage_prepared(
        &mut self,
        binding: &StableBinding,
        secret: Zeroizing<Vec<u8>>,
        originating_generation_fingerprint: &str,
        exact_old_reference: Option<&str>,
    ) -> Result<(), FiniteCode> {
        if secret.is_empty() || secret.len() > MAX_ENVELOPE / 2 {
            return Err(FiniteCode::NativeEnrollmentTooLarge);
        }
        let account = prepared_account(&binding.operation_id);
        if let Some(existing) = self
            .backend
            .get(PREPARED_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialRecoveryRequired)?
        {
            let exact = decode_envelope(Zeroizing::new(existing)).is_some_and(
                |(header, existing_secret)| {
                    header.binding == *binding
                        && header.originating_generation_fingerprint
                            == originating_generation_fingerprint
                        && header
                            .exact_old_reference
                            .as_ref()
                            .map(PrivateString::as_str)
                            == exact_old_reference
                        && receipt_from_header(binding, &header).is_ok()
                        && constant_eq_bytes(existing_secret.as_ref(), secret.as_ref())
                },
            );
            return if exact {
                Ok(())
            } else {
                Err(FiniteCode::CredentialRecoveryRequired)
            };
        }
        let mut identity = Zeroizing::new([0u8; 32]);
        fill(identity.as_mut()).map_err(|_| FiniteCode::InternalError)?;
        let mut commit_handle = Zeroizing::new([0u8; 32]);
        fill(commit_handle.as_mut()).map_err(|_| FiniteCode::InternalError)?;
        let mut destination = Zeroizing::new([0u8; 16]);
        fill(destination.as_mut()).map_err(|_| FiniteCode::InternalError)?;
        let header = SecureEnvelopeHeader {
            binding: binding.clone(),
            originating_generation_fingerprint: originating_generation_fingerprint.to_owned(),
            exact_old_reference: exact_old_reference
                .map(|reference| PrivateString::new(reference.to_owned())),
            prepared_destination: Some(PrivateString::new(format!(
                "{}:{}:{}",
                binding.owner_id,
                binding.connection_id,
                hex(destination.as_ref())
            ))),
            private_grant_identity: Some(PrivateString::new(hex(identity.as_ref()))),
            commit_handle: Some(PrivateString::new(hex(commit_handle.as_ref()))),
        };
        let envelope = encode_envelope(&header, secret.as_ref())
            .ok_or(FiniteCode::NativeEnrollmentTooLarge)?;
        self.backend
            .put(PREPARED_SERVICE, &account, envelope.as_ref())
            .map_err(|_| FiniteCode::CredentialRecoveryRequired)?;
        let readback = Zeroizing::new(self
            .backend
            .get(PREPARED_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialRecoveryRequired)?
            .ok_or(FiniteCode::CredentialRecoveryRequired)?);
        if !constant_eq_bytes(readback.as_ref(), envelope.as_ref())
            || !envelope_matches(readback, &header, secret.as_ref())
        {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        Ok(())
    }

    fn commit_prepared(
        &mut self,
        binding: &StableBinding,
        current_generation_fingerprint: &str,
    ) -> Result<CommitReceipt, FiniteCode> {
        let account = prepared_account(&binding.operation_id);
        let value = self
            .backend
            .get(PREPARED_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
            .ok_or(FiniteCode::CredentialRecoveryRequired)?;
        let (header, secret) = decode_envelope(Zeroizing::new(value))
            .ok_or(FiniteCode::CredentialRecoveryRequired)?;
        if header.originating_generation_fingerprint != current_generation_fingerprint {
            return Err(FiniteCode::ConfigurationChanged);
        }
        let receipt = receipt_from_header(binding, &header)?;
        let existing = self
            .backend
            .get(PROVIDER_SERVICE, receipt.prepared_ref.as_str())
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
            .map(Zeroizing::new);
        match existing {
            Some(candidate) if !constant_eq_bytes(candidate.as_ref(), secret.as_ref()) => {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
            Some(_) => {}
            None => {
                self.backend
                    .put(
                        PROVIDER_SERVICE,
                        receipt.prepared_ref.as_str(),
                        secret.as_ref(),
                    )
                    .map_err(|_| FiniteCode::CredentialStoreUnavailable)?;
            }
        }
        let committed = self
            .backend
            .get(PROVIDER_SERVICE, receipt.prepared_ref.as_str())
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
            .map(Zeroizing::new)
            .is_some_and(|candidate| constant_eq_bytes(candidate.as_ref(), secret.as_ref()));
        if committed {
            self.permits.invalidate_connection(binding.owner_id, binding.connection_id);
            Ok(receipt)
        } else {
            Err(FiniteCode::CredentialRecoveryRequired)
        }
    }

    fn generation_matches(&self, presented: &GenerationBinding) -> bool {
        self.generation.as_ref().is_some_and(|current| {
            lower_hex(presented.nonce.as_str(), 64)
                && lower_hex(&presented.current_generation_fingerprint, 64)
                && constant_eq(presented.nonce.as_str(), current.nonce_hex())
                && constant_eq(
                    &presented.current_generation_fingerprint,
                    current.fingerprint(),
                )
        })
    }

    fn selector_failure(
        selector: &RecoverySelector,
        code: FiniteCode,
    ) -> RecoveryInspectReply {
        RecoveryInspectReply {
            operation_id: selector.operation_id.clone(),
            state: RecoveryInspectState::Failed,
            material_state: RecoveryMaterialState::None,
            receipt_state: RecoveryReceiptState::None,
            binding_fingerprint: None,
            binding_summary: None,
            code: Some(code),
        }
    }

    fn inspect_reply(
        selector: &RecoverySelector,
        snapshot: &RecoverySnapshot,
    ) -> RecoveryInspectReply {
        let state = if snapshot.material_state != RecoveryMaterialState::None {
            RecoveryInspectState::Recovering
        } else {
            match snapshot.receipt.as_ref().map(|receipt| receipt.outcome) {
                Some(CompletionOutcome::Restored) => RecoveryInspectState::Restored,
                Some(CompletionOutcome::Cleaned) => RecoveryInspectState::Cleaned,
                None => RecoveryInspectState::Absent,
            }
        };
        RecoveryInspectReply {
            operation_id: selector.operation_id.clone(),
            state,
            material_state: snapshot.material_state,
            receipt_state: if snapshot.receipt.is_some() {
                RecoveryReceiptState::Present
            } else {
                RecoveryReceiptState::None
            },
            binding_fingerprint: snapshot.fingerprint.clone(),
            binding_summary: snapshot.binding_summary.clone(),
            code: None,
        }
    }

    fn recovery_snapshot(
        &mut self,
        selector: &RecoverySelector,
    ) -> Result<RecoverySnapshot, FiniteCode> {
        let recovery_value = self
            .backend
            .get(RECOVERY_SERVICE, &recovery_account(&selector.operation_id))
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?;
        let prepared_value = self
            .backend
            .get(PREPARED_SERVICE, &prepared_account(&selector.operation_id))
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?;
        let receipt_value = self
            .backend
            .get(COMPLETION_SERVICE, &completion_account(&selector.operation_id))
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?;

        let had_recovery = recovery_value.is_some();
        let had_prepared = prepared_value.is_some();
        let mut binding = None;
        let mut fingerprint = None;
        let mut recovery_reference = None;
        if let Some(value) = recovery_value {
            let (header, _) = decode_envelope(Zeroizing::new(value))
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
            self.validate_selected_binding(selector, &header.binding)?;
            if !lower_hex(&header.originating_generation_fingerprint, 64)
                || header
                    .exact_old_reference
                    .as_ref()
                    .is_none_or(|reference| {
                        !ordinary_reference_matches(&header.binding, reference.as_str())
                    })
                || header.prepared_destination.is_some()
                || header.private_grant_identity.is_some()
                || header.commit_handle.is_some()
            {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
            let candidate = stable_binding_fingerprint(&header.binding)
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
            recovery_reference = header.exact_old_reference.clone();
            fingerprint = Some(candidate);
            binding = Some(header.binding);
        }
        if let Some(value) = prepared_value {
            let (header, _) = decode_envelope(Zeroizing::new(value))
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
            self.validate_selected_binding(selector, &header.binding)?;
            if !lower_hex(&header.originating_generation_fingerprint, 64)
                || match header.binding.kind.as_str() {
                    "auth" => header.exact_old_reference.is_some(),
                    "grant_replace" => header
                        .exact_old_reference
                        .as_ref()
                        .is_some_and(|reference| {
                            !ordinary_reference_matches(&header.binding, reference.as_str())
                        }),
                    _ => false,
                }
                || receipt_from_header(&header.binding, &header).is_err()
            {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
            if recovery_reference.as_ref().is_some_and(|reference| {
                header
                    .exact_old_reference
                    .as_ref()
                    .is_some_and(|prepared| prepared != reference)
            }) {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
            let candidate = stable_binding_fingerprint(&header.binding)
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
            if fingerprint.as_ref().is_some_and(|value| !constant_eq(value, &candidate)) {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
            if binding.as_ref().is_some_and(|value| value != &header.binding) {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
            fingerprint = Some(candidate);
            binding = Some(header.binding);
        }
        let material_state = match (had_recovery, had_prepared) {
            (false, false) => RecoveryMaterialState::None,
            (true, false) => RecoveryMaterialState::Recovery,
            (false, true) => RecoveryMaterialState::Prepared,
            (true, true) => RecoveryMaterialState::RecoveryAndPrepared,
        };
        let receipt = receipt_value
            .map(|value| {
                let value = Zeroizing::new(value);
                let receipt = serde_json::from_slice::<CompletionReceipt>(value.as_slice())
                    .map_err(|_| FiniteCode::CredentialRecoveryRequired)?;
                let canonical = Zeroizing::new(
                    serde_json::to_vec(&receipt)
                        .map_err(|_| FiniteCode::CredentialRecoveryRequired)?,
                );
                if !constant_eq_bytes(value.as_slice(), canonical.as_slice()) {
                    return Err(FiniteCode::CredentialRecoveryRequired);
                }
                Ok(receipt)
            })
            .transpose()?;
        if let Some(receipt) = receipt.as_ref() {
            self.validate_completion_receipt(
                selector,
                binding.as_ref(),
                recovery_reference.as_ref(),
                receipt,
            )?;
            if fingerprint
                .as_ref()
                .is_some_and(|value| !constant_eq(value, &receipt.binding_fingerprint))
            {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
            if fingerprint.is_none() {
                fingerprint = Some(receipt.binding_fingerprint.clone());
            }
        }
        let material_summary = binding.as_ref().map(RecoveryBindingSummary::from_binding);
        let receipt_summary = receipt
            .as_ref()
            .and_then(|value| value.binding_summary.clone());
        if material_summary.is_some() && receipt_summary.is_some()
            && material_summary != receipt_summary
        {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        let binding_summary = material_summary.or(receipt_summary);
        Ok(RecoverySnapshot {
            binding,
            fingerprint,
            binding_summary,
            material_state,
            receipt,
        })
    }

    fn validate_selected_binding(
        &self,
        selector: &RecoverySelector,
        binding: &StableBinding,
    ) -> Result<(), FiniteCode> {
        if !selector.matches_binding(binding)
            || !binding.valid()
            || self
                .owner
                .as_ref()
                .is_none_or(|owner| owner.owner_id != binding.owner_id)
        {
            Err(FiniteCode::CredentialRecoveryRequired)
        } else {
            Ok(())
        }
    }

    fn validate_completion_receipt(
        &mut self,
        selector: &RecoverySelector,
        binding: Option<&StableBinding>,
        recovery_reference: Option<&PrivateString>,
        receipt: &CompletionReceipt,
    ) -> Result<(), FiniteCode> {
        if receipt.version != 1
            || receipt.fingerprint_version != 1
            || !lower_hex(&receipt.binding_fingerprint, 64)
            || receipt.completed_at < 0
            || &receipt.locator != selector
            || receipt
                .binding_summary
                .as_ref()
                .is_some_and(|summary| !summary.valid())
        {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        if binding.is_some_and(|binding| {
            stable_binding_fingerprint(binding).as_deref()
                != Some(receipt.binding_fingerprint.as_str())
                || receipt.binding_summary.as_ref()
                    != Some(&RecoveryBindingSummary::from_binding(binding))
        }) {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        let target_handle = match &receipt.postcondition {
            PostconditionTargetV1::RestoredValue {
                target_service,
                target_handle,
                value_verifier_version,
                value_verifier,
            } => {
                if receipt.outcome != CompletionOutcome::Restored
                    || target_service != "provider_v1"
                    || *value_verifier_version != 1
                    || !lower_hex(value_verifier.as_str(), 64)
                {
                    return Err(FiniteCode::CredentialRecoveryRequired);
                }
                let value = Zeroizing::new(self
                    .backend
                    .get(PROVIDER_SERVICE, target_handle.as_str())
                    .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
                    .ok_or(FiniteCode::CredentialRecoveryRequired)?);
                let expected = value_verifier_for(
                    &receipt.binding_fingerprint,
                    target_handle.as_str(),
                    value.as_slice(),
                )
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
                if !constant_eq(&expected, value_verifier.as_str()) {
                    return Err(FiniteCode::CredentialRecoveryRequired);
                }
                target_handle
            }
            PostconditionTargetV1::DisconnectOldAbsent {
                target_service,
                target_handle,
            } => {
                if receipt.outcome != CompletionOutcome::Cleaned
                    || selector.kind != "disconnect"
                    || target_service != "provider_v1"
                    || self
                        .backend
                        .get(PROVIDER_SERVICE, target_handle.as_str())
                        .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
                        .is_some()
                {
                    return Err(FiniteCode::CredentialRecoveryRequired);
                }
                target_handle
            }
            PostconditionTargetV1::GrantReplacementOldAbsent {
                target_service,
                target_handle,
            } => {
                if receipt.outcome != CompletionOutcome::Cleaned
                    || !matches!(selector.kind.as_str(), "grant_replace" | "same_grant_refresh")
                    || target_service != "provider_v1"
                    || self
                        .backend
                        .get(PROVIDER_SERVICE, target_handle.as_str())
                        .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
                        .is_some()
                {
                    return Err(FiniteCode::CredentialRecoveryRequired);
                }
                target_handle
            }
        };
        if !ordinary_reference_matches_selector(selector, target_handle.as_str())
            || recovery_reference
                .is_some_and(|reference| !constant_eq(reference.as_str(), target_handle.as_str()))
        {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        Ok(())
    }

    fn handle_recovery_selector(
        &mut self,
        target: &RecoveryDiscardTarget,
        generation: &GenerationBinding,
        action: CredentialDiscardAction,
        commit_handle: Option<&str>,
        adopted_prepared_ref: Option<&str>,
        now: i64,
    ) -> RecoveryInspectReply {
        let selector = &target.recovery_selector;
        if !selector.valid()
            || self
                .owner
                .as_ref()
                .is_none_or(|owner| owner.owner_id != selector.owner_id)
        {
            return Self::selector_failure(selector, FiniteCode::OwnerEpochChanged);
        }
        if !self.generation_matches(generation) {
            return Self::selector_failure(selector, FiniteCode::OperationInterrupted);
        }
        let snapshot = match self.recovery_snapshot(selector) {
            Ok(snapshot) => snapshot,
            Err(code) => return Self::selector_failure(selector, code),
        };
        if action == CredentialDiscardAction::Inspect {
            return Self::inspect_reply(selector, &snapshot);
        }
        let Some(expected) = target.binding_fingerprint.as_deref() else {
            return Self::selector_failure(selector, FiniteCode::ConfigurationChanged);
        };
        if !lower_hex(expected, 64)
            || snapshot
                .fingerprint
                .as_deref()
                .is_none_or(|actual| !constant_eq(actual, expected))
        {
            return Self::selector_failure(selector, FiniteCode::ConfigurationChanged);
        }
        if let Some(receipt) = snapshot.receipt.as_ref() {
            let compatible = match (receipt.outcome, action) {
                (CompletionOutcome::Restored, CredentialDiscardAction::RestoreOld)
                | (CompletionOutcome::Restored, CredentialDiscardAction::DiscardPrepared)
                | (CompletionOutcome::Cleaned, CredentialDiscardAction::CleanupCommitted)
                | (_, CredentialDiscardAction::AcknowledgeTerminal) => true,
                _ => false,
            };
            if !compatible {
                return Self::selector_failure(selector, FiniteCode::ConfigurationChanged);
            }
        }
        let result = if action == CredentialDiscardAction::AcknowledgeTerminal {
            self.acknowledge_terminal_receipt(selector, &snapshot)
        } else {
            let Some(binding) = snapshot.binding.as_ref() else {
                return Self::selector_failure(selector, FiniteCode::CredentialRecoveryRequired);
            };
            match action {
                CredentialDiscardAction::RestoreOld => self.restore_old(binding, now),
                CredentialDiscardAction::DiscardPrepared => {
                    self.discard_uncommitted_prepared(binding, commit_handle, true)
                }
                CredentialDiscardAction::CleanupCommitted => {
                    self.cleanup_committed(
                        binding,
                        commit_handle,
                        adopted_prepared_ref,
                        now,
                        false,
                    )
                }
                CredentialDiscardAction::Inspect | CredentialDiscardAction::AcknowledgeTerminal => {
                    unreachable!()
                }
            }
        };
        if let Err(code) = result {
            return Self::selector_failure(selector, code);
        }
        match self.recovery_snapshot(selector) {
            Ok(snapshot) => Self::inspect_reply(selector, &snapshot),
            Err(code) => Self::selector_failure(selector, code),
        }
    }

    fn acknowledge_terminal_receipt(
        &mut self,
        selector: &RecoverySelector,
        snapshot: &RecoverySnapshot,
    ) -> Result<AuthState, FiniteCode> {
        if snapshot.material_state != RecoveryMaterialState::None || snapshot.receipt.is_none() {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        let account = completion_account(&selector.operation_id);
        let expected = Zeroizing::new(
            serde_json::to_vec(snapshot.receipt.as_ref().expect("checked receipt"))
                .map_err(|_| FiniteCode::CredentialRecoveryRequired)?,
        );
        let current = Zeroizing::new(
            self.backend
                .get(COMPLETION_SERVICE, &account)
                .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
                .ok_or(FiniteCode::CredentialRecoveryRequired)?,
        );
        if !constant_eq_bytes(current.as_slice(), expected.as_slice()) {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        self.backend
            .delete(COMPLETION_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?;
        if self
            .backend
            .get(COMPLETION_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?
            .is_some()
        {
            Err(FiniteCode::CredentialCleanupUnconfirmed)
        } else {
            Ok(AuthState::Succeeded)
        }
    }

    fn write_completion_receipt(
        &mut self,
        binding: &StableBinding,
        outcome: CompletionOutcome,
        postcondition: PostconditionTargetV1,
        completed_at: i64,
    ) -> Result<(), FiniteCode> {
        let mut receipt = CompletionReceipt {
            version: 1,
            locator: selector_from_binding(binding),
            fingerprint_version: 1,
            binding_fingerprint: stable_binding_fingerprint(binding)
                .ok_or(FiniteCode::ConfigurationChanged)?,
            binding_summary: Some(RecoveryBindingSummary::from_binding(binding)),
            outcome,
            completed_at,
            postcondition,
        };
        let encoded = Zeroizing::new(
            serde_json::to_vec(&receipt).map_err(|_| FiniteCode::CredentialStoreUnavailable)?,
        );
        let account = completion_account(&binding.operation_id);
        if let Some(existing) = self
            .backend
            .get(COMPLETION_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
        {
            let existing = Zeroizing::new(existing);
            let existing_receipt = serde_json::from_slice::<CompletionReceipt>(existing.as_slice())
                .map_err(|_| FiniteCode::CredentialRecoveryRequired)?;
            let mut semantic = receipt.clone();
            semantic.completed_at = existing_receipt.completed_at;
            if existing_receipt != semantic {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
            receipt = existing_receipt;
        } else {
            self.backend
                .put(COMPLETION_SERVICE, &account, encoded.as_slice())
                .map_err(|_| FiniteCode::CredentialStoreUnavailable)?;
        }
        let readback = Zeroizing::new(self
            .backend
            .get(COMPLETION_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
            .ok_or(FiniteCode::CredentialStoreUnavailable)?);
        let canonical = Zeroizing::new(
            serde_json::to_vec(&receipt).map_err(|_| FiniteCode::CredentialStoreUnavailable)?,
        );
        if !constant_eq_bytes(readback.as_slice(), canonical.as_slice())
            || serde_json::from_slice::<CompletionReceipt>(readback.as_slice())
                .ok()
                .as_ref()
                != Some(&receipt)
        {
            return Err(FiniteCode::CredentialStoreUnavailable);
        }
        Ok(())
    }

    fn handle_recovery_action(
        &mut self,
        binding: &StableBinding,
        action: CredentialDiscardAction,
        commit_handle: Option<&str>,
        adopted_prepared_ref: Option<&str>,
        _now: i64,
    ) -> AuthReply {
        let selector = selector_from_binding(binding);
        let snapshot = match self.recovery_snapshot(&selector) {
            Ok(snapshot) => snapshot,
            Err(code) => {
                return AuthReply::error(binding, AuthState::Recovering, code);
            }
        };
        let expected_fingerprint = stable_binding_fingerprint(binding);
        if snapshot.fingerprint.as_deref().is_some_and(|actual| {
            expected_fingerprint
                .as_deref()
                .is_none_or(|expected| !constant_eq(actual, expected))
        }) {
            return AuthReply::error(
                binding,
                AuthState::Recovering,
                FiniteCode::ConfigurationChanged,
            );
        }
        if let Some(receipt) = snapshot.receipt.as_ref() {
            let compatible = match (receipt.outcome, action) {
                (CompletionOutcome::Restored, CredentialDiscardAction::Inspect)
                | (CompletionOutcome::Restored, CredentialDiscardAction::RestoreOld)
                | (CompletionOutcome::Restored, CredentialDiscardAction::DiscardPrepared)
                | (CompletionOutcome::Cleaned, CredentialDiscardAction::Inspect)
                | (CompletionOutcome::Cleaned, CredentialDiscardAction::CleanupCommitted)
                | (_, CredentialDiscardAction::AcknowledgeTerminal) => true,
                _ => false,
            };
            if !compatible {
                return AuthReply::error(
                    binding,
                    AuthState::Recovering,
                    FiniteCode::ConfigurationChanged,
                );
            }
        }
        let result = match action {
            CredentialDiscardAction::Inspect => self.inspect(binding),
            CredentialDiscardAction::RestoreOld => {
                self.restore_old(binding, _now).map(|state| (state, None))
            }
            CredentialDiscardAction::DiscardPrepared => self
                .discard_uncommitted_prepared(binding, commit_handle, false)
                .map(|state| (state, None)),
            CredentialDiscardAction::CleanupCommitted => self
                .cleanup_committed(binding, commit_handle, adopted_prepared_ref, _now, true)
                .map(|state| (state, None)),
            CredentialDiscardAction::AcknowledgeTerminal => self
                .acknowledge_terminal_receipt(&selector, &snapshot)
                .map(|state| (state, None)),
        };
        if matches!(result, Ok((AuthState::Succeeded | AuthState::Restored, _))) {
            self.set_phase(binding, OperationPhase::Terminal, None, None);
        } else if result.is_err() {
            self.set_phase(binding, OperationPhase::Recovering, None, None);
        }
        match result {
            Ok((state, Some(receipt))) => {
                AuthReply::state(binding, state).with_receipt(&receipt)
            }
            Ok((state, None)) => AuthReply::state(binding, state),
            Err(FiniteCode::CredentialCleanupUnconfirmed) => AuthReply::error(
                binding,
                AuthState::Recovering,
                FiniteCode::CredentialCleanupUnconfirmed,
            ),
            Err(code) => AuthReply::error(binding, AuthState::Recovering, code),
        }
    }

    fn inspect(
        &mut self,
        binding: &StableBinding,
    ) -> Result<(AuthState, Option<CommitReceipt>), FiniteCode> {
        let prepared_account = prepared_account(&binding.operation_id);
        if let Some(value) = self
            .backend
            .get(PREPARED_SERVICE, &prepared_account)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
        {
            let (header, secret) = decode_envelope(Zeroizing::new(value))
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
            let receipt = receipt_from_header(binding, &header)?;
            let destination = self
                .backend
                .get(PROVIDER_SERVICE, receipt.prepared_ref.as_str())
                .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
                .map(Zeroizing::new);
            return match destination {
                Some(candidate)
                    if constant_eq_bytes(candidate.as_ref(), secret.as_ref()) =>
                {
                    Ok((AuthState::Committing, Some(receipt)))
                }
                Some(_) => Err(FiniteCode::CredentialRecoveryRequired),
                None => Ok((AuthState::Recovering, Some(receipt))),
            };
        }
        let recovery_account = recovery_account(&binding.operation_id);
        if let Some(value) = self
            .backend
            .get(RECOVERY_SERVICE, &recovery_account)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
        {
            let (header, _) = decode_envelope(Zeroizing::new(value))
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
            if header.binding != *binding {
                return Err(FiniteCode::ConfigurationChanged);
            }
            return Ok((AuthState::Recovering, None));
        }
        Ok((AuthState::Succeeded, None))
    }

    fn restore_old(
        &mut self,
        binding: &StableBinding,
        completed_at: i64,
    ) -> Result<AuthState, FiniteCode> {
        let account = recovery_account(&binding.operation_id);
        let Some(value) = self
            .backend
            .get(RECOVERY_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
        else {
            return Err(FiniteCode::CredentialRecoveryRequired);
        };
        let Some((header, secret)) = decode_envelope(Zeroizing::new(value)) else {
            return Err(FiniteCode::CredentialRecoveryRequired);
        };
        if header.binding != *binding {
            return Err(FiniteCode::ConfigurationChanged);
        }
        let reference = header
            .exact_old_reference
            .as_ref()
            .map(PrivateString::as_str)
            .filter(|reference| ordinary_reference_matches(binding, reference))
            .ok_or(FiniteCode::CredentialRecoveryRequired)?;
        self.backend
            .put(PROVIDER_SERVICE, reference, secret.as_ref())
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?;
        let restored = self
            .backend
            .get(PROVIDER_SERVICE, reference)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
            .map(Zeroizing::new)
            .is_some_and(|candidate| constant_eq_bytes(candidate.as_ref(), secret.as_ref()));
        if !restored {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        let fingerprint = stable_binding_fingerprint(binding)
            .ok_or(FiniteCode::ConfigurationChanged)?;
        let verifier = value_verifier_for(&fingerprint, reference, secret.as_ref())
            .ok_or(FiniteCode::CredentialRecoveryRequired)?;
        self.write_completion_receipt(
            binding,
            CompletionOutcome::Restored,
            PostconditionTargetV1::RestoredValue {
                target_service: "provider_v1".into(),
                target_handle: PrivateString::new(reference.to_owned()),
                value_verifier_version: 1,
                value_verifier: PrivateString::new(verifier),
            },
            completed_at,
        )?;
        self.backend
            .delete(RECOVERY_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?;
        if self
            .backend
            .get(RECOVERY_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?
            .is_some()
        {
            return Err(FiniteCode::CredentialCleanupUnconfirmed);
        }
        Ok(AuthState::Restored)
    }

    fn discard_uncommitted_prepared(
        &mut self,
        binding: &StableBinding,
        presented_commit_handle: Option<&str>,
        adopted_cleanup: bool,
    ) -> Result<AuthState, FiniteCode> {
        let account = prepared_account(&binding.operation_id);
        if let Some(value) = self
            .backend
            .get(PREPARED_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
        {
            let (header, secret) = decode_envelope(Zeroizing::new(value))
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
            let receipt = receipt_from_header(binding, &header)?;
            if let Some(presented) = presented_commit_handle {
                if !constant_eq(presented, receipt.commit_handle.as_str()) {
                    return Err(FiniteCode::ConfigurationChanged);
                }
            }
            if let Some(candidate) = self
                .backend
                .get(PROVIDER_SERVICE, receipt.prepared_ref.as_str())
                .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
                .map(Zeroizing::new)
            {
                if !constant_eq_bytes(candidate.as_ref(), secret.as_ref()) {
                    return Err(FiniteCode::CredentialRecoveryRequired);
                }
                if presented_commit_handle.is_none() && !adopted_cleanup {
                    return Err(FiniteCode::CredentialRecoveryRequired);
                }
                self.backend
                    .delete(PROVIDER_SERVICE, receipt.prepared_ref.as_str())
                    .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?;
                if self
                    .backend
                    .get(PROVIDER_SERVICE, receipt.prepared_ref.as_str())
                    .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?
                    .is_some()
                {
                    return Err(FiniteCode::CredentialCleanupUnconfirmed);
                }
            }
            if header.binding != *binding {
                return Err(FiniteCode::ConfigurationChanged);
            }
        }
        self.backend
            .delete(PREPARED_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?;
        if self
            .backend
            .get(PREPARED_SERVICE, &account)
            .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?
            .is_some()
        {
            Err(FiniteCode::CredentialCleanupUnconfirmed)
        } else {
            Ok(AuthState::Succeeded)
        }
    }

    fn cleanup_committed(
        &mut self,
        binding: &StableBinding,
        presented_commit_handle: Option<&str>,
        adopted_prepared_ref: Option<&str>,
        completed_at: i64,
        require_commit_handle: bool,
    ) -> Result<AuthState, FiniteCode> {
        self.permits.invalidate_connection(binding.owner_id, binding.connection_id);
        let prepared = prepared_account(&binding.operation_id);
        let prepared_envelope = self
            .backend
            .get(PREPARED_SERVICE, &prepared)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?;
        if let Some(value) = prepared_envelope.as_ref() {
            let (header, secret) = decode_envelope(Zeroizing::new(value.clone()))
                .ok_or(FiniteCode::CredentialRecoveryRequired)?;
            let receipt = receipt_from_header(binding, &header)?;
            let adopted = adopted_prepared_ref.ok_or(FiniteCode::ConfigurationChanged)?;
            if (require_commit_handle
                && presented_commit_handle
                    .is_none_or(|handle| !constant_eq(handle, receipt.commit_handle.as_str())))
                || presented_commit_handle
                    .is_some_and(|handle| !constant_eq(handle, receipt.commit_handle.as_str()))
                || !constant_eq(adopted, receipt.prepared_ref.as_str())
            {
                return Err(FiniteCode::ConfigurationChanged);
            }
            let winner = self
                .backend
                .get(PROVIDER_SERVICE, receipt.prepared_ref.as_str())
                .map_err(|_| FiniteCode::CredentialStoreUnavailable)?
                .map(Zeroizing::new)
                .is_some_and(|candidate| {
                    constant_eq_bytes(candidate.as_ref(), secret.as_ref())
                });
            if !winner {
                return Err(FiniteCode::CredentialRecoveryRequired);
            }
        } else if binding.kind != "disconnect" {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }

        let recovery = recovery_account(&binding.operation_id);
        let mut completion_target = None;
        let recovery_value = self
            .backend
            .get(RECOVERY_SERVICE, &recovery)
            .map_err(|_| FiniteCode::CredentialStoreUnavailable)?;
        if let Some(value) = recovery_value {
            let Some((header, _)) = decode_envelope(Zeroizing::new(value)) else {
                return Err(FiniteCode::CredentialRecoveryRequired);
            };
            if header.binding != *binding {
                return Err(FiniteCode::ConfigurationChanged);
            }
            if let Some(reference) = header.exact_old_reference.as_ref() {
                let reference = reference.as_str();
                if !ordinary_reference_matches(binding, reference) {
                    return Err(FiniteCode::CredentialRecoveryRequired);
                }
                self.backend
                    .delete(PROVIDER_SERVICE, reference)
                    .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?;
                if self
                    .backend
                    .get(PROVIDER_SERVICE, reference)
                    .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?
                    .is_some()
                {
                    return Err(FiniteCode::CredentialCleanupUnconfirmed);
                }
                completion_target = Some(match binding.kind.as_str() {
                    "disconnect" => PostconditionTargetV1::DisconnectOldAbsent {
                        target_service: "provider_v1".into(),
                        target_handle: PrivateString::new(reference.to_owned()),
                    },
                    "grant_replace" | "same_grant_refresh" => {
                        PostconditionTargetV1::GrantReplacementOldAbsent {
                            target_service: "provider_v1".into(),
                            target_handle: PrivateString::new(reference.to_owned()),
                        }
                    }
                    _ => return Err(FiniteCode::ConfigurationChanged),
                });
            }
        }
        if let Some(target) = completion_target {
            self.write_completion_receipt(
                binding,
                CompletionOutcome::Cleaned,
                target,
                completed_at,
            )?;
        } else if prepared_envelope.is_none() {
            return Err(FiniteCode::CredentialRecoveryRequired);
        }
        self.backend
            .delete(RECOVERY_SERVICE, &recovery)
            .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?;
        if self
            .backend
            .get(RECOVERY_SERVICE, &recovery)
            .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?
            .is_some()
        {
            return Err(FiniteCode::CredentialCleanupUnconfirmed);
        }
        if prepared_envelope.is_some() {
            self.backend
                .delete(PREPARED_SERVICE, &prepared)
                .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?;
            if self
                .backend
                .get(PREPARED_SERVICE, &prepared)
                .map_err(|_| FiniteCode::CredentialCleanupUnconfirmed)?
                .is_some()
            {
                return Err(FiniteCode::CredentialCleanupUnconfirmed);
            }
        }
        Ok(AuthState::Succeeded)
    }
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn vault_profile_revoke<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,version:u8,owner:u64,epoch:&str,handoff_id:&str)->serde_json::Value {
    let unavailable=||crate::vault_profile::unavailable(handoff_id);
    let snapshot=(|| -> Result<_,&'static str> {
        let mut auth=broker.lock().map_err(|_|"vault_unavailable")?;
        if version!=1 || !auth.vault_attestation_allowed {return Err("vault_unavailable");}
        let realm=auth.vault_realm(owner,epoch).ok_or("vault_unavailable")?.to_owned();
        if auth.vault_profile_id.as_deref()!=Some(handoff_id) {return Err("vault_unavailable");}
        if auth.vault_engine_busy || auth.vault_pending.is_some() || auth.vault_profile.is_none() {
            if let Some(cancel)=&auth.vault_profile_cancel {cancel.store(true,std::sync::atomic::Ordering::SeqCst);}
            if let Some(socket)=&auth.vault_pending {let _=socket.shutdown(std::net::Shutdown::Both);}
            return Err("vault_unavailable");
        }
        let current=auth.vault_profile.as_ref().ok_or("vault_unavailable")?;
        if current.grant.record.handoff_id!=handoff_id || current.realm!=realm {return Err("vault_unavailable");}
        if current.state=="closed" || (!current.attempted && current.browser.is_none()) {
            return Ok((realm,None,Some(auth.vault_profile_status(version,owner,epoch,handoff_id,true))));
        }
        if current.browser.is_none() {return Err("vault_unavailable");}
        let mut current=auth.vault_profile.take().ok_or("vault_unavailable")?;
        current.cancel.store(true,std::sync::atomic::Ordering::SeqCst);
        current.grant.proxy_secret.zeroize();current.state="unknown";current.fill_status="unknown";
        auth.vault_engine_busy=true;Ok((realm,Some(current),None))
    })();
    let Ok((realm,profile,ready))=snapshot else{return unavailable();};
    if let Some(ready)=ready{return ready;}
    let Some(mut profile)=profile else{return unavailable();};
    // The owned browser is taken out of AuthBroker before bounded shutdown;
    // neither owner transitions nor provider operations wait on this mutex.
    let closed=profile.browser.as_mut().is_some_and(|browser|browser.shutdown_confirmed().is_ok());
    if closed {profile.browser.take();profile.state="closed";} else {profile.state="unknown";}
    profile.fill_status="unknown";
    let Ok(mut auth)=broker.lock() else{return unavailable();};
    if !auth.vault_attestation_allowed || auth.vault_realm(owner,epoch)!=Some(realm.as_str()) || auth.vault_profile_id.as_deref()!=Some(handoff_id) {return unavailable();}
    auth.vault_engine_busy=false;
    if auth.vault_profile.is_some() || auth.vault_pending.is_some() {return unavailable();}
    let reply=profile.public();auth.vault_profile=Some(profile);
    if closed {reply} else {unavailable()}
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn vault_profile_recover<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,version:u8,owner:u64,epoch:&str,handoff_id:&str,profile_id:&str,metadata_sha:&str,record_sha:&str,journal_sha:Option<&str>)->serde_json::Value {
    let unavailable=||crate::vault_profile::unavailable(handoff_id);
    let snapshot=(|| -> Result<_,&'static str> {
        let mut auth=broker.lock().map_err(|_|"vault_unavailable")?;
        if version!=1 || !auth.vault_attestation_allowed || auth.vault_engine_busy {return Err("vault_unavailable");}
        let realm=auth.vault_realm(owner,epoch).ok_or("vault_unavailable")?.to_owned();
        if auth.vault_pending.is_some() {
            if auth.vault_profile_id.as_deref()==Some(handoff_id) {if let Some(cancel)=&auth.vault_profile_cancel {cancel.store(true,std::sync::atomic::Ordering::SeqCst);}if let Some(socket)=&auth.vault_pending {let _=socket.shutdown(std::net::Shutdown::Both);}}
            return Err("vault_unavailable");
        }
        if auth.vault_profile_id.as_deref().is_some_and(|id|id!=handoff_id) {return Err("vault_unavailable");}
        if auth.vault_profile.as_ref().is_some_and(|p|p.grant.record.profile_id!=profile_id || p.grant.record.handoff_id!=handoff_id) {return Err("vault_unavailable");}
        // In-flight local custody cannot be inferred closed from a missing slot.
        if auth.vault_profile.is_none() && auth.vault_profile_id.is_some() {if let Some(cancel)=&auth.vault_profile_cancel {cancel.store(true,std::sync::atomic::Ordering::SeqCst);}return Err("vault_unavailable");}
        let config=auth.vault_config.clone().ok_or("vault_unavailable")?;
        let profile=auth.vault_profile.take();
        if let Some(cancel)=&auth.vault_profile_cancel {cancel.store(true,std::sync::atomic::Ordering::SeqCst);}
        auth.vault_engine_busy=true;Ok((config,realm,profile))
    })();
    let Ok((config,realm,mut profile))=snapshot else{return unavailable();};
    let outcome=(|| -> Result<_,&'static str> {
        if let Some(current)=profile.as_mut() {
            current.cancel.store(true,std::sync::atomic::Ordering::SeqCst);
            if let Some(browser)=current.browser.as_mut() {browser.shutdown_confirmed()?;current.browser.take();current.state="closed";}
        }
        let app_data=config.directory.parent().ok_or("vault_unavailable")?;
        let (proof,lease)=crate::vault_profile::recover_proof(app_data,owner,profile_id,handoff_id,metadata_sha,record_sha,journal_sha)?;
        let auth=broker.lock().map_err(|_|"vault_unavailable")?;
        if !auth.vault_engine_busy || auth.vault_realm(owner,epoch)!=Some(realm.as_str()) || auth.vault_profile.is_some() || auth.vault_pending.is_some() {lease.release_after_confirmed_exit();return Err("vault_unavailable");}
        if lease.check().is_err(){lease.release_after_confirmed_exit();return Err("vault_unavailable");}
        let revision=proof.record.profile_revision;
        lease.release_after_confirmed_exit();
        Ok(serde_json::json!({"version":1,"status":"recovered","handoff_id":handoff_id,"profile_id":profile_id,"profile_revision":revision,"fill_status":"unknown"}))
    })();
    if let Ok(mut auth)=broker.lock() {
        auth.vault_engine_busy=false;
        if auth.vault_realm(owner,epoch)==Some(realm.as_str()) {
            if outcome.is_ok() {auth.vault_profile_id=None;auth.vault_profile_cancel=None;}
            else if auth.vault_profile.is_none() {if let Some(mut value)=profile.take(){if value.state!="closed" {value.state="unknown";value.fill_status="unknown";}auth.vault_profile=Some(value);}}
        }
    }
    outcome.unwrap_or_else(|_|unavailable())
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn custody_borrow<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,version:u8,owner:u64,epoch:&str,handoff:&str,profile_id:&str,revision:u64,metadata_sha:&str,record_sha:&str,custody_id:&str,requested:crate::vault_profile::CustodyBorrow)->serde_json::Value {
    let unavailable=||serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff,"custody_id":null,"borrow_id":null,"context_id":null});
    if version!=1 {return unavailable();}
    let (mut profile,session,config)=match broker.lock() {Ok(mut auth)=>{
        let canonical=serde_json::to_value(&requested.execution_binding).and_then(|v|serde_json::to_vec(&v));
        let digest=canonical.ok().map(|v|format!("{:x}",Sha256::digest(v)));
        let valid=requested.execution_binding.as_ref().map_or(requested.context_mode=="user_room",|b|
            b.valid(owner as i64,requested.agent_id as i64,&requested.root_request_event_id,requested.run_generation)
            && b.execution_room_id==requested.room_id as i64 && b.context_mode==requested.context_mode
            && b.lineage.iter().all(|item|auth.custody_revoked_scopes.get(&(owner,item.scope_id.clone())).is_none_or(|v|item.epoch>*v)));
        if auth.custody_scope_closed || !valid || digest.as_deref()!=Some(requested.execution_binding_sha256.as_str())
            || auth.custody_scope.as_ref().is_some_and(|(id,b)|id!=handoff || b!=&requested.execution_binding) {return unavailable();}
        match auth.custody_take(owner,epoch,handoff,custody_id) {
            Ok(v)=>{auth.custody_scope=Some((handoff.to_owned(),requested.execution_binding.clone()));v},Err(_)=>return unavailable()
        }
    },Err(_)=>return unavailable()};
    let realm=profile.realm.clone();
    let result=(|| -> Result<String,&'static str> {
        if profile.grant.record.profile_id!=profile_id || profile.grant.record.profile_revision!=revision || profile.grant.metadata_sha256!=metadata_sha || profile.grant.record_sha256!=record_sha || !requested.valid(&profile.grant.record) {return Err("vault_unavailable");}
        profile.grant.proof().validate_recipient(config.directory.parent().ok_or("vault_unavailable")?,owner,epoch,requested.room_id,requested.agent_id,&requested.origin,&requested.context_mode)?;
        let custody=profile.custody.as_ref().ok_or("vault_unavailable")?;
        if custody.phase=="borrowed" && custody.context_id.as_deref()==Some(requested.review_context_id.as_str()) && custody.borrow.as_ref()==Some(&requested) {
            if !profile.browser.as_mut().is_some_and(|b|b.custody_live()) {return Err("vault_unavailable");}
            return custody.context_id.clone().ok_or("vault_unavailable");
        }
        if custody.used_actions.contains(&requested.action_id) || custody.used_borrows.contains(&requested.id) {return Err("vault_unavailable");}
        let first=match custody.phase {
            "parked" if custody.context_id.as_deref()==Some(requested.review_context_id.as_str()) && custody.borrow.is_none() && custody.workflow.is_none()=>true,
            "borrowed" if custody.borrow.is_none() && custody.context_id.as_deref()==Some(requested.review_context_id.as_str())
                && requested.workflow_id.is_some() && custody.workflow.as_ref()==Some(&requested.workflow())=>false,
            _=>return Err("vault_unavailable"),
        };
        {let custody=profile.custody.as_mut().ok_or("vault_unavailable")?;custody.used_actions.insert(requested.action_id.clone());custody.used_borrows.insert(requested.id.clone());custody.phase="unknown";}
        let browser=profile.browser.as_mut().ok_or("vault_unavailable")?;
        if !browser.custody_live() {return Err("vault_unavailable");}
        if first {browser.borrow_blank()?;}
        let context=profile.custody.as_ref().and_then(|c|c.context_id.clone()).ok_or("vault_unavailable")?;
        if !browser.custody_live() || profile.cancel.load(std::sync::atomic::Ordering::SeqCst) || !requested.valid(&profile.grant.record) {return Err("vault_unavailable");}
        profile.grant.proof().validate_recipient(config.directory.parent().ok_or("vault_unavailable")?,owner,epoch,requested.room_id,requested.agent_id,&requested.origin,&requested.context_mode)?;
        let custody=profile.custody.as_mut().ok_or("vault_unavailable")?;custody.context_id=Some(context.clone());custody.workflow=Some(requested.workflow());custody.borrow=Some(requested.clone());custody.phase="borrowed";Ok(context)
    })();
    let mut auth=match broker.lock(){Ok(v)=>v,Err(_)=>return unavailable()};
    if !auth.custody_scope_allowed(owner,handoff,&requested.execution_binding) {profile.cancel.store(true,std::sync::atomic::Ordering::SeqCst);}
    let restored=auth.custody_restore(owner,epoch,profile,session).is_ok();
    if !restored && auth.vault_realm(owner,epoch)==Some(realm.as_str()) && auth.vault_profile_id.as_deref()==Some(handoff) {auth.vault_engine_busy=false;}
    if restored {if let Ok(context)=result {return serde_json::json!({"version":1,"status":"borrowed","handoff_id":handoff,"custody_id":custody_id,"borrow_id":requested.id,"context_id":context});}}unavailable()
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn custody_end<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,version:u8,owner:u64,epoch:&str,handoff:&str,custody_id:&str,borrow_id:&str,context_id:&str)->serde_json::Value {
    let unavailable=||serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff,"profile_id":null,"profile_revision":null,"custody_id":null,"review_context_id":null});
    if version!=1{return unavailable();}
    let (mut profile,session,config)=match broker.lock(){Ok(mut auth)=>{
        let finished=auth.vault_custody_task.as_ref().and_then(|v|v.lock().ok()).is_some_and(|t|t.handoff_id==handoff && t.custody_id==custody_id && t.borrow_id==borrow_id && matches!(t.status,"completed"|"failed"));
        if !finished{return unavailable();}match auth.custody_take(owner,epoch,handoff,custody_id){Ok(v)=>v,Err(_)=>return unavailable()}
    },Err(_)=>return unavailable()};
    let realm=profile.realm.clone();
    let result=(|| -> Result<(),&'static str> {
        let custody=profile.custody.as_ref().ok_or("vault_unavailable")?;
        if custody.phase!="borrowed" || custody.context_id.as_deref()!=Some(context_id) || custody.borrow.as_ref().is_none_or(|b|b.id!=borrow_id) {return Err("vault_unavailable");}
        if !profile.browser.as_mut().is_some_and(|browser|browser.custody_live()) || profile.cancel.load(std::sync::atomic::Ordering::SeqCst) {return Err("vault_unavailable");}
        profile.grant.validate(config.directory.parent().ok_or("vault_unavailable")?,owner,epoch)?;
        profile.custody.as_mut().ok_or("vault_unavailable")?.borrow=None;Ok(())
    })();
    let mut auth=match broker.lock(){Ok(v)=>v,Err(_)=>return unavailable()};
    let restored=auth.custody_restore(owner,epoch,profile,session).is_ok();
    if !restored && auth.vault_realm(owner,epoch)==Some(realm.as_str()) && auth.vault_profile_id.as_deref()==Some(handoff) {auth.vault_engine_busy=false;}
    if restored && result.is_ok(){auth.vault_custody_task.take();auth.vault_custody_status(1,owner,epoch,handoff)}else{unavailable()}
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn custody_park_commit_allowed<B:CredentialBackend,T:SyntheticAuthTransport>(auth:&AuthBroker<B,T>,owner:u64,handoff:&str,cancel:&std::sync::atomic::AtomicBool)->bool {
    !cancel.load(std::sync::atomic::Ordering::SeqCst)
        && auth.custody_scope.clone().is_some_and(|(id,binding)|id==handoff && auth.custody_scope_allowed(owner,handoff,&binding))
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn custody_park<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,version:u8,owner:u64,epoch:&str,handoff:&str,custody_id:&str,context_id:&str,workflow:crate::vault_profile::CustodyWorkflow)->serde_json::Value {
    let unavailable=||serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff,"profile_id":null,"profile_revision":null,"custody_id":null,"review_context_id":null});
    if version!=1{return unavailable();}
    let (mut profile,session,config)=match broker.lock(){Ok(mut auth)=>{
        if auth.vault_custody_task.is_some(){return unavailable();}
        let Some((id,binding))=auth.custody_scope.clone() else{return unavailable();};
        if id!=handoff || !auth.custody_scope_allowed(owner,handoff,&binding) {return unavailable();}
        match auth.custody_take(owner,epoch,handoff,custody_id){Ok(v)=>v,Err(_)=>return unavailable()}
    },Err(_)=>return unavailable()};let realm=profile.realm.clone();
    let result=(|| -> Result<(),&'static str> {
        let custody=profile.custody.as_mut().ok_or("vault_unavailable")?;
        if custody.phase!="borrowed" || custody.borrow.is_some() || custody.context_id.as_deref()!=Some(context_id) || custody.workflow.as_ref()!=Some(&workflow) {return Err("vault_unavailable");}
        custody.phase="unknown";
        let proof=profile.grant.proof();let browser=profile.browser.as_mut().ok_or("vault_unavailable")?;
        if !browser.custody_live(){return Err("vault_unavailable");}browser.park_targets()?;
        if !browser.custody_live() || profile.cancel.load(std::sync::atomic::Ordering::SeqCst) {return Err("vault_unavailable");}
        let lease=browser.profile_lease().ok_or("vault_unavailable")?;let lease=lease.lock().map_err(|_|"vault_unavailable")?;
        crate::vault_profile::verify_parked_receipt(&proof,config.directory.parent().ok_or("vault_unavailable")?,owner,epoch,custody_id,&lease)?;
        let mut random=[0u8;16];getrandom::fill(&mut random).map_err(|_|"vault_unavailable")?;let next=random.iter().map(|b|format!("{b:02x}")).collect::<String>();
        if next==context_id {return Err("vault_unavailable");}
        let custody=profile.custody.as_mut().ok_or("vault_unavailable")?;custody.context_id=Some(next);custody.workflow=None;custody.phase="parked";Ok(())
    })();
    let mut auth=match broker.lock(){Ok(v)=>v,Err(_)=>return unavailable()};
    let scope_ok=custody_park_commit_allowed(&auth,owner,handoff,&profile.cancel);
    if !scope_ok {profile.cancel.store(true,std::sync::atomic::Ordering::SeqCst);}
    let restored=auth.custody_restore(owner,epoch,profile,session).is_ok();
    if restored && result.is_ok() && scope_ok {auth.custody_scope=None;}
    if !restored && auth.vault_realm(owner,epoch)==Some(realm.as_str()) && auth.vault_profile_id.as_deref()==Some(handoff) {auth.vault_engine_busy=false;}
    if restored && result.is_ok() && scope_ok {auth.vault_custody_status(1,owner,epoch,handoff)}else{unavailable()}
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn custody_guard<B:CredentialBackend,T:SyntheticAuthTransport>(auth:&AuthBroker<B,T>,owner:u64,epoch:&str,handoff:&str,custody_id:&str,realm:&str,proof:&crate::vault_profile::ProfileProof,request:&crate::vault_profile::CustodyBorrow,lease:&crate::vault_profile::ProfileLease,manifest:&str,generation:&str,monitor:&std::os::unix::net::UnixStream,cancel:&std::sync::atomic::AtomicBool)->bool {
    if !auth.custody_scope_allowed(owner,handoff,&request.execution_binding) || cancel.load(std::sync::atomic::Ordering::SeqCst) || !crate::vault_control::active(monitor) || !auth.vault_attestation_allowed || !auth.vault_engine_busy
        || auth.vault_realm(owner,epoch)!=Some(realm) || auth.generation.as_ref().is_none_or(|g|g.fingerprint()!=generation) || auth.vault_profile_id.as_deref()!=Some(handoff) || !request.valid(&proof.record) || lease.check().is_err() {return false;}
    let Some(config)=auth.vault_config.as_ref() else{return false;};let Some(app_data)=config.directory.parent() else{return false;};
    if proof.validate_recipient(app_data,owner,epoch,request.room_id,request.agent_id,&request.origin,&request.context_mode).is_err() || crate::vault_profile::verify_parked_receipt(proof,app_data,owner,epoch,custody_id,lease).is_err(){return false;}
    let Some(path)=config.core.parent().and_then(std::path::Path::parent).map(|p|p.join("core-manifest.json")) else{return false;};
    crate::broker_runtime::SourcePin::open(&path,Some(manifest)).and_then(|p|p.revalidate()).is_ok()
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn custody_call_start<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,monitor:Option<&std::os::unix::net::UnixStream>,version:u8,owner:u64,epoch:&str,handoff:&str,custody_id:&str,borrow_id:&str,action_id:&str,operation:&str,selector:Option<&str>,url:&str,url_sha:&str)->serde_json::Value {
    let reply=|status|serde_json::json!({"version":1,"status":status,"borrow_id":borrow_id,"action_id":action_id,"text":""});
    if version!=1 || !matches!(operation,"navigate"|"inspect"|"click"|"screenshot") {return reply("unknown");}
    let monitor=match monitor.and_then(|v|v.try_clone().ok()) {Some(v) if crate::vault_control::active(&v)=>Arc::new(v),_=>return reply("unknown")};
    let (mut profile,session,config,task,request,generation)={
        let mut auth=match broker.lock(){Ok(v)=>v,Err(_)=>return reply("unknown")};
        if let Some(existing)=auth.vault_custody_task.as_ref() {
            let task=match existing.lock(){Ok(v)=>v,Err(_)=>return reply("unknown")};
            if task.handoff_id==handoff && task.custody_id==custody_id && task.borrow_id==borrow_id && task.action_id==action_id {return reply(if task.status=="pending" {"pending"} else {"unknown"});}
            return reply("unknown");
        }
        let generation=match auth.generation.as_ref(){Some(v)=>v.fingerprint().to_owned(),None=>return reply("unknown")};
        let (profile,session,config)=match auth.custody_take(owner,epoch,handoff,custody_id){Ok(v)=>v,Err(_)=>return reply("unknown")};
        let requested=profile.custody.as_ref().and_then(|c|if c.phase=="borrowed" {c.borrow.clone()} else {None});
        let Some(requested)=requested else {let _=auth.custody_restore(owner,epoch,profile,session);return reply("unknown");};
        if requested.id!=borrow_id || requested.action_id!=action_id || requested.operation!=operation || requested.selector.as_ref().map(|v|v.as_str())!=selector || requested.url.as_str()!=url || requested.url_sha256!=url_sha || !requested.valid(&profile.grant.record) {
            let _=auth.custody_restore(owner,epoch,profile,session);return reply("unknown");
        }
        let task=Arc::new(Mutex::new(crate::vault_profile::CustodyTask{handoff_id:handoff.into(),custody_id:custody_id.into(),borrow_id:borrow_id.into(),action_id:action_id.into(),status:"pending",text:Zeroizing::new(String::new()),started:std::time::Instant::now(),capture_id:None,capture:None}));
        auth.vault_custody_task=Some(task.clone());(profile,session,config,task,requested,generation)
    };
    let weak=Arc::downgrade(&task);
    let expiry_wait=if request.operation=="screenshot" {chrono::DateTime::parse_from_rfc3339(&request.expires_at).ok().and_then(|v|(v.with_timezone(&chrono::Utc)-chrono::Utc::now()).to_std().ok()).unwrap_or_default().min(std::time::Duration::from_secs(60))} else {std::time::Duration::from_secs(60)};
    if std::thread::Builder::new().name("vault-custody-expiry".into()).spawn(move || {
        std::thread::sleep(expiry_wait);
        if let Some(task)=weak.upgrade(){if let Ok(mut value)=task.lock(){value.text.zeroize();value.capture.take();value.status="unknown";}}
    }).is_err() {
        if let Ok(mut auth)=broker.lock(){let _=auth.custody_restore(owner,epoch,profile,session);auth.vault_custody_task.take();}
        return reply("unknown");
    }
    let worker_broker=broker.clone();let worker_task=task.clone();let owner_epoch=epoch.to_owned();let hid=handoff.to_owned();let cid=custody_id.to_owned();let realm=profile.realm.clone();
    let worker=std::thread::Builder::new().name("vault-custody-call".into()).spawn(move || {
        let result=(|| -> Result<(Option<Zeroizing<String>>,Option<Zeroizing<Vec<u8>>>),&'static str> {
            let proof=profile.grant.proof();let cancel=profile.cancel.clone();let browser=profile.browser.as_mut().ok_or("vault_unavailable")?;
            if !browser.custody_live(){return Err("vault_unavailable");}let manifest=browser.checked_manifest()?;let lease=browser.profile_lease().ok_or("vault_unavailable")?;
            let make_guard=|| {
                let broker=worker_broker.clone();let proof=proof.clone();let request=request.clone();let lease=lease.clone();let monitor=monitor.clone();let cancel=cancel.clone();let epoch=owner_epoch.clone();let hid=hid.clone();let cid=cid.clone();let realm=realm.clone();let manifest=manifest.clone();let generation=generation.clone();
                Box::new(move |pipe:&mut std::os::unix::net::UnixStream,frame:&[u8]| {
                    let auth=broker.lock().map_err(|_|"vault_unavailable")?;let locked=lease.lock().map_err(|_|"vault_unavailable")?;
                    if !custody_guard(&auth,owner,&epoch,&hid,&cid,&realm,&proof,&request,&locked,&manifest,&generation,&monitor,&cancel){return Err("vault_unavailable");}
                    use std::io::Write;pipe.write_all(frame).and_then(|_|pipe.write_all(&[0])).map_err(|_|"vault_unavailable")
                }) as crate::vault_proxy_pump::WriteGuard
            };
            let (result,image)=if request.operation=="screenshot" {
                let outcome=browser.custody_screenshot(&request.url,&request.origin,make_guard());
                if !matches!(&outcome,Ok(Some(_))) {browser.record_cdp_failure();}
                (None,outcome?)
            } else {
                let outcome=match request.operation.as_str() {
                    "navigate"=>browser.custody_navigate(&request.url,&request.origin,make_guard(),make_guard()),
                    "inspect"=>browser.custody_inspect(&request.url,&request.origin,make_guard()),
                    "click"=>browser.custody_click(&request.url,&request.origin,request.selector.as_ref().ok_or("vault_unavailable")?.as_str(),make_guard()),
                    _=>Err("vault_unavailable"),
                };
                if !matches!(&outcome,Ok(Some(_))) {browser.record_cdp_failure();}
                (outcome?.map(Zeroizing::new),None)
            };
            let auth=worker_broker.lock().map_err(|_|"vault_unavailable")?;let locked=lease.lock().map_err(|_|"vault_unavailable")?;
            if !custody_guard(&auth,owner,&owner_epoch,&hid,&cid,&realm,&proof,&request,&locked,&manifest,&generation,&monitor,&cancel) {return Err("vault_unavailable");}
            Ok((result,image))
        })();
        if let Ok(mut auth)=worker_broker.lock() {
            let current=auth.vault_custody_task.as_ref().is_some_and(|v|Arc::ptr_eq(v,&worker_task));
            if !current || !crate::vault_control::active(&monitor) || auth.generation.as_ref().is_none_or(|g|g.fingerprint()!=generation) {
                if current {auth.vault_cancel();}drop(auth);drop(profile);drop(session);return;
            }
            if result.is_err() {if let Some(c)=profile.custody.as_mut(){c.phase="unknown";}}
            let restored=auth.custody_restore(owner,&owner_epoch,profile,session).is_ok();
            if !restored && auth.vault_realm(owner,&owner_epoch)==Some(realm.as_str()) && auth.vault_profile_id.as_deref()==Some(hid.as_str()) {auth.vault_engine_busy=false;}
            if current {if let Ok(mut t)=worker_task.lock(){
                let live=t.status=="pending" && t.started.elapsed()<std::time::Duration::from_secs(60)
                    && chrono::DateTime::parse_from_rfc3339(&request.expires_at).is_ok_and(|v|v>chrono::Utc::now());
                if live {match result {
                    Ok((Some(text),None)) if restored=>{t.status="completed";t.text=text;},
                    Ok((None,Some(image))) if restored=>{
                        use sha2::{Digest,Sha256};let mut random=[0u8;16];
                        if getrandom::fill(&mut random).is_ok() {let capture_id=random.iter().map(|b|format!("{b:02x}")).collect::<String>();let size=image.len();let sha=format!("{:x}",Sha256::digest(&*image));let chunks=(size+23_999)/24_000;
                            t.text=Zeroizing::new(serde_json::json!({"version":1,"capture_id":capture_id.as_str(),"bytes":size,"sha256":sha,"chunks":chunks}).to_string());t.capture_id=Some(capture_id);t.capture=Some(image);t.status="completed";
                        } else {t.status="unknown";}
                    },
                    Ok((None,None)) if restored=>t.status="failed",_=>t.status="unknown"
                }}
                else {t.text.zeroize();t.capture.take();t.status="unknown";}
            }}
        }
        let _=config;
    });
    if worker.is_err(){if let Ok(mut auth)=broker.lock(){auth.vault_cancel();}return reply("unknown");}
    reply("pending")
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn custody_call_poll<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,monitor:Option<&std::os::unix::net::UnixStream>,version:u8,owner:u64,epoch:&str,handoff:&str,custody_id:&str,borrow_id:&str,action_id:&str)->serde_json::Value {
    let empty=||serde_json::json!({"version":1,"status":"unknown","borrow_id":borrow_id,"action_id":action_id,"text":""});
    if version!=1 || monitor.is_none_or(|v|!crate::vault_control::active(v)) {return empty();}
    let mut auth=match broker.lock(){Ok(v)=>v,Err(_)=>return empty()};
    if !auth.vault_attestation_allowed || auth.vault_realm(owner,epoch).is_none() || auth.vault_profile_id.as_deref()!=Some(handoff) || auth.vault_profile_cancel.as_ref().is_none_or(|v|v.load(std::sync::atomic::Ordering::SeqCst)) {return empty();}
    let Some(task)=auth.vault_custody_task.clone() else{return empty();};let mut task=match task.lock(){Ok(v)=>v,Err(_)=>return empty()};
    if task.handoff_id!=handoff || task.custody_id!=custody_id || task.borrow_id!=borrow_id || task.action_id!=action_id {return empty();}
    if task.started.elapsed()>std::time::Duration::from_secs(60) {task.text.zeroize();task.capture.take();task.status="unknown";return empty();}
    if task.status=="pending" {return serde_json::json!({"version":1,"status":"pending","borrow_id":borrow_id,"action_id":action_id,"text":""});}
    let Some(app_data)=auth.vault_config.as_ref().and_then(|c|c.directory.parent()).map(std::path::Path::to_owned) else{return empty();};
    let Some(profile)=auth.vault_profile.as_mut() else{return empty();};
    let Some(custody)=profile.custody.as_ref() else{return empty();};let Some(request)=custody.borrow.as_ref() else{return empty();};
    if custody.id!=custody_id || custody.phase!="borrowed" || custody.context_id.as_deref()!=Some(request.review_context_id.as_str()) || request.id!=borrow_id || request.action_id!=action_id || !request.valid(&profile.grant.record)
        || profile.grant.proof().validate_recipient(&app_data,owner,epoch,request.room_id,request.agent_id,&request.origin,&request.context_mode).is_err() {return empty();}
    let Some(browser)=profile.browser.as_mut() else{return empty();};if !browser.custody_live(){return empty();}
    let Some(lease)=browser.profile_lease() else{return empty();};let Ok(lease)=lease.lock() else{return empty();};
    if crate::vault_profile::verify_parked_receipt(&profile.grant.proof(),&app_data,owner,epoch,custody_id,&lease).is_err(){return empty();}
    let text=if task.status=="completed" {task.text.as_str()}else{""};
    serde_json::json!({"version":1,"status":task.status,"borrow_id":borrow_id,"action_id":action_id,"text":text})
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn custody_capture<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,monitor:Option<&std::os::unix::net::UnixStream>,version:u8,owner:u64,epoch:&str,handoff:&str,custody_id:&str,borrow_id:&str,action_id:&str,context_id:&str,capture_id:&str,index:Option<u64>)->serde_json::Value {
    use base64::Engine as _;
    let unavailable=||if let Some(i)=index {serde_json::json!({"version":1,"status":"unavailable","capture_id":capture_id,"index":i,"chunks":0,"data":""})} else {serde_json::json!({"version":1,"status":"unavailable","capture_id":capture_id})};
    if version!=1 || monitor.is_none_or(|v|!crate::vault_control::active(v)) {return unavailable();}
    let mut auth=match broker.lock(){Ok(v)=>v,Err(_)=>return unavailable()};
    if !auth.vault_attestation_allowed || auth.vault_realm(owner,epoch).is_none() || auth.vault_profile_id.as_deref()!=Some(handoff) {return unavailable();}
    let Some(task)=auth.vault_custody_task.clone() else{return unavailable();};let mut task=match task.lock(){Ok(v)=>v,Err(_)=>return unavailable()};
    if task.handoff_id!=handoff || task.custody_id!=custody_id || task.borrow_id!=borrow_id || task.action_id!=action_id || task.capture_id.as_deref()!=Some(capture_id) {return unavailable();}
    let Some(app_data)=auth.vault_config.as_ref().and_then(|c|c.directory.parent()).map(std::path::Path::to_owned) else{return unavailable();};
    let Some(profile)=auth.vault_profile.as_mut() else{return unavailable();};
    let Some(custody)=profile.custody.as_ref() else{return unavailable();};let Some(request)=custody.borrow.as_ref() else{return unavailable();};
    if custody.id!=custody_id || custody.phase!="borrowed" || custody.context_id.as_deref()!=Some(context_id) || request.review_context_id!=context_id || request.id!=borrow_id || request.action_id!=action_id || request.operation!="screenshot" {return unavailable();}
    if index.is_none() {task.capture.take();return serde_json::json!({"version":1,"status":"released","capture_id":capture_id});}
    if task.status!="completed" || task.started.elapsed()>=std::time::Duration::from_secs(60) || profile.cancel.load(std::sync::atomic::Ordering::SeqCst) || !request.valid(&profile.grant.record)
        || profile.grant.proof().validate_recipient(&app_data,owner,epoch,request.room_id,request.agent_id,&request.origin,&request.context_mode).is_err() {task.capture.take();return unavailable();}
    let Some(browser)=profile.browser.as_mut() else{return unavailable();};if !browser.custody_live(){task.capture.take();return unavailable();}
    let Some(lease)=browser.profile_lease() else{return unavailable();};let Ok(lease)=lease.lock() else{return unavailable();};
    if crate::vault_profile::verify_parked_receipt(&profile.grant.proof(),&app_data,owner,epoch,custody_id,&lease).is_err(){task.capture.take();return unavailable();}
    let Some(image)=task.capture.as_ref() else{return unavailable();};let chunks=(image.len()+23_999)/24_000;let Some(i)=index.and_then(|v|usize::try_from(v).ok()) else{return unavailable();};
    if i>=chunks {return unavailable();}let start=i*24_000;let end=(start+24_000).min(image.len());let data=base64::engine::general_purpose::STANDARD.encode(&image[start..end]);
    serde_json::json!({"version":1,"status":"ok","capture_id":capture_id,"index":i,"chunks":chunks,"data":data})
}

fn wipe_capture_value(value:serde_json::Value) {use zeroize::Zeroize;match value {serde_json::Value::String(mut s)=>s.zeroize(),serde_json::Value::Array(v)=>for x in v {wipe_capture_value(x)},serde_json::Value::Object(v)=>for (mut k,x) in v {k.zeroize();wipe_capture_value(x)},_=>{}}}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn vault_metadata_status<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>, version:u8, owner:u64, epoch:&str, item:&str, website:&str, username:&str)->Result<crate::vault::MetadataStatus,crate::vault_metadata_diagnostic::MetadataFailure> {
    use crate::vault_metadata_diagnostic::MetadataFailure as F;
    if version!=1 {return Err(F::SnapshotRejected);}
    let (realm,config,existing)=broker.lock().map_err(|_|F::SnapshotRejected)?.vault_engine_snapshot(owner,epoch,item,website,username).map_err(|_|F::SnapshotRejected)?;
    let result=(|| {
        if !existing {
            let session=crate::vault::VaultSession::start(&config.core,&config.engine,&config.engine_hash,config.engine_pid,&config.directory,&realm).map_err(|_|F::HelperStart)?;
            broker.lock().map_err(|_|F::Install)?.vault_engine_install(owner,epoch,&realm,session).map_err(|_|F::Install)?;
        }
        broker.lock().map_err(|_|F::Read)?.vault_engine_read(owner,epoch,&realm,item,website,username).map_err(|_|F::Read)
    })();
    if let Ok(mut auth)=broker.lock() {auth.vault_engine_busy=false;}
    result
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn vault_card_metadata_status<B:CredentialBackend,T:SyntheticAuthTransport>(broker:&Arc<Mutex<AuthBroker<B,T>>>,version:u8,owner:u64,epoch:&str,item:&str,metadata:&crate::vault_card::CardMetadata)->Result<crate::vault_card::CardMetadataStatus,crate::vault_metadata_diagnostic::MetadataFailure> {
    use crate::vault_metadata_diagnostic::MetadataFailure as F;
    if version!=1 {return Err(F::SnapshotRejected);}
    let (realm,config,existing)=broker.lock().map_err(|_|F::SnapshotRejected)?.vault_engine_card_snapshot(owner,epoch,item,metadata).map_err(|_|F::SnapshotRejected)?;
    let result=(|| {
        if !existing {
            let session=crate::vault::VaultSession::start(&config.core,&config.engine,&config.engine_hash,config.engine_pid,&config.directory,&realm).map_err(|_|F::HelperStart)?;
            broker.lock().map_err(|_|F::Install)?.vault_engine_install(owner,epoch,&realm,session).map_err(|_|F::Install)?;
        }
        broker.lock().map_err(|_|F::Read)?.vault_engine_card_read(owner,epoch,&realm,item,metadata).map_err(|_|F::Read)
    })();
    if let Ok(mut auth)=broker.lock(){auth.vault_engine_busy=false;}
    result
}

/// Serves one authenticated backend attachment over a private inherited
/// stream. The first outbound frame is the one-shot generation handshake;
/// all subsequent traffic is request/reply framed JSON.
pub fn serve_auth_channel<S, B, T>(
    mut stream: S,
    mut broker: AuthBroker<B, T>,
) -> Result<(), ()>
where
    S: Read + Write,
    B: CredentialBackend,
    T: SyntheticAuthTransport,
{
    broker.attach_backend(&mut stream).map_err(|_| ())?;
    serve_attached_auth_channel(stream, Arc::new(Mutex::new(broker)))
}

/// Serves an already-attached auth channel while the provider channel shares
/// the exact same broker generation and permit authority.
pub fn serve_attached_auth_channel<S, B, T>(stream:S,broker:Arc<Mutex<AuthBroker<B,T>>>)->Result<(),()>
where S:Read+Write,B:CredentialBackend,T:SyntheticAuthTransport {
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    {serve_attached_auth_channel_inner(stream,broker,None)}
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
    {serve_attached_auth_channel_inner(stream,broker)}
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
pub(crate) fn serve_attached_auth_channel_monitored<S,B,T>(stream:S,broker:Arc<Mutex<AuthBroker<B,T>>>,monitor:Option<std::os::unix::net::UnixStream>)->Result<(),()>
where S:Read+Write,B:CredentialBackend,T:SyntheticAuthTransport {serve_attached_auth_channel_inner(stream,broker,monitor)}

fn serve_attached_auth_channel_inner<S,B,T>(mut stream:S,broker:Arc<Mutex<AuthBroker<B,T>>>,#[cfg(all(target_os="macos",target_arch="aarch64"))] monitor:Option<std::os::unix::net::UnixStream>)->Result<(),()>
where S:Read+Write,B:CredentialBackend,T:SyntheticAuthTransport {
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    let metadata_inflight=Arc::new(std::sync::atomic::AtomicBool::new(false));
    // Captured once before processing requests: diagnostics never wait on the worker mutex.
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    let metadata_diagnostic_directory=broker.lock().ok().and_then(|auth|auth.vault_config.as_ref().map(|c|c.directory.clone()));
    loop {
        let mut header = [0u8; 4];
        match stream.read(&mut header[..1]) {
            Ok(0) => {
                if let Ok(mut broker) = broker.lock() { broker.owner_channel_lost(); }
                return Ok(());
            }
            Ok(1) => {}
            Ok(_) => unreachable!(),
            Err(_) => {
                if let Ok(mut broker) = broker.lock() { broker.owner_channel_lost(); }
                return Err(());
            }
        }
        if stream.read_exact(&mut header[1..]).is_err() {
            if let Ok(mut broker) = broker.lock() { broker.owner_channel_lost(); }
            return Err(());
        }
        let size = u32::from_be_bytes(header) as usize;
        if size == 0 || size > MAX_ENVELOPE {
            if let Ok(mut broker) = broker.lock() { broker.owner_channel_lost(); }
            return Err(());
        }
        let mut request = Zeroizing::new(vec![0u8; size]);
        if stream.read_exact(&mut request).is_err() {
            if let Ok(mut broker) = broker.lock() { broker.owner_channel_lost(); }
            return Err(());
        }
        let request = match serde_json::from_slice::<PrivateRequest>(&request) {
            Ok(request) => request,
            Err(_) => {
                if let Ok(mut broker) = broker.lock() { broker.owner_channel_lost(); }
                return Err(());
            }
        };
        let request = match request {
            PrivateRequest::Control(OwnerControl::VaultCustodySeal {version,owner_id,current_owner_epoch,handoff_id,profile_id,profile_revision,profile_metadata_sha256,record_sha256})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=broker.lock().map_err(|_|())?.vault_custody_seal(version,owner_id,&current_owner_epoch,&handoff_id,&profile_id,profile_revision,&profile_metadata_sha256,&record_sha256);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,profile_id,profile_revision,profile_metadata_sha256,record_sha256);serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"custody_id":null})};
                let reply=serde_json::to_vec(&result).map_err(|_|())?;let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCustodyStatus {version,owner_id,current_owner_epoch,handoff_id})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=broker.lock().map_err(|_|())?.vault_custody_status(version,owner_id,&current_owner_epoch,&handoff_id);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch);serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":null,"profile_revision":null,"custody_id":null,"review_context_id":null})};
                let reply=serde_json::to_vec(&result).map_err(|_|())?;let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCustodyBorrow {version,owner_id,current_owner_epoch,handoff_id,profile_id,profile_revision,profile_metadata_sha256,record_sha256,custody_id,borrow_id,review_context_id,workflow_id,room_id,agent_id,action_id,review_id,root_request_event_id,run_generation,execution_binding_sha256,execution_binding,origin,scope,context_mode,operation,selector,url,url_sha256,payload_digest,call_id,expires_at})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result={let requested=crate::vault_profile::CustodyBorrow{id:borrow_id,review_context_id,workflow_id,room_id,agent_id,action_id,review_id,root_request_event_id,run_generation,execution_binding_sha256,execution_binding,origin,scope,context_mode,operation,selector:selector.map(|v|Zeroizing::new(v.as_str().to_owned())),url:Zeroizing::new(url.as_str().to_owned()),url_sha256,payload_digest,call_id,expires_at};custody_borrow(&broker,version,owner_id,&current_owner_epoch,&handoff_id,&profile_id,profile_revision,&profile_metadata_sha256,&record_sha256,&custody_id,requested)};
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,profile_id,profile_revision,profile_metadata_sha256,record_sha256,custody_id,borrow_id,review_context_id,workflow_id,room_id,agent_id,action_id,review_id,root_request_event_id,run_generation,execution_binding_sha256,execution_binding,origin,scope,context_mode,operation,selector,url,url_sha256,payload_digest,call_id,expires_at);serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"custody_id":null,"borrow_id":null,"context_id":null})};
                let reply=serde_json::to_vec(&result).map_err(|_|())?;let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCustodyEnd {version,owner_id,current_owner_epoch,handoff_id,custody_id,borrow_id,context_id})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=custody_end(&broker,version,owner_id,&current_owner_epoch,&handoff_id,&custody_id,&borrow_id,&context_id);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,custody_id,borrow_id,context_id);serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":null,"profile_revision":null,"custody_id":null,"review_context_id":null})};
                let reply=serde_json::to_vec(&result).map_err(|_|())?;let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCustodyPark {version,owner_id,current_owner_epoch,handoff_id,custody_id,context_id,room_id,agent_id,root_request_event_id,run_generation,workflow_id})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=custody_park(&broker,version,owner_id,&current_owner_epoch,&handoff_id,&custody_id,&context_id,crate::vault_profile::CustodyWorkflow{room_id,agent_id,root_request_event_id,run_generation,workflow_id});
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,custody_id,context_id,room_id,agent_id,root_request_event_id,run_generation,workflow_id);serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":null,"profile_revision":null,"custody_id":null,"review_context_id":null})};
                let reply=serde_json::to_vec(&result).map_err(|_|())?;let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCustodyCallStart {version,owner_id,current_owner_epoch,handoff_id,custody_id,borrow_id,action_id,operation,selector,url,url_sha256})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=custody_call_start(&broker,monitor.as_ref(),version,owner_id,&current_owner_epoch,&handoff_id,&custody_id,&borrow_id,&action_id,&operation,selector.as_ref().map(|v|v.as_str()),url.as_str(),&url_sha256);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,handoff_id,custody_id,operation,selector,url,url_sha256);serde_json::json!({"version":1,"status":"unknown","borrow_id":borrow_id,"action_id":action_id,"text":""})};
                let reply=Zeroizing::new(serde_json::to_vec(&result).map_err(|_|())?);let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCustodyCallPoll {version,owner_id,current_owner_epoch,handoff_id,custody_id,borrow_id,action_id})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=custody_call_poll(&broker,monitor.as_ref(),version,owner_id,&current_owner_epoch,&handoff_id,&custody_id,&borrow_id,&action_id);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,handoff_id,custody_id);serde_json::json!({"version":1,"status":"unknown","borrow_id":borrow_id,"action_id":action_id,"text":""})};
                let reply=Zeroizing::new(serde_json::to_vec(&result).map_err(|_|())?);let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCustodyCaptureChunk {version,owner_id,current_owner_epoch,handoff_id,custody_id,borrow_id,action_id,context_id,capture_id,index})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=custody_capture(&broker,monitor.as_ref(),version,owner_id,&current_owner_epoch,&handoff_id,&custody_id,&borrow_id,&action_id,&context_id,&capture_id,Some(index));
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,handoff_id,custody_id,borrow_id,action_id,context_id);serde_json::json!({"version":1,"status":"unavailable","capture_id":capture_id,"index":index,"chunks":0,"data":""})};
                let encoded=serde_json::to_vec(&result);wipe_capture_value(result);let reply=Zeroizing::new(encoded.map_err(|_|())?);let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCustodyCaptureRelease {version,owner_id,current_owner_epoch,handoff_id,custody_id,borrow_id,action_id,context_id,capture_id})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=custody_capture(&broker,monitor.as_ref(),version,owner_id,&current_owner_epoch,&handoff_id,&custody_id,&borrow_id,&action_id,&context_id,&capture_id,None);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,handoff_id,custody_id,borrow_id,action_id,context_id);serde_json::json!({"version":1,"status":"unavailable","capture_id":capture_id})};
                let reply=serde_json::to_vec(&result).map_err(|_|())?;let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultProfileRevoke {version,owner_id,current_owner_epoch,handoff_id})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=vault_profile_revoke(&broker,version,owner_id,&current_owner_epoch,&handoff_id);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch);serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":null,"profile_revision":null,"fill_status":"unknown"})};
                let reply=serde_json::to_vec(&result).map_err(|_|())?;let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultProfileRecover {version,owner_id,current_owner_epoch,handoff_id,profile_id,profile_metadata_sha256,record_sha256,pendingjournal_sha256})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=vault_profile_recover(&broker,version,owner_id,&current_owner_epoch,&handoff_id,&profile_id,&profile_metadata_sha256,&record_sha256,pendingjournal_sha256.as_deref());
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch,profile_id,profile_metadata_sha256,record_sha256,pendingjournal_sha256);serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":null,"profile_revision":null,"fill_status":"unknown"})};
                let reply=serde_json::to_vec(&result).map_err(|_|())?;let size=u32::try_from(reply.len()).map_err(|_|())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut auth)=broker.lock(){auth.owner_channel_lost();}return Err(());}continue;
            },
            PrivateRequest::Control(OwnerControl::VaultCardMetadataStatus {version,owner_id,current_owner_epoch,item_id,cardholder,expiry_month,expiry_year,billing,last4}) => {
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let reply={
                    use std::sync::atomic::Ordering;
                    let unavailable=||crate::vault_card::CardMetadataStatus::unavailable();
                    let record=|stage| {if let Some(dir)=metadata_diagnostic_directory.as_deref(){crate::vault_metadata_diagnostic::record_failure(dir,stage);}};
                    use crate::vault_metadata_diagnostic::MetadataFailure as F;
                    let metadata=serde_json::from_value::<crate::vault_card::CardBilling>(billing).ok().map(|billing|crate::vault_card::CardMetadata{cardholder,expiry_month,expiry_year,billing,last4});
                    let result=if metadata.is_none() {record(F::SnapshotRejected);unavailable()} else if metadata_inflight.swap(true,Ordering::SeqCst) {record(F::Inflight);unavailable()} else {
                        let (send,receive)=std::sync::mpsc::sync_channel(1);let worker_broker=broker.clone();let flag=metadata_inflight.clone();
                        let worker=std::thread::Builder::new().name("vault-card-metadata".into()).spawn(move || {
                            struct Reset(Arc<std::sync::atomic::AtomicBool>);
                            impl Drop for Reset {fn drop(&mut self){self.0.store(false,Ordering::SeqCst);}}
                            let _reset=Reset(flag);
                            let value=vault_card_metadata_status(&worker_broker,version,owner_id,&current_owner_epoch,&item_id,&metadata.expect("checked metadata"));
                            let _=send.send(value);
                        });
                        match worker {Ok(_)=>match receive.recv_timeout(std::time::Duration::from_secs(3)) {Ok(Ok(value))=>{if let Some(stage)=crate::vault_metadata_diagnostic::classify(value.version,value.status,value.revision,value.command_id.as_deref(),Some(value.last4.as_deref())){record(stage);}value},Ok(Err(stage))=>{record(stage);unavailable()},Err(std::sync::mpsc::RecvTimeoutError::Timeout)=>{record(F::WorkerTimeout);unavailable()},Err(std::sync::mpsc::RecvTimeoutError::Disconnected)=>{record(F::WorkerDisconnected);unavailable()}},Err(_)=>{metadata_inflight.store(false,Ordering::SeqCst);record(F::WorkerSpawn);unavailable()}}
                    };
                    serde_json::to_vec(&result).map_err(|_| ())?
                };
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let reply={let _=(version,owner_id,current_owner_epoch,item_id,cardholder,expiry_month,expiry_year,billing,last4);serde_json::to_vec(&serde_json::json!({"version":1,"status":"unavailable","revision":null,"command_id":null,"last4":null})).map_err(|_| ())?};
                let size=u32::try_from(reply.len()).map_err(|_| ())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err(){if let Ok(mut authority)=broker.lock(){authority.owner_channel_lost();}return Err(());}
                continue;
            }
            PrivateRequest::Control(OwnerControl::VaultMetadataStatus {version,owner_id,current_owner_epoch,item_id,website,username}) => {
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let reply={
                    use std::sync::atomic::Ordering;
                    let unavailable=||crate::vault::MetadataStatus::unavailable();
                    let record=|stage| {if let Some(dir)=metadata_diagnostic_directory.as_deref(){crate::vault_metadata_diagnostic::record_failure(dir,stage);}};
                    use crate::vault_metadata_diagnostic::MetadataFailure as F;
                    let result=if metadata_inflight.swap(true,Ordering::SeqCst) {record(F::Inflight);unavailable()} else {
                        let (send,receive)=std::sync::mpsc::sync_channel(1);
                        let worker_broker=broker.clone();let flag=metadata_inflight.clone();
                        let worker=std::thread::Builder::new().name("vault-metadata".into()).spawn(move || {
                            struct Reset(Arc<std::sync::atomic::AtomicBool>);
                            impl Drop for Reset {fn drop(&mut self){self.0.store(false,Ordering::SeqCst);}}
                            let _reset=Reset(flag);
                            let value=vault_metadata_status(&worker_broker,version,owner_id,&current_owner_epoch,&item_id,&website,&username);
                            let _=send.send(value);
                        });
                        match worker {
                            Ok(_)=>match receive.recv_timeout(std::time::Duration::from_secs(3)) {Ok(Ok(value))=>{if let Some(stage)=crate::vault_metadata_diagnostic::classify(value.version,value.status,value.revision,value.command_id.as_deref(),None){record(stage);}value},Ok(Err(stage))=>{record(stage);unavailable()},Err(std::sync::mpsc::RecvTimeoutError::Timeout)=>{record(F::WorkerTimeout);unavailable()},Err(std::sync::mpsc::RecvTimeoutError::Disconnected)=>{record(F::WorkerDisconnected);unavailable()}},
                            Err(_)=>{metadata_inflight.store(false,Ordering::SeqCst);record(F::WorkerSpawn);unavailable()},
                        }
                    };
                    serde_json::to_vec(&result).map_err(|_| ())?
                };
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let reply={let _=(version,owner_id,current_owner_epoch,item_id,website,username);serde_json::to_vec(&serde_json::json!({"version":1,"status":"unavailable","revision":null,"command_id":null})).map_err(|_| ())?};
                let size=u32::try_from(reply.len()).map_err(|_| ())?;
                if stream.write_all(&size.to_be_bytes()).and_then(|_|stream.write_all(&reply)).is_err() {
                    if let Ok(mut authority)=broker.lock() {authority.owner_channel_lost();}
                    return Err(());
                }
                continue;
            }
            other=>other,
        };
        let mut authority = broker.lock().map_err(|_| ())?;
        let reply = match match request {
            PrivateRequest::Control(OwnerControl::OwnerBind { version: 1, owner_id }) => {
                serde_json::to_vec(&authority.bind_owner(owner_id).map_err(|_| ())?)
            }
            PrivateRequest::Control(OwnerControl::OwnerUnbind {
                version: 1,
                expected_owner_id,
                expected_owner_epoch,
            }) => serde_json::to_vec(
                &authority.unbind_owner(expected_owner_id, &expected_owner_epoch),
            ),
            PrivateRequest::Control(OwnerControl::VaultProfilePrepare {version,owner_id,current_owner_epoch,grant,profile_metadata_sha256,record_sha256,proxy_port,proxy_secret})=>{
                let id=grant.get("handoff_id").and_then(serde_json::Value::as_str).unwrap_or("").to_owned();
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let prepared=serde_json::from_value::<crate::vault_profile::Record>(grant).ok().is_some_and(|record|authority.vault_profile_prepare(version,owner_id,&current_owner_epoch,record,profile_metadata_sha256,record_sha256,proxy_port,proxy_secret));
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let prepared={let _=(version,owner_id,current_owner_epoch,grant,profile_metadata_sha256,record_sha256,proxy_port,proxy_secret);false};
                serde_json::to_vec(&serde_json::json!({"version":1,"status":if prepared {"prepared"}else{"unavailable"},"handoff_id":id}))
            },
            PrivateRequest::Control(OwnerControl::VaultProfileStatus {version,owner_id,current_owner_epoch,handoff_id})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let result=authority.vault_profile_status(version,owner_id,&current_owner_epoch,&handoff_id,false);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let result={let _=(version,owner_id,current_owner_epoch);serde_json::json!({"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":null,"profile_revision":null,"fill_status":"unknown"})};
                serde_json::to_vec(&result)
            },
            PrivateRequest::Control(OwnerControl::VaultCsvBind {version,owner_id,current_owner_epoch,draft_id,row,item_id,website,username,reservation_command})=>{
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let bound=authority.vault_csv_bind(version,owner_id,&current_owner_epoch,&draft_id,row,&item_id,&website,&username,&reservation_command);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let bound={let _=(version,owner_id,current_owner_epoch,draft_id,row,item_id,website,username,reservation_command);false};
                serde_json::to_vec(&serde_json::json!({"version":1,"state":if bound {"bound"} else {"unavailable"}}))
            }
            PrivateRequest::Control(OwnerControl::VaultRealmBind {
                version, owner_id, current_owner_epoch, vault_account_id,
            }) => {
                let bound = version == 1 && authority.bind_vault_realm(owner_id, &current_owner_epoch, &vault_account_id);
                serde_json::to_vec(&serde_json::json!({"version": 1, "state": if bound {"bound"} else {"unavailable"}}))
            }
            PrivateRequest::Control(OwnerControl::OperationAllocate {
                version: 1,
                owner_id,
                current_owner_epoch,
            }) => serde_json::to_vec(
                &authority.allocate_operation(owner_id, &current_owner_epoch),
            ),
            PrivateRequest::Control(_) => {
                authority.owner_channel_lost();
                return Err(());
            }
            PrivateRequest::Frame(AuthFrame::CredentialDiscard {
                target: CredentialDiscardTarget::Recovery(target),
                generation,
                action,
                commit_handle,
                adopted_prepared_ref,
            }) => serde_json::to_vec(&authority.handle_recovery_selector(
                &target,
                &generation,
                action,
                commit_handle.as_ref().map(PrivateString::as_str),
                adopted_prepared_ref.as_ref().map(PrivateString::as_str),
                chrono::Utc::now().timestamp(),
            )),
            PrivateRequest::Frame(frame) => {
                serde_json::to_vec(&authority.handle(frame, chrono::Utc::now().timestamp()))
            }
        } {
            Ok(reply) => reply,
            Err(_) => {
                authority.owner_channel_lost();
                return Err(());
            }
        };
        drop(authority);
        let response = Zeroizing::new(reply);
        let size = match u32::try_from(response.len()) {
            Ok(size) => size,
            Err(_) => {
                if let Ok(mut broker) = broker.lock() { broker.owner_channel_lost(); }
                return Err(());
            }
        };
        if stream.write_all(&size.to_be_bytes()).is_err()
            || stream.write_all(&response).is_err()
        {
            if let Ok(mut broker) = broker.lock() { broker.owner_channel_lost(); }
            return Err(());
        }
    }
}

fn ordinary_reference_matches(binding: &StableBinding, reference: &str) -> bool {
    let mut fields = reference.split(':');
    let owner = fields.next().unwrap_or_default();
    let connection = fields.next().unwrap_or_default();
    let opaque = fields.next().unwrap_or_default();
    fields.next().is_none()
        && owner == binding.owner_id.to_string()
        && connection == binding.connection_id.to_string()
        && (lower_hex(opaque, 32) || legacy_uuid(opaque))
}

fn push_fingerprint_string(output: &mut Vec<u8>, value: &str) -> Option<()> {
    let size = u32::try_from(value.len()).ok()?;
    output.extend_from_slice(&size.to_be_bytes());
    output.extend_from_slice(value.as_bytes());
    Some(())
}

fn generated_reference_matches(binding: &StableBinding, reference: &str) -> bool {
    let mut fields = reference.split(':');
    let owner = fields.next().unwrap_or_default();
    let connection = fields.next().unwrap_or_default();
    let opaque = fields.next().unwrap_or_default();
    fields.next().is_none()
        && owner == binding.owner_id.to_string()
        && connection == binding.connection_id.to_string()
        && lower_hex(opaque, 32)
}

fn recovery_account(operation_id: &str) -> String {
    format!("recovery:{operation_id}")
}

fn prepared_account(operation_id: &str) -> String {
    format!("prepared:{operation_id}")
}

fn completion_account(operation_id: &str) -> String {
    format!("completion:{operation_id}")
}

fn selector_from_binding(binding: &StableBinding) -> RecoverySelector {
    RecoverySelector {
        operation_id: binding.operation_id.clone(),
        owner_id: binding.owner_id,
        connection_id: binding.connection_id,
        kind: binding.kind.clone(),
        intent_fingerprint: binding.intent_fingerprint.clone(),
        before_state_fingerprint: binding.before_state_fingerprint.clone(),
        revoke_requested: binding.revoke_requested,
    }
}

fn ordinary_reference_matches_selector(selector: &RecoverySelector, reference: &str) -> bool {
    let mut fields = reference.split(':');
    let owner = fields.next().unwrap_or_default();
    let connection = fields.next().unwrap_or_default();
    let opaque = fields.next().unwrap_or_default();
    fields.next().is_none()
        && owner == selector.owner_id.to_string()
        && connection == selector.connection_id.to_string()
        && (lower_hex(opaque, 32) || legacy_uuid(opaque))
}

fn value_verifier_for(
    binding_fingerprint: &str,
    target_handle: &str,
    secret: &[u8],
) -> Option<String> {
    if !lower_hex(binding_fingerprint, 64) {
        return None;
    }
    let fingerprint = decode_lower_hex(binding_fingerprint)?;
    let target_size = u32::try_from(target_handle.len()).ok()?;
    let secret_size = u32::try_from(secret.len()).ok()?;
    let mut digest = Sha256::new();
    digest.update(b"gamja-auth-receipt-value-verifier\0v1\0");
    digest.update(&fingerprint);
    digest.update(target_size.to_be_bytes());
    digest.update(target_handle.as_bytes());
    digest.update(secret_size.to_be_bytes());
    digest.update(secret);
    Some(hex(digest.finalize().as_ref()))
}

fn decode_lower_hex(value: &str) -> Option<Zeroizing<Vec<u8>>> {
    if value.len() % 2 != 0 || !lower_hex(value, value.len()) {
        return None;
    }
    let mut output = Zeroizing::new(Vec::with_capacity(value.len() / 2));
    for pair in value.as_bytes().chunks_exact(2) {
        let high = (pair[0] as char).to_digit(16)? as u8;
        let low = (pair[1] as char).to_digit(16)? as u8;
        output.push((high << 4) | low);
    }
    Some(output)
}

fn encode_envelope(header: &SecureEnvelopeHeader, secret: &[u8]) -> Option<Zeroizing<Vec<u8>>> {
    let header = Zeroizing::new(serde_json::to_vec(header).ok()?);
    let total = 8usize.checked_add(header.len())?.checked_add(secret.len())?;
    if secret.is_empty() || total > MAX_ENVELOPE || header.len() > u32::MAX as usize {
        return None;
    }
    let mut output = Zeroizing::new(Vec::with_capacity(total));
    output.extend_from_slice(ENVELOPE_MAGIC);
    output.extend_from_slice(&(header.len() as u32).to_be_bytes());
    output.extend_from_slice(&header);
    output.extend_from_slice(secret);
    Some(output)
}

fn envelope_matches(
    input: Zeroizing<Vec<u8>>,
    expected_header: &SecureEnvelopeHeader,
    expected_secret: &[u8],
) -> bool {
    decode_envelope(input).is_some_and(|(header, secret)| {
        header == *expected_header && constant_eq_bytes(secret.as_ref(), expected_secret)
    })
}

fn receipt_from_header(
    binding: &StableBinding,
    header: &SecureEnvelopeHeader,
) -> Result<CommitReceipt, FiniteCode> {
    if header.binding != *binding
        || header
            .private_grant_identity
            .as_ref()
            .is_none_or(|identity| !lower_hex(identity.as_str(), 64))
    {
        return Err(FiniteCode::ConfigurationChanged);
    }
    let prepared_ref = header
        .prepared_destination
        .as_ref()
        .filter(|reference| generated_reference_matches(binding, reference.as_str()))
        .cloned()
        .ok_or(FiniteCode::CredentialRecoveryRequired)?;
    let commit_handle = header
        .commit_handle
        .as_ref()
        .filter(|handle| lower_hex(handle.as_str(), 64))
        .cloned()
        .ok_or(FiniteCode::CredentialRecoveryRequired)?;
    Ok(CommitReceipt {
        prepared_ref,
        commit_handle,
    })
}

fn decode_envelope(
    input: Zeroizing<Vec<u8>>,
) -> Option<(SecureEnvelopeHeader, Zeroizing<Vec<u8>>)> {
    if input.len() < 9 || input.len() > MAX_ENVELOPE || &input[..4] != ENVELOPE_MAGIC {
        return None;
    }
    let header_size = u32::from_be_bytes(input[4..8].try_into().ok()?) as usize;
    let secret_start = 8usize.checked_add(header_size)?;
    if secret_start >= input.len() {
        return None;
    }
    let header = serde_json::from_slice(&input[8..secret_start]).ok()?;
    let secret = Zeroizing::new(input[secret_start..].to_vec());
    Some((header, secret))
}

fn post_commit_revisions(binding: &StableBinding) -> Option<(u64, u64)> {
    match binding.kind.as_str() {
        "auth" | "grant_replace" => Some((
            binding.config_revision.checked_add(1)?,
            binding.auth_grant_revision.checked_add(1)?,
        )),
        "same_grant_refresh" => Some((binding.config_revision, binding.auth_grant_revision)),
        _ => None,
    }
}

fn encode_native_provider_value(
    binding: &StableBinding,
    native_enrollment: &NativeEnrollmentSpecV1,
    source_generation_fingerprint: &str,
    canonical_payload: &[u8],
) -> Result<Zeroizing<Vec<u8>>, FiniteCode> {
    if binding.auth_method_id != "native_chain"
        || native_enrollment.provider_id() != binding.provider_id
        || !lower_hex(source_generation_fingerprint, 64)
        || canonical_payload.is_empty()
    {
        return Err(FiniteCode::ConfigurationChanged);
    }
    let (config_revision, auth_grant_revision) = post_commit_revisions(binding)
        .ok_or(FiniteCode::ConfigurationChanged)?;
    let header = CommittedProviderHeaderV1 {
        version: 1,
        owner_id: binding.owner_id,
        connection_id: binding.connection_id,
        provider_id: binding.provider_id.clone(),
        auth_method_id: binding.auth_method_id.clone(),
        config_revision,
        auth_grant_revision,
        public_config_fingerprint: binding.public_config_fingerprint.clone(),
        execution_fields_fingerprint: native_enrollment.execution_fingerprint()
            .ok_or(FiniteCode::ConfigurationChanged)?,
        source_generation_fingerprint: source_generation_fingerprint.to_owned(),
        native_enrollment: native_enrollment.clone(),
    };
    let encoded_header = Zeroizing::new(
        serde_json::to_vec(&header).map_err(|_| FiniteCode::ConfigurationChanged)?,
    );
    let total = 8usize.checked_add(encoded_header.len())
        .and_then(|size| size.checked_add(canonical_payload.len()))
        .ok_or(FiniteCode::NativeEnrollmentTooLarge)?;
    if total > MAX_PROVIDER_VALUE || encoded_header.len() > u32::MAX as usize {
        return Err(FiniteCode::NativeEnrollmentTooLarge);
    }
    let mut output = Zeroizing::new(Vec::with_capacity(total));
    output.extend_from_slice(PROVIDER_VALUE_MAGIC);
    output.extend_from_slice(&(encoded_header.len() as u32).to_be_bytes());
    output.extend_from_slice(encoded_header.as_ref());
    output.extend_from_slice(canonical_payload);
    Ok(output)
}

fn encode_azure_api_key_provider_value(
    binding: &StableBinding,
    api_key: &[u8],
) -> Result<Zeroizing<Vec<u8>>, FiniteCode> {
    if binding.provider_id != "azure-openai"
        || binding.auth_method_id != "api_key"
        || !valid_azure_api_key(api_key)
    {
        return Err(FiniteCode::ConfigurationChanged);
    }
    let (config_revision, auth_grant_revision) = post_commit_revisions(binding)
        .ok_or(FiniteCode::ConfigurationChanged)?;
    let header = CommittedAzureApiKeyHeaderV1 {
        version: 1,
        credential_kind: CommittedCredentialKindV1::AzureApiKey,
        owner_id: binding.owner_id,
        connection_id: binding.connection_id,
        provider_id: binding.provider_id.clone(),
        auth_method_id: binding.auth_method_id.clone(),
        config_revision,
        auth_grant_revision,
        public_config_fingerprint: binding.public_config_fingerprint.clone(),
    };
    let encoded_header = Zeroizing::new(
        serde_json::to_vec(&header).map_err(|_| FiniteCode::ConfigurationChanged)?,
    );
    let total = 8usize
        .checked_add(encoded_header.len())
        .and_then(|size| size.checked_add(api_key.len()))
        .ok_or(FiniteCode::NativeEnrollmentTooLarge)?;
    if total > MAX_PROVIDER_VALUE || encoded_header.len() > u32::MAX as usize {
        return Err(FiniteCode::NativeEnrollmentTooLarge);
    }
    let mut output = Zeroizing::new(Vec::with_capacity(total));
    output.extend_from_slice(PROVIDER_VALUE_MAGIC);
    output.extend_from_slice(&(encoded_header.len() as u32).to_be_bytes());
    output.extend_from_slice(encoded_header.as_ref());
    output.extend_from_slice(api_key);
    Ok(output)
}

fn decode_provider_value(
    input: Zeroizing<Vec<u8>>,
) -> Option<DecodedProviderValue> {
    if input.len() < 9 || input.len() > MAX_PROVIDER_VALUE
        || &input[..4] != PROVIDER_VALUE_MAGIC
    {
        return None;
    }
    let header_size = u32::from_be_bytes(input[4..8].try_into().ok()?) as usize;
    let payload_start = 8usize.checked_add(header_size)?;
    if payload_start >= input.len() { return None; }
    let payload = Zeroizing::new(input[payload_start..].to_vec());
    let encoded_header = &input[8..payload_start];
    if let Ok(header) = serde_json::from_slice::<CommittedProviderHeaderV1>(encoded_header) {
        return Some(DecodedProviderValue {
            version: header.version,
            owner_id: header.owner_id,
            connection_id: header.connection_id,
            provider_id: header.provider_id,
            auth_method_id: header.auth_method_id,
            config_revision: header.config_revision,
            auth_grant_revision: header.auth_grant_revision,
            public_config_fingerprint: header.public_config_fingerprint,
            authority: CommittedProviderAuthority::Native {
                execution_fields_fingerprint: header.execution_fields_fingerprint,
                source_generation_fingerprint: header.source_generation_fingerprint,
                native_enrollment: header.native_enrollment,
            },
            payload,
        });
    }
    let header = serde_json::from_slice::<CommittedAzureApiKeyHeaderV1>(encoded_header).ok()?;
    Some(DecodedProviderValue {
        version: header.version,
        owner_id: header.owner_id,
        connection_id: header.connection_id,
        provider_id: header.provider_id,
        auth_method_id: header.auth_method_id,
        config_revision: header.config_revision,
        auth_grant_revision: header.auth_grant_revision,
        public_config_fingerprint: header.public_config_fingerprint,
        authority: CommittedProviderAuthority::AzureApiKey,
        payload,
    })
}

fn valid_azure_api_key(value: &[u8]) -> bool {
    !value.is_empty() && value.iter().all(|byte| matches!(byte, 0x21..=0x7e))
}

fn committed_authority_matches(
    candidate: &CommittedProviderAuthority,
    expected: &CommittedProviderAuthority,
) -> bool {
    match (candidate, expected) {
        (
            CommittedProviderAuthority::Native {
                execution_fields_fingerprint: candidate_execution,
                source_generation_fingerprint: candidate_source,
                native_enrollment: candidate_enrollment,
            },
            CommittedProviderAuthority::Native {
                execution_fields_fingerprint: expected_execution,
                source_generation_fingerprint: expected_source,
                native_enrollment: expected_enrollment,
            },
        ) => {
            constant_eq(candidate_execution, expected_execution)
                && constant_eq(candidate_source, expected_source)
                && candidate_enrollment == expected_enrollment
        }
        (
            CommittedProviderAuthority::AzureApiKey,
            CommittedProviderAuthority::AzureApiKey,
        ) => true,
        _ => false,
    }
}

fn authority_matches_request(
    authority: &CommittedProviderAuthority,
    request: &ProviderBindingRequest,
) -> bool {
    match authority {
        CommittedProviderAuthority::Native {
            execution_fields_fingerprint,
            native_enrollment,
            ..
        } => {
            native_enrollment.execution_fingerprint().as_deref()
                == Some(execution_fields_fingerprint.as_str())
                && native_spec_matches_request(native_enrollment, request)
        }
        CommittedProviderAuthority::AzureApiKey => {
            request.provider_id == "azure-openai"
                && request.auth_method_id == "api_key"
                && request.protocol
                    == crate::provider_transport::ProviderProtocol::AzureOpenAiResponsesV1
        }
    }
}

fn ordinary_reference_parts_match(reference: &str, owner_id: u64, connection_id: u64) -> bool {
    let prefix = format!("{owner_id}:{connection_id}:");
    reference.strip_prefix(&prefix).is_some_and(|suffix| {
        lower_hex(suffix, 32) || legacy_uuid(suffix)
    })
}

fn native_spec_matches_request(
    spec: &NativeEnrollmentSpecV1,
    request: &ProviderBindingRequest,
) -> bool {
    use crate::provider_transport::ProviderProtocol;
    let expected_fields = match spec {
        NativeEnrollmentSpecV1::AzureCli { resource_endpoint, api_version } => {
            if request.protocol != ProviderProtocol::AzureOpenAiResponsesV1 { return false; }
            serde_json::json!({
                "api_version": api_version,
                "resource_endpoint": resource_endpoint,
            })
        }
        NativeEnrollmentSpecV1::AwsProfile { profile_name, region } => {
            if request.protocol != ProviderProtocol::AwsBedrockConverse { return false; }
            serde_json::json!({"profile_name": profile_name, "region": region})
        }
        NativeEnrollmentSpecV1::VertexAdc { project_id, location } => {
            if request.protocol != ProviderProtocol::VertexGenerateContentV1 { return false; }
            serde_json::json!({"location": location, "project_id": project_id})
        }
    };
    request.provider_id == spec.provider_id()
        && request.auth_method_id == "native_chain"
        && request.public_fields == expected_fields
}

fn bounded_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
}

fn valid_browser_fixture(fixture: &BrowserFixture) -> bool {
    fixture.authorization_url.len() <= 2048
        && fixture.authorization_url.starts_with("https://")
        && fixture.callback_path.starts_with('/')
        && fixture.callback_path.len() <= 256
        && fixture.callback_port > 0
        && !fixture.state.is_empty()
        && fixture.state.len() <= 512
        && !fixture.pkce_verifier.is_empty()
        && fixture.pkce_verifier.len() <= 512
        && fixture.issuer.starts_with("https://")
        && fixture.issuer.len() <= 512
        && bounded_identifier(&fixture.audience)
        && !fixture.prepared_secret.is_empty()
        && fixture.prepared_secret.len() <= MAX_ENVELOPE / 2
}

fn valid_device_fixture(fixture: &DeviceFixture) -> bool {
    fixture.verification_uri.len() <= 2048
        && fixture.verification_uri.starts_with("https://")
        && !fixture.user_code.is_empty()
        && fixture.user_code.len() <= 128
        && (1..=300).contains(&fixture.poll_interval)
        && !fixture.prepared_secret.is_empty()
        && fixture.prepared_secret.len() <= MAX_ENVELOPE / 2
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

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn constant_eq(left: &str, right: &str) -> bool {
    constant_eq_bytes(left.as_bytes(), right.as_bytes())
}

fn constant_eq_bytes(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len() && bool::from(left.ct_eq(right))
}

fn mcp_secret_reference(reference: &str, owner_id: u64, server_id: &str) -> bool {
    let mut parts = reference.split(':');
    let owner = owner_id.to_string();
    matches!(parts.next(), Some("mcp" | "mcp-oauth")) && parts.next() == Some(owner.as_str())
        && parts.next() == Some(server_id) && server_id.len() == 36
        && server_id.bytes().enumerate().all(|(i,b)| if matches!(i,8|13|18|23) { b==b'-' } else { b.is_ascii_digit() || (b'a'..=b'f').contains(&b) })
        && parts.next().is_some_and(|s| s.len()==32 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
        && parts.next().is_none()
}

#[cfg(all(test,target_os="macos",target_arch="aarch64"))]
mod custody_scope_tests {
    use super::*;
    use crate::credential::CredentialBackendError;
    use std::sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}};
    #[derive(Default)] struct Secrets;
    impl CredentialBackend for Secrets {
        fn get(&mut self,_:&str,_:&str)->Result<Option<Vec<u8>>,CredentialBackendError>{Ok(None)}
        fn put(&mut self,_:&str,_:&str,_:&[u8])->Result<(),CredentialBackendError>{Ok(())}
        fn delete(&mut self,_:&str,_:&str)->Result<(),CredentialBackendError>{Ok(())}
    }
    fn binding(root:&str,child:&str)->crate::mcp_transport::ExecutionBinding {
        serde_json::from_value(serde_json::json!({"version":1,"root_scope_id":root,"scope_id":child,
            "owner_id":7,"root_room_id":1,"root_request_event_id":"33333333-3333-4333-8333-333333333333",
            "execution_room_id":1,"agent_id":8,"run_generation":1,"job_id":null,"phase":null,
            "context_mode":"user_room","deadline_at":"2099-01-01T00:00:00Z",
            "lineage":[{"scope_id":root,"epoch":0},{"scope_id":child,"epoch":0}]})).unwrap()
    }
    #[test]
    fn custody_revocation_duplicate_at_capacity_does_not_close() {
        let mut auth=AuthBroker::new(Secrets,UnavailableAuthTransport);
        let generation=auth.attach_backend(&mut Vec::new()).unwrap();
        let epoch=auth.bind_owner(7).unwrap().current_owner_epoch.unwrap();
        let core=generation.fingerprint().to_owned();
        for n in 0..16384 {auth.custody_revoked_scopes.insert((7,format!("{n:08x}-0000-4000-8000-000000000000")),1);}
        let old=crate::mcp_transport::ScopeEpoch{scope_id:"00000000-0000-4000-8000-000000000000".into(),epoch:2};
        assert!(auth.custody_revoke_scopes(7,&epoch,&core,&[old.clone(),old]).is_ok());
        assert!(!auth.custody_scope_closed);
        let new=crate::mcp_transport::ScopeEpoch{scope_id:"ffffffff-0000-4000-8000-000000000000".into(),epoch:1};
        assert!(auth.custody_revoke_scopes(7,&epoch,&core,&[new]).is_err());
        assert!(auth.custody_scope_closed);
    }
    // Controlled detached-profile boundary only. No Browser, VaultSession,
    // manifest, profile lease, CDP target or successful real park is created.
    #[test]
    fn controlled_detached_park_commit_and_real_revoke_ordering() {
        for revoke_first in [true,false] {
            let handoff="a".repeat(32);
            let mut auth=AuthBroker::new(Secrets,UnavailableAuthTransport);
            auth.attach_backend(&mut Vec::new()).unwrap();
            let epoch=auth.bind_owner(7).unwrap().current_owner_epoch.unwrap();
            auth.allow_vault_attestation();
            assert!(auth.bind_vault_realm(7,&epoch,&"b".repeat(32)));
            let cancelled=Arc::new(AtomicBool::new(false));
            auth.vault_profile_cancel=Some(cancelled.clone());
            auth.vault_profile_id=Some(handoff.clone());
            auth.vault_engine_busy=true;
            auth.custody_scope=Some((handoff.clone(),None));
            let broker=Arc::new(Mutex::new(auth));
            if revoke_first {
                let reply=vault_profile_revoke(&broker,1,7,&epoch,&handoff);
                assert_eq!(reply["status"],"unavailable");
                assert!(cancelled.load(Ordering::SeqCst));
                assert!(!custody_park_commit_allowed(&broker.lock().unwrap(),7,&handoff,&cancelled));
            } else {
                // This evaluates the actual commit gate before a later revoke;
                // it deliberately does not claim that any browser was parked.
                assert!(custody_park_commit_allowed(&broker.lock().unwrap(),7,&handoff,&cancelled));
                let reply=vault_profile_revoke(&broker,1,7,&epoch,&handoff);
                assert_eq!(reply["status"],"unavailable");
                assert!(cancelled.load(Ordering::SeqCst));
                assert!(!custody_park_commit_allowed(&broker.lock().unwrap(),7,&handoff,&cancelled));
            }
        }
    }
    #[test]
    fn custody_scope_revoke_and_park_are_serialized_and_new_scope_is_distinct() {
        let root="11111111-1111-4111-8111-111111111111";
        let old="22222222-2222-4222-8222-222222222222";
        let new="44444444-4444-4444-8444-444444444444";
        let mut auth=AuthBroker::new(Secrets,UnavailableAuthTransport);
        let generation=auth.attach_backend(&mut Vec::new()).unwrap();
        let epoch=auth.bind_owner(7).unwrap().current_owner_epoch.unwrap();
        let core=generation.fingerprint().to_owned();
        let cancel=Arc::new(AtomicBool::new(false));auth.vault_profile_cancel=Some(cancel.clone());
        let old_proof=Some(binding(root,old));let new_proof=Some(binding(root,new));
        let canonical=serde_json::to_vec(&serde_json::to_value(&old_proof).unwrap()).unwrap();
        assert_eq!(format!("{:x}",Sha256::digest(canonical)),"f3039d37cca86f63627d0cdf3925f63fce49896fc3b90bd7cebe439dc1657c24");
        auth.custody_scope=Some(("handoff".into(),old_proof.clone()));
        assert!(auth.custody_scope_allowed(7,"handoff",&old_proof));
        let shared=Arc::new(Mutex::new(auth));
        let worker=shared.clone();let epoch2=epoch.clone();let core2=core.clone();
        std::thread::spawn(move || worker.lock().unwrap().custody_revoke_scopes(7,&epoch2,&core2,
            &[crate::mcp_transport::ScopeEpoch{scope_id:old.into(),epoch:1}]).unwrap()).join().unwrap();
        let mut auth=shared.lock().unwrap();
        assert!(cancel.load(Ordering::SeqCst));
        assert!(!auth.custody_scope_allowed(7,"handoff",&old_proof));
        // A completed park drops the old association; an unrelated later scope is not cancelled.
        cancel.store(false,Ordering::SeqCst);auth.custody_scope=Some(("handoff".into(),new_proof.clone()));
        auth.custody_revoke_scopes(7,&epoch,&core,&[crate::mcp_transport::ScopeEpoch{scope_id:old.into(),epoch:2}]).unwrap();
        assert!(!cancel.load(Ordering::SeqCst));assert!(auth.custody_scope_allowed(7,"handoff",&new_proof));
        auth.custody_revoke_scopes(7,&epoch,&core,&[crate::mcp_transport::ScopeEpoch{scope_id:root.into(),epoch:1}]).unwrap();
        assert!(cancel.load(Ordering::SeqCst));assert!(!auth.custody_scope_allowed(7,"handoff",&new_proof));
    }
}
