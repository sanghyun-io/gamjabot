#![cfg(unix)]

use gamja_core_host::auth_broker::{
    serve_auth_channel, stable_binding_fingerprint, AuthBroker, AuthFrame, AuthReply, AuthState,
    BrowserCallback, BrowserFixture, CredentialDiscardAction, DeviceFixture, FiniteCode,
    GenerationBinding, NativeEnrollmentSpecV1, ProviderBindingRequest,
    OperationAllocationState, OwnerControlState, RecoveryInspectState, RecoveryMaterialState,
    RecoveryReceiptState,
    StableBinding, SyntheticAuthTransport, SyntheticFixtureTransport, UnavailableAuthTransport,
};
use gamja_core_host::provider_transport::{
    BoundedCancellation, BoundedProviderRequest, CoreProviderTransport, ProtocolCall,
    ProviderProtocol, SafeProviderResult, SdkAdapter,
};
use async_trait::async_trait;
use gamja_core_host::credential::{
    CredentialBackend, CredentialBackendError, CredentialOperation,
};
use serde_json::json;
use std::cell::RefCell;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use zeroize::Zeroizing;

const OPERATION: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const INTENT: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const BEFORE: &str = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const CURRENT: &str = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
const EPOCH: &str = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
const PUBLIC: &str = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";

thread_local! {
    static CURRENT_OWNER_EPOCH: RefCell<String> = RefCell::new(EPOCH.to_owned());
}

#[derive(Clone, Default)]
struct MemoryBackend {
    items: Arc<Mutex<HashMap<(String, String), Vec<u8>>>>,
    fail_put_account: Arc<Mutex<Option<String>>>,
    ignore_delete_account: Arc<Mutex<Option<String>>>,
    drop_put_account: Arc<Mutex<Option<String>>>,
    corrupt_put_account: Arc<Mutex<Option<String>>>,
    fail_provider_read_after_put: Arc<AtomicBool>,
    fail_next_provider_read: Arc<AtomicBool>,
}

impl MemoryBackend {
    fn insert(&self, service: &str, account: &str, value: &[u8]) {
        self.items
            .lock()
            .unwrap()
            .insert((service.into(), account.into()), value.to_vec());
    }

    fn value(&self, service: &str, account: &str) -> Option<Vec<u8>> {
        self.items
            .lock()
            .unwrap()
            .get(&(service.into(), account.into()))
            .cloned()
    }

    fn fail_put(&self, account: &str) {
        *self.fail_put_account.lock().unwrap() = Some(account.into());
    }

    fn ignore_delete(&self, account: &str) {
        *self.ignore_delete_account.lock().unwrap() = Some(account.into());
    }

    fn allow_delete(&self) {
        *self.ignore_delete_account.lock().unwrap() = None;
    }

    fn drop_put(&self, account: &str) {
        *self.drop_put_account.lock().unwrap() = Some(account.into());
    }

    fn corrupt_put(&self, account: &str) {
        *self.corrupt_put_account.lock().unwrap() = Some(account.into());
    }

    fn fail_read_after_next_provider_put(&self) {
        self.fail_provider_read_after_put.store(true, Ordering::SeqCst);
    }

    fn remove(&self, service: &str, account: &str) {
        self.items
            .lock()
            .unwrap()
            .remove(&(service.into(), account.into()));
    }

    fn accounts(&self, service: &str) -> Vec<String> {
        self.items
            .lock()
            .unwrap()
            .keys()
            .filter(|(candidate, _)| candidate == service)
            .map(|(_, account)| account.clone())
            .collect()
    }
}

impl CredentialBackend for MemoryBackend {
    fn get(
        &mut self,
        service: &str,
        account: &str,
    ) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        if service == gamja_core_host::credential::PROVIDER_SERVICE
            && self.fail_next_provider_read.swap(false, Ordering::SeqCst)
        {
            return Err(CredentialBackendError::failed(
                CredentialOperation::SecItemCopyMatching,
                -1,
            ));
        }
        Ok(self.value(service, account))
    }

    fn put(
        &mut self,
        service: &str,
        account: &str,
        value: &[u8],
    ) -> Result<(), CredentialBackendError> {
        if self.fail_put_account.lock().unwrap().as_deref() == Some(account) {
            return Err(CredentialBackendError::failed(
                CredentialOperation::SecItemAdd,
                -1,
            ));
        }
        if self.drop_put_account.lock().unwrap().as_deref() == Some(account) {
            return Ok(());
        }
        if self.corrupt_put_account.lock().unwrap().as_deref() == Some(account) {
            self.insert(service, account, b"corrupt-envelope");
            return Ok(());
        }
        self.insert(service, account, value);
        if service == gamja_core_host::credential::PROVIDER_SERVICE
            && self
                .fail_provider_read_after_put
                .swap(false, Ordering::SeqCst)
        {
            self.fail_next_provider_read.store(true, Ordering::SeqCst);
        }
        Ok(())
    }

    fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError> {
        if self.ignore_delete_account.lock().unwrap().as_deref() == Some(account) {
            return Ok(());
        }
        self.items
            .lock()
            .unwrap()
            .remove(&(service.into(), account.into()));
        Ok(())
    }
}

#[derive(Default)]
struct FakeTransport {
    prepared: Option<Zeroizing<Vec<u8>>>,
    cancel_count: Arc<AtomicUsize>,
}

struct FakeNativeTransport {
    payload: Vec<u8>,
    starts: Arc<AtomicUsize>,
}

impl SyntheticAuthTransport for FakeNativeTransport {
    fn start(
        &mut self,
        _binding: &StableBinding,
        native_enrollment: Option<&NativeEnrollmentSpecV1>,
    ) -> gamja_core_host::auth_broker::TransportResult {
        self.starts.fetch_add(1, Ordering::SeqCst);
        if native_enrollment.is_none() {
            return gamja_core_host::auth_broker::TransportResult::Unavailable;
        }
        gamja_core_host::auth_broker::TransportResult::NativeReady {
            source_generation_fingerprint: "1".repeat(64),
            canonical_payload: Zeroizing::new(self.payload.clone()),
        }
    }

    fn status(&mut self, _binding: &StableBinding) -> gamja_core_host::auth_broker::TransportResult {
        gamja_core_host::auth_broker::TransportResult::Unavailable
    }

    fn cancel(&mut self, _binding: &StableBinding) {}
}

#[derive(Debug)]
struct WaitingAwsAdapter;

#[async_trait]
impl SdkAdapter for WaitingAwsAdapter {
    fn protocol(&self) -> ProviderProtocol { ProviderProtocol::AwsBedrockConverse }

    async fn execute(&self, _call: &ProtocolCall) -> SafeProviderResult {
        tokio::time::sleep(std::time::Duration::from_secs(30)).await;
        SafeProviderResult::failure("unexpected_completion")
    }
}

impl SyntheticAuthTransport for FakeTransport {
    fn start(
        &mut self,
        _binding: &StableBinding,
        native_enrollment: Option<&gamja_core_host::auth_broker::NativeEnrollmentSpecV1>,
    ) -> gamja_core_host::auth_broker::TransportResult {
        if native_enrollment.is_some() {
            return gamja_core_host::auth_broker::TransportResult::Unavailable;
        }
        gamja_core_host::auth_broker::TransportResult::AwaitingUser {
            authorization_url: "https://synthetic.invalid/authorize".into(),
        }
    }

    fn status(&mut self, _binding: &StableBinding) -> gamja_core_host::auth_broker::TransportResult {
        match self.prepared.take() {
            Some(secret) => gamja_core_host::auth_broker::TransportResult::Ready(secret),
            None => gamja_core_host::auth_broker::TransportResult::Pending,
        }
    }

    fn cancel(&mut self, _binding: &StableBinding) {
        self.prepared = None;
        self.cancel_count.fetch_add(1, Ordering::SeqCst);
    }
}

fn binding(expires_at: i64) -> StableBinding {
    StableBinding {
        version: 1,
        operation_id: OPERATION.into(),
        owner_id: 7,
        connection_id: 9,
        kind: "grant_replace".into(),
        intent_fingerprint: INTENT.into(),
        before_state_fingerprint: BEFORE.into(),
        revoke_requested: false,
        expires_at,
        owner_epoch: CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone()),
        provider_id: "synthetic".into(),
        auth_method_id: "device".into(),
        config_revision: 3,
        auth_grant_revision: 4,
        public_config_fingerprint: PUBLIC.into(),
    }
}

fn attach_bound<B: CredentialBackend, T: SyntheticAuthTransport>(
    broker: &mut AuthBroker<B, T>,
    output: &mut impl Write,
) -> gamja_core_host::auth_broker::BackendGeneration {
    let generation = broker.attach_backend(output).unwrap();
    let epoch = broker
        .bind_owner(7)
        .unwrap()
        .current_owner_epoch
        .unwrap();
    CURRENT_OWNER_EPOCH.with(|current| *current.borrow_mut() = epoch);
    generation
}

fn frame(kind: &str, binding: &StableBinding, nonce: &str, current: &str) -> AuthFrame {
    serde_json::from_value(json!({
        "type": kind,
        "binding": binding,
        "generation": {
            "nonce": nonce,
            "current_generation_fingerprint": current
        }
    }))
    .unwrap()
}

fn native_start_frame(
    binding: &StableBinding,
    generation: &gamja_core_host::auth_broker::BackendGeneration,
    native_enrollment: serde_json::Value,
) -> AuthFrame {
    serde_json::from_value(json!({
        "type":"auth_start",
        "binding":binding,
        "generation":{
            "nonce":generation.nonce_hex(),
            "current_generation_fingerprint":generation.fingerprint()
        },
        "native_enrollment":native_enrollment
    })).unwrap()
}

fn generation_binding(
    generation: &gamja_core_host::auth_broker::BackendGeneration,
) -> GenerationBinding {
    serde_json::from_value(json!({
        "nonce":generation.nonce_hex(),
        "current_generation_fingerprint":generation.fingerprint()
    })).unwrap()
}

fn direct_prepare_frame(
    binding: &StableBinding,
    generation: &gamja_core_host::auth_broker::BackendGeneration,
    exact_old_reference: Option<&str>,
    direct_secret: &str,
) -> AuthFrame {
    let mut value = json!({
        "type":"credential_prepare",
        "binding":binding,
        "generation":{
            "nonce":generation.nonce_hex(),
            "current_generation_fingerprint":generation.fingerprint()
        },
        "direct_secret":direct_secret
    });
    if let Some(reference) = exact_old_reference {
        value["exact_old_reference"] = json!(reference);
    }
    serde_json::from_value(value).unwrap()
}

fn azure_api_key_binding(kind: &str, operation_id: &str) -> StableBinding {
    let mut value = binding(2_000_000_000);
    value.operation_id = operation_id.into();
    value.kind = kind.into();
    value.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    value.provider_id = "azure-openai".into();
    value.auth_method_id = "api_key".into();
    value
}

fn private_receipt(reply: &impl serde::Serialize) -> (String, String) {
    let value = serde_json::to_value(reply).unwrap();
    (
        value["prepared_ref"].as_str().unwrap().to_owned(),
        value["commit_handle"].as_str().unwrap().to_owned(),
    )
}

fn recovery_frame(
    action: &str,
    binding: &StableBinding,
    generation: &gamja_core_host::auth_broker::BackendGeneration,
    binding_fingerprint: Option<&str>,
    commit_handle: Option<&str>,
    adopted_prepared_ref: Option<&str>,
) -> AuthFrame {
    let mut target = json!({
        "recovery_selector": {
            "operation_id":binding.operation_id,
            "owner_id":binding.owner_id,
            "connection_id":binding.connection_id,
            "kind":binding.kind,
            "intent_fingerprint":binding.intent_fingerprint,
            "before_state_fingerprint":binding.before_state_fingerprint,
            "revoke_requested":binding.revoke_requested
        }
    });
    if let Some(fingerprint) = binding_fingerprint {
        target["binding_fingerprint"] = json!(fingerprint);
    }
    let mut value = json!({
        "type":"credential_discard",
        "target":target,
        "generation":{
            "nonce":generation.nonce_hex(),
            "current_generation_fingerprint":generation.fingerprint()
        },
        "action":action
    });
    if let Some(handle) = commit_handle {
        value["commit_handle"] = json!(handle);
    }
    if let Some(reference) = adopted_prepared_ref {
        value["adopted_prepared_ref"] = json!(reference);
    }
    serde_json::from_value(value).unwrap()
}

#[test]
fn exactly_seven_closed_frames_require_the_full_binding() {
    let binding = binding(2_000_000_000);
    let nonce = "11".repeat(32);
    for kind in [
        "auth_start",
        "auth_status",
        "auth_cancel",
        "credential_prepare",
        "credential_commit",
        "credential_refresh",
    ] {
        assert!(serde_json::to_value(frame(kind, &binding, &nonce, CURRENT)).is_ok());
    }
    let discard: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard",
        "target":{"exact_binding":binding},
        "generation":{"nonce":nonce,"current_generation_fingerprint":CURRENT},
        "action":"inspect"
    }))
    .unwrap();
    assert_eq!(discard.discard_action(), Some(CredentialDiscardAction::Inspect));

    for rejected in [
        json!({"type":"eighth_frame","binding":binding}),
        json!({"type":"auth_start","binding":binding,"generation":{"nonce":"11".repeat(32),"current_generation_fingerprint":CURRENT},"unknown":true}),
        json!({"type":"auth_start","binding":{"version":1},"generation":{"nonce":"11".repeat(32),"current_generation_fingerprint":CURRENT}}),
    ] {
        assert!(serde_json::from_value::<AuthFrame>(rejected).is_err());
    }
}

#[tokio::test]
async fn native_commit_issues_a_single_use_exact_permit_and_owner_change_cancels_it() {
    let backend = MemoryBackend::default();
    let starts = Arc::new(AtomicUsize::new(0));
    let mut broker = AuthBroker::new(
        backend.clone(),
        FakeNativeTransport { payload: br#"{"source":"aws_profile","config_contents":"[profile work]\\nsource_profile=leaf\\n","credentials_contents":"[leaf]\\naws_access_key_id=fixture\\naws_secret_access_key=fixture-secret\\n"}"#.to_vec(), starts: starts.clone() },
    );
    let generation = attach_bound(&mut broker, &mut Vec::new());
    let mut stable = binding(2_000_000_000);
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    stable.provider_id = "amazon-bedrock".into();
    stable.auth_method_id = "native_chain".into();

    let ready = broker.handle(
        native_start_frame(&stable, &generation, json!({
            "provider":"aws_profile", "profile_name":"work", "region":"us-east-1"
        })),
        1_900_000_000,
    );
    assert_eq!(ready.state, AuthState::Prepared);
    assert_eq!(starts.load(Ordering::SeqCst), 1);
    let committed = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_001,
    );
    assert_eq!(committed.state, AuthState::Committing);
    let (credential_ref, _) = private_receipt(&committed);
    let provider_value = backend.value(
        gamja_core_host::credential::PROVIDER_SERVICE,
        &credential_ref,
    ).unwrap();
    let prepared_value = backend.value(
        gamja_core_host::credential::PREPARED_SERVICE,
        &format!("prepared:{OPERATION}"),
    ).unwrap();
    assert!(provider_value.len() <= 32_768);
    assert!(prepared_value.len() <= 65_536);
    assert_eq!(&provider_value[..4], b"GPC1");

    let fields = json!({"profile_name":"work", "region":"us-east-1"});
    let request = ProviderBindingRequest {
        credential_ref,
        owner_id: stable.owner_id,
        owner_epoch: stable.owner_epoch.clone(),
        connection_id: stable.connection_id,
        config_revision: stable.config_revision + 1,
        auth_grant_revision: stable.auth_grant_revision + 1,
        provider_id: stable.provider_id.clone(),
        auth_method_id: stable.auth_method_id.clone(),
        public_config_fingerprint: stable.public_config_fingerprint.clone(),
        descriptor_revision: 1,
        protocol: ProviderProtocol::AwsBedrockConverse,
        model_id: "anthropic.claude-v1:0".into(),
        public_fields: fields.clone(),
    };
    let mut forged = request.clone();
    forged.descriptor_revision = 2;
    assert_eq!(
        broker.issue_provider_binding(forged, &generation_binding(&generation)),
        Err(FiniteCode::ConfigurationChanged)
    );
    let mut forged = request.clone();
    forged.protocol = ProviderProtocol::VertexGenerateContentV1;
    assert_eq!(
        broker.issue_provider_binding(forged, &generation_binding(&generation)),
        Err(FiniteCode::ConfigurationChanged)
    );
    let mut forged = request.clone();
    forged.model_id = "anthropic//escape".into();
    assert_eq!(
        broker.issue_provider_binding(forged, &generation_binding(&generation)),
        Err(FiniteCode::ConfigurationChanged)
    );
    let mut drifted = request.clone();
    drifted.config_revision += 1;
    assert_eq!(
        broker.issue_provider_binding(drifted, &generation_binding(&generation)),
        Err(FiniteCode::ConfigurationChanged)
    );
    let mut drifted = request.clone();
    drifted.public_fields["region"] = json!("us-west-2");
    assert_eq!(
        broker.issue_provider_binding(drifted, &generation_binding(&generation)),
        Err(FiniteCode::ConfigurationChanged)
    );
    let stale_handle = broker
        .issue_provider_binding(request.clone(), &generation_binding(&generation))
        .unwrap();
    let mut corrupted = provider_value.clone();
    corrupted[0] ^= 1;
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        &request.credential_ref,
        &corrupted,
    );
    assert!(matches!(
        broker.authorize_provider_source(&stale_handle, generation.fingerprint()),
        Err(FiniteCode::CredentialRecoveryRequired)
    ));
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        &request.credential_ref,
        &provider_value,
    );
    let handle = broker
        .issue_provider_binding(request, &generation_binding(&generation))
        .unwrap();
    let permits = broker.permit_registry();
    assert!(permits.authorize(&handle, &"0".repeat(64)).is_none());
    let permit = permits.authorize(&handle, generation.fingerprint()).unwrap();
    assert!(permits.authorize(&handle, generation.fingerprint()).is_none());
    let transport = CoreProviderTransport::from_authorized_permit(
        permit,
        Arc::new(WaitingAwsAdapter),
    ).unwrap();
    let call = ProtocolCall::new(
        handle,
        1,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-v1:0",
        fields.clone(),
        BoundedProviderRequest::new("safe request", 32).unwrap(),
    );

    let mut replacement = stable.clone();
    replacement.operation_id = "2".repeat(64);
    replacement.config_revision += 1;
    replacement.auth_grant_revision += 1;
    let replacement_ready = broker.handle(
        native_start_frame(&replacement, &generation, json!({
            "provider":"aws_profile", "profile_name":"work", "region":"us-east-1"
        })),
        1_900_000_002,
    );
    assert_eq!(replacement_ready.state, AuthState::Prepared);
    let replacement_commit = broker.handle(
        frame(
            "credential_commit",
            &replacement,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_003,
    );
    assert_eq!(replacement_commit.state, AuthState::Committing);
    let result = tokio::time::timeout(
        std::time::Duration::from_millis(200),
        transport.execute(call, &BoundedCancellation::new()),
    ).await.unwrap();
    assert_eq!(result.error_code, Some("cancelled"));

    let (replacement_ref, _) = private_receipt(&replacement_commit);
    let replacement_handle = broker.issue_provider_binding(
        ProviderBindingRequest {
            credential_ref: replacement_ref,
            owner_id: replacement.owner_id,
            owner_epoch: replacement.owner_epoch.clone(),
            connection_id: replacement.connection_id,
            config_revision: replacement.config_revision + 1,
            auth_grant_revision: replacement.auth_grant_revision + 1,
            provider_id: replacement.provider_id.clone(),
            auth_method_id: replacement.auth_method_id.clone(),
            public_config_fingerprint: replacement.public_config_fingerprint.clone(),
            descriptor_revision: 1,
            protocol: ProviderProtocol::AwsBedrockConverse,
            model_id: "anthropic.claude-v1:0".into(),
            public_fields: fields.clone(),
        },
        &generation_binding(&generation),
    ).unwrap();
    let replacement_permit = permits
        .authorize(&replacement_handle, generation.fingerprint())
        .unwrap();
    let replacement_transport = CoreProviderTransport::from_authorized_permit(
        replacement_permit,
        Arc::new(WaitingAwsAdapter),
    ).unwrap();
    let replacement_call = ProtocolCall::new(
        replacement_handle,
        1,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-v1:0",
        fields,
        BoundedProviderRequest::new("safe request", 32).unwrap(),
    );
    let rebound = broker.bind_owner(stable.owner_id).unwrap();
    assert_ne!(rebound.current_owner_epoch.as_deref(), Some(stable.owner_epoch.as_str()));
    let rebound_result = tokio::time::timeout(
        std::time::Duration::from_millis(200),
        replacement_transport.execute(replacement_call, &BoundedCancellation::new()),
    ).await.unwrap();
    assert_eq!(rebound_result.error_code, Some("cancelled"));
}

#[test]
fn historical_untagged_native_bytes_restore_then_issue_and_authorize_unchanged() {
    let backend = MemoryBackend::default();
    let mut broker = AuthBroker::new(
        backend.clone(),
        FakeNativeTransport {
            payload: br#"{"source":"aws_profile","config_contents":"[profile work]","credentials_contents":"[leaf]"}"#.to_vec(),
            starts: Arc::new(AtomicUsize::new(0)),
        },
    );
    let generation = attach_bound(&mut broker, &mut Vec::new());
    let mut original = binding(2_000_000_000);
    original.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    original.provider_id = "amazon-bedrock".into();
    original.auth_method_id = "native_chain".into();
    assert_eq!(
        broker
            .handle(
                native_start_frame(
                    &original,
                    &generation,
                    json!({"provider":"aws_profile","profile_name":"work","region":"us-east-1"}),
                ),
                1_900_000_000,
            )
            .state,
        AuthState::Prepared
    );
    let committed = broker.handle(
        frame(
            "credential_commit",
            &original,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_001,
    );
    let (credential_ref, _) = private_receipt(&committed);
    let historical = backend
        .value(gamja_core_host::credential::PROVIDER_SERVICE, &credential_ref)
        .unwrap();
    let header_len = u32::from_be_bytes(historical[4..8].try_into().unwrap()) as usize;
    let header: serde_json::Value = serde_json::from_slice(&historical[8..8 + header_len])
        .unwrap();
    assert!(header.get("credential_kind").is_none());

    let mut replacement = original.clone();
    replacement.operation_id = "8".repeat(64);
    replacement.config_revision += 1;
    replacement.auth_grant_revision += 1;
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":replacement,
        "generation":{
            "nonce":generation.nonce_hex(),
            "current_generation_fingerprint":generation.fingerprint()
        },
        "exact_old_reference":credential_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_002).state, AuthState::Prepared);
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        &credential_ref,
        b"changed-before-restore",
    );
    let restore: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard",
        "target":{"exact_binding":replacement},
        "generation":{
            "nonce":generation.nonce_hex(),
            "current_generation_fingerprint":generation.fingerprint()
        },
        "action":"restore_old"
    }))
    .unwrap();
    assert_eq!(broker.handle(restore, 1_900_000_003).state, AuthState::Restored);
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, &credential_ref),
        Some(historical.clone())
    );

    let request = ProviderBindingRequest {
        credential_ref: credential_ref.clone(),
        owner_id: original.owner_id,
        owner_epoch: original.owner_epoch.clone(),
        connection_id: original.connection_id,
        config_revision: original.config_revision + 1,
        auth_grant_revision: original.auth_grant_revision + 1,
        provider_id: original.provider_id.clone(),
        auth_method_id: original.auth_method_id.clone(),
        public_config_fingerprint: original.public_config_fingerprint.clone(),
        descriptor_revision: 1,
        protocol: ProviderProtocol::AwsBedrockConverse,
        model_id: "anthropic.claude-v1:0".into(),
        public_fields: json!({"profile_name":"work","region":"us-east-1"}),
    };
    let handle = broker
        .issue_provider_binding(request.clone(), &generation_binding(&generation))
        .unwrap();
    assert!(broker
        .authorize_provider_source(&handle, generation.fingerprint())
        .is_ok());

    let mut mixed_header = header;
    mixed_header["credential_kind"] = json!("azure_api_key");
    let mixed_header = serde_json::to_vec(&mixed_header).unwrap();
    let mut mixed = b"GPC1".to_vec();
    mixed.extend_from_slice(&(mixed_header.len() as u32).to_be_bytes());
    mixed.extend_from_slice(&mixed_header);
    mixed.extend_from_slice(&historical[8 + header_len..]);
    backend.insert(gamja_core_host::credential::PROVIDER_SERVICE, &credential_ref, &mixed);
    assert_eq!(
        broker.issue_provider_binding(request, &generation_binding(&generation)),
        Err(FiniteCode::CredentialRecoveryRequired)
    );
}

#[test]
fn azure_api_key_direct_prepare_is_bounded_idempotent_and_exactly_authorized() {
    let backend = MemoryBackend::default();
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    let stable = azure_api_key_binding("auth", OPERATION);
    let first = direct_prepare_frame(&stable, &generation, None, "fixture-api-key");
    assert!(!format!("{first:?}").contains("fixture-api-key"));
    assert_eq!(broker.handle(first, 1_900_000_000).state, AuthState::Prepared);
    let prepared_account = format!("prepared:{OPERATION}");
    let first_prepared = backend
        .value(gamja_core_host::credential::PREPARED_SERVICE, &prepared_account)
        .unwrap();

    let replay = broker.handle(
        direct_prepare_frame(&stable, &generation, None, "fixture-api-key"),
        1_900_000_001,
    );
    assert_eq!(replay.state, AuthState::Prepared);
    assert_eq!(
        backend.value(gamja_core_host::credential::PREPARED_SERVICE, &prepared_account),
        Some(first_prepared.clone())
    );
    let committed = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_002,
    );
    assert_eq!(committed.state, AuthState::Committing);
    let (credential_ref, _) = private_receipt(&committed);
    let provider_value = backend
        .value(gamja_core_host::credential::PROVIDER_SERVICE, &credential_ref)
        .unwrap();
    assert_eq!(&provider_value[..4], b"GPC1");
    assert!(provider_value.len() <= 32_768);
    let header_len = u32::from_be_bytes(provider_value[4..8].try_into().unwrap()) as usize;
    let header: serde_json::Value = serde_json::from_slice(&provider_value[8..8 + header_len])
        .unwrap();
    assert_eq!(header["credential_kind"], "azure_api_key");
    assert!(header.get("native_enrollment").is_none());
    assert!(header.get("source_generation_fingerprint").is_none());

    let request = ProviderBindingRequest {
        credential_ref: credential_ref.clone(),
        owner_id: stable.owner_id,
        owner_epoch: stable.owner_epoch.clone(),
        connection_id: stable.connection_id,
        config_revision: stable.config_revision + 1,
        auth_grant_revision: stable.auth_grant_revision + 1,
        provider_id: stable.provider_id.clone(),
        auth_method_id: stable.auth_method_id.clone(),
        public_config_fingerprint: stable.public_config_fingerprint.clone(),
        descriptor_revision: 1,
        protocol: ProviderProtocol::AzureOpenAiResponsesV1,
        model_id: "deployment-test".into(),
        public_fields: json!({
            "resource_endpoint":"https://potato.openai.azure.com",
            "api_version":"v1"
        }),
    };
    let handle = broker
        .issue_provider_binding(request.clone(), &generation_binding(&generation))
        .unwrap();
    assert!(broker
        .authorize_provider_source(&handle, generation.fingerprint())
        .is_ok());

    let mismatch = broker.handle(
        direct_prepare_frame(&stable, &generation, None, "different-api-key"),
        1_900_000_003,
    );
    assert_eq!(mismatch.state, AuthState::Recovering);
    assert_eq!(mismatch.code, Some(FiniteCode::CredentialRecoveryRequired));
    assert_eq!(
        backend.value(gamja_core_host::credential::PREPARED_SERVICE, &prepared_account),
        Some(first_prepared)
    );

    let mut mixed_header = header;
    mixed_header["native_enrollment"] = json!({
        "provider":"azure_cli",
        "resource_endpoint":"https://potato.openai.azure.com",
        "api_version":"v1"
    });
    let mixed_header = serde_json::to_vec(&mixed_header).unwrap();
    let mut mixed = b"GPC1".to_vec();
    mixed.extend_from_slice(&(mixed_header.len() as u32).to_be_bytes());
    mixed.extend_from_slice(&mixed_header);
    mixed.extend_from_slice(b"fixture-api-key");
    backend.insert(gamja_core_host::credential::PROVIDER_SERVICE, &credential_ref, &mixed);
    assert_eq!(
        broker.issue_provider_binding(request, &generation_binding(&generation)),
        Err(FiniteCode::CredentialRecoveryRequired)
    );
}

#[test]
fn azure_api_key_replace_stages_recovery_first_and_keeps_ambiguity_actionless() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"GPC1-old-provider-bytes",
    );
    backend.drop_put(&format!("prepared:{OPERATION}"));
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    let stable = azure_api_key_binding("grant_replace", OPERATION);

    let failed = broker.handle(
        direct_prepare_frame(&stable, &generation, Some(old_ref), "replacement-key"),
        1_900_000_000,
    );
    assert_eq!(failed.state, AuthState::Recovering);
    assert_eq!(failed.code, Some(FiniteCode::CredentialRecoveryRequired));
    let recovery_account = format!("recovery:{OPERATION}");
    let recovery = backend
        .value(gamja_core_host::credential::RECOVERY_SERVICE, &recovery_account)
        .unwrap();
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_none());

    let exact_retry = broker.handle(
        direct_prepare_frame(&stable, &generation, Some(old_ref), "replacement-key"),
        1_900_000_001,
    );
    assert_eq!(exact_retry.state, AuthState::Recovering);
    assert_eq!(exact_retry.code, Some(FiniteCode::CredentialRecoveryRequired));
    assert_eq!(
        backend.value(gamja_core_host::credential::RECOVERY_SERVICE, &recovery_account),
        Some(recovery.clone())
    );

    let mut drifted = stable.clone();
    drifted.public_config_fingerprint = "1".repeat(64);
    let drift = broker.handle(
        direct_prepare_frame(&drifted, &generation, Some(old_ref), "replacement-key"),
        1_900_000_002,
    );
    assert_eq!(drift.state, AuthState::Recovering);
    assert_eq!(drift.code, Some(FiniteCode::CredentialRecoveryRequired));
    assert_eq!(
        backend.value(gamja_core_host::credential::RECOVERY_SERVICE, &recovery_account),
        Some(recovery)
    );
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, old_ref),
        Some(b"GPC1-old-provider-bytes".to_vec())
    );
}

#[test]
fn azure_api_key_replace_reply_loss_replays_only_when_both_materials_are_exact() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"GPC1-exact-old-provider-bytes",
    );
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    let stable = azure_api_key_binding("grant_replace", OPERATION);
    assert_eq!(
        broker
            .handle(
                direct_prepare_frame(&stable, &generation, Some(old_ref), "replacement-key"),
                1_900_000_000,
            )
            .state,
        AuthState::Prepared
    );
    let recovery_account = format!("recovery:{OPERATION}");
    let prepared_account = format!("prepared:{OPERATION}");
    let recovery = backend
        .value(gamja_core_host::credential::RECOVERY_SERVICE, &recovery_account)
        .unwrap();
    let prepared = backend
        .value(gamja_core_host::credential::PREPARED_SERVICE, &prepared_account)
        .unwrap();

    let replay = broker.handle(
        direct_prepare_frame(&stable, &generation, Some(old_ref), "replacement-key"),
        1_900_000_001,
    );
    assert_eq!(replay.state, AuthState::Prepared);
    assert_eq!(
        backend.value(gamja_core_host::credential::RECOVERY_SERVICE, &recovery_account),
        Some(recovery)
    );
    assert_eq!(
        backend.value(gamja_core_host::credential::PREPARED_SERVICE, &prepared_account),
        Some(prepared)
    );
}

#[test]
fn azure_api_key_provider_value_enforces_the_actual_32768_byte_boundary() {
    let seed_backend = MemoryBackend::default();
    let mut seed_broker = AuthBroker::new(seed_backend.clone(), FakeTransport::default());
    let seed_generation = attach_bound(&mut seed_broker, &mut Vec::new());
    let seed = azure_api_key_binding("auth", OPERATION);
    assert_eq!(
        seed_broker
            .handle(
                direct_prepare_frame(&seed, &seed_generation, None, "x"),
                1_900_000_000,
            )
            .state,
        AuthState::Prepared
    );
    let committed = seed_broker.handle(
        frame(
            "credential_commit",
            &seed,
            seed_generation.nonce_hex(),
            seed_generation.fingerprint(),
        ),
        1_900_000_001,
    );
    let (seed_ref, _) = private_receipt(&committed);
    let seed_value = seed_backend
        .value(gamja_core_host::credential::PROVIDER_SERVICE, &seed_ref)
        .unwrap();
    let overhead = seed_value.len() - 1;
    let max_key_len = 32_768 - overhead;

    for (index, key_len, expected) in [
        (30usize, max_key_len, AuthState::Prepared),
        (31usize, max_key_len + 1, AuthState::Failed),
    ] {
        let backend = MemoryBackend::default();
        let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
        let generation = attach_bound(&mut broker, &mut Vec::new());
        let stable = azure_api_key_binding("auth", &format!("{index:064x}"));
        let key = "k".repeat(key_len);
        let reply = broker.handle(
            direct_prepare_frame(&stable, &generation, None, &key),
            1_900_000_000,
        );
        assert_eq!(reply.state, expected, "key_len={key_len}");
        if expected == AuthState::Prepared {
            let committed = broker.handle(
                frame(
                    "credential_commit",
                    &stable,
                    generation.nonce_hex(),
                    generation.fingerprint(),
                ),
                1_900_000_001,
            );
            let (reference, _) = private_receipt(&committed);
            assert_eq!(
                backend
                    .value(gamja_core_host::credential::PROVIDER_SERVICE, &reference)
                    .unwrap()
                    .len(),
                32_768
            );
        } else {
            assert_eq!(reply.code, Some(FiniteCode::NativeEnrollmentTooLarge));
        }
    }
}

#[test]
fn native_start_rejects_cross_provider_spec_and_oversized_final_value() {
    let starts = Arc::new(AtomicUsize::new(0));
    let mut broker = AuthBroker::new(
        MemoryBackend::default(),
        FakeNativeTransport { payload: vec![b'x'; 32_768], starts: starts.clone() },
    );
    let generation = attach_bound(&mut broker, &mut Vec::new());
    let mut stable = binding(2_000_000_000);
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    stable.provider_id = "google-vertex".into();
    stable.auth_method_id = "native_chain".into();

    let mismatched = broker.handle(
        native_start_frame(&stable, &generation, json!({
            "provider":"aws_profile", "profile_name":"work", "region":"us-east-1"
        })),
        1_900_000_000,
    );
    assert_eq!(mismatched.code, Some(FiniteCode::ConfigurationChanged));
    assert_eq!(starts.load(Ordering::SeqCst), 0);

    let too_large = broker.handle(
        native_start_frame(&stable, &generation, json!({
            "provider":"vertex_adc", "project_id":"potato-project", "location":"us-central1"
        })),
        1_900_000_001,
    );
    assert_eq!(too_large.code, Some(FiniteCode::NativeEnrollmentTooLarge));
    assert_eq!(too_large.state, AuthState::Recovering);
    assert_eq!(starts.load(Ordering::SeqCst), 1);
}

#[test]
fn compiled_native_descriptors_fix_protocol_revision_shape_and_provider_model_ids() {
    let cases = [
        (
            "azure-openai",
            json!({"provider":"azure_cli","resource_endpoint":"https://potato.openai.azure.com","api_version":"v1"}),
            ProviderProtocol::AzureOpenAiResponsesV1,
            json!({"resource_endpoint":"https://potato.openai.azure.com","api_version":"v1"}),
            "deployment-1",
            "deployment?bad",
        ),
        (
            "amazon-bedrock",
            json!({"provider":"aws_profile","profile_name":"work","region":"us-east-1"}),
            ProviderProtocol::AwsBedrockConverse,
            json!({"profile_name":"work","region":"us-east-1"}),
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile//bad",
        ),
        (
            "google-vertex",
            json!({"provider":"vertex_adc","project_id":"potato-project","location":"us-central1"}),
            ProviderProtocol::VertexGenerateContentV1,
            json!({"project_id":"potato-project","location":"us-central1"}),
            "gemini-2.5-pro",
            "gemini:bad",
        ),
    ];
    for (index, (provider, spec, protocol, fields, model, invalid_model))
        in cases.into_iter().enumerate()
    {
        let backend = MemoryBackend::default();
        let mut broker = AuthBroker::new(
            backend,
            FakeNativeTransport {
                payload: br#"{"source":"fixture"}"#.to_vec(),
                starts: Arc::new(AtomicUsize::new(0)),
            },
        );
        let generation = attach_bound(&mut broker, &mut Vec::new());
        let mut stable = binding(2_000_000_000);
        stable.operation_id = format!("{:064x}", index + 10);
        stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
        stable.provider_id = provider.into();
        stable.auth_method_id = "native_chain".into();
        assert_eq!(
            broker.handle(native_start_frame(&stable, &generation, spec), 1_900_000_000).state,
            AuthState::Prepared,
        );
        let committed = broker.handle(
            frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
            1_900_000_001,
        );
        let (credential_ref, _) = private_receipt(&committed);
        let request = ProviderBindingRequest {
            credential_ref,
            owner_id: stable.owner_id,
            owner_epoch: stable.owner_epoch.clone(),
            connection_id: stable.connection_id,
            config_revision: stable.config_revision + 1,
            auth_grant_revision: stable.auth_grant_revision + 1,
            provider_id: stable.provider_id.clone(),
            auth_method_id: stable.auth_method_id.clone(),
            public_config_fingerprint: stable.public_config_fingerprint.clone(),
            descriptor_revision: 1,
            protocol,
            model_id: model.into(),
            public_fields: fields,
        };
        let mut forged = request.clone();
        forged.descriptor_revision = 2;
        assert_eq!(broker.issue_provider_binding(forged, &generation_binding(&generation)),
            Err(FiniteCode::ConfigurationChanged));
        let mut forged = request.clone();
        forged.protocol = match protocol {
            ProviderProtocol::AzureOpenAiResponsesV1 => ProviderProtocol::AwsBedrockConverse,
            _ => ProviderProtocol::AzureOpenAiResponsesV1,
        };
        assert_eq!(broker.issue_provider_binding(forged, &generation_binding(&generation)),
            Err(FiniteCode::ConfigurationChanged));
        let mut forged = request.clone();
        forged.public_fields["forged"] = json!("field");
        assert_eq!(broker.issue_provider_binding(forged, &generation_binding(&generation)),
            Err(FiniteCode::ConfigurationChanged));
        let mut forged = request.clone();
        forged.model_id = invalid_model.into();
        assert_eq!(broker.issue_provider_binding(forged, &generation_binding(&generation)),
            Err(FiniteCode::ConfigurationChanged));
        assert!(broker.issue_provider_binding(request.clone(), &generation_binding(&generation)).is_ok());
        let mut boundary = request.clone();
        boundary.model_id = format!("m{}", "a".repeat(199));
        assert!(broker.issue_provider_binding(boundary, &generation_binding(&generation)).is_ok());
        let mut too_long = request;
        too_long.model_id = format!("m{}", "a".repeat(200));
        assert_eq!(broker.issue_provider_binding(too_long, &generation_binding(&generation)),
            Err(FiniteCode::ConfigurationChanged));
    }
}

#[test]
fn r3_binding_fingerprint_v1_matches_the_independent_byte_golden_vector() {
    assert_eq!(
        stable_binding_fingerprint(&binding(2_000_000_000)).as_deref(),
        Some("c3723a0039197a3120d1cd99c0b5da384af8ecd7c30c3948d44acfc2833355eb")
    );
    let mut invalid = binding(2_000_000_000);
    invalid.version = 2;
    assert_eq!(stable_binding_fingerprint(&invalid), None);
    let mut no_user_expiry = binding(i64::MAX);
    assert_eq!(stable_binding_fingerprint(&no_user_expiry), None);
    no_user_expiry.kind = "disconnect".into();
    assert!(stable_binding_fingerprint(&no_user_expiry).is_some());
}

#[test]
fn r3_owner_bind_rotates_epoch_and_compare_unbind_preserves_newer_binding() {
    let mut broker = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    let generation = broker.attach_backend(&mut Vec::new()).unwrap();
    let first = broker.bind_owner(7).unwrap();
    assert_eq!(first.state, OwnerControlState::Bound);
    let first_epoch = first.current_owner_epoch.unwrap();
    assert_eq!(first_epoch.len(), 64);
    let mut old_binding = binding(2_000_000_000);
    old_binding.owner_epoch = first_epoch.clone();
    assert_eq!(
        broker
            .handle(
                frame("auth_start", &old_binding, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_000,
            )
            .state,
        AuthState::AwaitingUser
    );

    let second = broker.bind_owner(7).unwrap();
    let second_epoch = second.current_owner_epoch.unwrap();
    assert_ne!(first_epoch, second_epoch);
    assert_eq!(
        broker
            .handle(
                frame("auth_status", &old_binding, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_001,
            )
            .code,
        Some(FiniteCode::OwnerMismatch)
    );
    assert_eq!(
        broker.unbind_owner(7, &first_epoch).state,
        OwnerControlState::MismatchPreserved
    );
    let mut current_binding = binding(2_000_000_000);
    current_binding.owner_epoch = second_epoch.clone();
    assert_ne!(
        broker
            .handle(
                frame("auth_start", &current_binding, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_002,
            )
            .code,
        Some(FiniteCode::OwnerMismatch)
    );
    assert_eq!(
        broker.unbind_owner(7, &second_epoch).state,
        OwnerControlState::Unbound
    );
    assert_eq!(
        broker
            .handle(
                frame("auth_start", &current_binding, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_003,
            )
            .code,
        Some(FiniteCode::OwnerMismatch)
    );
}

#[test]
fn core_allocates_unpredictable_operation_ids_only_for_the_exact_bound_owner_epoch() {
    let mut broker = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    broker.attach_backend(&mut Vec::new()).unwrap();
    let unbound = broker.allocate_operation(7, EPOCH);
    assert_eq!(unbound.state, OperationAllocationState::Failed);
    assert_eq!(unbound.code, Some(FiniteCode::OwnerEpochUnavailable));
    assert!(unbound.operation_id.is_none());

    let bound = broker.bind_owner(7).unwrap();
    let epoch = bound.current_owner_epoch.unwrap();
    let stale = broker.allocate_operation(7, EPOCH);
    assert_eq!(stale.state, OperationAllocationState::Failed);
    assert_eq!(stale.code, Some(FiniteCode::OwnerEpochChanged));
    assert!(stale.operation_id.is_none());

    let first = broker.allocate_operation(7, &epoch);
    let second = broker.allocate_operation(7, &epoch);
    assert_eq!(first.state, OperationAllocationState::Allocated);
    assert_eq!(second.state, OperationAllocationState::Allocated);
    let first_id = first.operation_id.unwrap();
    let second_id = second.operation_id.unwrap();
    assert_eq!(first_id.len(), 64);
    assert!(first_id.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)));
    assert_ne!(first_id, second_id);
}

#[test]
fn attach_replaces_generation_and_old_nonce_cannot_drive_user_actions() {
    let mut broker = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    let mut first_handshake = Vec::new();
    let first = attach_bound(&mut broker, &mut first_handshake);
    let mut second_handshake = Vec::new();
    let second = attach_bound(&mut broker, &mut second_handshake);

    assert_ne!(first.nonce_hex(), second.nonce_hex());
    assert_ne!(first.fingerprint(), second.fingerprint());
    assert_eq!(first_handshake.len() >= 4, true);
    assert_eq!(second_handshake.len() >= 4, true);
    let reply = broker.handle(
        frame("auth_start", &binding(2_000_000_000), first.nonce_hex(), first.fingerprint()),
        1_900_000_000,
    );
    assert_eq!(reply.code, Some(FiniteCode::OperationInterrupted));
}

#[test]
fn replacement_interrupts_origin_and_all_six_non_cleanup_frames_fail_closed() {
    let transport = FakeTransport::default();
    let cancellations = transport.cancel_count.clone();
    let mut broker = AuthBroker::new(MemoryBackend::default(), transport);
    let first = attach_bound(&mut broker, &mut Vec::new());
    let mut stable = binding(2_000_000_000);
    assert_eq!(
        broker
            .handle(
                frame("auth_start", &stable, first.nonce_hex(), first.fingerprint()),
                1_900_000_000,
            )
            .state,
        AuthState::AwaitingUser
    );
    let replacement = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    assert_eq!(cancellations.load(Ordering::SeqCst), 1);

    for kind in [
        "auth_start",
        "auth_status",
        "auth_cancel",
        "credential_prepare",
        "credential_commit",
        "credential_refresh",
    ] {
        let reply = broker.handle(
            frame(kind, &stable, replacement.nonce_hex(), replacement.fingerprint()),
            1_900_000_001,
        );
        assert_eq!(reply.state, AuthState::Failed, "{kind}");
        assert_eq!(reply.code, Some(FiniteCode::ConfigurationChanged), "{kind}");
    }
}

#[test]
fn legal_phase_and_idempotent_prepared_commit_are_core_enforced() {
    let mut stable = binding(2_000_000_000);

    let mut before_start = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    let generation = attach_bound(&mut before_start, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    assert_eq!(
        before_start
            .handle(
                frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_000,
            )
            .code,
        Some(FiniteCode::ConfigurationChanged)
    );
    assert_eq!(
        before_start
            .handle(
                frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_000,
            )
            .code,
        Some(FiniteCode::ConfigurationChanged)
    );

    let backend = MemoryBackend::default();
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"single-use-new-token".to_vec())),
        ..FakeTransport::default()
    };
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    assert_eq!(
        broker
            .handle(
                frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_000,
            )
            .state,
        AuthState::AwaitingUser
    );
    assert_eq!(
        broker
            .handle(
                frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_001,
            )
            .state,
        AuthState::Prepared
    );
    let committed_reply = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_002,
    );
    assert_eq!(committed_reply.state, AuthState::Committing);
    let receipt = private_receipt(&committed_reply);
    let committed = backend.accounts(gamja_core_host::credential::PROVIDER_SERVICE);
    assert_eq!(committed.len(), 1);
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, &committed[0]),
        Some(b"single-use-new-token".to_vec())
    );
    let replay = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_003,
    );
    assert_eq!(replay.state, AuthState::Committing);
    assert_eq!(private_receipt(&replay), receipt);

    let stale_backend = MemoryBackend::default();
    let stale_transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"stale-prepared-token".to_vec())),
        ..FakeTransport::default()
    };
    let mut stale_broker = AuthBroker::new(stale_backend.clone(), stale_transport);
    let stale_generation = attach_bound(&mut stale_broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    stale_broker.handle(
        frame(
            "auth_start",
            &stable,
            stale_generation.nonce_hex(),
            stale_generation.fingerprint(),
        ),
        1_900_000_000,
    );
    stale_broker.handle(
        frame(
            "auth_status",
            &stable,
            stale_generation.nonce_hex(),
            stale_generation.fingerprint(),
        ),
        1_900_000_001,
    );
    let mut changed = stable.clone();
    changed.config_revision += 1;
    let stale = stale_broker.handle(
        frame(
            "credential_commit",
            &changed,
            stale_generation.nonce_hex(),
            stale_generation.fingerprint(),
        ),
        1_900_000_002,
    );
    assert_eq!(stale.code, Some(FiniteCode::ConfigurationChanged));
    assert!(stale_backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_none());
}

#[test]
fn recovery_stage_flows_through_auth_to_idempotent_private_commit_receipt() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"old-recovery-secret",
    );
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"replacement-secret".to_vec())),
        ..FakeTransport::default()
    };
    let mut stable = binding(2_000_000_000);
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    assert_eq!(
        broker
            .handle(
                frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_001,
            )
            .state,
        AuthState::AwaitingUser
    );
    assert_eq!(
        broker
            .handle(
                frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_002,
            )
            .state,
        AuthState::Prepared
    );
    let committed = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_003,
    );
    assert_eq!(committed.state, AuthState::Committing);
    let first_receipt = private_receipt(&committed);
    let roundtrip: AuthReply = serde_json::from_value(serde_json::to_value(&committed).unwrap())
        .unwrap();
    assert_eq!(roundtrip.prepared_ref(), Some(first_receipt.0.as_str()));
    assert_eq!(roundtrip.commit_handle(), Some(first_receipt.1.as_str()));
    let public = serde_json::to_value(committed.public_state()).unwrap();
    assert!(public.get("prepared_ref").is_none());
    assert!(public.get("commit_handle").is_none());
    assert_eq!(first_receipt.0.split(':').count(), 3);
    assert_eq!(first_receipt.1.len(), 64);
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, &first_receipt.0),
        Some(b"replacement-secret".to_vec())
    );
    assert!(backend
        .value(
            gamja_core_host::credential::RECOVERY_SERVICE,
            &format!("recovery:{OPERATION}")
        )
        .is_some());
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_some());

    let replay = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_004,
    );
    assert_eq!(replay.state, AuthState::Committing);
    assert_eq!(private_receipt(&replay), first_receipt);

    let mut changed = stable.clone();
    changed.auth_grant_revision += 1;
    let conflict = broker.handle(
        frame("auth_status", &changed, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_005,
    );
    assert_eq!(conflict.state, AuthState::Recovering);
    assert!(backend
        .value(
            gamja_core_host::credential::RECOVERY_SERVICE,
            &format!("recovery:{OPERATION}")
        )
        .is_some());
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_some());
    assert_eq!(
        backend.value(
            gamja_core_host::credential::PROVIDER_SERVICE,
            &first_receipt.0,
        ),
        Some(b"replacement-secret".to_vec())
    );
}

#[test]
fn no_old_prepare_and_refresh_never_leave_a_false_prepared_state() {
    let mut stable = binding(2_000_000_000);
    let mut empty = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    let generation = attach_bound(&mut empty, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let no_old = empty.handle(
        frame("credential_prepare", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_000,
    );
    assert_eq!(no_old.state, AuthState::Failed);
    assert_eq!(no_old.code, Some(FiniteCode::ConfigurationChanged));

    let backend = MemoryBackend::default();
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"refresh-unavailable-secret".to_vec())),
        ..FakeTransport::default()
    };
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    broker.handle(
        frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_001,
    );
    assert_eq!(
        broker
            .handle(
                frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_002,
            )
            .state,
        AuthState::Prepared
    );
    let refresh = broker.handle(
        frame("credential_refresh", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_003,
    );
    assert_eq!(refresh.state, AuthState::Failed);
    assert_eq!(refresh.code, Some(FiniteCode::AuthMethodUnavailable));
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_none());
}

#[test]
fn binding_conflict_cannot_terminalize_while_recovery_material_remains() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"binding-conflict-old-secret",
    );
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    let mut changed = stable.clone();
    changed.config_revision += 1;
    let conflict = broker.handle(
        frame("auth_start", &changed, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_001,
    );
    assert_eq!(conflict.state, AuthState::Recovering);
    assert!(backend
        .value(
            gamja_core_host::credential::RECOVERY_SERVICE,
            &format!("recovery:{OPERATION}")
        )
        .is_some());
}

#[test]
fn recovery_staged_transport_failure_and_cancel_remain_pending() {
    for action in ["unavailable", "cancel"] {
        let backend = MemoryBackend::default();
        let mut stable = binding(2_000_000_000);
        let old_ref = "7:9:0123456789abcdef0123456789abcdef";
        backend.insert(
            gamja_core_host::credential::PROVIDER_SERVICE,
            old_ref,
            b"pending-old-secret",
        );
        if action == "unavailable" {
            let mut broker = AuthBroker::new(backend.clone(), UnavailableAuthTransport);
            let generation = attach_bound(&mut broker, &mut Vec::new());
            stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
            let prepare: AuthFrame = serde_json::from_value(json!({
                "type":"credential_prepare","binding":stable,
                "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
                "exact_old_reference":old_ref
            }))
            .unwrap();
            broker.handle(prepare, 1_900_000_000);
            let failed = broker.handle(
                frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_001,
            );
            assert_eq!(failed.state, AuthState::Recovering);
            assert_eq!(failed.code, Some(FiniteCode::AuthMethodUnavailable));
        } else {
            let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
            let generation = attach_bound(&mut broker, &mut Vec::new());
            stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
            let prepare: AuthFrame = serde_json::from_value(json!({
                "type":"credential_prepare","binding":stable,
                "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
                "exact_old_reference":old_ref
            }))
            .unwrap();
            broker.handle(prepare, 1_900_000_000);
            broker.handle(
                frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_001,
            );
            let cancelled = broker.handle(
                frame("auth_cancel", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_002,
            );
            assert_eq!(cancelled.state, AuthState::Recovering);
            assert_eq!(cancelled.code, Some(FiniteCode::CredentialRecoveryRequired));
        }
        assert!(backend
            .value(
                gamja_core_host::credential::RECOVERY_SERVICE,
                &format!("recovery:{OPERATION}")
            )
            .is_some());
    }
}

#[test]
fn private_commit_receipt_survives_crash_and_separates_cas_outcomes() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"crash-after-destination-write".to_vec())),
        ..FakeTransport::default()
    };
    let mut first = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut first, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    first.handle(
        frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_000,
    );
    first.handle(
        frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_001,
    );
    let commit = first.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_002,
    );
    let receipt = private_receipt(&commit);
    drop(first);

    let mut replacement = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut replacement, &mut Vec::new());
    let inspect = replacement
        .handle_recovery_frame(
            recovery_frame("inspect", &stable, &generation, None, None, None),
            1_900_000_003,
        )
        .unwrap();
    assert_eq!(inspect.state, RecoveryInspectState::Recovering);
    assert_eq!(inspect.material_state, RecoveryMaterialState::Prepared);
    let fingerprint = inspect.binding_fingerprint.unwrap();

    let rejected = replacement
        .handle_recovery_frame(
            recovery_frame(
                "cleanup_committed",
                &stable,
                &generation,
                Some(&fingerprint),
                Some(&"00".repeat(32)),
                Some(&receipt.0),
            ),
            1_900_000_004,
        )
        .unwrap();
    assert_eq!(rejected.state, RecoveryInspectState::Failed);
    assert_eq!(rejected.code, Some(FiniteCode::ConfigurationChanged));

    backend.ignore_delete(&format!("prepared:{OPERATION}"));
    let pending = replacement
        .handle_recovery_frame(
            recovery_frame(
                "cleanup_committed",
                &stable,
                &generation,
                Some(&fingerprint),
                None,
                Some(&receipt.0),
            ),
            1_900_000_005,
        )
        .unwrap();
    assert_eq!(pending.state, RecoveryInspectState::Failed);
    assert_eq!(pending.code, Some(FiniteCode::CredentialCleanupUnconfirmed));
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, &receipt.0),
        Some(b"crash-after-destination-write".to_vec())
    );
    backend.allow_delete();
    let cleaned = replacement
        .handle_recovery_frame(
            recovery_frame(
                "cleanup_committed",
                &stable,
                &generation,
                Some(&fingerprint),
                None,
                Some(&receipt.0),
            ),
            1_900_000_006,
        )
        .unwrap();
    assert_eq!(cleaned.state, RecoveryInspectState::Absent);
    assert_eq!(cleaned.material_state, RecoveryMaterialState::None);
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, &receipt.0),
        Some(b"crash-after-destination-write".to_vec())
    );
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_none());
}

#[test]
fn pre_cas_discard_removes_destination_before_envelope() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"unadopted-destination-secret".to_vec())),
        ..FakeTransport::default()
    };
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    broker.handle(
        frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_000,
    );
    broker.handle(
        frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_001,
    );
    let commit = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_002,
    );
    let receipt = private_receipt(&commit);
    backend.ignore_delete(&receipt.0);
    let discard: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard","target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"discard_prepared",
        "commit_handle":receipt.1.clone()
    }))
    .unwrap();
    let destination_pending = broker.handle(discard, 1_900_000_003);
    assert_eq!(destination_pending.state, AuthState::Recovering);
    assert_eq!(
        destination_pending.code,
        Some(FiniteCode::CredentialCleanupUnconfirmed)
    );
    assert!(backend
        .value(gamja_core_host::credential::PROVIDER_SERVICE, &receipt.0)
        .is_some());
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_some());
    backend.allow_delete();
    backend.ignore_delete(&format!("prepared:{OPERATION}"));
    let discard: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard","target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"discard_prepared",
        "commit_handle":receipt.1.clone()
    }))
    .unwrap();
    let pending = broker.handle(discard, 1_900_000_004);
    assert_eq!(pending.state, AuthState::Recovering);
    assert_eq!(pending.code, Some(FiniteCode::CredentialCleanupUnconfirmed));
    assert!(backend
        .value(gamja_core_host::credential::PROVIDER_SERVICE, &receipt.0)
        .is_none());
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_some());
    backend.allow_delete();
    let discard: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard","target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"discard_prepared",
        "commit_handle":receipt.1.clone()
    }))
    .unwrap();
    assert_eq!(broker.handle(discard, 1_900_000_005).state, AuthState::Succeeded);
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_none());
}

#[test]
fn destination_write_without_commit_reply_is_recovered_from_durable_mapping() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"write-before-reply-secret".to_vec())),
        ..FakeTransport::default()
    };
    let mut first = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut first, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    first.handle(
        frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_000,
    );
    first.handle(
        frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_001,
    );
    backend.fail_read_after_next_provider_put();
    let lost_reply = first.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_002,
    );
    assert_eq!(lost_reply.state, AuthState::Recovering);
    assert_eq!(lost_reply.code, Some(FiniteCode::CredentialStoreUnavailable));
    assert!(lost_reply.prepared_ref().is_none());
    drop(first);

    let mut replacement = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut replacement, &mut Vec::new());
    let recovered = replacement
        .handle_recovery_frame(
            recovery_frame("inspect", &stable, &generation, None, None, None),
            1_900_000_003,
        )
        .unwrap();
    assert_eq!(recovered.state, RecoveryInspectState::Recovering);
    assert_eq!(recovered.material_state, RecoveryMaterialState::Prepared);
    assert_eq!(recovered.receipt_state, RecoveryReceiptState::None);
    let destinations = backend.accounts(gamja_core_host::credential::PROVIDER_SERVICE);
    assert_eq!(destinations.len(), 1);
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, &destinations[0]),
        Some(b"write-before-reply-secret".to_vec())
    );
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_some());
}

#[test]
fn changed_binding_epoch_and_expiry_fail_closed_without_transport_reopen() {
    let mut broker = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());

    let expired = broker.handle(
        frame("auth_start", &binding(100), generation.nonce_hex(), generation.fingerprint()),
        101,
    );
    assert_eq!(expired.state, AuthState::Expired);

    let mut changed = binding(2_000_000_000);
    changed.owner_epoch = "00".repeat(32);
    let mismatch = broker.handle(
        frame("auth_start", &changed, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_000,
    );
    assert_eq!(mismatch.code, Some(FiniteCode::OwnerMismatch));
}

#[test]
fn staging_failure_leaves_old_credential_untouched_and_exposes_no_secret() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"sentinel-old-secret",
    );
    backend.fail_put(&format!("recovery:{OPERATION}"));
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":binding(2_000_000_000),
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    })).unwrap();

    let reply = broker.handle(prepare, 1_900_000_000);
    assert_eq!(reply.code, Some(FiniteCode::CredentialStoreUnavailable));
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, old_ref),
        Some(b"sentinel-old-secret".to_vec())
    );
    let serialized = serde_json::to_string(&reply).unwrap();
    assert!(!serialized.contains("sentinel"));
    assert!(!serialized.contains(old_ref));
}

#[test]
fn staging_requires_exact_readback_and_missing_recovery_never_claims_restored() {
    let mut stable = binding(2_000_000_000);
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    for corrupt in [false, true] {
        let backend = MemoryBackend::default();
        backend.insert(
            gamja_core_host::credential::PROVIDER_SERVICE,
            old_ref,
            b"sentinel-old-secret",
        );
        let recovery = format!("recovery:{OPERATION}");
        if corrupt {
            backend.corrupt_put(&recovery);
        } else {
            backend.drop_put(&recovery);
        }
        let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
        let generation = attach_bound(&mut broker, &mut Vec::new());
        stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
        let prepare: AuthFrame = serde_json::from_value(json!({
            "type":"credential_prepare",
            "binding":stable,
            "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
            "exact_old_reference":old_ref
        }))
        .unwrap();
        let reply = broker.handle(prepare, 1_900_000_000);
        assert_ne!(reply.state, AuthState::Prepared, "corrupt={corrupt}");
        assert_eq!(reply.code, Some(FiniteCode::CredentialRecoveryRequired));
        assert_eq!(
            backend.value(gamja_core_host::credential::PROVIDER_SERVICE, old_ref),
            Some(b"sentinel-old-secret".to_vec())
        );
    }

    let backend = MemoryBackend::default();
    let mut broker = AuthBroker::new(backend, FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let restore: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard",
        "target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"restore_old"
    }))
    .unwrap();
    let reply = broker.handle(restore, 2_100_000_000);
    assert_eq!(reply.state, AuthState::Recovering);
    assert_eq!(reply.code, Some(FiniteCode::CredentialRecoveryRequired));
}

#[test]
fn prepared_staging_requires_exact_readback() {
    for corrupt in [false, true] {
        let backend = MemoryBackend::default();
        let account = format!("prepared:{OPERATION}");
        if corrupt {
            backend.corrupt_put(&account);
        } else {
            backend.drop_put(&account);
        }
        let transport = FakeTransport {
            prepared: Some(Zeroizing::new(b"prepared-readback-secret".to_vec())),
            ..FakeTransport::default()
        };
        let mut stable = binding(2_000_000_000);
        let mut broker = AuthBroker::new(backend, transport);
        let generation = attach_bound(&mut broker, &mut Vec::new());
        stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
        broker.handle(
            frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
            1_900_000_000,
        );
        let reply = broker.handle(
            frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
            1_900_000_001,
        );
        assert_eq!(reply.state, AuthState::Recovering, "corrupt={corrupt}");
        assert_eq!(reply.code, Some(FiniteCode::CredentialRecoveryRequired));
    }
}

#[test]
fn restore_write_mismatch_stays_pending_with_recovery_envelope() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"sentinel-old-secret",
    );
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    backend.remove(gamja_core_host::credential::PROVIDER_SERVICE, old_ref);
    backend.drop_put(old_ref);
    let restore: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard",
        "target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"restore_old"
    }))
    .unwrap();
    let reply = broker.handle(restore, 2_100_000_000);
    assert_eq!(reply.state, AuthState::Recovering);
    assert_eq!(reply.code, Some(FiniteCode::CredentialRecoveryRequired));
    assert!(backend
        .value(
            gamja_core_host::credential::RECOVERY_SERVICE,
            &format!("recovery:{OPERATION}")
        )
        .is_some());
}

#[test]
fn recovery_survives_broker_restart_and_cleanup_is_idempotent() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"sentinel-old-secret",
    );
    let mut stable = binding(2_000_000_000);

    let mut first = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut first, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    })).unwrap();
    assert_eq!(first.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    drop(first);

    assert!(backend
        .value(
            gamja_core_host::credential::RECOVERY_SERVICE,
            &format!("recovery:{OPERATION}")
        )
        .is_some());
    let mut replacement = AuthBroker::new(backend.clone(), FakeTransport::default());
    let replacement_generation = attach_bound(&mut replacement, &mut Vec::new());
    let inspect = replacement
        .handle_recovery_frame(
            recovery_frame(
                "inspect",
                &stable,
                &replacement_generation,
                None,
                None,
                None,
            ),
            2_100_000_000,
        )
        .unwrap();
    assert_eq!(inspect.state, RecoveryInspectState::Recovering);
    let fingerprint = inspect.binding_fingerprint.unwrap();
    let restored = replacement
        .handle_recovery_frame(
            recovery_frame(
                "restore_old",
                &stable,
                &replacement_generation,
                Some(&fingerprint),
                None,
                None,
            ),
            2_100_000_001,
        )
        .unwrap();
    assert_eq!(restored.state, RecoveryInspectState::Restored);
    let replayed = replacement
        .handle_recovery_frame(
            recovery_frame(
                "inspect",
                &stable,
                &replacement_generation,
                None,
                None,
                None,
            ),
            2_100_000_002,
        )
        .unwrap();
    assert_eq!(replayed.state, RecoveryInspectState::Restored);
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, old_ref),
        Some(b"sentinel-old-secret".to_vec())
    );
}

#[test]
fn legacy_refresh_has_no_invented_same_grant_identity() {
    let mut broker = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    let reply = broker.handle(
        frame(
            "credential_refresh",
            &binding(2_000_000_000),
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_000,
    );
    assert_eq!(reply.code, Some(FiniteCode::AuthMethodUnavailable));
}

#[test]
fn private_framed_channel_handshakes_once_and_returns_only_safe_state() {
    let (mut client, server) = UnixStream::pair().unwrap();
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"private-channel-secret".to_vec())),
        ..FakeTransport::default()
    };
    let broker = AuthBroker::new(MemoryBackend::default(), transport);
    let worker = std::thread::spawn(move || serve_auth_channel(server, broker));

    let handshake = read_frame(&mut client);
    assert_eq!(handshake["version"], 1);
    let nonce = handshake["nonce"].as_str().unwrap();
    let generation = handshake["current_generation_fingerprint"]
        .as_str()
        .unwrap();
    write_frame(
        &mut client,
        &json!({"type":"owner_bind","version":1,"owner_id":7}),
    );
    let bound = read_frame(&mut client);
    assert_eq!(bound["state"], "bound");
    let mut stable = binding(2_000_000_000);
    stable.owner_epoch = bound["current_owner_epoch"].as_str().unwrap().to_owned();
    write_frame(
        &mut client,
        &json!({
            "type":"auth_start",
            "binding":stable,
            "generation":{"nonce":nonce,"current_generation_fingerprint":generation}
        }),
    );
    let reply = read_frame(&mut client);
    assert_eq!(reply["state"], "awaiting_user");
    let serialized = reply.to_string();
    for forbidden in ["nonce", "credential", "token", "verifier", "secret_ref"] {
        assert!(!serialized.contains(forbidden), "{forbidden}: {serialized}");
    }

    write_frame(
        &mut client,
        &json!({
            "type":"auth_status",
            "binding":stable,
            "generation":{"nonce":nonce,"current_generation_fingerprint":generation}
        }),
    );
    assert_eq!(read_frame(&mut client)["state"], "prepared");
    write_frame(
        &mut client,
        &json!({
            "type":"credential_commit",
            "binding":stable,
            "generation":{"nonce":nonce,"current_generation_fingerprint":generation}
        }),
    );
    let commit = read_frame(&mut client);
    assert_eq!(commit["state"], "committing");
    assert_eq!(commit["commit_handle"].as_str().unwrap().len(), 64);
    assert!(commit["prepared_ref"].as_str().unwrap().starts_with("7:9:"));
    assert!(!commit.to_string().contains("private-channel-secret"));

    drop(client);
    assert!(worker.join().unwrap().is_ok());
}

#[test]
fn operation_allocate_channel_is_closed_and_owner_bound_without_private_values() {
    let (mut client, server) = UnixStream::pair().unwrap();
    let broker = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    let worker = std::thread::spawn(move || serve_auth_channel(server, broker));
    let _handshake = read_frame(&mut client);

    write_frame(&mut client, &json!({
        "type":"operation_allocate","version":1,"owner_id":7,
        "current_owner_epoch":EPOCH
    }));
    let unbound = read_frame(&mut client);
    assert_eq!(unbound, json!({
        "version":1,"state":"failed","code":"owner_epoch_unavailable"
    }));

    write_frame(&mut client, &json!({"type":"owner_bind","version":1,"owner_id":7}));
    let epoch = read_frame(&mut client)["current_owner_epoch"]
        .as_str().unwrap().to_owned();
    write_frame(&mut client, &json!({
        "type":"operation_allocate","version":1,"owner_id":7,
        "current_owner_epoch":EPOCH
    }));
    assert_eq!(read_frame(&mut client), json!({
        "version":1,"state":"failed","code":"owner_epoch_changed"
    }));

    write_frame(&mut client, &json!({
        "type":"operation_allocate","version":1,"owner_id":7,
        "current_owner_epoch":epoch
    }));
    let allocated = read_frame(&mut client);
    assert_eq!(allocated["version"], 1);
    assert_eq!(allocated["state"], "allocated");
    assert_eq!(allocated["operation_id"].as_str().unwrap().len(), 64);
    let serialized = allocated.to_string();
    for forbidden in ["nonce", "owner_epoch", "credential", "token", "secret"] {
        assert!(!serialized.contains(forbidden), "{forbidden}: {serialized}");
    }

    write_frame(&mut client, &json!({
        "type":"operation_allocate","version":1,"owner_id":7,
        "current_owner_epoch":epoch,"unknown":true
    }));
    let closed = client.read(&mut [0u8; 1]);
    assert!(matches!(closed, Ok(0) | Err(_)));
    drop(client);
    assert!(worker.join().unwrap().is_err());
}

#[test]
fn prepared_secret_is_private_and_repeated_discard_leaves_no_durable_copy() {
    let backend = MemoryBackend::default();
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"sentinel-new-token".to_vec())),
        ..FakeTransport::default()
    };
    let mut stable = binding(2_000_000_000);
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    assert_eq!(
        broker
            .handle(
                frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_000,
            )
            .state,
        AuthState::AwaitingUser
    );
    let reply = broker.handle(
        frame(
            "auth_status",
            &stable,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_001,
    );
    assert_eq!(reply.state, AuthState::Prepared);
    assert!(!serde_json::to_string(&reply)
        .unwrap()
        .contains("sentinel-new-token"));
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_some());

    for _ in 0..2 {
        let discard: AuthFrame = serde_json::from_value(json!({
            "type":"credential_discard",
            "target":{"exact_binding":stable},
            "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
            "action":"discard_prepared"
        }))
        .unwrap();
        assert_eq!(broker.handle(discard, 2_100_000_000).state, AuthState::Succeeded);
    }
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_none());
}

#[test]
fn cancel_expiry_and_shutdown_confirm_prepared_absence_or_stay_recovering() {
    for action in ["cancel", "expiry", "shutdown"] {
        let backend = MemoryBackend::default();
        let mut stable = binding(if action == "expiry" { 100 } else { 2_000_000_000 });
        let transport = FakeTransport {
            prepared: Some(Zeroizing::new(format!("prepared-{action}").into_bytes())),
            ..FakeTransport::default()
        };
        let mut broker = AuthBroker::new(backend.clone(), transport);
        let generation = attach_bound(&mut broker, &mut Vec::new());
        stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
        broker.handle(
            frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
            50,
        );
        assert_eq!(
            broker
                .handle(
                    frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
                    51,
                )
                .state,
            AuthState::Prepared
        );
        let reply = match action {
            "cancel" => broker.handle(
                frame("auth_cancel", &stable, generation.nonce_hex(), generation.fingerprint()),
                52,
            ),
            "expiry" => broker.handle(
                frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
                101,
            ),
            "shutdown" => broker.shutdown().remove(0),
            _ => unreachable!(),
        };
        assert!(matches!(
            reply.state,
            AuthState::Cancelled | AuthState::Expired | AuthState::Interrupted
        ));
        assert!(backend
            .value(
                gamja_core_host::credential::PREPARED_SERVICE,
                &format!("prepared:{OPERATION}")
            )
            .is_none());
    }

    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"prepared-cleanup-failure".to_vec())),
        ..FakeTransport::default()
    };
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    broker.handle(
        frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
        50,
    );
    broker.handle(
        frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
        51,
    );
    backend.ignore_delete(&format!("prepared:{OPERATION}"));
    let pending = broker.handle(
        frame("auth_cancel", &stable, generation.nonce_hex(), generation.fingerprint()),
        52,
    );
    assert_eq!(pending.state, AuthState::Recovering);
    assert_eq!(pending.code, Some(FiniteCode::CredentialCleanupUnconfirmed));
}

#[test]
fn cleanup_committed_retains_recovery_until_old_absence_is_confirmed() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"sentinel-old-secret",
    );
    let mut stable = binding(2_000_000_000);
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"cleanup-winner-secret".to_vec())),
        ..FakeTransport::default()
    };
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    assert_eq!(
        broker
            .handle(
                frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_001,
            )
            .state,
        AuthState::AwaitingUser
    );
    assert_eq!(
        broker
            .handle(
                frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_002,
            )
            .state,
        AuthState::Prepared
    );
    let commit = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_003,
    );
    let receipt = private_receipt(&commit);

    backend.ignore_delete(old_ref);
    let cleanup: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard",
        "target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"cleanup_committed",
        "commit_handle":receipt.1.clone(),
        "adopted_prepared_ref":receipt.0.clone()
    }))
    .unwrap();
    let reply = broker.handle(cleanup, 2_100_000_000);
    assert_eq!(reply.state, AuthState::Recovering);
    assert_eq!(reply.code, Some(FiniteCode::CredentialCleanupUnconfirmed));
    assert!(backend
        .value(
            gamja_core_host::credential::RECOVERY_SERVICE,
            &format!("recovery:{OPERATION}")
        )
        .is_some());
}

#[test]
fn same_grant_refresh_kind_cleans_old_material_with_its_exact_selector() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"same-refresh-old-secret",
    );
    let mut stable = binding(2_000_000_000);
    stable.kind = "same_grant_refresh".into();
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"same-refresh-winner-secret".to_vec())),
        ..FakeTransport::default()
    };
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    assert_eq!(
        broker
            .handle(
                frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_001,
            )
            .state,
        AuthState::AwaitingUser
    );
    assert_eq!(
        broker
            .handle(
                frame("auth_status", &stable, generation.nonce_hex(), generation.fingerprint()),
                1_900_000_002,
            )
            .state,
        AuthState::Prepared
    );
    let commit = broker.handle(
        frame("credential_commit", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_003,
    );
    let receipt = private_receipt(&commit);
    let cleanup: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard",
        "target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"cleanup_committed",
        "commit_handle":receipt.1,
        "adopted_prepared_ref":receipt.0
    }))
    .unwrap();
    assert_eq!(broker.handle(cleanup, 1_900_000_004).state, AuthState::Succeeded);
    let inspected = broker
        .handle_recovery_frame(
            recovery_frame("inspect", &stable, &generation, None, None, None),
            1_900_000_005,
        )
        .unwrap();
    assert_eq!(inspected.state, RecoveryInspectState::Cleaned);
    assert_eq!(inspected.material_state, RecoveryMaterialState::None);
    assert_eq!(inspected.receipt_state, RecoveryReceiptState::Present);
}

#[test]
fn cleanup_revalidates_private_reference_and_adoption_full_binding() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    stable.kind = "disconnect".into();
    let old_ref = "7:9:0123456789abcdef0123456789abcdef";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"sentinel-old-secret",
    );
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare","binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    let account = format!("recovery:{OPERATION}");
    let mut corrupted = backend
        .value(gamja_core_host::credential::RECOVERY_SERVICE, &account)
        .unwrap();
    let marker = b"7:9:0123456789abcdef0123456789abcdef";
    let offset = corrupted.windows(marker.len()).position(|part| part == marker).unwrap();
    corrupted[offset] = b'8';
    backend.insert(
        gamja_core_host::credential::RECOVERY_SERVICE,
        &account,
        &corrupted,
    );
    let cleanup: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard","target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"cleanup_committed"
    }))
    .unwrap();
    let reply = broker.handle(cleanup, 2_100_000_000);
    assert_eq!(reply.state, AuthState::Recovering);
    assert_eq!(reply.code, Some(FiniteCode::CredentialRecoveryRequired));
    assert_eq!(
        backend.value(gamja_core_host::credential::PROVIDER_SERVICE, old_ref),
        Some(b"sentinel-old-secret".to_vec())
    );
}

#[test]
fn crash_retains_prepared_material_and_restart_allows_cleanup_only() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let transport = FakeTransport {
        prepared: Some(Zeroizing::new(b"crash-prepared-secret".to_vec())),
        ..FakeTransport::default()
    };
    let mut first = AuthBroker::new(backend.clone(), transport);
    let first_generation = attach_bound(&mut first, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    first.handle(
        frame("auth_start", &stable, first_generation.nonce_hex(), first_generation.fingerprint()),
        1_900_000_000,
    );
    assert_eq!(
        first
            .handle(
                frame("auth_status", &stable, first_generation.nonce_hex(), first_generation.fingerprint()),
                1_900_000_001,
            )
            .state,
        AuthState::Prepared
    );
    drop(first);
    assert!(backend
        .value(
            gamja_core_host::credential::PREPARED_SERVICE,
            &format!("prepared:{OPERATION}")
        )
        .is_some());

    let mut replacement = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut replacement, &mut Vec::new());
    let reopen = replacement.handle(
        frame("auth_start", &stable, generation.nonce_hex(), generation.fingerprint()),
        1_900_000_002,
    );
    assert_eq!(reopen.code, Some(FiniteCode::OwnerMismatch));
    let inspect = replacement
        .handle_recovery_frame(
            recovery_frame("inspect", &stable, &generation, None, None, None),
            1_900_000_003,
        )
        .unwrap();
    let fingerprint = inspect.binding_fingerprint.unwrap();
    let discarded = replacement
        .handle_recovery_frame(
            recovery_frame(
                "discard_prepared",
                &stable,
                &generation,
                Some(&fingerprint),
                None,
                None,
            ),
            1_900_000_004,
        )
        .unwrap();
    assert_eq!(discarded.state, RecoveryInspectState::Absent);
}

#[test]
fn synthetic_browser_callback_is_exact_one_shot_and_never_serializes_code_or_verifier() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let mut transport = SyntheticFixtureTransport::default();
    transport.register_browser(
        &stable,
        BrowserFixture {
            authorization_url: "https://synthetic.invalid/authorize".into(),
            callback_path: "/callback/opaque".into(),
            callback_port: 48123,
            state: "state-value".into(),
            pkce_verifier: Zeroizing::new("pkce-verifier".into()),
            issuer: "https://issuer.synthetic.invalid".into(),
            audience: "synthetic-client".into(),
            prepared_secret: Zeroizing::new(b"sentinel-browser-token".to_vec()),
        },
    )
    .unwrap();
    let mut broker = AuthBroker::new(backend.clone(), transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    assert_eq!(
        broker
            .handle(
                frame(
                    "auth_start",
                    &stable,
                    generation.nonce_hex(),
                    generation.fingerprint(),
                ),
                1_900_000_000,
            )
            .state,
        AuthState::AwaitingUser
    );

    for field in 0..6 {
        let mut mismatch = browser_callback();
        match field {
            0 => mismatch.path = "/wrong".into(),
            1 => mismatch.port = 48124,
            2 => mismatch.state = "wrong-state".into(),
            3 => mismatch.pkce_verifier = Zeroizing::new("wrong-verifier".into()),
            4 => mismatch.issuer = "https://wrong-issuer.synthetic.invalid".into(),
            5 => mismatch.audience = "wrong-client".into(),
            _ => unreachable!(),
        }
        assert_eq!(
            broker
                .transport_mut()
                .submit_browser_callback(OPERATION, mismatch),
            Err(FiniteCode::CallbackMismatch)
        );
    }

    assert_eq!(
        broker
            .transport_mut()
            .submit_browser_callback(OPERATION, browser_callback()),
        Ok(())
    );
    assert_eq!(
        broker
            .transport_mut()
            .submit_browser_callback(OPERATION, browser_callback()),
        Err(FiniteCode::CallbackMismatch)
    );
    let reply = broker.handle(
        frame(
            "auth_status",
            &stable,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_000,
    );
    assert_eq!(reply.state, AuthState::Prepared);
    let public = serde_json::to_string(&reply).unwrap();
    for forbidden in [
        "sentinel-code",
        "pkce-verifier",
        "sentinel-browser-token",
    ] {
        assert!(!public.contains(forbidden));
    }
}

fn browser_callback() -> BrowserCallback {
    BrowserCallback {
        path: "/callback/opaque".into(),
        port: 48123,
        state: "state-value".into(),
        pkce_verifier: Zeroizing::new("pkce-verifier".into()),
        issuer: "https://issuer.synthetic.invalid".into(),
        audience: "synthetic-client".into(),
        authorization_code: Zeroizing::new("sentinel-code".into()),
    }
}

#[test]
fn synthetic_device_flow_reports_bounded_slowdown_then_prepares_privately() {
    let backend = MemoryBackend::default();
    let mut stable = binding(2_000_000_000);
    let mut transport = SyntheticFixtureTransport::default();
    transport
        .register_device(
            &stable,
            DeviceFixture {
                verification_uri: "https://synthetic.invalid/device".into(),
                user_code: "ABCD-EFGH".into(),
                poll_interval: 5,
                slow_downs: 1,
                prepared_secret: Zeroizing::new(b"sentinel-device-token".to_vec()),
            },
        )
        .unwrap();
    let mut broker = AuthBroker::new(backend, transport);
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let start = broker.handle(
        frame(
            "auth_start",
            &stable,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_000,
    );
    assert_eq!(start.state, AuthState::Polling);
    assert_eq!(start.verification_uri.as_deref(), Some("https://synthetic.invalid/device"));
    assert_eq!(start.user_code.as_deref(), Some("ABCD-EFGH"));
    assert_eq!(start.poll_interval, Some(5));

    let early = broker.handle(
        frame(
            "auth_status",
            &stable,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_001,
    );
    assert_eq!(early.state, AuthState::Polling);
    assert_eq!(early.code, None);
    assert_eq!(early.poll_interval, Some(4));
    let slow = broker.handle(
        frame(
            "auth_status",
            &stable,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_005,
    );
    assert_eq!(slow.state, AuthState::Polling);
    assert_eq!(slow.code, Some(FiniteCode::DeviceSlowDown));
    assert_eq!(slow.poll_interval, Some(10));
    let still_early = broker.handle(
        frame(
            "auth_status",
            &stable,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_010,
    );
    assert_eq!(still_early.code, None);
    assert_eq!(still_early.poll_interval, Some(5));
    let ready = broker.handle(
        frame(
            "auth_status",
            &stable,
            generation.nonce_hex(),
            generation.fingerprint(),
        ),
        1_900_000_015,
    );
    assert_eq!(ready.state, AuthState::Prepared);
    assert!(!serde_json::to_string(&ready)
        .unwrap()
        .contains("sentinel-device-token"));
}

#[test]
fn r3_restore_writes_completion_receipt_before_deleting_recovery_material() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:11111111111111111111111111111111";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"r3-old-secret",
    );
    let mut stable = binding(2_000_000_000);
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    let restore: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard",
        "target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"restore_old"
    }))
    .unwrap();
    assert_eq!(broker.handle(restore, 1_900_000_001).state, AuthState::Restored);
    assert_eq!(
        backend.accounts("cloud.gamja.credential-core.v1.provider-completion"),
        vec![format!("completion:{OPERATION}")]
    );
}

#[test]
fn r3_private_channel_starts_unbound_and_rejects_auth_before_owner_bind() {
    let (mut client, server) = UnixStream::pair().unwrap();
    let broker = AuthBroker::new(MemoryBackend::default(), FakeTransport::default());
    let worker = std::thread::spawn(move || serve_auth_channel(server, broker));
    let handshake = read_frame(&mut client);
    write_frame(
        &mut client,
        &json!({
            "type":"auth_start",
            "binding":binding(2_000_000_000),
            "generation":{
                "nonce":handshake["nonce"],
                "current_generation_fingerprint":handshake["current_generation_fingerprint"]
            }
        }),
    );
    let reply = read_frame(&mut client);
    assert_eq!(reply["state"], "failed");
    assert_eq!(reply["code"], "owner_mismatch");
    drop(client);
    assert_eq!(worker.join().unwrap(), Ok(()));
}

#[test]
fn r3_replacement_selector_cross_validates_receipt_and_acknowledges_terminal() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:22222222222222222222222222222222";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"selector-old-secret",
    );
    let mut stable = binding(2_000_000_000);
    let mut origin = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut origin, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(origin.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    drop(origin);

    let (mut client, server) = UnixStream::pair().unwrap();
    let broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let worker = std::thread::spawn(move || serve_auth_channel(server, broker));
    let handshake = read_frame(&mut client);
    write_frame(
        &mut client,
        &json!({"type":"owner_bind","version":1,"owner_id":7}),
    );
    let bound = read_frame(&mut client);
    assert_eq!(bound["state"], "bound");
    let selector = json!({
        "operation_id":OPERATION,
        "owner_id":7,
        "connection_id":9,
        "kind":"grant_replace",
        "intent_fingerprint":INTENT,
        "before_state_fingerprint":BEFORE,
        "revoke_requested":false
    });
    let generation = json!({
        "nonce":handshake["nonce"],
        "current_generation_fingerprint":handshake["current_generation_fingerprint"]
    });
    write_frame(
        &mut client,
        &json!({
            "type":"credential_discard",
            "target":{"recovery_selector":selector},
            "generation":generation,
            "action":"inspect"
        }),
    );
    let inspect = read_frame(&mut client);
    assert_eq!(inspect["state"], "recovering");
    assert_eq!(inspect["material_state"], "recovery");
    assert_eq!(inspect["receipt_state"], "none");
    assert_eq!(inspect["binding_summary"]["provider_id"], "synthetic");
    assert_eq!(inspect["binding_summary"]["config_revision"], 3);
    let fingerprint = inspect["binding_fingerprint"].as_str().unwrap().to_owned();
    assert_eq!(fingerprint.len(), 64);

    let mut wrong_selector = selector.clone();
    wrong_selector["before_state_fingerprint"] = json!("0".repeat(64));
    write_frame(
        &mut client,
        &json!({
            "type":"credential_discard",
            "target":{"recovery_selector":wrong_selector},
            "generation":generation,
            "action":"inspect"
        }),
    );
    assert_eq!(read_frame(&mut client)["state"], "failed");
    assert!(backend
        .value(
            gamja_core_host::credential::RECOVERY_SERVICE,
            &format!("recovery:{OPERATION}")
        )
        .is_some());

    write_frame(
        &mut client,
        &json!({
            "type":"credential_discard",
            "target":{"recovery_selector":selector,"binding_fingerprint":fingerprint},
            "generation":generation,
            "action":"restore_old"
        }),
    );
    let restored = read_frame(&mut client);
    assert_eq!(restored["state"], "restored");
    assert_eq!(restored["material_state"], "none");
    assert_eq!(restored["receipt_state"], "present");
    assert!(!restored.to_string().contains(old_ref));
    assert!(!restored.to_string().contains("selector-old-secret"));

    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"tampered-after-receipt",
    );
    write_frame(
        &mut client,
        &json!({
            "type":"credential_discard",
            "target":{"recovery_selector":selector},
            "generation":generation,
            "action":"inspect"
        }),
    );
    let tampered = read_frame(&mut client);
    assert_eq!(tampered["state"], "failed");
    assert_eq!(tampered["code"], "credential_recovery_required");
    assert!(backend
        .value(
            gamja_core_host::credential::COMPLETION_SERVICE,
            &format!("completion:{OPERATION}")
        )
        .is_some());

    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"selector-old-secret",
    );
    write_frame(
        &mut client,
        &json!({
            "type":"credential_discard",
            "target":{"recovery_selector":selector,"binding_fingerprint":fingerprint},
            "generation":generation,
            "action":"acknowledge_terminal"
        }),
    );
    let acknowledged = read_frame(&mut client);
    assert_eq!(acknowledged["state"], "absent");
    assert_eq!(acknowledged["receipt_state"], "none");
    drop(client);
    assert_eq!(worker.join().unwrap(), Ok(()));
}

#[test]
fn r3_receipt_readback_failure_preserves_recovery_material() {
    let backend = MemoryBackend::default();
    let old_ref = "7:9:33333333333333333333333333333333";
    backend.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"receipt-order-secret",
    );
    let mut stable = binding(2_000_000_000);
    let mut broker = AuthBroker::new(backend.clone(), FakeTransport::default());
    let generation = attach_bound(&mut broker, &mut Vec::new());
    stable.owner_epoch = CURRENT_OWNER_EPOCH.with(|epoch| epoch.borrow().clone());
    let prepare: AuthFrame = serde_json::from_value(json!({
        "type":"credential_prepare",
        "binding":stable,
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "exact_old_reference":old_ref
    }))
    .unwrap();
    assert_eq!(broker.handle(prepare, 1_900_000_000).state, AuthState::Prepared);
    backend.drop_put(&format!("completion:{OPERATION}"));
    let restore: AuthFrame = serde_json::from_value(json!({
        "type":"credential_discard",
        "target":{"exact_binding":stable},
        "generation":{"nonce":generation.nonce_hex(),"current_generation_fingerprint":generation.fingerprint()},
        "action":"restore_old"
    }))
    .unwrap();
    let failed = broker.handle(restore, 1_900_000_001);
    assert_eq!(failed.state, AuthState::Recovering);
    assert_eq!(failed.code, Some(FiniteCode::CredentialStoreUnavailable));
    assert!(backend
        .value(
            gamja_core_host::credential::RECOVERY_SERVICE,
            &format!("recovery:{OPERATION}")
        )
        .is_some());
}

#[test]
fn r3_protocol_error_closes_channel_and_interrupts_active_transport() {
    let (mut client, server) = UnixStream::pair().unwrap();
    let cancels = Arc::new(AtomicUsize::new(0));
    let broker = AuthBroker::new(
        MemoryBackend::default(),
        FakeTransport {
            cancel_count: cancels.clone(),
            ..FakeTransport::default()
        },
    );
    let worker = std::thread::spawn(move || serve_auth_channel(server, broker));
    let handshake = read_frame(&mut client);
    write_frame(
        &mut client,
        &json!({"type":"owner_bind","version":1,"owner_id":7}),
    );
    let bound = read_frame(&mut client);
    let mut stable = binding(2_000_000_000);
    stable.owner_epoch = bound["current_owner_epoch"].as_str().unwrap().to_owned();
    write_frame(
        &mut client,
        &json!({
            "type":"auth_start",
            "binding":stable,
            "generation":{
                "nonce":handshake["nonce"],
                "current_generation_fingerprint":handshake["current_generation_fingerprint"]
            }
        }),
    );
    assert_eq!(read_frame(&mut client)["state"], "awaiting_user");
    let malformed = br#"{"type":"owner_bind","version":1,"owner_id":7,"unknown":true}"#;
    client
        .write_all(&(malformed.len() as u32).to_be_bytes())
        .unwrap();
    client.write_all(malformed).unwrap();
    drop(client);
    assert_eq!(worker.join().unwrap(), Err(()));
    assert_eq!(cancels.load(Ordering::SeqCst), 1);
}

#[test]
fn r3_python_bridge_interoperates_with_actual_rust_private_channel() {
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;
    use std::time::{Duration, Instant};

    let (python_stream, server) = UnixStream::pair().unwrap();
    let backend_store = MemoryBackend::default();
    let old_ref = "1:1:77777777777777777777777777777777";
    backend_store.insert(
        gamja_core_host::credential::PROVIDER_SERVICE,
        old_ref,
        b"actual-channel-disconnect-secret",
    );
    let broker = AuthBroker::new(backend_store.clone(), UnavailableAuthTransport);
    let worker = std::thread::spawn(move || serve_auth_channel(server, broker));
    let descriptor = python_stream.as_raw_fd();
    let backend = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("backend");
    let python = backend.join(".venv/bin/python");
    let script = r#"
import os, sys, tempfile, warnings
warnings.filterwarnings("ignore", message='Field "model_id" has conflict.*')
sys.path.insert(0, os.environ["GAMJA_TEST_BACKEND"])
from fastapi.testclient import TestClient
from app.main import create_app
from app.models.collaboration import ConnectionModelCache, ProviderConnection
from app.models.models import User
from app.services.connection_recovery import ConnectionRecoveryCoordinator
from app.services.core_auth_broker import CoreAuthBrokerClient, TrustedOwnerEpochProvider
client = CoreAuthBrokerClient.from_fd(int(os.environ["GAMJA_TEST_AUTH_FD"]))
provider = TrustedOwnerEpochProvider(client)
first = provider.bind(7)
assert first is not None and provider.current(7) == first.owner_epoch
first_operation = client.operation_allocate(7, first.owner_epoch)
second_operation = client.operation_allocate(7, first.owner_epoch)
assert len(first_operation) == 64 and first_operation != second_operation
old = provider.snapshot()
second = provider.bind(8)
assert second is not None and second.owner_epoch != first.owner_epoch
assert provider.compare_and_unbind(old) == "mismatch_preserved"
assert provider.current(8) == second.owner_epoch
current = provider.snapshot()
assert provider.compare_and_unbind(current) == "unbound"
assert provider.current(8) is None
os.environ["GAMJA_LOCAL_SESSION_TOKEN"] = "r3-current-unix-fixture"
with tempfile.TemporaryDirectory() as temp:
    app = create_app(database_url=f"sqlite:///{temp}/r3.sqlite")
    with TestClient(app):
        with app.state.session_factory() as db:
            owner = User(email="r3-core@example.test", username="r3-core", hashed_password="unused")
            db.add(owner)
            db.commit()
            row = ProviderConnection(
                user_id=owner.id, kind="compatible", name="R3 actual Core",
                base_url="https://openrouter.ai/api/v1", secret_ref="1:1:" + "7" * 32,
                default_model_id="vendor/model", enabled=True, auth_required=True,
                capabilities={"declared": {}, "verified": {}}, readiness_revision=4,
                provider_id="openrouter", protocol_id="openai_chat_completions",
                auth_method_id="api_key", public_config_version=1, public_config={},
                auth_grant_revision=3, credential_state="present",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            assert row.id == 1 and owner.id == 1
            bound = provider.bind(owner.id)
            assert bound is not None
            result = ConnectionRecoveryCoordinator(
                broker=client, owner_epochs=provider,
            ).disconnect(db, row, revoke_remote=False)
            assert result.state == "succeeded", result.safe_dict()
            assert result.result["credential_state"] == "missing"
            db.refresh(row)
            assert row.secret_ref is None and row.credential_state == "missing"
client.close()
"#;
    let mut command = Command::new(python);
    command
        .arg("-c")
        .arg(script)
        .env("GAMJA_TEST_BACKEND", &backend)
        .env("GAMJA_TEST_AUTH_FD", descriptor.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    unsafe {
        command.pre_exec(move || {
            if libc::fcntl(descriptor, libc::F_SETFD, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn().unwrap();
    drop(python_stream);
    let deadline = Instant::now() + Duration::from_secs(8);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("Python bridge fixture exceeded its bounded deadline");
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    child.stdout.take().unwrap().read_to_end(&mut stdout).unwrap();
    child.stderr.take().unwrap().read_to_end(&mut stderr).unwrap();
    assert!(status.success(), "{}", String::from_utf8_lossy(&stderr));
    assert!(stdout.is_empty());
    assert!(stderr.is_empty(), "{}", String::from_utf8_lossy(&stderr));
    assert!(backend_store
        .value(gamja_core_host::credential::PROVIDER_SERVICE, old_ref)
        .is_none());
    assert_eq!(worker.join().unwrap(), Ok(()));
}

fn read_frame(stream: &mut UnixStream) -> serde_json::Value {
    let mut header = [0u8; 4];
    stream.read_exact(&mut header).unwrap();
    let mut body = vec![0u8; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut body).unwrap();
    serde_json::from_slice(&body).unwrap()
}

fn write_frame(stream: &mut UnixStream, value: &serde_json::Value) {
    let body = serde_json::to_vec(value).unwrap();
    stream
        .write_all(&(body.len() as u32).to_be_bytes())
        .unwrap();
    stream.write_all(&body).unwrap();
}

// Real private framed Core handlers, but no browser/profile/credentials fixture.
// This exercises only the unavailable path and must not be cited as a
// successful custody handoff or as a real Chromium test.
#[test]
fn unprepared_custody_park_and_revoke_fail_closed_over_private_channel() {
    for park_first in [true, false] {
        let (mut client,server)=UnixStream::pair().unwrap();
        let broker=AuthBroker::new(MemoryBackend::default(),UnavailableAuthTransport);
        let worker=std::thread::spawn(move || serve_auth_channel(server,broker));
        let handshake=read_frame(&mut client);assert_eq!(handshake["version"],1);
        write_frame(&mut client,&json!({"type":"owner_bind","version":1,"owner_id":7}));
        let owner=read_frame(&mut client);assert_eq!(owner["state"],"bound");
        let epoch=owner["current_owner_epoch"].as_str().unwrap();
        let handoff="a".repeat(32);let custody="b".repeat(32);let context="c".repeat(32);
        let park=json!({"type":"vault_custody_park","version":1,"owner_id":7,"current_owner_epoch":epoch,
            "handoff_id":handoff,"custody_id":custody,"context_id":context,"room_id":1,"agent_id":2,
            "root_request_event_id":"33333333-3333-4333-8333-333333333333","run_generation":1,"workflow_id":"synthetic"});
        let revoke=json!({"type":"vault_profile_revoke","version":1,"owner_id":7,"current_owner_epoch":epoch,"handoff_id":handoff});
        let ordered=if park_first {[&park,&revoke]} else {[&revoke,&park]};
        for request in ordered {
            write_frame(&mut client,request);
            let response=read_frame(&mut client);
            assert_eq!(response["version"],1);
            assert_eq!(response["status"],"unavailable");
            assert_eq!(response["handoff_id"],handoff);
        }
        drop(client);let _=worker.join();
    }
}
