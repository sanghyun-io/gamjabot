use async_trait::async_trait;
use base64::Engine as _;
use azure_core::credentials::{TokenCredential, TokenRequestOptions};
use azure_identity::Executor;
use google_cloud_auth::credentials::{
    CacheableResource, Credentials, CredentialsProvider, EntityTag,
};
use http::{Extensions, HeaderMap, HeaderValue};
use gamja_core_host::provider_transport::{
    azure_cli_credential, bedrock_converse_input, fixed_request_spec,
    vertex_generate_content_request, AvailabilityReason, BoundedCancellation,
    BoundedProviderRequest, CoreProviderTransport, NativeChainInspector,
    ProtocolCall, ProviderProtocol, SafeProviderResult, SdkAdapter,
    AwsBedrockSdkAdapter, AwsLegacySsoSdkAdapter, AzureOpenAiSdkAdapter, VertexSdkAdapter,
    BoundedAzureCliExecutor, NativeChainEnrollmentTransport,
    ProviderExecuteCallV1, ProviderHandshakeV1, ProviderOperationV1,
    ProviderWireReply, ProviderWireRequest, serve_provider_channel_with_test_handles,
    transport_from_resolved_permit,
};
use gamja_core_host::auth_broker::{
    AuthBroker, AuthFrame, AuthState, GenerationBinding, NativeEnrollmentSpecV1,
    ProviderBindingRequest, StableBinding,
    SyntheticAuthTransport, TransportResult, UnavailableAuthTransport,
};
use gamja_core_host::credential::{CredentialBackend, CredentialBackendError};
use serde_json::{json, Value};
use std::ffi::OsStr;
use std::collections::HashMap;
use std::fs;
use std::io;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{ExitStatus, Output};
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::thread::JoinHandle;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

static ENV_LOCK: std::sync::OnceLock<Mutex<()>> = std::sync::OnceLock::new();

struct EnvRestore {
    key: &'static str,
    value: Option<std::ffi::OsString>,
}

impl EnvRestore {
    fn set(key: &'static str, value: &str) -> Self {
        let previous = std::env::var_os(key);
        std::env::set_var(key, value);
        Self {
            key,
            value: previous,
        }
    }
}

impl Drop for EnvRestore {
    fn drop(&mut self) {
        if let Some(value) = self.value.take() {
            std::env::set_var(self.key, value);
        } else {
            std::env::remove_var(self.key);
        }
    }
}

struct TestDir(PathBuf);

impl TestDir {
    fn new(label: &str) -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("gamja-{label}-{}-{unique}", std::process::id()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }

    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}

#[derive(Clone, Debug)]
struct SafeHttpObservation {
    path: String,
    authorization: bool,
    api_key: bool,
    cloud_scope: bool,
    system_and_tools: bool,
    function_output: bool,
    x_goog_api_client: Option<String>,
}

struct LoopbackProvider {
    endpoint: String,
    observations: Arc<Mutex<Vec<SafeHttpObservation>>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl LoopbackProvider {
    fn start() -> Self {
        Self::start_with_options(Duration::ZERO, false, Duration::ZERO)
    }

    fn start_with_token_delay(token_delay: Duration) -> Self {
        Self::start_with_options(token_delay, false, Duration::ZERO)
    }

    fn start_with_tool_response() -> Self {
        Self::start_with_options(Duration::ZERO, true, Duration::ZERO)
    }

    fn start_with_response_delay(response_delay: Duration) -> Self {
        Self::start_with_options(Duration::ZERO, false, response_delay)
    }

    fn start_redirecting() -> Self {
        Self::start_with_http_status(Duration::ZERO, false, Duration::ZERO, 302)
    }

    fn start_with_options(
        token_delay: Duration,
        tool_response: bool,
        response_delay: Duration,
    ) -> Self {
        Self::start_with_http_status(token_delay, tool_response, response_delay, 200)
    }

    fn start_with_http_status(
        token_delay: Duration,
        tool_response: bool,
        response_delay: Duration,
        status: u16,
    ) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let observations = Arc::new(Mutex::new(Vec::new()));
        let server_observations = observations.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let server_stop = stop.clone();
        let thread = std::thread::spawn(move || {
            while !server_stop.load(Ordering::Acquire) {
                let (mut stream, _) = match listener.accept() {
                    Ok(value) => value,
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(_) => break,
                };
                stream.set_nonblocking(false).unwrap();
                stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                let mut raw = zeroize::Zeroizing::new(Vec::new());
                let mut chunk = [0u8; 4096];
                loop {
                    let count = stream.read(&mut chunk).unwrap_or(0);
                    if count == 0 {
                        break;
                    }
                    raw.extend_from_slice(&chunk[..count]);
                    let text = String::from_utf8_lossy(raw.as_slice());
                    if let Some(split) = text.find("\r\n\r\n") {
                        let content_length = text[..split]
                            .lines()
                            .find_map(|line| {
                                line.to_ascii_lowercase()
                                    .strip_prefix("content-length:")
                                    .and_then(|value| value.trim().parse::<usize>().ok())
                            })
                            .unwrap_or(0);
                        if raw.len() >= split + 4 + content_length {
                            break;
                        }
                    }
                }
                let text = String::from_utf8_lossy(raw.as_slice());
                let path = text
                    .lines()
                    .next()
                    .and_then(|line| line.split_whitespace().nth(1))
                    .unwrap_or("")
                    .to_owned();
                server_observations.lock().unwrap().push(SafeHttpObservation {
                    path: path.clone(),
                    authorization: text
                        .lines()
                        .any(|line| line.starts_with("authorization: Bearer ")),
                    api_key: text
                        .lines()
                        .any(|line| line == "api-key: fixture-api-key"),
                    cloud_scope: text.contains("https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform")
                        || text.contains("https://www.googleapis.com/auth/cloud-platform"),
                    system_and_tools: text.contains("System rule") && text.contains("gamja_62726f77736572"),
                    function_output: text.contains("function_call_output") && text.contains("succeeded: done"),
                    x_goog_api_client: text.lines().find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("x-goog-api-client:")
                            .map(|value| value.trim().to_owned())
                    }),
                });
                if path == "/token" {
                    std::thread::sleep(token_delay);
                } else {
                    std::thread::sleep(response_delay);
                }
                let body = if path == "/token" {
                    r#"{"access_token":"fixture-source-access","token_type":"Bearer","expires_in":3600}"#
                } else if path.ends_with(":generateAccessToken") {
                    r#"{"accessToken":"fixture-impersonated-access","expireTime":"2099-01-01T00:00:00Z"}"#
                } else if path == "/openai/v1/responses?api-version=v1" {
                    if tool_response {
                        r#"{"id":"resp-safe","model":"deployment-test","output":[{"type":"message","content":[{"type":"output_text","text":"safe text"}]},{"type":"function_call","call_id":"call-1","name":"gamja_62726f77736572","arguments":"{\"url\":\"https://example.test\"}"}],"usage":{"input_tokens":1,"output_tokens":1}}"#
                    } else {
                        r#"{"id":"resp-safe","model":"deployment-test","output":[{"type":"message","content":[{"type":"output_text","text":"safe text"}]}],"usage":{"input_tokens":1,"output_tokens":1}}"#
                    }
                } else {
                    if tool_response {
                        r#"{"responseId":"safe-response","modelVersion":"gemini-test","candidates":[{"content":{"role":"model","parts":[{"text":"safe text"},{"functionCall":{"name":"gamja_62726f77736572","args":{"url":"https://example.test"}},"thoughtSignature":"c2ln"}]}}],"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1}}"#
                    } else {
                        r#"{"responseId":"safe-response","modelVersion":"gemini-test","candidates":[{"content":{"role":"model","parts":[{"text":"safe text"}]}}],"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1}}"#
                    }
                };
                let status_line = if status == 302 {
                    "HTTP/1.1 302 Found"
                } else {
                    "HTTP/1.1 200 OK"
                };
                let location = if status == 302 {
                    "location: /forbidden-redirect\r\n"
                } else {
                    ""
                };
                let _ = write!(stream,
                    "{status_line}\r\n{location}content-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                    body.len(), body);
            }
        });
        Self {
            endpoint,
            observations,
            stop,
            thread: Some(thread),
        }
    }
}

impl Drop for LoopbackProvider {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

struct BedrockRetryLoopback {
    endpoint: String,
    attempts: Arc<AtomicU32>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl BedrockRetryLoopback {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let attempts = Arc::new(AtomicU32::new(0));
        let server_attempts = attempts.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let server_stop = stop.clone();
        let thread = std::thread::spawn(move || {
            while !server_stop.load(Ordering::Acquire) {
                let (mut stream, _) = match listener.accept() {
                    Ok(value) => value,
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(_) => break,
                };
                stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                let mut raw = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    let count = stream.read(&mut chunk).unwrap_or(0);
                    if count == 0 { break; }
                    raw.extend_from_slice(&chunk[..count]);
                    let text = String::from_utf8_lossy(&raw);
                    if let Some(split) = text.find("\r\n\r\n") {
                        let content_length = text[..split].lines().find_map(|line| {
                            line.to_ascii_lowercase().strip_prefix("content-length:")
                                .and_then(|value| value.trim().parse::<usize>().ok())
                        }).unwrap_or(0);
                        if raw.len() >= split + 4 + content_length { break; }
                    }
                }
                let attempt = server_attempts.fetch_add(1, Ordering::AcqRel) + 1;
                if attempt == 1 {
                    let body = r#"{"message":"retry"}"#;
                    let _ = write!(stream,
                        "HTTP/1.1 500 Internal Server Error\r\ncontent-type: application/json\r\nx-amzn-errortype: InternalServerException\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                        body.len(), body);
                } else {
                    let body = r#"{"output":{"message":{"role":"assistant","content":[{"text":"safe"}]}},"stopReason":"end_turn","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2},"metrics":{"latencyMs":1}}"#;
                    let _ = write!(stream,
                        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nx-amzn-requestid: fixture-request\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                        body.len(), body);
                }
            }
        });
        Self { endpoint, attempts, stop, thread: Some(thread) }
    }
}

impl Drop for BedrockRetryLoopback {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() { thread.join().unwrap(); }
    }
}

#[derive(Debug)]
struct StaticTestCredentials;

impl CredentialsProvider for StaticTestCredentials {
    fn headers(
        &self,
        _extensions: Extensions,
    ) -> impl std::future::Future<
        Output = Result<CacheableResource<HeaderMap>, google_cloud_auth::errors::CredentialsError>,
    > + Send {
        async {
            let mut headers = HeaderMap::new();
            headers.insert(
                http::header::AUTHORIZATION,
                HeaderValue::from_static("Bearer fixture-static-access"),
            );
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

#[derive(Debug)]
struct NotModifiedTestCredentials;

impl CredentialsProvider for NotModifiedTestCredentials {
    fn headers(
        &self,
        _extensions: Extensions,
    ) -> impl std::future::Future<
        Output = Result<CacheableResource<HeaderMap>, google_cloud_auth::errors::CredentialsError>,
    > + Send {
        async { Ok(CacheableResource::NotModified) }
    }

    fn universe_domain(&self) -> impl std::future::Future<Output = Option<String>> + Send {
        async { Some("googleapis.com".to_owned()) }
    }
}

#[derive(Clone, Debug)]
struct AwsLegacyObservation {
    stage: &'static str,
    request: String,
}

struct AwsLegacyLoopback {
    endpoint: String,
    observations: Arc<Mutex<Vec<AwsLegacyObservation>>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl AwsLegacyLoopback {
    fn start(delayed_stage: Option<&'static str>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let observations = Arc::new(Mutex::new(Vec::new()));
        let server_observations = observations.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let server_stop = stop.clone();
        let thread = std::thread::spawn(move || {
            while !server_stop.load(Ordering::Acquire) {
                let (mut stream, _) = match listener.accept() {
                    Ok(value) => value,
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(_) => break,
                };
                stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                let mut bytes = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    let count = stream.read(&mut chunk).unwrap_or(0);
                    if count == 0 { break; }
                    bytes.extend_from_slice(&chunk[..count]);
                    let text = String::from_utf8_lossy(&bytes);
                    if let Some(split) = text.find("\r\n\r\n") {
                        let length = text[..split].lines().find_map(|line| {
                            line.to_ascii_lowercase().strip_prefix("content-length:")
                                .and_then(|value| value.trim().parse::<usize>().ok())
                        }).unwrap_or(0);
                        if bytes.len() >= split + 4 + length { break; }
                    }
                }
                let request = String::from_utf8_lossy(&bytes).into_owned();
                let stage = if request.contains("/federation/credentials") {
                    "sso"
                } else if request.contains("Action=AssumeRole")
                    || request.contains("Action%3DAssumeRole")
                {
                    "sts"
                } else {
                    "bedrock"
                };
                server_observations.lock().unwrap().push(AwsLegacyObservation {
                    stage,
                    request,
                });
                if delayed_stage == Some(stage) {
                    std::thread::sleep(Duration::from_millis(250));
                }
                let (content_type, body) = match stage {
                    "sso" => (
                        "application/json",
                        r#"{"roleCredentials":{"accessKeyId":"SSOLEAF","secretAccessKey":"fixture-sso-secret","sessionToken":"fixture-sso-session","expiration":4070908800000}}"#,
                    ),
                    "sts" => (
                        "text/xml",
                        r#"<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult><Credentials><AccessKeyId>STSFINAL</AccessKeyId><SecretAccessKey>fixture-sts-secret</SecretAccessKey><SessionToken>fixture-sts-session</SessionToken><Expiration>2099-01-01T00:00:00Z</Expiration></Credentials><AssumedRoleUser><Arn>arn:aws:sts::123456789012:assumed-role/Outer/gamja-test</Arn><AssumedRoleId>fixture:gamja-test</AssumedRoleId></AssumedRoleUser></AssumeRoleResult><ResponseMetadata><RequestId>fixture</RequestId></ResponseMetadata></AssumeRoleResponse>"#,
                    ),
                    _ => (
                        "application/json",
                        r#"{"output":{"message":{"role":"assistant","content":[{"text":"safe"}]}},"stopReason":"end_turn","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2},"metrics":{"latencyMs":1}}"#,
                    ),
                };
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\ncontent-type: {content_type}\r\nx-amzn-requestid: fixture\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len(),
                );
            }
        });
        Self { endpoint, observations, stop, thread: Some(thread) }
    }
}

impl Drop for AwsLegacyLoopback {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() { thread.join().unwrap(); }
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[derive(Debug)]
struct RecordingExecutor {
    invocation: Arc<Mutex<Option<(String, Vec<String>)>>>,
}

#[async_trait]
impl Executor for RecordingExecutor {
    async fn run(&self, program: &OsStr, args: &[&OsStr]) -> io::Result<Output> {
        *self.invocation.lock().unwrap() = Some((
            program.to_string_lossy().into_owned(),
            args.iter().map(|arg| arg.to_string_lossy().into_owned()).collect(),
        ));
        #[cfg(unix)]
        let status = {
            use std::os::unix::process::ExitStatusExt;
            ExitStatus::from_raw(0)
        };
        Ok(Output {
            status,
            stdout: br#"{"accessToken":"must-not-escape","expires_on":2147483647,"tenant":"safe","tokenType":"Bearer"}"#.to_vec(),
            stderr: Vec::new(),
        })
    }
}

#[tokio::test]
async fn azure_uses_only_the_exact_cli_scope_without_returning_the_token() {
    let invocation = Arc::new(Mutex::new(None));
    let credential = azure_cli_credential(Arc::new(RecordingExecutor {
        invocation: invocation.clone(),
    }))
    .unwrap();

    let token = credential
        .get_token(
            &["https://cognitiveservices.azure.com/.default"],
            None::<TokenRequestOptions<'_>>,
        )
        .await
        .unwrap();
    assert_eq!(token.token.secret(), "must-not-escape");

    let (program, args) = invocation.lock().unwrap().clone().unwrap();
    assert_eq!(program, "/bin/sh");
    assert_eq!(args[0], "-c");
    assert_eq!(
        args[1],
        "cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default"
    );
    assert!(!format!("{credential:?}").contains("must-not-escape"));
}

#[tokio::test]
async fn azure_adapter_posts_only_the_fixed_responses_v1_request() {
    let provider = LoopbackProvider::start();
    let invocation = Arc::new(Mutex::new(None));
    let credential = azure_cli_credential(Arc::new(RecordingExecutor {
        invocation: invocation.clone(),
    }))
    .unwrap();
    let adapter = AzureOpenAiSdkAdapter::with_test_endpoint(
        "https://potato.openai.azure.com",
        credential,
        &provider.endpoint,
    )
    .unwrap();
    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-azure",
        1,
        ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment-test",
        json!({
            "resource_endpoint":"https://potato.openai.azure.com",
            "api_version":"v1"
        }),
        BoundedProviderRequest::new("hello", 16).unwrap(),
        1,
    );
    let result = adapter.execute(&call).await;
    assert!(
        result.ok,
        "{result:?} observations={:?}",
        provider.observations.lock().unwrap()
    );
    let observations = provider.observations.lock().unwrap().clone();
    assert_eq!(observations.len(), 1, "{observations:?}");
    assert_eq!(observations[0].path, "/openai/v1/responses?api-version=v1");
    assert!(observations[0].authorization);
    let (_, args) = invocation.lock().unwrap().clone().unwrap();
    assert!(args[1].ends_with(AZURE_TEST_SCOPE));
}

#[tokio::test]
async fn azure_api_key_adapter_sends_only_api_key_and_never_invokes_cli() {
    let provider = LoopbackProvider::start();
    let adapter = AzureOpenAiSdkAdapter::with_test_api_key_endpoint(
        "https://potato.openai.azure.com",
        zeroize::Zeroizing::new(b"fixture-api-key".to_vec()),
        &provider.endpoint,
    )
    .unwrap();
    assert!(!format!("{adapter:?}").contains("fixture-api-key"));
    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-azure-api-key",
        1,
        ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment-test",
        json!({
            "resource_endpoint":"https://potato.openai.azure.com",
            "api_version":"v1"
        }),
        BoundedProviderRequest::new("hello", 16).unwrap(),
        1,
    );
    let result = adapter.execute(&call).await;
    assert!(result.ok, "{result:?}");
    let observations = provider.observations.lock().unwrap().clone();
    assert_eq!(observations.len(), 1);
    assert!(observations[0].api_key);
    assert!(!observations[0].authorization);
}

#[tokio::test]
async fn azure_zero_budget_prevents_model_http() {
    let provider = LoopbackProvider::start();
    let adapter = AzureOpenAiSdkAdapter::with_test_api_key_endpoint(
        "https://potato.openai.azure.com",
        zeroize::Zeroizing::new(b"fixture-api-key".to_vec()),
        &provider.endpoint,
    ).unwrap();
    let fields = json!({
        "resource_endpoint":"https://potato.openai.azure.com",
        "api_version":"v1"
    });
    let transport = CoreProviderTransport::bind_test_fixture(
        "opaque-azure-zero-budget", 1, ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment-test", fields.clone(), Arc::new(adapter),
    ).unwrap();
    let result = transport.execute(ProtocolCall::with_physical_model_request_budget(
        "opaque-azure-zero-budget", 1, ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment-test", fields, BoundedProviderRequest::new("hello", 16).unwrap(), 0,
    ), &BoundedCancellation::new()).await;
    assert_eq!(result.error_code, Some("provider_request_budget_exhausted"));
    assert_eq!(result.physical_model_request_count, 0);
    assert!(provider.observations.lock().unwrap().is_empty());
}

#[tokio::test]
async fn azure_api_key_inflight_cancel_is_terminal_without_followup_http() {
    let provider = LoopbackProvider::start_with_response_delay(Duration::from_millis(250));
    let adapter = Arc::new(
        AzureOpenAiSdkAdapter::with_test_api_key_endpoint(
            "https://potato.openai.azure.com",
            zeroize::Zeroizing::new(b"fixture-api-key".to_vec()),
            &provider.endpoint,
        )
        .unwrap(),
    );
    let fields = json!({
        "resource_endpoint":"https://potato.openai.azure.com",
        "api_version":"v1"
    });
    let transport = Arc::new(CoreProviderTransport::bind_test_fixture(
        "opaque-azure-api-key-cancel",
        1,
        ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment-test",
        fields.clone(),
        adapter,
    )
    .unwrap());
    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-azure-api-key-cancel",
        1,
        ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment-test",
        fields,
        BoundedProviderRequest::new("hello", 16).unwrap(),
        1,
    );
    let cancel = BoundedCancellation::new();
    let task_transport = transport.clone();
    let task_cancel = cancel.clone();
    let execution = tokio::spawn(async move {
        task_transport.execute(call, &task_cancel).await
    });
    for _ in 0..50 {
        if provider.observations.lock().unwrap().len() == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(provider.observations.lock().unwrap().len(), 1);
    cancel.cancel();
    let result = execution.await.unwrap();
    assert_eq!(result.error_code, Some("cancelled"));
    assert_eq!(result.physical_model_request_count, 1);
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(provider.observations.lock().unwrap().len(), 1);
}

fn cloud_contract_wire(protocol: &str, provider_kind: &str, model: &str) -> serde_json::Value {
    json!({
        "scope":{"connection_id":71,"provider_kind":provider_kind,"base_url":null,
                 "room_id":9,"agent_id":7,"watermark_seq":3,"descriptor_revision":12,"model_id":model},
        "protocol":protocol,"system":"System rule",
        "messages":[{"role":"user","content":"user Owner (room seq 1): Do it"},
                    {"role":"assistant","content":"Prior reply"}],
        "tools":[{"family":"browser","wire_name":"gamja_62726f77736572",
                  "description":"Use browser","parameters":{"type":"object","properties":{"url":{"type":"string"}}}}],
        "observations":[],"vision":null,"continuation":null,"temperature":null,"max_output_tokens":64
    })
}

#[tokio::test]
async fn azure_returns_text_typed_tools_and_scope_bound_continuation() {
    let provider = LoopbackProvider::start_with_tool_response();
    let credential = azure_cli_credential(Arc::new(RecordingExecutor {
        invocation: Arc::new(Mutex::new(None)),
    })).unwrap();
    let adapter = AzureOpenAiSdkAdapter::with_test_endpoint(
        "https://potato.openai.azure.com", credential, &provider.endpoint,
    ).unwrap();
    let fields = json!({"resource_endpoint":"https://potato.openai.azure.com","api_version":"v1"});
    let request = BoundedProviderRequest::from_wire_value(cloud_contract_wire(
        "azure_openai_responses_v1", "azure", "deployment-test",
    )).unwrap();
    let first = adapter.execute(&ProtocolCall::with_physical_model_request_budget(
        "opaque-azure-contract", 12, ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment-test", fields.clone(), request, 1,
    )).await;
    assert_eq!(first.text, "safe text");
    assert_eq!(first.tool_calls.len(), 1);
    assert_eq!(first.tool_calls[0].arguments, json!({"url":"https://example.test"}));
    let continuation = first.continuation.unwrap();
    assert_eq!(continuation.room_id, 9);
    assert_eq!(continuation.descriptor_revision, 12);

    let mut next = cloud_contract_wire("azure_openai_responses_v1", "azure", "deployment-test");
    next["continuation"] = serde_json::to_value(continuation).unwrap();
    next["observations"] = json!([{"call_id":"call-1","state":"succeeded",
        "text":"succeeded: done","screenshot_ref":null,"image":null}]);
    let second_request = BoundedProviderRequest::from_wire_value(next).unwrap();
    assert!(adapter.execute(&ProtocolCall::with_physical_model_request_budget(
        "opaque-azure-contract", 12, ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment-test", fields, second_request, 1,
    )).await.ok);
    let observations = provider.observations.lock().unwrap().clone();
    assert!(observations[0].system_and_tools);
    assert!(observations[1].function_output);
}

const AZURE_TEST_SCOPE: &str = "https://cognitiveservices.azure.com/.default";

#[test]
fn fixed_request_shapes_use_the_pinned_sdk_types() {
    let request = BoundedProviderRequest::new("hello", 64).unwrap();

    let azure = fixed_request_spec(
        ProviderProtocol::AzureOpenAiResponsesV1,
        "deploy-a",
        &json!({"resource_endpoint":"https://potato.openai.azure.com","api_version":"v1"}),
        &request,
    )
    .unwrap();
    assert_eq!(azure.endpoint, "https://potato.openai.azure.com/openai/v1/responses?api-version=v1");
    assert_eq!(azure.method, "POST");

    let bedrock = bedrock_converse_input("anthropic.claude-test", &request).unwrap();
    assert_eq!(bedrock.model_id(), Some("anthropic.claude-test"));
    assert_eq!(bedrock.inference_config().unwrap().max_tokens(), Some(64));
    assert_eq!(bedrock.messages().len(), 1);

    let vertex = vertex_generate_content_request(
        "example-project",
        "us-central1",
        "gemini-test",
        &request,
    )
    .unwrap();
    assert_eq!(
        vertex.model,
        "projects/example-project/locations/us-central1/publishers/google/models/gemini-test"
    );
    assert_eq!(vertex.contents.len(), 1);
    assert_eq!(vertex.generation_config.unwrap().max_output_tokens, Some(64));

    assert!(fixed_request_spec(
        ProviderProtocol::AzureOpenAiResponsesV1,
        "deploy-a",
        &json!({"resource_endpoint":"https://nested.potato.openai.azure.com","api_version":"v1"}),
        &request,
    )
    .is_err());
    assert!(fixed_request_spec(
        ProviderProtocol::AwsBedrockConverse,
        "model-a",
        &json!({"profile_name":"work","region":"us-future-9"}),
        &request,
    )
    .is_err());
    assert!(fixed_request_spec(
        ProviderProtocol::VertexGenerateContentV1,
        "model-a",
        &json!({"project_id":"example-project","location":"antarctica-south1"}),
        &request,
    )
    .is_err());
}

#[test]
fn provider_model_contract_preserves_the_common_200_byte_boundary() {
    let request = BoundedProviderRequest::new("hello", 64).unwrap();
    let azure_fields = json!({
        "resource_endpoint":"https://potato.openai.azure.com",
        "api_version":"v1"
    });
    let aws_fields = json!({"profile_name":"work","region":"us-east-1"});
    for length in [129, 200] {
        let model = format!("m{}", "a".repeat(length - 1));
        assert!(fixed_request_spec(
            ProviderProtocol::AzureOpenAiResponsesV1,
            &model,
            &azure_fields,
            &request,
        ).is_ok());
        assert!(fixed_request_spec(
            ProviderProtocol::AwsBedrockConverse,
            &model,
            &aws_fields,
            &request,
        ).is_ok());
        assert!(vertex_generate_content_request(
            "example-project", "us-central1", &model, &request,
        ).is_ok());
    }
    let too_long = format!("m{}", "a".repeat(200));
    assert!(fixed_request_spec(
        ProviderProtocol::AzureOpenAiResponsesV1,
        &too_long,
        &azure_fields,
        &request,
    ).is_err());
    assert!(fixed_request_spec(
        ProviderProtocol::AwsBedrockConverse,
        &too_long,
        &aws_fields,
        &request,
    ).is_err());
    assert!(vertex_generate_content_request(
        "example-project", "us-central1", &too_long, &request,
    ).is_err());
    assert!(fixed_request_spec(
        ProviderProtocol::AzureOpenAiResponsesV1,
        "deployment:variant/segment-2",
        &azure_fields,
        &request,
    ).is_ok());
    assert!(vertex_generate_content_request(
        "example-project", "us-central1", "gemini:variant/segment-2", &request,
    ).is_err());
}

#[test]
fn bedrock_model_contract_accepts_reviewed_sdk_targets_and_rejects_path_drift() {
    let request = BoundedProviderRequest::new("hello", 64).unwrap();
    let fields = json!({"profile_name":"work","region":"us-east-1"});
    let targets = [
        "anthropic.claude-3-sonnet-20240229-v1:0",
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "arn:aws:bedrock:us-east-1:123456789012:provisioned-model/abc123",
        "arn:aws:bedrock:us-east-1:123456789012:prompt/ABCDEFGHIJ:1",
    ];
    for target in targets {
        assert!(bedrock_converse_input(target, &request).is_ok(), "target={target}");
        assert!(fixed_request_spec(
            ProviderProtocol::AwsBedrockConverse,
            target,
            &fields,
            &request,
        ).is_ok(), "target={target}");
    }
    for target in [
        "arn:aws:bedrock:us-east-1:123456789012:prompt//ABCDEFGHIJ:1",
        "arn:aws:bedrock:us-east-1:123456789012:prompt/../ABCDEFGHIJ:1",
        "arn:aws:bedrock:us-east-1:123456789012:prompt/ABCDEFGHIJ:1?draft=true",
        "arn:aws:bedrock:us-east-1:123456789012:prompt/ABCDEFGHIJ:1#draft",
        "arn:aws:bedrock:us-east-1:123456789012:prompt\\ABCDEFGHIJ:1",
    ] {
        assert!(bedrock_converse_input(target, &request).is_err(), "target={target}");
        assert!(fixed_request_spec(
            ProviderProtocol::AwsBedrockConverse,
            target,
            &fields,
            &request,
        ).is_err(), "target={target}");
    }
}

#[test]
fn bounded_contract_preserves_projected_context_tools_and_actual_sdk_parts() {
    let wire = json!({
        "scope": {"connection_id":71,"provider_kind":"vertex","base_url":null,
                  "room_id":9,"agent_id":7,"watermark_seq":3,
                  "descriptor_revision":12,"model_id":"gemini-test"},
        "protocol":"vertex_generate_content_v1",
        "system":"System rule",
        "messages":[
            {"role":"user","content":"user Owner (room seq 1): Do it"},
            {"role":"assistant","content":"Prior reply"}
        ],
        "tools":[{"family":"browser","wire_name":"gamja_62726f77736572",
                  "description":"Use the browser","parameters":{"type":"object","properties":{}}}],
        "observations":[],
        "vision":{"prompt":"inspect","image":{"mime_type":"image/png","data_base64":"eA==",
                  "width":null,"height":null}},
        "continuation":null,
        "temperature":null,
        "max_output_tokens":64
    });
    let request = BoundedProviderRequest::from_wire_value(wire).unwrap();
    assert_eq!(request.messages.len(), 2);
    assert_eq!(request.tools[0].wire_name, "gamja_62726f77736572");

    let vertex = vertex_generate_content_request(
        "example-project", "us-central1", "gemini-test", &request,
    ).unwrap();
    assert_eq!(vertex.system_instruction.as_ref().unwrap().parts.len(), 1);
    assert_eq!(vertex.contents.len(), 3);
    assert!(vertex.contents.last().unwrap().parts.iter().any(|part| {
        matches!(part.data, Some(google_cloud_aiplatform_v1::model::part::Data::InlineData(_)))
    }));
    assert_eq!(vertex.tools.len(), 1);

    let mut bedrock_wire = serde_json::to_value(&request).unwrap();
    bedrock_wire["protocol"] = json!("aws_bedrock_converse");
    bedrock_wire["scope"]["model_id"] = json!("anthropic.claude-test");
    let bedrock_request = BoundedProviderRequest::from_wire_value(bedrock_wire).unwrap();
    let bedrock = bedrock_converse_input("anthropic.claude-test", &bedrock_request).unwrap();
    assert_eq!(bedrock.system().len(), 1);
    assert_eq!(bedrock.messages().len(), 3);
    assert!(bedrock.messages().last().unwrap().content().iter().any(|block| block.is_image()));
    assert_eq!(bedrock.tool_config().unwrap().tools().len(), 1);

    let mut bedrock_next = cloud_contract_wire("aws_bedrock_converse", "aws", "anthropic.claude-test");
    bedrock_next["continuation"] = json!({"provider_kind":"aws","protocol":"aws_bedrock_converse",
        "connection_id":71,"model_id":"anthropic.claude-test","base_url":null,"room_id":9,"agent_id":7,
        "watermark_seq":3,"descriptor_revision":12,"response_id":"bedrock-converse",
        "pending_call_ids":["call-1"],"provider_state":{"kind":"bedrock_converse","assistant_text":"",
        "tool_calls":[{"call_id":"call-1","name":"gamja_62726f77736572","arguments":{"url":"x"}}]}});
    bedrock_next["observations"] = json!([{"call_id":"call-1","state":"succeeded","text":"done",
        "screenshot_ref":null,"image":{"mime_type":"image/png","data_base64":"eA==","width":1,"height":1}}]);
    let bedrock_next = BoundedProviderRequest::from_wire_value(bedrock_next).unwrap();
    let bedrock = bedrock_converse_input("anthropic.claude-test", &bedrock_next).unwrap();
    assert!(bedrock.messages().last().unwrap().content().iter().any(|block| {
        block.as_tool_result().is_ok_and(|result| result.content().iter().any(|content| content.is_image()))
    }));

    let mut vertex_next = serde_json::to_value(&bedrock_next).unwrap();
    vertex_next["protocol"] = json!("vertex_generate_content_v1");
    vertex_next["scope"]["provider_kind"] = json!("vertex");
    vertex_next["scope"]["model_id"] = json!("gemini-test");
    vertex_next["continuation"]["provider_kind"] = json!("vertex");
    vertex_next["continuation"]["protocol"] = json!("vertex_generate_content_v1");
    vertex_next["continuation"]["model_id"] = json!("gemini-test");
    vertex_next["continuation"]["response_id"] = json!("safe-response");
    vertex_next["continuation"]["provider_state"]["kind"] = json!("vertex_generate_content");
    let vertex_next = BoundedProviderRequest::from_wire_value(vertex_next).unwrap();
    let vertex = vertex_generate_content_request("example-project", "us-central1", "gemini-test", &vertex_next).unwrap();
    assert!(vertex.contents.last().unwrap().parts.iter().any(|part| {
        part.function_response().is_some_and(|response| response.parts.iter().any(|part| part.inline_data().is_some()))
    }));
}

#[test]
fn bounded_contract_rejects_unknown_fields_bad_base64_and_utf8_frame_overflow() {
    let base = json!({
        "scope":{"connection_id":1,"provider_kind":"azure","base_url":null,
                 "room_id":1,"agent_id":1,"watermark_seq":0,
                 "descriptor_revision":1,"model_id":"model-a"},
        "protocol":"azure_openai_responses_v1","system":"","messages":[{"role":"user","content":"hello"}],
        "tools":[],"observations":[],"vision":null,"continuation":null,"temperature":null,
        "max_output_tokens":16
    });
    let mut unknown = base.clone();
    unknown["secret"] = json!("must-not-pass");
    assert_eq!(BoundedProviderRequest::from_wire_value(unknown).unwrap_err(), "invalid_bounded_request");
    let mut bad_base64 = base.clone();
    bad_base64["vision"] = json!({"prompt":"p","image":{"mime_type":"image/png",
        "data_base64":"***","width":null,"height":null}});
    assert_eq!(BoundedProviderRequest::from_wire_value(bad_base64).unwrap_err(), "invalid_bounded_request");
    let mut too_large = base;
    too_large["system"] = json!("감".repeat(14_000_000));
    assert_eq!(BoundedProviderRequest::from_wire_value(too_large).unwrap_err(), "invalid_bounded_request");

    let mut exact_image = json!({
        "scope":{"connection_id":1,"provider_kind":"vertex","base_url":null,"room_id":1,"agent_id":1,
                 "watermark_seq":0,"descriptor_revision":1,"model_id":"model-a"},
        "protocol":"vertex_generate_content_v1","system":"","messages":[{"role":"user","content":"hello"}],
        "tools":[],"observations":[],"vision":{"prompt":"inspect","image":{"mime_type":"image/png",
        "data_base64":base64::engine::general_purpose::STANDARD.encode(vec![b'x'; 10_000_000]),
        "width":null,"height":null}},"continuation":null,"temperature":null,"max_output_tokens":16
    });
    assert!(BoundedProviderRequest::from_wire_value(exact_image.clone()).is_ok());
    exact_image["vision"]["image"]["data_base64"] = json!(
        base64::engine::general_purpose::STANDARD.encode(vec![b'x'; 10_000_001]));
    assert_eq!(BoundedProviderRequest::from_wire_value(exact_image).unwrap_err(), "invalid_bounded_request");

    let mut max_items = json!({
        "scope":{"connection_id":1,"provider_kind":"azure","base_url":null,"room_id":1,"agent_id":1,
                 "watermark_seq":0,"descriptor_revision":1,"model_id":"model-a"},
        "protocol":"azure_openai_responses_v1","system":"","messages":[],"tools":[],"observations":[],
        "vision":null,"continuation":null,"temperature":null,"max_output_tokens":16
    });
    max_items["messages"] = Value::Array((0..64).map(|index| json!({"role":"user","content":format!("m{index}")})).collect());
    assert!(BoundedProviderRequest::from_wire_value(max_items.clone()).is_ok());
    max_items["messages"].as_array_mut().unwrap().push(json!({"role":"user","content":"overflow"}));
    assert_eq!(BoundedProviderRequest::from_wire_value(max_items).unwrap_err(), "invalid_bounded_request");

    let mut frame = json!({
        "scope":{"connection_id":1,"provider_kind":"vertex","base_url":null,"room_id":1,"agent_id":1,
                 "watermark_seq":0,"descriptor_revision":1,"model_id":"model-a"},
        "protocol":"vertex_generate_content_v1","system":"","messages":[],"tools":[],"observations":[],
        "vision":{"prompt":"inspect","image":{"mime_type":"image/png",
        "data_base64":base64::engine::general_purpose::STANDARD.encode(vec![b'x'; 10_000_000]),
        "width":null,"height":null}},"continuation":null,"temperature":null,"max_output_tokens":16
    });
    frame["messages"] = Value::Array((0..11).map(|_| json!({"role":"user","content":"x".repeat(55_000)})).collect());
    let base_size = serde_json::to_vec(&frame).unwrap().len();
    frame["messages"].as_array_mut().unwrap().push(json!({"role":"user","content":"x"}));
    let probe_size = serde_json::to_vec(&frame).unwrap().len();
    let overhead = probe_size - base_size - 1;
    let final_chars = 13_999_999usize - base_size - overhead;
    assert!((1..65_000).contains(&final_chars));
    frame["messages"][11]["content"] = json!("x".repeat(final_chars));
    assert_eq!(serde_json::to_vec(&frame).unwrap().len(), 13_999_999);
    assert!(BoundedProviderRequest::from_wire_value(frame.clone()).is_ok());
    frame["messages"][11]["content"] = json!("x".repeat(final_chars + 2));
    assert_eq!(serde_json::to_vec(&frame).unwrap().len(), 14_000_001);
    assert_eq!(BoundedProviderRequest::from_wire_value(frame).unwrap_err(), "invalid_bounded_request");
}

#[test]
fn continuation_rejects_room_revision_and_protocol_scope_mismatch() {
    let mut wire = cloud_contract_wire("azure_openai_responses_v1", "azure", "deployment-test");
    wire["continuation"] = json!({"provider_kind":"azure","protocol":"azure_openai_responses_v1",
        "connection_id":71,"model_id":"deployment-test","base_url":null,"room_id":9,"agent_id":7,
        "watermark_seq":3,"descriptor_revision":12,"response_id":"resp-safe",
        "pending_call_ids":["call-1"],"provider_state":{"kind":"azure_responses",
        "previous_response_id":"resp-safe"}});
    wire["observations"] = json!([{"call_id":"call-1","state":"succeeded","text":"done",
        "screenshot_ref":null,"image":null}]);
    assert!(BoundedProviderRequest::from_wire_value(wire.clone()).is_ok());
    for (path, value) in [
        (("continuation", "room_id"), json!(10)),
        (("continuation", "descriptor_revision"), json!(13)),
        (("continuation", "protocol"), json!("aws_bedrock_converse")),
        (("continuation", "provider_kind"), json!("aws")),
        (("continuation", "base_url"), json!("https://other.example")),
    ] {
        let mut changed = wire.clone();
        changed[path.0][path.1] = value;
        assert_eq!(BoundedProviderRequest::from_wire_value(changed).unwrap_err(), "invalid_continuation");
    }
}

#[test]
fn native_inspector_accepts_only_allowlisted_aws_and_vertex_sources() {
    let tmp = TestDir::new("provider-sources");
    let config = tmp.path("config");
    let credentials = tmp.path("credentials");
    fs::write(
        &config,
        "[profile work]\nrole_arn=arn:aws:iam::123456789012:role/Work\nsource_profile=leaf\n",
    )
    .unwrap();
    fs::write(
        &credentials,
        "[leaf]\naws_access_key_id=fixture-access\naws_secret_access_key=fixture-secret\n",
    )
    .unwrap();
    let allowed = NativeChainInspector::aws_profile(&config, &credentials, "work");
    assert!(allowed.available);
    assert_eq!(allowed.finite_reason, AvailabilityReason::Available);
    assert!(!allowed.source_generation_fingerprint.contains("fixture"));

    fs::write(
        &config,
        "[profile work]\ncredential_process=/tmp/steal\nregion=us-east-1\n",
    )
    .unwrap();
    let rejected = NativeChainInspector::aws_profile(&config, &credentials, "work");
    assert!(!rejected.available);
    assert_eq!(rejected.finite_reason, AvailabilityReason::NativeSourceDisallowed);

    for rejected_key in [
        "credential_source=Environment",
        "web_identity_token_file=/tmp/token",
        "login_session=work",
        "endpoint_url=https://example.invalid",
        "services=custom-endpoints",
    ] {
        fs::write(
            &config,
            format!("[profile work]\n{rejected_key}\nregion=us-east-1\n"),
        )
        .unwrap();
        assert_eq!(
            NativeChainInspector::aws_profile(&config, &credentials, "work").finite_reason,
            AvailabilityReason::NativeSourceDisallowed
        );
    }

    fs::write(
        &config,
        "[profile work]\nsso_start_url=https://example.awsapps.com/start\nsso_region=us-east-1\nsso_account_id=123456789012\nsso_role_name=ReadOnly\n",
    )
    .unwrap();
    assert!(NativeChainInspector::aws_profile(&config, &credentials, "work").available);

    fs::write(
        &config,
        "[profile work]\nsso_session=modern\nsso_account_id=123456789012\nsso_role_name=ReadOnly\n[sso-session modern]\nsso_start_url=https://example.awsapps.com/start\nsso_region=us-east-1\n",
    )
    .unwrap();
    assert_eq!(
        NativeChainInspector::aws_profile(&config, &credentials, "work").finite_reason,
        AvailabilityReason::NativeSourceUnsupported
    );

    let adc = tmp.path("application_default_credentials.json");
    fs::write(
        &adc,
        br#"{"type":"authorized_user","client_id":"fixture-client","client_secret":"fixture-secret","refresh_token":"fixture-refresh","quota_project_id":"example-project"}"#,
    )
    .unwrap();
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o600)).unwrap();
    let adc_allowed = NativeChainInspector::vertex_adc(&adc);
    assert!(adc_allowed.available);
    assert!(!adc_allowed.source_generation_fingerprint.contains("fixture"));

    fs::write(&adc, br#"{"type":"service_account","private_key":"fixture-private"}"#).unwrap();
    let adc_rejected = NativeChainInspector::vertex_adc(&adc);
    assert!(!adc_rejected.available);
    assert_eq!(adc_rejected.finite_reason, AvailabilityReason::NativeSourceDisallowed);

    fs::write(
        &adc,
        br#"{"type":"authorized_user","client_id":"fixture-client","client_secret":"fixture-secret","refresh_token":"fixture-refresh","token_uri":"http://127.0.0.1/token"}"#,
    )
    .unwrap();
    let endpoint_override = NativeChainInspector::vertex_adc(&adc);
    assert!(!endpoint_override.available);
    assert_eq!(
        endpoint_override.finite_reason,
        AvailabilityReason::NativeSourceDisallowed
    );
}

#[derive(Debug)]
struct SyntheticAdapter {
    calls: Arc<Mutex<Vec<String>>>,
    delay: Duration,
}

#[async_trait]
impl SdkAdapter for SyntheticAdapter {
    fn protocol(&self) -> ProviderProtocol {
        ProviderProtocol::AwsBedrockConverse
    }

    async fn execute(&self, call: &ProtocolCall) -> SafeProviderResult {
        tokio::time::sleep(self.delay).await;
        self.calls.lock().unwrap().push(call.model_id().to_owned());
        SafeProviderResult::success("response-1", "model-safe", Some(3), Some(5))
    }
}

#[tokio::test]
async fn transport_requires_the_committed_binding_and_cancels_without_raw_output() {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let fields = json!({"region":"us-east-1","profile_name":"work"});
    let transport = CoreProviderTransport::bind_test_fixture(
        "opaque-handle-1",
        7,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-test",
        fields.clone(),
        Arc::new(SyntheticAdapter {
            calls: calls.clone(),
            delay: Duration::from_millis(200),
        }),
    )
    .unwrap();

    let mismatched = ProtocolCall::with_physical_model_request_budget(
        "opaque-handle-1",
        8,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-test",
        fields.clone(),
        BoundedProviderRequest::new("secret prompt", 32).unwrap(),
        1,
    );
    assert_eq!(
        transport
            .execute(mismatched, &BoundedCancellation::new())
            .await
            .error_code,
        Some("configuration_changed")
    );

    let cancel = BoundedCancellation::new();
    cancel.cancel();
    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-handle-1",
        7,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-test",
        fields,
        BoundedProviderRequest::new("secret prompt", 32).unwrap(),
        1,
    );
    let debug_call = format!("{call:?}");
    assert!(!debug_call.contains("secret prompt"));
    assert!(!debug_call.contains("opaque-handle-1"));
    assert!(!format!("{transport:?}").contains("opaque-handle-1"));
    let cancelled = transport.execute(call, &cancel).await;
    assert_eq!(cancelled.error_code, Some("cancelled"));
    assert!(calls.lock().unwrap().is_empty());
    let serialized = serde_json::to_string(&cancelled).unwrap();
    assert!(!serialized.contains("secret prompt"));
    assert!(!serialized.contains("opaque-handle-1"));
}

#[test]
fn vertex_rejects_symlink_and_broad_file_permissions() {
    use std::os::unix::fs::symlink;
    let tmp = TestDir::new("vertex-metadata");
    let adc = tmp.path("adc.json");
    fs::write(&adc, br#"{"type":"authorized_user"}"#).unwrap();
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o644)).unwrap();
    assert_eq!(
        NativeChainInspector::vertex_adc(&adc).finite_reason,
        AvailabilityReason::NativeSourceDisallowed
    );
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o600)).unwrap();
    let link = tmp.path("adc-link.json");
    symlink(Path::new(&adc), &link).unwrap();
    assert_eq!(
        NativeChainInspector::vertex_adc(&link).finite_reason,
        AvailabilityReason::NativeSourceDisallowed
    );
    fs::write(&adc, vec![b'x'; 65_537]).unwrap();
    assert_eq!(
        NativeChainInspector::vertex_adc(&adc).finite_reason,
        AvailabilityReason::NativeSourceDisallowed
    );
}

fn native_binding(provider_id: &str) -> StableBinding {
    StableBinding {
        version: 1,
        operation_id: "a".repeat(64),
        owner_id: 7,
        connection_id: 9,
        kind: "grant_replace".into(),
        intent_fingerprint: "b".repeat(64),
        before_state_fingerprint: "c".repeat(64),
        revoke_requested: false,
        expires_at: 2_000_000_000,
        owner_epoch: "d".repeat(64),
        provider_id: provider_id.into(),
        auth_method_id: "native_chain".into(),
        config_revision: 3,
        auth_grant_revision: 4,
        public_config_fingerprint: "e".repeat(64),
    }
}

#[test]
fn native_enrollment_snapshots_only_selected_aws_chain_and_vertex_fields() {
    let tmp = TestDir::new("native-enrollment-snapshot");
    let azure = tmp.path("az");
    let config = tmp.path("config");
    let credentials = tmp.path("credentials");
    let sso_cache = tmp.path("sso-cache");
    let adc = tmp.path("adc.json");
    fs::create_dir(&sso_cache).unwrap();
    fs::set_permissions(&sso_cache, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(&azure, "#!/bin/sh\nexit 1\n").unwrap();
    fs::set_permissions(&azure, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(
        &config,
        "[profile work]\nrole_arn=arn:aws:iam::123456789012:role/fixture\nsource_profile=leaf\nregion=us-east-1\n\n[profile ignored]\ncredential_process=/tmp/must-not-run\n",
    ).unwrap();
    fs::write(
        &credentials,
        "[leaf]\naws_access_key_id=fixture-access\naws_secret_access_key=fixture-secret\n",
    ).unwrap();
    fs::write(
        &adc,
        br#"{"type":"authorized_user","client_id":"fixture-client","client_secret":"fixture-secret","refresh_token":"fixture-refresh","quota_project_id":"example-project","ignored":"must-not-persist"}"#,
    ).unwrap();
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o600)).unwrap();
    let mut transport = NativeChainEnrollmentTransport::from_paths(
        azure, config.clone(), credentials.clone(), sso_cache.clone(), adc.clone(),
    );

    let aws = transport.start(
        &native_binding("amazon-bedrock"),
        Some(&NativeEnrollmentSpecV1::AwsProfile {
            profile_name: "work".into(), region: "us-east-1".into(),
        }),
    );
    fs::write(&credentials, "[work]\ncredential_process=/tmp/mutated-marker\n").unwrap();
    let TransportResult::NativeReady { canonical_payload, .. } = aws else {
        panic!("selected AWS chain must be enrolled");
    };
    let aws_text = String::from_utf8(canonical_payload.to_vec()).unwrap();
    let aws_json: Value = serde_json::from_str(&aws_text).unwrap();
    assert_eq!(aws_json["config_path"], config.to_str().unwrap());
    assert_eq!(aws_json["credentials_path"], credentials.to_str().unwrap());
    assert!(aws_text.contains("fixture-access"));
    assert!(aws_text.contains("fixture-secret"));
    assert!(!aws_text.contains("must-not-run"));
    assert!(!aws_text.contains("mutated-marker"));

    fs::write(
        &config,
        "[profile work]\nsso_start_url=https://example.awsapps.com/start\nsso_region=us-east-1\nsso_account_id=123456789012\nsso_role_name=ReadOnly\n",
    ).unwrap();
    fs::write(&credentials, "\n").unwrap();
    let cache_path = sso_cache.join("e8be5486177c5b5392bd9aa76563515b29358e6e.json");
    fs::write(
        &cache_path,
        r#"{"accessToken":"fixture-sso-access","expiresAt":"2099-01-01T00:00:00Z","startUrl":"https://example.awsapps.com/start","refreshToken":"must-not-persist"}"#,
    ).unwrap();
    fs::set_permissions(&cache_path, fs::Permissions::from_mode(0o600)).unwrap();
    let legacy_sso = transport.start(
        &native_binding("amazon-bedrock"),
        Some(&NativeEnrollmentSpecV1::AwsProfile {
            profile_name: "work".into(), region: "us-east-1".into(),
        }),
    );
    let TransportResult::NativeReady { canonical_payload, .. } = legacy_sso else {
        panic!("exact selected legacy SSO cache must be enrolled");
    };
    let legacy_text = String::from_utf8(canonical_payload.to_vec()).unwrap();
    assert!(legacy_text.contains("fixture-sso-access"));
    assert!(!legacy_text.contains("must-not-persist"));

    let vertex = transport.start(
        &native_binding("google-vertex"),
        Some(&NativeEnrollmentSpecV1::VertexAdc {
            project_id: "example-project".into(), location: "us-central1".into(),
        }),
    );
    fs::write(&adc, br#"{"type":"external_account","marker":"mutated"}"#).unwrap();
    let TransportResult::NativeReady { canonical_payload, .. } = vertex else {
        panic!("selected Vertex ADC must be enrolled");
    };
    let vertex_text = String::from_utf8(canonical_payload.to_vec()).unwrap();
    let vertex_json: Value = serde_json::from_str(&vertex_text).unwrap();
    assert_eq!(vertex_json["document_path"], adc.to_str().unwrap());
    assert!(vertex_text.contains("fixture-refresh"));
    assert!(!vertex_text.contains("must-not-persist"));
    assert!(!vertex_text.contains("mutated"));
}

fn write_legacy_sso_fixture(tmp: &TestDir) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    let config = tmp.path("config");
    let credentials = tmp.path("credentials");
    let cache = tmp.path("sso-cache");
    fs::create_dir(&cache).unwrap();
    fs::set_permissions(&cache, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(
        &config,
        "[profile work]\nrole_arn=arn:aws:iam::123456789012:role/Outer\nsource_profile=leaf\nexternal_id=fixture-external\nrole_session_name=gamja-test\nduration_seconds=900\n[profile leaf]\nsso_start_url=https://example.awsapps.com/start\nsso_region=us-west-2\nsso_account_id=123456789012\nsso_role_name=ReadOnly\n",
    ).unwrap();
    fs::write(&credentials, "\n").unwrap();
    let cache_file = cache.join("e8be5486177c5b5392bd9aa76563515b29358e6e.json");
    fs::write(
        &cache_file,
        r#"{"accessToken":"fixture-sso-access","expiresAt":"2099-01-01T00:00:00Z","startUrl":"https://example.awsapps.com/start","refreshToken":"must-not-persist","clientSecret":"must-not-persist"}"#,
    ).unwrap();
    fs::set_permissions(&cache_file, fs::Permissions::from_mode(0o600)).unwrap();
    (config, credentials, cache, cache_file)
}

fn enrolled_fingerprint(result: TransportResult) -> Option<(String, Vec<u8>)> {
    match result {
        TransportResult::NativeReady {
            source_generation_fingerprint,
            canonical_payload,
        } => Some((source_generation_fingerprint, canonical_payload.to_vec())),
        _ => None,
    }
}

#[test]
fn legacy_sso_cache_is_exact_bounded_private_and_three_source_bound() {
    let tmp = TestDir::new("legacy-sso-cache-contract");
    let azure = tmp.path("az");
    let adc = tmp.path("adc.json");
    fs::write(&azure, "#!/bin/sh\nexit 1\n").unwrap();
    fs::set_permissions(&azure, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(&adc, r#"{"type":"authorized_user"}"#).unwrap();
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o600)).unwrap();
    let (config, credentials, cache, cache_file) = write_legacy_sso_fixture(&tmp);
    let spec = NativeEnrollmentSpecV1::AwsProfile {
        profile_name: "work".into(),
        region: "us-east-1".into(),
    };
    let make_transport = || NativeChainEnrollmentTransport::from_paths_with_test_time(
        azure.clone(), config.clone(), credentials.clone(), cache.clone(), adc.clone(),
        4_070_908_799,
    );
    let mut transport = make_transport();
    let (initial, payload) = enrolled_fingerprint(
        transport.start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).expect("valid exact cache must enroll");
    assert!(payload.len() <= 32_768);
    let text = String::from_utf8(payload).unwrap();
    assert!(text.contains("fixture-sso-access"));
    assert!(text.contains("fixture-external"));
    assert!(!text.contains("must-not-persist"));

    fs::write(&config, format!("{}\n", fs::read_to_string(&config).unwrap())).unwrap();
    let config_drift = enrolled_fingerprint(
        make_transport().start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).unwrap().0;
    assert_ne!(initial, config_drift);
    fs::write(&credentials, "\n\n").unwrap();
    let credential_drift = enrolled_fingerprint(
        make_transport().start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).unwrap().0;
    assert_ne!(config_drift, credential_drift);
    let original_cache = fs::read_to_string(&cache_file).unwrap();
    fs::write(&cache_file, original_cache.replace("fixture-sso-access", "fixture-sso-change")).unwrap();
    let cache_drift = enrolled_fingerprint(
        make_transport().start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).unwrap().0;
    assert_ne!(credential_drift, cache_drift);

    fs::set_permissions(&cache_file, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(enrolled_fingerprint(
        make_transport().start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).is_none());
    fs::set_permissions(&cache_file, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&cache_file, format!(
        "{{\"accessToken\":\"{}\",\"expiresAt\":\"2099-01-01T00:00:00Z\",\"startUrl\":\"https://example.awsapps.com/start\"}}",
        "x".repeat(32_000),
    )).unwrap();
    assert!(enrolled_fingerprint(
        make_transport().start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).is_none(), "whole committed value must remain within 32 KiB");

    fs::write(&cache_file, r#"{"accessToken":"fixture","expiresAt":"2099-01-01T00:00:00Z","startUrl":"https://other.awsapps.com/start"}"#).unwrap();
    assert!(enrolled_fingerprint(
        make_transport().start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).is_none());
    let symlink_target = tmp.path("cache-target.json");
    fs::write(&symlink_target, r#"{"accessToken":"fixture","expiresAt":"2099-01-01T00:00:00Z","startUrl":"https://example.awsapps.com/start"}"#).unwrap();
    fs::set_permissions(&symlink_target, fs::Permissions::from_mode(0o600)).unwrap();
    fs::remove_file(&cache_file).unwrap();
    std::os::unix::fs::symlink(&symlink_target, &cache_file).unwrap();
    assert!(enrolled_fingerprint(
        make_transport().start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).is_none());
    fs::remove_file(&cache_file).unwrap();
    fs::write(&cache_file, r#"{"accessToken":"fixture","expiresAt":"2099-01-01T00:00:00Z","startUrl":"https://example.awsapps.com/start"}"#).unwrap();
    fs::set_permissions(&cache_file, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&cache_file, r#"{"accessToken":"fixture","expiresAt":"not-a-time","startUrl":"https://example.awsapps.com/start"}"#).unwrap();
    assert!(enrolled_fingerprint(
        make_transport().start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).is_none(), "malformed expiry must fail after the token guard owns the selected token");
    fs::write(&cache_file, r#"{"accessToken":"fixture","expiresAt":"2099-01-01T00:00:00Z","startUrl":"https://example.awsapps.com/start"}"#).unwrap();
    let mut expired = NativeChainEnrollmentTransport::from_paths_with_test_time(
        azure, config, credentials, cache, adc, 4_070_908_800,
    );
    assert!(enrolled_fingerprint(
        expired.start(&native_binding("amazon-bedrock"), Some(&spec)),
    ).is_none(), "expiry equal to current time must fail closed");
}

#[tokio::test]
async fn legacy_sso_uses_only_selected_token_then_exact_sts_graph_and_bedrock() {
    let _guard = ENV_LOCK.get_or_init(|| Mutex::new(())).lock().unwrap();
    let _access = EnvRestore::set("AWS_ACCESS_KEY_ID", "AMBIENTMUSTNOTBEUSED");
    let _profile = EnvRestore::set("AWS_PROFILE", "ambient-must-not-be-used");
    let _endpoint = EnvRestore::set("AWS_ENDPOINT_URL", "http://127.0.0.1:1/ambient");
    let server = AwsLegacyLoopback::start(None);
    let tmp = TestDir::new("legacy-sso-explicit-flow");
    let (config, credentials, cache, _) = write_legacy_sso_fixture(&tmp);
    let adapter = AwsLegacySsoSdkAdapter::from_profile_files_with_test_endpoints(
        &config, &credentials, &cache, "work", "us-east-1",
        &server.endpoint, &server.endpoint, &server.endpoint,
    ).unwrap();
    assert!(format!("{adapter:?}").contains("sso_region: \"us-west-2\""));
    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-legacy-sso",
        1,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-test",
        json!({"profile_name":"work","region":"us-east-1"}),
        BoundedProviderRequest::new("hello", 16).unwrap(),
        1,
    );
    let result = adapter.execute(&call).await;
    assert!(result.ok, "{result:?}");
    assert_eq!(result.text, "safe");
    let observations = server.observations.lock().unwrap().clone();
    assert_eq!(observations.iter().map(|value| value.stage).collect::<Vec<_>>(),
        vec!["sso", "sts", "bedrock"]);
    let sso = observations[0].request.to_ascii_lowercase();
    assert!(sso.starts_with("get /federation/credentials?"));
    assert!(sso.contains("account_id=123456789012"));
    assert!(sso.contains("role_name=readonly"));
    assert!(sso.contains("x-amz-sso_bearer_token: fixture-sso-access"));
    assert!(!sso.contains("authorization:"), "GetRoleCredentials is unsigned");
    let sts = &observations[1].request;
    assert!(sts.contains("RoleArn=arn%3Aaws%3Aiam%3A%3A123456789012%3Arole%2FOuter"));
    assert!(sts.contains("ExternalId=fixture-external"));
    assert!(sts.contains("RoleSessionName=gamja-test"));
    assert!(sts.contains("DurationSeconds=900"));
    assert!(sts.contains("/us-east-1/sts/aws4_request"));
    let bedrock = &observations[2].request;
    assert!(bedrock.starts_with("POST /model/anthropic.claude-test/converse HTTP/1.1"));
    assert!(bedrock.contains("Credential=STSFINAL/"));
    assert!(bedrock.contains("/us-east-1/bedrock/aws4_request"));
    let all = observations.iter().map(|value| value.request.as_str()).collect::<String>();
    assert!(!all.contains("AMBIENTMUSTNOTBEUSED"));
    assert!(!all.contains("ambient-must-not-be-used"));
}

#[tokio::test]
async fn legacy_sso_cancel_is_terminal_at_each_sdk_stage_without_followup() {
    for (delayed_stage, expected_count) in [("sso", 1usize), ("sts", 2), ("bedrock", 3)] {
        let server = AwsLegacyLoopback::start(Some(delayed_stage));
        let tmp = TestDir::new(&format!("legacy-sso-cancel-{delayed_stage}"));
        let (config, credentials, cache, _) = write_legacy_sso_fixture(&tmp);
        let adapter = AwsLegacySsoSdkAdapter::from_profile_files_with_test_endpoints(
            &config, &credentials, &cache, "work", "us-east-1",
            &server.endpoint, &server.endpoint, &server.endpoint,
        ).unwrap();
        let transport = CoreProviderTransport::bind_test_fixture(
            "opaque-legacy-cancel",
            1,
            ProviderProtocol::AwsBedrockConverse,
            "anthropic.claude-test",
            json!({"profile_name":"work","region":"us-east-1"}),
            Arc::new(adapter),
        ).unwrap();
        let call = ProtocolCall::with_physical_model_request_budget(
            "opaque-legacy-cancel",
            1,
            ProviderProtocol::AwsBedrockConverse,
            "anthropic.claude-test",
            json!({"profile_name":"work","region":"us-east-1"}),
            BoundedProviderRequest::new("hello", 16).unwrap(),
            1,
        );
        let cancel = BoundedCancellation::new();
        let observations = server.observations.clone();
        let driver_cancel = cancel.clone();
        let driver = tokio::spawn(async move {
            for _ in 0..200 {
                if observations.lock().unwrap().len() >= expected_count { break; }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            driver_cancel.cancel();
        });
        let result = transport.execute(call, &cancel).await;
        driver.await.unwrap();
        assert_eq!(result.error_code, Some("cancelled"), "stage={delayed_stage}");
        drop(transport);
        tokio::time::sleep(Duration::from_millis(320)).await;
        let stages = server.observations.lock().unwrap().iter()
            .map(|value| value.stage).collect::<Vec<_>>();
        assert_eq!(stages.len(), expected_count, "stage={delayed_stage} stages={stages:?}");
        assert_eq!(stages.last().copied(), Some(delayed_stage));
    }
}

#[tokio::test]
async fn sdk_clients_are_bound_to_explicit_local_sources_without_resolving_them() {
    let tmp = TestDir::new("sdk-client-lifetime");
    let config = tmp.path("config");
    let credentials = tmp.path("credentials");
    fs::write(&config, "[profile work]\nregion=us-east-1\n").unwrap();
    fs::write(
        &credentials,
        "[work]\naws_access_key_id=fixture-access\naws_secret_access_key=fixture-secret\n",
    )
    .unwrap();
    let aws = AwsBedrockSdkAdapter::from_profile_files(
        &config,
        &credentials,
        "work",
        "us-east-1",
    )
    .unwrap();
    assert_eq!(aws.signing_name(), "bedrock");
    assert_eq!(aws.region(), "us-east-1");
    assert!(!format!("{aws:?}").contains("fixture-secret"));

    let adc = tmp.path("application_default_credentials.json");
    fs::write(
        &adc,
        br#"{"type":"authorized_user","client_id":"fixture-client","client_secret":"fixture-secret","refresh_token":"fixture-refresh","quota_project_id":"example-project"}"#,
    )
    .unwrap();
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o600)).unwrap();
    let vertex = VertexSdkAdapter::from_adc_file(
        &adc,
        "example-project",
        "us-central1",
        "gemini-test",
    )
    .await
    .unwrap();
    assert_eq!(
        vertex.endpoint(),
        "https://us-central1-aiplatform.googleapis.com"
    );
    assert!(!format!("{vertex:?}").contains("fixture-refresh"));
}

#[tokio::test]
async fn pinned_bedrock_sdk_signs_the_exact_converse_endpoint() {
    let model_id = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0";
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let captured = Arc::new(Mutex::new(String::new()));
    let captured_server = captured.clone();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut bytes = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            let count = stream.read(&mut chunk).unwrap_or(0);
            if count == 0 {
                break;
            }
            bytes.extend_from_slice(&chunk[..count]);
            let text = String::from_utf8_lossy(&bytes);
            if let Some(split) = text.find("\r\n\r\n") {
                let content_length = text[..split]
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length:")
                            .and_then(|value| value.trim().parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                if bytes.len() >= split + 4 + content_length {
                    break;
                }
            }
        }
        *captured_server.lock().unwrap() = String::from_utf8_lossy(&bytes).into_owned();
        let body = r#"{"output":{"message":{"role":"assistant","content":[{"text":"safe"},{"toolUse":{"toolUseId":"call-1","name":"gamja_62726f77736572","input":{"url":"https://example.test"}}}]}},"stopReason":"tool_use","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2},"metrics":{"latencyMs":1}}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nx-amzn-requestid: fixture-request\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });

    let tmp = TestDir::new("bedrock-request");
    let config = tmp.path("config");
    let credentials = tmp.path("credentials");
    fs::write(&config, "[profile work]\nregion=us-east-1\n").unwrap();
    fs::write(
        &credentials,
        "[work]\naws_access_key_id=fixture-access\naws_secret_access_key=fixture-secret\n",
    )
    .unwrap();
    let adapter = AwsBedrockSdkAdapter::from_profile_files_with_test_endpoint(
        &config,
        &credentials,
        "work",
        "us-east-1",
        &format!("http://{address}"),
    )
    .unwrap();
    let mut contract = cloud_contract_wire("aws_bedrock_converse", "aws", model_id);
    contract["scope"]["descriptor_revision"] = json!(1);
    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-bedrock",
        1,
        ProviderProtocol::AwsBedrockConverse,
        model_id,
        json!({"profile_name":"work","region":"us-east-1"}),
        BoundedProviderRequest::from_wire_value(contract).unwrap(),
        1,
    );
    let result = adapter.execute(&call).await;
    assert!(result.ok, "{result:?}");
    assert_eq!(result.text, "safe");
    assert_eq!(result.tool_calls[0].call_id, "call-1");
    assert!(result.continuation.is_some());
    server.join().unwrap();

    let request = captured.lock().unwrap().clone();
    assert!(request.starts_with(
        "POST /model/arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Ainference-profile%2Fus.anthropic.claude-3-5-sonnet-20241022-v2%3A0/converse HTTP/1.1\r\n"
    ));
    let lower = request.to_ascii_lowercase();
    assert!(lower.contains("authorization: aws4-hmac-sha256"));
    assert!(request.contains("/us-east-1/bedrock/aws4_request"));
    assert!(request.contains("System rule"));
    assert!(request.contains("gamja_62726f77736572"));
    assert!(!request.contains("fixture-secret"));
}

#[tokio::test]
async fn bedrock_uses_the_inspected_immutable_profile_snapshot() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let captured = Arc::new(Mutex::new(String::new()));
    let captured_server = captured.clone();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut bytes = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            let count = stream.read(&mut chunk).unwrap_or(0);
            if count == 0 {
                break;
            }
            bytes.extend_from_slice(&chunk[..count]);
            let text = String::from_utf8_lossy(&bytes);
            if let Some(split) = text.find("\r\n\r\n") {
                let content_length = text[..split]
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length:")
                            .and_then(|value| value.trim().parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                if bytes.len() >= split + 4 + content_length {
                    break;
                }
            }
        }
        *captured_server.lock().unwrap() = String::from_utf8_lossy(&bytes).into_owned();
        let body = r#"{"output":{"message":{"role":"assistant","content":[{"text":"safe"}]}},"stopReason":"end_turn","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2},"metrics":{"latencyMs":1}}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nx-amzn-requestid: fixture-request\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });

    let tmp = TestDir::new("bedrock-profile-snapshot");
    let config = tmp.path("config");
    let credentials = tmp.path("credentials");
    fs::write(&config, "[profile work]\nregion=us-east-1\n").unwrap();
    fs::write(
        &credentials,
        "[work]\naws_access_key_id=OLDINSPECTED\naws_secret_access_key=old-fixture-secret\n",
    )
    .unwrap();
    let adapter = AwsBedrockSdkAdapter::from_profile_files_with_test_endpoint(
        &config,
        &credentials,
        "work",
        "us-east-1",
        &format!("http://{address}"),
    )
    .unwrap();
    fs::write(
        &credentials,
        "[work]\naws_access_key_id=NEWAFTERCHECK\naws_secret_access_key=new-fixture-secret\n",
    )
    .unwrap();

    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-bedrock-snapshot",
        1,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-test",
        json!({"profile_name":"work","region":"us-east-1"}),
        BoundedProviderRequest::new("hello", 16).unwrap(),
        1,
    );
    assert!(adapter.execute(&call).await.ok);
    server.join().unwrap();

    let request = captured.lock().unwrap().clone();
    assert!(request.contains("Credential=OLDINSPECTED/"), "{request}");
    assert!(!request.contains("NEWAFTERCHECK"), "{request}");
}

#[tokio::test]
async fn bedrock_counts_each_retry_attempt_and_prevents_attempt_above_budget() {
    for (budget, expected_ok, expected_code, expected_attempts) in [
        (2, true, None, 2),
        (1, false, Some("provider_request_budget_exhausted"), 1),
    ] {
        let server = BedrockRetryLoopback::start();
        let tmp = TestDir::new(&format!("bedrock-meter-{budget}"));
        let config = tmp.path("config");
        let credentials = tmp.path("credentials");
        fs::write(&config, "[profile work]\nregion=us-east-1\n").unwrap();
        fs::write(&credentials,
            "[work]\naws_access_key_id=fixture-access\naws_secret_access_key=fixture-secret\n")
            .unwrap();
        let adapter = AwsBedrockSdkAdapter::from_profile_files_with_test_endpoint(
            &config, &credentials, "work", "us-east-1", &server.endpoint,
        ).unwrap();
        let fields = json!({"profile_name":"work","region":"us-east-1"});
        let transport = CoreProviderTransport::bind_test_fixture(
            &format!("opaque-bedrock-meter-{budget}"),
            1,
            ProviderProtocol::AwsBedrockConverse,
            "anthropic.claude-test",
            fields.clone(),
            Arc::new(adapter),
        ).unwrap();
        let call = ProtocolCall::with_physical_model_request_budget(
            &format!("opaque-bedrock-meter-{budget}"),
            1,
            ProviderProtocol::AwsBedrockConverse,
            "anthropic.claude-test",
            fields,
            BoundedProviderRequest::new("hello", 16).unwrap(),
            budget,
        );
        let result = transport.execute(call, &BoundedCancellation::new()).await;
        assert_eq!(result.ok, expected_ok, "budget={budget} result={result:?}");
        assert_eq!(result.error_code, expected_code, "budget={budget}");
        assert_eq!(result.physical_model_request_count, expected_attempts);
        assert_eq!(server.attempts.load(Ordering::Acquire), expected_attempts);
    }
}

#[tokio::test]
async fn bedrock_never_resolves_metadata_after_the_inspected_profile_changes() {
    let _guard = ENV_LOCK.get_or_init(|| Mutex::new(())).lock().unwrap();
    let metadata = LoopbackProvider::start();
    let _endpoint = EnvRestore::set("AWS_EC2_METADATA_SERVICE_ENDPOINT", &metadata.endpoint);

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut bytes = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            let count = stream.read(&mut chunk).unwrap_or(0);
            if count == 0 {
                break;
            }
            bytes.extend_from_slice(&chunk[..count]);
            if String::from_utf8_lossy(&bytes).contains("\r\n\r\n") {
                break;
            }
        }
        let body = r#"{"output":{"message":{"role":"assistant","content":[{"text":"safe"}]}},"stopReason":"end_turn","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2},"metrics":{"latencyMs":1}}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nx-amzn-requestid: fixture-request\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .unwrap();
    });

    let tmp = TestDir::new("bedrock-no-metadata-race");
    let config = tmp.path("config");
    let credentials = tmp.path("credentials");
    fs::write(&config, "[profile work]\nregion=us-east-1\n").unwrap();
    fs::write(
        &credentials,
        "[work]\naws_access_key_id=OLDINSPECTED\naws_secret_access_key=old-fixture-secret\n",
    )
    .unwrap();
    let adapter = AwsBedrockSdkAdapter::from_profile_files_with_test_endpoint(
        &config,
        &credentials,
        "work",
        "us-east-1",
        &format!("http://{address}"),
    )
    .unwrap();
    fs::write(
        &config,
        "[profile work]\nrole_arn=arn:aws:iam::123456789012:role/Work\ncredential_source=Ec2InstanceMetadata\nregion=us-east-1\n",
    )
    .unwrap();
    fs::write(&credentials, "").unwrap();

    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-bedrock-metadata",
        1,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-test",
        json!({"profile_name":"work","region":"us-east-1"}),
        BoundedProviderRequest::new("hello", 16).unwrap(),
        1,
    );
    let result = adapter.execute(&call).await;
    assert!(
        result.ok,
        "result={result:?} metadata={:?}",
        metadata.observations.lock().unwrap()
    );
    server.join().unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(metadata.observations.lock().unwrap().is_empty());
}

#[tokio::test]
async fn bounded_azure_executor_rejects_argv_drift_and_kills_then_waits_on_deadline() {
    let _guard = ENV_LOCK.get_or_init(|| Mutex::new(())).lock().unwrap();
    let executor = BoundedAzureCliExecutor::with_deadline(Duration::from_millis(300)).unwrap();
    let bad = executor
        .run(
            OsStr::new("/bin/sh"),
            &[OsStr::new("-c"), OsStr::new("az account show")],
        )
        .await
        .unwrap_err();
    assert_eq!(bad.kind(), io::ErrorKind::PermissionDenied);

    let tmp = TestDir::new("fake-az");
    let fake_az = tmp.path("az");
    fs::write(
        &fake_az,
        "#!/bin/sh\n/bin/sleep 30\n",
    )
    .unwrap();
    fs::set_permissions(&fake_az, fs::Permissions::from_mode(0o700)).unwrap();
    let fake_executor = BoundedAzureCliExecutor::with_test_program(
        Duration::from_millis(300),
        &fake_az,
    )
    .unwrap();
    let result = fake_executor
        .run(
            OsStr::new("/bin/sh"),
            &[
                OsStr::new("-c"),
                OsStr::new("cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default"),
            ],
    )
    .await;
    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::TimedOut);

    let pid_file = tmp.path("cancelled.pid");
    fs::write(
        &fake_az,
        format!("#!/bin/sh\necho $$ > {}\n/bin/sleep 30\n", pid_file.display()),
    )
    .unwrap();
    let cancel_executor = Arc::new(
        BoundedAzureCliExecutor::with_test_program(Duration::from_secs(5), &fake_az).unwrap(),
    );
    let running = {
        let executor = cancel_executor.clone();
        tokio::spawn(async move {
            executor
                .run(
                    OsStr::new("/bin/sh"),
                    &[
                        OsStr::new("-c"),
                        OsStr::new("cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default"),
                    ],
                )
                .await
        })
    };
    let mut published_pid = None;
    for _ in 0..100 {
        // Opening the shell redirection creates an empty file before echo writes
        // its PID. Observe complete publication, not just filesystem existence.
        if let Ok(contents) = fs::read_to_string(&pid_file) {
            if let Ok(pid) = contents.trim().parse::<i32>() {
                if pid > 1 { published_pid = Some(pid); break; }
            }
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let pid = published_pid.expect("Azure fixture did not publish its owned PID");
    running.abort();
    let _ = running.await;
    let mut reaped = false;
    for _ in 0..100 {
        let alive = unsafe { libc::kill(pid, 0) } == 0;
        if !alive {
            reaped = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(reaped, "cancelled Azure CLI process was not killed and reaped");

    let descendant_pid_file = tmp.path("descendant.pid");
    fs::write(
        &fake_az,
        format!(
            "#!/usr/bin/python3\nimport os, time\nchild = os.fork()\nif child == 0:\n    with open({:?}, 'w') as output:\n        output.write(str(os.getpid()))\n    time.sleep(2)\n    os._exit(0)\nos._exit(0)\n",
            descendant_pid_file.display().to_string()
        ),
    )
    .unwrap();
    let fork_executor = BoundedAzureCliExecutor::with_test_program(
        Duration::from_millis(500),
        &fake_az,
    )
    .unwrap();
    let started = std::time::Instant::now();
    let fork_result = fork_executor
        .run(
            OsStr::new("/bin/sh"),
            &[
                OsStr::new("-c"),
                OsStr::new("cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default"),
            ],
        )
        .await;
    assert_eq!(fork_result.unwrap_err().kind(), io::ErrorKind::TimedOut);
    assert!(started.elapsed() < Duration::from_millis(1500));
    let descendant_pid: i32 = fs::read_to_string(&descendant_pid_file)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    let mut descendant_gone = false;
    for _ in 0..100 {
        if unsafe { libc::kill(descendant_pid, 0) } != 0 {
            descendant_gone = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(descendant_gone, "pipe-holding descendant was not killed");

    fs::write(
        &fake_az,
        "#!/bin/sh\nprintf '%s' '{\"accessToken\":\"fixture-only\",\"expires_on\":2147483647}'\n",
    )
    .unwrap();
    let success_executor = BoundedAzureCliExecutor::with_test_program(
        Duration::from_secs(1),
        &fake_az,
    )
    .unwrap();
    let success = success_executor
        .run(
            OsStr::new("/bin/sh"),
            &[
                OsStr::new("-c"),
                OsStr::new("cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default"),
            ],
        )
        .await
        .unwrap();
    assert!(success.status.success());
    assert_eq!(
        success.stdout,
        br#"{"accessToken":"fixture-only","expires_on":2147483647}"#
    );
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[tokio::test]
async fn bounded_azure_executor_closes_ambient_read_write_socket_fds() {
    use std::os::{fd::{AsRawFd, FromRawFd, OwnedFd}, unix::net::UnixStream};
    let _guard = ENV_LOCK.get_or_init(|| Mutex::new(())).lock().unwrap();
    let tmp=TestDir::new("azure-fd-boundary");
    let program=tmp.path("az");
    fs::write(&program,"#!/usr/bin/python3\nimport os,json\nclosed=[]\nfor s in os.environ['GAMJA_TEST_AZURE_EXCLUDED'].split(','):\n try: os.fstat(int(s)); closed.append(False)\n except OSError as e: closed.append(e.errno==9)\nprint(json.dumps(closed))\n").unwrap();
    fs::set_permissions(&program,fs::Permissions::from_mode(0o700)).unwrap();
    let mut pipe=[-1;2];assert_eq!(unsafe {libc::pipe(pipe.as_mut_ptr())},0);
    let input=unsafe {OwnedFd::from_raw_fd(pipe[0])};let output=unsafe {OwnedFd::from_raw_fd(pipe[1])};
    let (socket,_peer)=UnixStream::pair().unwrap();
    let sentinels:Vec<OwnedFd>=[input.as_raw_fd(),output.as_raw_fd(),socket.as_raw_fd()].into_iter().map(|fd|{
        let duplicate=unsafe {libc::fcntl(fd,libc::F_DUPFD_CLOEXEC,200)};assert!(duplicate>=200);
        let held=unsafe {OwnedFd::from_raw_fd(duplicate)};assert_eq!(unsafe {libc::fcntl(duplicate,libc::F_SETFD,0)},0);held
    }).collect();
    let _env=EnvRestore::set("GAMJA_TEST_AZURE_EXCLUDED",&sentinels.iter().map(|fd|fd.as_raw_fd().to_string()).collect::<Vec<_>>().join(","));
    let executor=BoundedAzureCliExecutor::with_test_program(Duration::from_secs(2),program).unwrap();
    let result=executor.run(OsStr::new("/bin/sh"),&[OsStr::new("-c"),OsStr::new("cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default")]).await.unwrap();
    assert!(result.status.success());
    assert_eq!(serde_json::from_slice::<Value>(&result.stdout).unwrap(),json!([true,true,true]));
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[tokio::test]
async fn bounded_azure_executor_bounds_each_pipe_and_preserves_path_lookup() {
    let _guard = ENV_LOCK.get_or_init(|| Mutex::new(())).lock().unwrap();
    let tmp=TestDir::new("azure-output-bound"); let program=tmp.path("az");
    for stream in ["stdout","stderr"] {
        fs::write(&program,format!("#!/usr/bin/python3\nimport sys,time\nsys.{stream}.write('x'*70000)\nsys.{stream}.flush()\ntime.sleep(30)\n")).unwrap();
        fs::set_permissions(&program,fs::Permissions::from_mode(0o700)).unwrap();
        let executor=BoundedAzureCliExecutor::with_test_program(Duration::from_secs(2),&program).unwrap();
        let start=std::time::Instant::now();
        let result=executor.run(OsStr::new("/bin/sh"),&[OsStr::new("-c"),OsStr::new("cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default")]).await;
        assert_eq!(result.unwrap_err().kind(),io::ErrorKind::InvalidData);
        assert!(start.elapsed()<Duration::from_secs(2));
    }
    fs::write(&program,"#!/bin/sh\nprintf safe\n").unwrap();
    let _path=EnvRestore::set("PATH",&format!("{}:/usr/bin:/bin",tmp.0.display()));
    let executor=BoundedAzureCliExecutor::with_deadline(Duration::from_secs(1)).unwrap();
    let result=executor.run(OsStr::new("/bin/sh"),&[OsStr::new("-c"),OsStr::new("cd /bin && az account get-access-token -o json --scope https://cognitiveservices.azure.com/.default")]).await.unwrap();
    assert!(result.status.success());assert_eq!(result.stdout,b"safe");
}

#[tokio::test]
async fn vertex_uses_bounded_per_request_credentials_without_background_refresh() {
    let provider = LoopbackProvider::start_with_tool_response();
    let tmp = TestDir::new("vertex-bounded-provider");
    let adc = tmp.path("application_default_credentials.json");
    fs::write(
        &adc,
        br#"{"type":"impersonated_service_account","service_account_impersonation_url":"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/runtime@example-project.iam.gserviceaccount.com:generateAccessToken","delegates":["projects/-/serviceAccounts/delegate@example-project.iam.gserviceaccount.com"],"quota_project_id":"billing-project","source_credentials":{"type":"authorized_user","client_id":"fixture-client","client_secret":"fixture-secret","refresh_token":"fixture-refresh"}}"#,
    )
    .unwrap();
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o600)).unwrap();
    let adapter = VertexSdkAdapter::from_adc_file_with_test_endpoints(
        &adc,
        "example-project",
        "us-central1",
        "gemini-test",
        &provider.endpoint,
        &format!("{}/token", provider.endpoint),
        &provider.endpoint,
    )
    .await
    .unwrap();
    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-handle-vertex",
        1,
        ProviderProtocol::VertexGenerateContentV1,
        "gemini-test",
        json!({"project_id":"example-project","location":"us-central1"}),
        BoundedProviderRequest::from_wire_value(cloud_contract_wire(
            "vertex_generate_content_v1", "vertex", "gemini-test",
        )).unwrap(),
        1,
    );
    let result = adapter.execute(&call).await;
    assert!(result.ok, "{result:?}");
    assert_eq!(result.text, "safe text");
    assert_eq!(result.tool_calls[0].name, "gamja_62726f77736572");
    let continuation = serde_json::to_string(&result.continuation).unwrap();
    assert!(continuation.contains("c2ln"));
    assert!(!continuation.contains("thought text"));
    drop(adapter);
    tokio::time::sleep(Duration::from_millis(100)).await;

    let observations = provider.observations.lock().unwrap().clone();
    assert_eq!(observations.len(), 3, "{observations:?}");
    assert_eq!(observations[0].path, "/token");
    assert!(!observations[0].authorization);
    assert!(observations[0].cloud_scope);
    assert_eq!(
        observations[1].path,
        "/v1/projects/-/serviceAccounts/runtime@example-project.iam.gserviceaccount.com:generateAccessToken"
    );
    assert!(observations[1].authorization);
    assert!(observations[1].cloud_scope);
    assert_eq!(
        observations[2].path,
        "/v1/projects/example-project/locations/us-central1/publishers/google/models/gemini-test:generateContent?%24alt=json%3Benum-encoding%3Dint"
    );
    assert!(observations[2].authorization);
    assert!(observations[2].system_and_tools);
    assert_eq!(
        observations[2].x_goog_api_client.as_deref(),
        Some("gl-rust/1.98.1 gax/0.7.18 rest/0.7.18-reqwest gapic/1.16.0")
    );
}

#[tokio::test]
async fn vertex_rejects_not_modified_without_model_http_or_budget_consumption() {
    let provider = LoopbackProvider::start();
    let adapter = VertexSdkAdapter::with_test_credentials(
        "example-project", "us-central1", "gemini-test", &provider.endpoint,
        Credentials::from(NotModifiedTestCredentials),
    ).unwrap();
    let fields = json!({"project_id":"example-project","location":"us-central1"});
    let transport = CoreProviderTransport::bind_test_fixture(
        "opaque-vertex-not-modified", 1, ProviderProtocol::VertexGenerateContentV1,
        "gemini-test", fields.clone(), Arc::new(adapter),
    ).unwrap();
    let result = transport.execute(ProtocolCall::with_physical_model_request_budget(
        "opaque-vertex-not-modified", 1, ProviderProtocol::VertexGenerateContentV1,
        "gemini-test", fields, BoundedProviderRequest::new("hello", 16).unwrap(), 1,
    ), &BoundedCancellation::new()).await;
    assert_eq!(result.error_code, Some("auth_expired_or_revoked"));
    assert_eq!(result.physical_model_request_count, 0);
    assert!(provider.observations.lock().unwrap().is_empty());
}

#[tokio::test]
async fn vertex_never_follows_redirect_and_never_retries_model_post() {
    let provider = LoopbackProvider::start_redirecting();
    let adapter = VertexSdkAdapter::with_test_credentials(
        "example-project", "us-central1", "gemini-test", &provider.endpoint,
        Credentials::from(StaticTestCredentials),
    ).unwrap();
    let fields = json!({"project_id":"example-project","location":"us-central1"});
    let transport = CoreProviderTransport::bind_test_fixture(
        "opaque-vertex-redirect", 1, ProviderProtocol::VertexGenerateContentV1,
        "gemini-test", fields.clone(), Arc::new(adapter),
    ).unwrap();
    let result = transport.execute(ProtocolCall::with_physical_model_request_budget(
        "opaque-vertex-redirect", 1, ProviderProtocol::VertexGenerateContentV1,
        "gemini-test", fields, BoundedProviderRequest::new("hello", 16).unwrap(), 1,
    ), &BoundedCancellation::new()).await;
    assert_eq!(result.error_code, Some("provider_error"));
    assert_eq!(result.physical_model_request_count, 1);
    let observations = provider.observations.lock().unwrap().clone();
    assert_eq!(observations.len(), 1, "redirect or retry produced extra HTTP");
    assert!(!observations[0].path.contains("forbidden-redirect"));
}

#[tokio::test]
async fn vertex_admitted_model_request_cancel_preserves_terminal_count() {
    let provider = LoopbackProvider::start_with_response_delay(Duration::from_millis(250));
    let adapter = VertexSdkAdapter::with_test_credentials(
        "example-project", "us-central1", "gemini-test", &provider.endpoint,
        Credentials::from(StaticTestCredentials),
    ).unwrap();
    let fields = json!({"project_id":"example-project","location":"us-central1"});
    let transport = Arc::new(CoreProviderTransport::bind_test_fixture(
        "opaque-vertex-meter-cancel", 1, ProviderProtocol::VertexGenerateContentV1,
        "gemini-test", fields.clone(), Arc::new(adapter),
    ).unwrap());
    let cancel = BoundedCancellation::new();
    let task_transport = transport.clone();
    let task_cancel = cancel.clone();
    let execution = tokio::spawn(async move {
        task_transport.execute(ProtocolCall::with_physical_model_request_budget(
            "opaque-vertex-meter-cancel", 1, ProviderProtocol::VertexGenerateContentV1,
            "gemini-test", fields, BoundedProviderRequest::new("hello", 16).unwrap(), 1,
        ), &task_cancel).await
    });
    for _ in 0..100 {
        if provider.observations.lock().unwrap().len() == 1 { break; }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(provider.observations.lock().unwrap().len(), 1);
    cancel.cancel();
    let result = execution.await.unwrap();
    assert_eq!(result.error_code, Some("cancelled"));
    assert_eq!(result.physical_model_request_count, 1);
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(provider.observations.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn vertex_cancel_drops_inflight_auth_without_starting_followup_requests() {
    let provider = LoopbackProvider::start_with_token_delay(Duration::from_millis(250));
    let tmp = TestDir::new("vertex-cancel-provider");
    let adc = tmp.path("application_default_credentials.json");
    fs::write(
        &adc,
        br#"{"type":"authorized_user","client_id":"fixture-client","client_secret":"fixture-secret","refresh_token":"fixture-refresh"}"#,
    )
    .unwrap();
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o600)).unwrap();
    let adapter = VertexSdkAdapter::from_adc_file_with_test_endpoints(
        &adc,
        "example-project",
        "us-central1",
        "gemini-test",
        &provider.endpoint,
        &format!("{}/token", provider.endpoint),
        &provider.endpoint,
    )
    .await
    .unwrap();
    let transport = CoreProviderTransport::bind_test_fixture(
        "opaque-handle-vertex-cancel",
        1,
        ProviderProtocol::VertexGenerateContentV1,
        "gemini-test",
        json!({"project_id":"example-project","location":"us-central1"}),
        Arc::new(adapter),
    )
    .unwrap();
    let call = ProtocolCall::with_physical_model_request_budget(
        "opaque-handle-vertex-cancel",
        1,
        ProviderProtocol::VertexGenerateContentV1,
        "gemini-test",
        json!({"project_id":"example-project","location":"us-central1"}),
        BoundedProviderRequest::new("fixture prompt", 16).unwrap(),
        1,
    );
    let cancel = BoundedCancellation::new();
    let cancel_driver = cancel.clone();
    let driver = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(40)).await;
        cancel_driver.cancel();
    });
    let result = transport.execute(call, &cancel).await;
    driver.await.unwrap();
    assert_eq!(
        result.error_code,
        Some("cancelled"),
        "observations={:?}",
        provider.observations.lock().unwrap()
    );
    drop(transport);
    tokio::time::sleep(Duration::from_millis(300)).await;

    let observations = provider.observations.lock().unwrap().clone();
    assert_eq!(observations.len(), 1, "{observations:?}");
    assert_eq!(observations[0].path, "/token");
}

#[test]
fn provider_private_wire_v1_serde_matches_cross_language_golden_vectors() {
    let fingerprint = "a".repeat(64);
    let epoch = "b".repeat(64);
    let public_fingerprint = "c".repeat(64);
    let handle = "d".repeat(64);
    let request = BoundedProviderRequest::new("hello", 32).unwrap();
    let vectors = vec![
        serde_json::to_value(ProviderHandshakeV1::new(fingerprint.clone())).unwrap(),
        serde_json::to_value(ProviderWireRequest::Bind {
            version: 1,
            request_id: 1,
            current_generation_fingerprint: fingerprint,
            binding: ProviderBindingRequest {
                credential_ref: "7:9:00112233445566778899aabbccddeeff".to_owned(),
                owner_id: 7,
                owner_epoch: epoch,
                connection_id: 9,
                config_revision: 3,
                auth_grant_revision: 4,
                provider_id: "azure-openai".to_owned(),
                auth_method_id: "native_chain".to_owned(),
                public_config_fingerprint: public_fingerprint,
                descriptor_revision: 1,
                protocol: ProviderProtocol::AzureOpenAiResponsesV1,
                model_id: "deployment-1".to_owned(),
                public_fields: json!({"resource_endpoint":"https://example.openai.azure.com"}),
            },
        }).unwrap(),
        serde_json::to_value(ProviderWireRequest::Execute {
            version: 1,
            request_id: 2,
            handle: handle.clone(),
            call: ProviderExecuteCallV1 {
                descriptor_revision: 1,
                protocol: ProviderProtocol::AzureOpenAiResponsesV1,
                model_id: "deployment-1".to_owned(),
                public_fields: json!({"resource_endpoint":"https://example.openai.azure.com"}),
                physical_model_request_budget: 1,
                request,
            },
        }).unwrap(),
        serde_json::to_value(ProviderWireRequest::Cancel {
            version: 1,
            request_id: 3,
            handle: handle.clone(),
            target_request_id: 2,
        }).unwrap(),
        serde_json::to_value(ProviderWireRequest::Release {
            version: 1,
            request_id: 4,
            handle: handle.clone(),
        }).unwrap(),
        serde_json::to_value(ProviderWireReply::Bound {
            version: 1,
            request_id: 1,
            handle: handle.clone(),
        }).unwrap(),
        serde_json::to_value(ProviderWireReply::Result {
            version: 1,
            request_id: 2,
            handle: handle.clone(),
            result: SafeProviderResult {
                ok: true,
                error_code: None,
                response_id: Some("response-1".to_owned()),
                model: Some("deployment-1".to_owned()),
                input_tokens: Some(1),
                output_tokens: Some(1),
                physical_model_request_count: 1,
                text: "safe".to_owned(),
                tool_calls: vec![],
                continuation: None,
            },
        }).unwrap(),
        serde_json::to_value(ProviderWireReply::Cancelled {
            version: 1,
            request_id: 3,
            handle: handle.clone(),
            target_request_id: 2,
        }).unwrap(),
        serde_json::to_value(ProviderWireReply::Released {
            version: 1,
            request_id: 4,
            handle: handle,
        }).unwrap(),
        serde_json::to_value(ProviderWireReply::Error {
            version: 1,
            request_id: 5,
            operation: ProviderOperationV1::Bind,
            error_code: "configuration_changed",
        }).unwrap(),
    ];
    for value in &vectors[1..5] {
        let decoded: ProviderWireRequest = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(serde_json::to_value(decoded).unwrap(), *value);
    }
    assert_eq!(vectors[0]["max_frame_bytes"], 14_000_000);
    assert_eq!(vectors[0]["max_in_flight"], 4);
    assert_eq!(vectors[1]["type"], "provider_bind_v1");
    assert_eq!(vectors[2]["call"]["physical_model_request_budget"], 1);
    assert_eq!(vectors[6]["result"]["response_id"], "response-1");
    assert_eq!(vectors[6]["result"]["physical_model_request_count"], 1);

    let mut unknown = vectors[2].clone();
    unknown.as_object_mut().unwrap().insert("extra".into(), json!(true));
    assert!(serde_json::from_value::<ProviderWireRequest>(unknown).is_err());
    for invalid in [json!(true), json!(-1), json!(8)] {
        let mut value = vectors[2].clone();
        value["call"]["physical_model_request_budget"] = invalid;
        assert!(serde_json::from_value::<ProviderWireRequest>(value).is_err());
    }
    let mut missing = vectors[2].clone();
    missing["call"].as_object_mut().unwrap().remove("physical_model_request_budget");
    assert!(serde_json::from_value::<ProviderWireRequest>(missing).is_err());
    assert!(serde_json::from_str::<SafeProviderResult>(
        r#"{"ok":true,"response_id":"r","model":"m","physical_model_request_count":1,"text":"safe","tool_calls":[]}"#,
    ).is_ok());
    for invalid in ["true", "-1", "8"] {
        let wire = format!(
            r#"{{"ok":true,"response_id":"r","model":"m","physical_model_request_count":{invalid},"text":"safe","tool_calls":[]}}"#
        );
        let wire: &'static str = Box::leak(wire.into_boxed_str());
        assert!(serde_json::from_str::<SafeProviderResult>(wire).is_err());
    }
    assert!(serde_json::from_str::<SafeProviderResult>(
        r#"{"ok":true,"response_id":"r","model":"m","text":"safe","tool_calls":[]}"#,
    ).is_err());
    if let Some(path) = std::env::var_os("GAMJA_PROVIDER_GOLDEN_OUT") {
        fs::write(path, serde_json::to_vec_pretty(&vectors).unwrap()).unwrap();
    }
}

#[derive(Default)]
struct EmptyCredentialBackend;

impl CredentialBackend for EmptyCredentialBackend {
    fn get(&mut self, _: &str, _: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        Ok(None)
    }
    fn put(&mut self, _: &str, _: &str, _: &[u8]) -> Result<(), CredentialBackendError> {
        Ok(())
    }
    fn delete(&mut self, _: &str, _: &str) -> Result<(), CredentialBackendError> {
        Ok(())
    }
}

#[derive(Clone, Default)]
struct MemoryCredentialBackend {
    values: Arc<Mutex<HashMap<(String, String), Vec<u8>>>>,
}

impl CredentialBackend for MemoryCredentialBackend {
    fn get(&mut self, service: &str, account: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        Ok(self.values.lock().unwrap().get(&(service.into(), account.into())).cloned())
    }

    fn put(&mut self, service: &str, account: &str, value: &[u8]) -> Result<(), CredentialBackendError> {
        self.values.lock().unwrap().insert((service.into(), account.into()), value.to_vec());
        Ok(())
    }

    fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError> {
        self.values.lock().unwrap().remove(&(service.into(), account.into()));
        Ok(())
    }
}

async fn legacy_resolver_after_source_mutation(
    mutate: impl FnOnce(&Path, &Path, &Path),
) -> Result<CoreProviderTransport, &'static str> {
    let tmp = TestDir::new("legacy-resolver-drift");
    let azure = tmp.path("az");
    let adc = tmp.path("adc.json");
    fs::write(&azure, "#!/bin/sh\nexit 1\n").unwrap();
    fs::set_permissions(&azure, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(&adc, r#"{"type":"authorized_user"}"#).unwrap();
    fs::set_permissions(&adc, fs::Permissions::from_mode(0o600)).unwrap();
    let (config, credentials, cache, cache_file) = write_legacy_sso_fixture(&tmp);
    let backend = MemoryCredentialBackend::default();
    let native = NativeChainEnrollmentTransport::from_paths(
        azure, config.clone(), credentials.clone(), cache, adc,
    );
    let mut broker = AuthBroker::new(backend, native);
    let generation = broker.attach_backend(&mut Vec::new()).unwrap();
    let owner_epoch = broker.bind_owner(7).unwrap().current_owner_epoch.unwrap();
    let mut binding = native_binding("amazon-bedrock");
    binding.operation_id = "9".repeat(64);
    binding.owner_epoch = owner_epoch.clone();
    let generation_value = json!({
        "nonce": generation.nonce_hex(),
        "current_generation_fingerprint": generation.fingerprint(),
    });
    let start: AuthFrame = serde_json::from_value(json!({
        "type":"auth_start",
        "binding":binding,
        "generation":generation_value,
        "native_enrollment":{
            "provider":"aws_profile","profile_name":"work","region":"us-east-1"
        }
    })).unwrap();
    assert_eq!(broker.handle(start, 1_900_000_000).state, AuthState::Prepared);
    let commit: AuthFrame = serde_json::from_value(json!({
        "type":"credential_commit",
        "binding":binding,
        "generation":generation_value,
    })).unwrap();
    let committed = broker.handle(commit, 1_900_000_001);
    assert_eq!(committed.state, AuthState::Committing);
    let receipt = serde_json::to_value(&committed).unwrap();
    let credential_ref = receipt["prepared_ref"].as_str().unwrap().to_owned();
    let request = ProviderBindingRequest {
        credential_ref,
        owner_id: 7,
        owner_epoch,
        connection_id: 9,
        config_revision: 4,
        auth_grant_revision: 5,
        provider_id: "amazon-bedrock".into(),
        auth_method_id: "native_chain".into(),
        public_config_fingerprint: "e".repeat(64),
        descriptor_revision: 1,
        protocol: ProviderProtocol::AwsBedrockConverse,
        model_id: "anthropic.claude-test".into(),
        public_fields: json!({"profile_name":"work","region":"us-east-1"}),
    };
    let generation_binding: GenerationBinding = serde_json::from_value(generation_value).unwrap();
    let handle = broker.issue_provider_binding(request, &generation_binding).unwrap();
    let resolved = broker.authorize_provider_source(&handle, generation.fingerprint()).unwrap();
    mutate(&config, &credentials, &cache_file);
    transport_from_resolved_permit(resolved).await
}

#[tokio::test]
async fn legacy_sso_resolver_rejects_each_mutable_source_after_authorization() {
    for source in ["config", "credentials", "cache"] {
        let result = legacy_resolver_after_source_mutation(|config, credentials, cache| {
            let path = match source {
                "config" => config,
                "credentials" => credentials,
                _ => cache,
            };
            let mut bytes = fs::read(path).unwrap();
            bytes.push(b'\n');
            fs::write(path, bytes).unwrap();
        }).await;
        assert_eq!(result.unwrap_err(), "native_source_stale", "source={source}");
    }
}

#[derive(Debug)]
struct ChannelAdapter {
    started: Arc<std::sync::atomic::AtomicUsize>,
    completed: Arc<std::sync::atomic::AtomicUsize>,
    dropped: Arc<std::sync::atomic::AtomicUsize>,
}

impl Drop for ChannelAdapter {
    fn drop(&mut self) {
        self.dropped.fetch_add(1, Ordering::SeqCst);
    }
}

#[async_trait]
impl SdkAdapter for ChannelAdapter {
    fn protocol(&self) -> ProviderProtocol { ProviderProtocol::AwsBedrockConverse }

    async fn execute(&self, call: &ProtocolCall) -> SafeProviderResult {
        self.started.fetch_add(1, Ordering::SeqCst);
        tokio::time::sleep(Duration::from_secs(30)).await;
        self.completed.fetch_add(1, Ordering::SeqCst);
        SafeProviderResult::success("response-1", call.model_id(), Some(1), Some(1))
    }
}

fn write_provider_test_frame(stream: &mut std::os::unix::net::UnixStream, value: &Value) {
    let bytes = serde_json::to_vec(value).unwrap();
    stream.write_all(&(bytes.len() as u32).to_be_bytes()).unwrap();
    stream.write_all(&bytes).unwrap();
}

fn read_provider_test_frame(stream: &mut std::os::unix::net::UnixStream) -> Value {
    let mut size = [0u8; 4];
    stream.read_exact(&mut size).unwrap();
    let mut bytes = vec![0u8; u32::from_be_bytes(size) as usize];
    stream.read_exact(&mut bytes).unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

#[test]
fn provider_channel_orders_cancel_release_and_eof_cleanup_without_late_results() {
    let broker = Arc::new(Mutex::new(AuthBroker::new(
        EmptyCredentialBackend,
        UnavailableAuthTransport,
    )));
    let generation = broker.lock().unwrap().attach_backend(&mut Vec::new()).unwrap();
    let fields = json!({"region":"us-east-1","profile_name":"work"});
    let started = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let completed = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let dropped = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let make_transport = |handle: &str| Arc::new(CoreProviderTransport::bind_test_fixture(
        handle,
        1,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-test",
        fields.clone(),
        Arc::new(ChannelAdapter {
            started: started.clone(),
            completed: completed.clone(),
            dropped: dropped.clone(),
        }),
    ).unwrap());
    let handle_one = "1".repeat(64);
    let handle_two = "2".repeat(64);
    let handles = vec![
        (handle_one.clone(), make_transport(&handle_one)),
        (handle_two.clone(), make_transport(&handle_two)),
    ];
    let (mut client, server) = std::os::unix::net::UnixStream::pair().unwrap();
    client.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
    let worker = std::thread::spawn(move || {
        serve_provider_channel_with_test_handles(server, broker, generation, handles)
    });
    let handshake = read_provider_test_frame(&mut client);
    assert_eq!(handshake["max_frame_bytes"], 14_000_000);
    assert_eq!(handshake["max_in_flight"], 4);

    write_provider_test_frame(&mut client, &json!({
        "type":"provider_bind_v1","version":1,"request_id":1,
        "current_generation_fingerprint":"0".repeat(64),
        "binding":{"credential_ref":"7:9:00112233445566778899aabbccddeeff",
            "owner_id":7,"owner_epoch":"b".repeat(64),"connection_id":9,
            "config_revision":3,"auth_grant_revision":4,"provider_id":"amazon-bedrock",
            "auth_method_id":"native_chain","public_config_fingerprint":"c".repeat(64),
            "descriptor_revision":1,"protocol":"aws_bedrock_converse",
            "model_id":"anthropic.claude-test","public_fields":fields}
    }));
    let mismatch = read_provider_test_frame(&mut client);
    assert_eq!(mismatch["type"], "provider_error_v1");
    assert_eq!(mismatch["error_code"], "operation_interrupted");

    let request = serde_json::to_value(BoundedProviderRequest::new("hello", 32).unwrap()).unwrap();
    let execute = |request_id: u64, handle: &str| json!({
        "type":"provider_execute_v1","version":1,"request_id":request_id,"handle":handle,
        "call":{"descriptor_revision":1,"protocol":"aws_bedrock_converse",
            "model_id":"anthropic.claude-test","public_fields":fields,
            "physical_model_request_budget":1,"request":request}
    });
    write_provider_test_frame(&mut client, &execute(2, &handle_one));
    while started.load(Ordering::SeqCst) < 1 { std::thread::sleep(Duration::from_millis(5)); }
    write_provider_test_frame(&mut client, &json!({
        "type":"provider_cancel_v1","version":1,"request_id":3,
        "handle":handle_one,"target_request_id":2
    }));
    let terminal = read_provider_test_frame(&mut client);
    let cancel_ack = read_provider_test_frame(&mut client);
    assert_eq!(terminal["type"], "provider_result_v1");
    assert_eq!(terminal["request_id"], 2);
    assert_eq!(terminal["result"]["error_code"], "cancelled");
    assert_eq!(cancel_ack["type"], "provider_cancelled_v1");
    assert_eq!(cancel_ack["request_id"], 3);
    write_provider_test_frame(&mut client, &json!({
        "type":"provider_release_v1","version":1,"request_id":4,"handle":handle_one
    }));
    assert_eq!(read_provider_test_frame(&mut client)["type"], "provider_released_v1");

    write_provider_test_frame(&mut client, &execute(5, &handle_two));
    while started.load(Ordering::SeqCst) < 2 { std::thread::sleep(Duration::from_millis(5)); }
    drop(client);
    assert!(worker.join().unwrap().is_ok());
    assert_eq!(completed.load(Ordering::SeqCst), 0);
    assert_eq!(dropped.load(Ordering::SeqCst), 2);
}

#[test]
fn provider_channel_caps_four_executes_and_poisons_reused_request_ids() {
    let broker = Arc::new(Mutex::new(AuthBroker::new(
        EmptyCredentialBackend,
        UnavailableAuthTransport,
    )));
    let generation = broker.lock().unwrap().attach_backend(&mut Vec::new()).unwrap();
    let started = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let completed = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let dropped = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let handle = "3".repeat(64);
    let fields = json!({"region":"us-east-1","profile_name":"work"});
    let transport = Arc::new(CoreProviderTransport::bind_test_fixture(
        &handle,
        1,
        ProviderProtocol::AwsBedrockConverse,
        "anthropic.claude-test",
        fields.clone(),
        Arc::new(ChannelAdapter {
            started: started.clone(), completed: completed.clone(), dropped: dropped.clone(),
        }),
    ).unwrap());
    let (mut client, server) = std::os::unix::net::UnixStream::pair().unwrap();
    client.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
    let worker_handle = handle.clone();
    let worker = std::thread::spawn(move || serve_provider_channel_with_test_handles(
        server, broker, generation, vec![(worker_handle, transport)],
    ));
    let _ = read_provider_test_frame(&mut client);
    let request = serde_json::to_value(BoundedProviderRequest::new("hello", 32).unwrap()).unwrap();
    for request_id in 1..=5 {
        write_provider_test_frame(&mut client, &json!({
            "type":"provider_execute_v1","version":1,"request_id":request_id,"handle":handle,
            "call":{"descriptor_revision":1,"protocol":"aws_bedrock_converse",
                "model_id":"anthropic.claude-test","public_fields":fields,
                "physical_model_request_budget":1,"request":request}
        }));
    }
    while started.load(Ordering::SeqCst) < 4 { std::thread::sleep(Duration::from_millis(5)); }
    let capacity = read_provider_test_frame(&mut client);
    assert_eq!(capacity["request_id"], 5);
    assert_eq!(capacity["error_code"], "provider_capacity_exceeded");
    write_provider_test_frame(&mut client, &json!({
        "type":"provider_release_v1","version":1,"request_id":5,"handle":handle
    }));
    let mut cleanup_replies = Vec::new();
    loop {
        let mut size = [0u8; 4];
        match client.read_exact(&mut size) {
            Ok(()) => {
                let mut bytes = vec![0u8; u32::from_be_bytes(size) as usize];
                client.read_exact(&mut bytes).unwrap();
                cleanup_replies.push(serde_json::from_slice::<Value>(&bytes).unwrap());
            }
            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => break,
            Err(error) => panic!("unexpected provider cleanup read: {error}"),
        }
    }
    assert!(cleanup_replies.iter().all(|reply| reply["type"] == "provider_result_v1"));
    assert!(worker.join().unwrap().is_err());
    assert_eq!(completed.load(Ordering::SeqCst), 0);
    assert_eq!(dropped.load(Ordering::SeqCst), 1);
}
