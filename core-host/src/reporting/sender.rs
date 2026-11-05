use super::{Receipt, SubmissionDraft};
use std::future::Future;
use std::pin::Pin;
use std::time::Duration;

const VERIFIED_ROUTE: &str = "https://gamja.top/api/gamja-bot/reports";
const MAX_RESPONSE_BYTES: usize = 1024;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SendError {
    Unavailable,
    Retryable,
    Rejected,
}

pub struct Sender {
    endpoint: reqwest::Url,
    client: reqwest::Client,
}

pub struct WireResponse {
    pub status: u16,
    pub body: Vec<u8>,
}

type TransportFuture<'a> =
    Pin<Box<dyn Future<Output = Result<WireResponse, SendError>> + Send + 'a>>;

pub trait Transport: Send + Sync {
    fn send<'a>(&'a self, endpoint: &'a reqwest::Url, body: &'a [u8]) -> TransportFuture<'a>;
}

pub fn configured_endpoint() -> Option<reqwest::Url> {
    option_env!("GAMJA_REPORT_ENDPOINT").and_then(validated_endpoint)
}

fn validated_endpoint(value: &str) -> Option<reqwest::Url> {
    if value != VERIFIED_ROUTE {
        return None;
    }
    let url = reqwest::Url::parse(value).ok()?;
    (url.scheme() == "https" && url.as_str() == VERIFIED_ROUTE).then_some(url)
}

impl Sender {
    pub fn production() -> Option<Self> {
        let endpoint = configured_endpoint()?;
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .https_only(true)
            .no_proxy()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .build()
            .ok()?;
        Some(Self { endpoint, client })
    }

    #[cfg(test)]
    pub(crate) fn loopback_test(endpoint: &str) -> Self {
        let endpoint = reqwest::Url::parse(endpoint).unwrap();
        assert_eq!(endpoint.host_str(), Some("127.0.0.1"));
        Self {
            endpoint,
            client: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(2))
                .build()
                .unwrap(),
        }
    }

    #[cfg(test)]
    fn test_sender() -> Self {
        Self {
            endpoint: validated_endpoint(VERIFIED_ROUTE).unwrap(),
            client: reqwest::Client::new(),
        }
    }

    #[allow(dead_code)] // Public sender interface retained for installed-app integration checks.
    pub async fn submit(&self, draft: &SubmissionDraft) -> Result<Receipt, SendError> {
        self.submit_with(draft, &HttpTransport(&self.client)).await
    }

    pub async fn submit_with<T: Transport>(
        &self,
        draft: &SubmissionDraft,
        transport: &T,
    ) -> Result<Receipt, SendError> {
        draft.envelope.validate().map_err(|_| SendError::Rejected)?;
        if draft.body().len() > super::WIRE_MAX_BYTES {
            return Err(SendError::Rejected);
        }
        let response = transport
            .send(&self.endpoint, draft.body().as_bytes())
            .await?;
        if response.status == 201 {
            if response.body.len() > MAX_RESPONSE_BYTES {
                return Err(SendError::Retryable);
            }
            let receipt: Receipt =
                serde_json::from_slice(&response.body).map_err(|_| SendError::Retryable)?;
            if receipt.report_id != draft.envelope.report_id
                || receipt.receipt_id.len() != 32
                || !receipt.receipt_id.bytes().all(|b| b.is_ascii_hexdigit())
                || !receipt.accepted_at.ends_with('Z')
                || chrono::DateTime::parse_from_rfc3339(&receipt.accepted_at).is_err()
            {
                return Err(SendError::Retryable);
            }
            return Ok(receipt);
        }
        if (400..500).contains(&response.status) && response.status != 408 && response.status != 429
        {
            Err(SendError::Rejected)
        } else {
            Err(SendError::Retryable)
        }
    }

    pub async fn submit_manual(
        &self,
        state: &crate::reporting::ReportingState,
        report_id: &str,
    ) -> Result<Receipt, SendError> {
        self.submit_manual_with(state, report_id, &HttpTransport(&self.client))
            .await
    }

    pub async fn submit_manual_with<T: Transport>(
        &self,
        state: &crate::reporting::ReportingState,
        report_id: &str,
        transport: &T,
    ) -> Result<Receipt, SendError> {
        let draft = {
            let mut guard = state.lock().map_err(|_| SendError::Unavailable)?;
            guard
                .as_mut()
                .ok_or(SendError::Unavailable)?
                .begin_manual_attempt(report_id)
                .map_err(|_| SendError::Rejected)?
        };
        let result = self.submit_with(&draft, transport).await;
        let mut guard = state.lock().map_err(|_| SendError::Unavailable)?;
        let store = guard.as_mut().ok_or(SendError::Unavailable)?;
        match &result {
            Ok(receipt) => store
                .finish_accepted(report_id, None, receipt.clone())
                .map_err(|_| SendError::Unavailable)?,
            Err(SendError::Rejected) => store
                .finish_rejected(report_id, None)
                .map_err(|_| SendError::Unavailable)?,
            Err(_) => store
                .finish_retryable(report_id, None, chrono::Utc::now().timestamp())
                .map_err(|_| SendError::Unavailable)?,
        }
        result
    }

    pub async fn run_auto_due(&self, state: &crate::reporting::ReportingState, now: i64) {
        self.run_auto_due_with(state, &HttpTransport(&self.client), now)
            .await;
    }

    pub async fn run_auto_due_with<T: Transport>(
        &self,
        state: &crate::reporting::ReportingState,
        transport: &T,
        now: i64,
    ) {
        let (ids, generation) = match state.lock() {
            Ok(mut guard) => match guard.as_mut() {
                Some(store) => (store.due_auto_ids(now), store.snapshot_consent().1),
                None => return,
            },
            Err(_) => return,
        };
        for id in ids {
            let draft = match state.lock() {
                Ok(mut guard) => match guard
                    .as_mut()
                    .and_then(|store| store.begin_auto_attempt(&id, generation, now).ok())
                {
                    Some(draft) => draft,
                    None => break,
                },
                Err(_) => break,
            };
            let submission = self.submit_with(&draft, transport);
            tokio::pin!(submission);
            let result = loop {
                tokio::select! {
                    response = &mut submission => break Some(response),
                    _ = tokio::time::sleep(Duration::from_millis(100)) => {
                        let still_enabled = state.lock().ok().and_then(|guard| guard.as_ref().map(|store| store.snapshot_consent() == (true, generation))).unwrap_or(false);
                        if !still_enabled { break None; }
                    }
                }
            };
            let Some(result) = result else { return };
            if let Ok(mut guard) = state.lock() {
                if let Some(store) = guard.as_mut() {
                    if store.snapshot_consent() != (true, generation) {
                        return;
                    }
                    match result {
                        Ok(receipt) => {
                            let _ = store.finish_accepted(&id, Some(generation), receipt);
                        }
                        Err(SendError::Rejected) => {
                            let _ = store.finish_rejected(&id, Some(generation));
                        }
                        Err(_) => {
                            let _ = store.finish_retryable(&id, Some(generation), now);
                        }
                    }
                }
            }
        }
    }
}

struct HttpTransport<'a>(&'a reqwest::Client);

impl Transport for HttpTransport<'_> {
    fn send<'a>(&'a self, endpoint: &'a reqwest::Url, body: &'a [u8]) -> TransportFuture<'a> {
        Box::pin(async move {
            let mut response = self
                .0
                .post(endpoint.clone())
                .header(reqwest::header::CONTENT_TYPE, "application/json")
                .body(body.to_vec())
                .send()
                .await
                .map_err(|_| SendError::Retryable)?;
            let status = response.status().as_u16();
            if status != 201 {
                return Ok(WireResponse {
                    status,
                    body: Vec::new(),
                });
            }
            if status == 201
                && response
                    .headers()
                    .get(reqwest::header::CONTENT_TYPE)
                    .and_then(|value| value.to_str().ok())
                    .is_none_or(|value| !value.starts_with("application/json"))
            {
                return Err(SendError::Retryable);
            }
            if response
                .content_length()
                .is_some_and(|len| len > MAX_RESPONSE_BYTES as u64)
            {
                return Err(SendError::Retryable);
            }
            let mut bytes = Vec::new();
            while let Some(chunk) = response.chunk().await.map_err(|_| SendError::Retryable)? {
                if bytes.len() + chunk.len() > MAX_RESPONSE_BYTES {
                    return Err(SendError::Retryable);
                }
                bytes.extend_from_slice(&chunk);
            }
            Ok(WireResponse {
                status,
                body: bytes,
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reporting::{Code, Component, DiagnosticEvent, ReportStore, Stage};
    #[cfg(target_os = "macos")]
    use std::io::{BufRead, BufReader};
    #[cfg(target_os = "macos")]
    use std::os::unix::fs::PermissionsExt;
    #[cfg(target_os = "macos")]
    use std::path::{Path, PathBuf};
    #[cfg(target_os = "macos")]
    use std::process::{Child, Command, Stdio};
    use std::sync::{Arc, Mutex};

    #[cfg(target_os = "macos")]
    struct LoopbackReceiver {
        root: PathBuf,
        child: Child,
        endpoint: reqwest::Url,
    }

    #[cfg(target_os = "macos")]
    impl LoopbackReceiver {
        fn start(root: PathBuf) -> Self {
            let endpoint_child = Self::spawn(&root);
            Self {
                root,
                child: endpoint_child.0,
                endpoint: endpoint_child.1,
            }
        }

        fn spawn(root: &Path) -> (Child, reqwest::Url) {
            let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .canonicalize()
                .unwrap();
            let mut child = Command::new("python3")
                .arg("-u")
                .arg("-c")
                .arg("from reporting.receiver.server import make_server; import sys; server = make_server(sys.argv[1], port=0); print(server.server_port, flush=True); server.serve_forever()")
                .arg(root.join("reports.sqlite3"))
                .env("PYTHONPATH", repo)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap();
            let mut line = String::new();
            BufReader::new(child.stdout.take().unwrap())
                .read_line(&mut line)
                .unwrap();
            let port: u16 = match line.trim().parse() {
                Ok(port) => port,
                Err(_) => {
                    let output = child.wait_with_output().unwrap();
                    panic!(
                        "receiver startup port: {}",
                        String::from_utf8_lossy(&output.stderr)
                    )
                }
            };
            (
                child,
                reqwest::Url::parse(&format!("http://127.0.0.1:{port}/reports")).unwrap(),
            )
        }

        fn restart(&mut self) {
            self.child.kill().unwrap();
            self.child.wait().unwrap();
            (self.child, self.endpoint) = Self::spawn(&self.root);
        }

        fn count(&self) -> usize {
            let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .canonicalize()
                .unwrap();
            let output = Command::new("python3")
                .args(["-m", "reporting.receiver.operator", "--db"])
                .arg(self.root.join("reports.sqlite3"))
                .arg("count")
                .env("PYTHONPATH", repo)
                .output()
                .unwrap();
            assert!(output.status.success());
            serde_json::from_slice::<serde_json::Value>(&output.stdout).unwrap()["total"]
                .as_u64()
                .unwrap() as usize
        }
    }

    #[cfg(target_os = "macos")]
    impl Drop for LoopbackReceiver {
        fn drop(&mut self) {
            let _ = self.child.kill();
            let _ = self.child.wait();
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[cfg(target_os = "macos")]
    struct CommitThenLoseReceipt<'a> {
        client: &'a reqwest::Client,
        committed: &'a Mutex<Option<WireResponse>>,
    }

    #[cfg(target_os = "macos")]
    impl Transport for CommitThenLoseReceipt<'_> {
        fn send<'a>(&'a self, endpoint: &'a reqwest::Url, body: &'a [u8]) -> TransportFuture<'a> {
            Box::pin(async move {
                let response = HttpTransport(self.client).send(endpoint, body).await?;
                *self.committed.lock().unwrap() = Some(response);
                Err(SendError::Retryable)
            })
        }
    }

    struct FakeTransport {
        replies: Mutex<Vec<Result<WireResponse, SendError>>>,
        bodies: Option<Arc<Mutex<Vec<Vec<u8>>>>>,
    }

    impl FakeTransport {
        fn new(replies: Vec<WireResponse>) -> Self {
            Self {
                replies: Mutex::new(replies.into_iter().map(Ok).rev().collect()),
                bodies: None,
            }
        }
        fn timeout_then_receipt(bodies: Arc<Mutex<Vec<Vec<u8>>>>, report_id: String) -> Self {
            let receipt = format!(
                "{{\"report_id\":\"{}\",\"receipt_id\":\"{}\",\"accepted_at\":\"2026-09-29T00:00:00Z\"}}",
                report_id, report_id
            );
            Self {
                replies: Mutex::new(vec![
                    Ok(WireResponse {
                        status: 201,
                        body: receipt.into_bytes(),
                    }),
                    Err(SendError::Retryable),
                ]),
                bodies: Some(bodies),
            }
        }
    }

    impl Transport for FakeTransport {
        fn send<'a>(&'a self, _endpoint: &'a reqwest::Url, body: &'a [u8]) -> TransportFuture<'a> {
            Box::pin(async move {
                if let Some(bodies) = &self.bodies {
                    bodies.lock().unwrap().push(body.to_vec());
                }
                self.replies.lock().unwrap().pop().unwrap()
            })
        }
    }

    fn draft() -> SubmissionDraft {
        let dir =
            std::env::temp_dir().join(format!("gamja-sender-test-{}", crate::reporting::new_id()));
        let mut store = ReportStore::open(&dir).unwrap();
        let event = DiagnosticEvent::new(Component::Host, Stage::Runtime, Code::UnexpectedFailure);
        store.record(event.clone()).unwrap();
        let draft = store.create_manual_draft(&[event.event_id], None).unwrap();
        std::fs::remove_dir_all(dir).unwrap();
        draft
    }

    #[tokio::test]
    async fn only_a_typed_201_receipt_confirms_submission() {
        let draft = draft();
        let receipt = format!(
            "{{\"report_id\":\"{}\",\"receipt_id\":\"{}\",\"accepted_at\":\"2026-09-29T00:00:00Z\"}}",
            draft.envelope.report_id, draft.envelope.report_id
        );
        let fake = FakeTransport::new(vec![WireResponse {
            status: 201,
            body: receipt.as_bytes().to_vec(),
        }]);
        let sender = Sender::test_sender();
        assert_eq!(
            sender.submit_with(&draft, &fake).await.unwrap().receipt_id,
            draft.envelope.report_id
        );
        for status in [200, 202] {
            let fake = FakeTransport::new(vec![WireResponse {
                status,
                body: receipt.as_bytes().to_vec(),
            }]);
            assert_eq!(
                sender.submit_with(&draft, &fake).await,
                Err(SendError::Retryable)
            );
        }
        let fake = FakeTransport::new(vec![WireResponse {
            status: 201,
            body: b"{}".to_vec(),
        }]);
        assert_eq!(
            sender.submit_with(&draft, &fake).await,
            Err(SendError::Retryable)
        );
    }

    #[tokio::test]
    async fn receipt_must_echo_the_submitted_report_id() {
        let draft = draft();
        let matching = format!(
            "{{\"report_id\":\"{}\",\"receipt_id\":\"{}\",\"accepted_at\":\"2026-09-29T00:00:00Z\"}}",
            draft.envelope.report_id, draft.envelope.report_id
        );
        let matching_fake = FakeTransport::new(vec![WireResponse {
            status: 201,
            body: matching.into_bytes(),
        }]);
        assert!(Sender::test_sender()
            .submit_with(&draft, &matching_fake)
            .await
            .is_ok());
        let mismatched = format!(
            "{{\"report_id\":\"{}\",\"receipt_id\":\"{}\",\"accepted_at\":\"2026-09-29T00:00:00Z\"}}",
            "0".repeat(32), draft.envelope.report_id
        );
        let fake = FakeTransport::new(vec![WireResponse {
            status: 201,
            body: mismatched.into_bytes(),
        }]);
        assert_eq!(
            Sender::test_sender().submit_with(&draft, &fake).await,
            Err(SendError::Retryable)
        );
    }

    #[tokio::test]
    async fn timeout_after_commit_retries_byte_identical_body_and_id() {
        let draft = draft();
        let observed = Arc::new(Mutex::new(Vec::new()));
        let fake =
            FakeTransport::timeout_then_receipt(observed.clone(), draft.envelope.report_id.clone());
        let sender = Sender::test_sender();
        assert_eq!(
            sender.submit_with(&draft, &fake).await,
            Err(SendError::Retryable)
        );
        assert!(sender.submit_with(&draft, &fake).await.is_ok());
        let bodies = observed.lock().unwrap();
        assert_eq!(bodies.len(), 2);
        assert_eq!(bodies[0], bodies[1]);
        assert_eq!(bodies[0], draft.body().as_bytes());
    }

    #[test]
    fn unverified_endpoint_cannot_be_configured() {
        assert!(validated_endpoint("http://gamja.top/api/gamja-bot/reports").is_none());
        assert!(validated_endpoint("https://gamja.top/api/gamja-bot/reports?x=1").is_none());
        assert!(validated_endpoint("https://other.example/reports").is_none());
        assert!(validated_endpoint("https://gamja.top/api/gamja-bot/reports").is_some());
    }

    struct PendingTransport(Arc<Mutex<usize>>);
    impl Transport for PendingTransport {
        fn send<'a>(&'a self, _endpoint: &'a reqwest::Url, _body: &'a [u8]) -> TransportFuture<'a> {
            Box::pin(async move {
                *self.0.lock().unwrap() += 1;
                std::future::pending().await
            })
        }
    }

    #[tokio::test]
    async fn revocation_during_in_flight_auto_send_stops_future_attempts() {
        let dir =
            std::env::temp_dir().join(format!("gamja-revoke-test-{}", crate::reporting::new_id()));
        let mut store = ReportStore::open(&dir).unwrap();
        store.set_auto_consent(true).unwrap();
        store
            .record(DiagnosticEvent::new(
                Component::Host,
                Stage::Startup,
                Code::SidecarMissing,
            ))
            .unwrap();
        let shared = Arc::new(Mutex::new(Some(store)));
        let attempts = Arc::new(Mutex::new(0));
        let sender = Sender::test_sender();
        let transport = PendingTransport(attempts.clone());
        let shared_for_revoke = shared.clone();
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(30)).await;
            shared_for_revoke
                .lock()
                .unwrap()
                .as_mut()
                .unwrap()
                .set_auto_consent(false)
                .unwrap();
        });
        sender
            .run_auto_due_with(&shared, &transport, chrono::Utc::now().timestamp())
            .await;
        sender
            .run_auto_due_with(&shared, &transport, chrono::Utc::now().timestamp() + 120)
            .await;
        assert_eq!(*attempts.lock().unwrap(), 1);
        assert!(shared
            .lock()
            .unwrap()
            .as_mut()
            .unwrap()
            .list()
            .reports
            .is_empty());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[tokio::test]
    async fn manual_pending_waits_for_explicit_retry_and_4xx_is_terminal() {
        let dir =
            std::env::temp_dir().join(format!("gamja-manual-test-{}", crate::reporting::new_id()));
        let mut store = ReportStore::open(&dir).unwrap();
        let draft = store.create_manual_draft(&[], None).unwrap();
        let id = draft.envelope.report_id.clone();
        let shared = Mutex::new(Some(store));
        let sender = Sender::test_sender();
        let offline = FakeTransport {
            replies: Mutex::new(vec![Err(SendError::Retryable)]),
            bodies: None,
        };
        assert_eq!(
            sender.submit_manual_with(&shared, &id, &offline).await,
            Err(SendError::Retryable)
        );
        assert_eq!(
            shared.lock().unwrap().as_mut().unwrap().list().reports[0].attempts,
            1
        );
        sender
            .run_auto_due_with(
                &shared,
                &FakeTransport::new(vec![]),
                chrono::Utc::now().timestamp(),
            )
            .await;
        assert_eq!(
            shared.lock().unwrap().as_mut().unwrap().list().reports[0].attempts,
            1
        );
        let rejected = FakeTransport::new(vec![WireResponse {
            status: 400,
            body: vec![],
        }]);
        assert_eq!(
            sender.submit_manual_with(&shared, &id, &rejected).await,
            Err(SendError::Rejected)
        );
        assert_eq!(
            shared.lock().unwrap().as_mut().unwrap().list().reports[0].state,
            crate::reporting::ReportState::Rejected
        );
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(target_os = "macos")]
    #[tokio::test]
    async fn rust_sender_replays_committed_report_to_real_python_receiver() {
        let root = std::env::temp_dir().join(format!(
            "gamja-cross-language-{}",
            crate::reporting::new_id()
        ));
        std::fs::create_dir(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut receiver = LoopbackReceiver::start(root.clone());
        let mut store = ReportStore::open(&root.join("client")).unwrap();
        let draft = store
            .create_manual_draft(&[], Some("synthetic 설명".into()))
            .unwrap();
        let id = draft.envelope.report_id.clone();
        let state = Mutex::new(Some(store));
        assert_eq!(receiver.count(), 0);

        let sender = Sender {
            endpoint: receiver.endpoint.clone(),
            client: reqwest::Client::new(),
        };
        let malformed = FakeTransport::new(vec![WireResponse {
            status: 201,
            body: b"{}".to_vec(),
        }]);
        assert_eq!(
            sender.submit_manual_with(&state, &id, &malformed).await,
            Err(SendError::Retryable)
        );
        assert_eq!(receiver.count(), 0);
        assert_eq!(
            state.lock().unwrap().as_mut().unwrap().list().reports[0].state,
            crate::reporting::ReportState::Pending
        );
        let committed = Mutex::new(None);
        let lost = CommitThenLoseReceipt {
            client: &sender.client,
            committed: &committed,
        };
        assert_eq!(
            sender.submit_manual_with(&state, &id, &lost).await,
            Err(SendError::Retryable)
        );
        let first = committed.lock().unwrap().take().unwrap();
        assert_eq!(first.status, 201);
        assert_eq!(receiver.count(), 1);
        assert_eq!(
            state.lock().unwrap().as_mut().unwrap().list().reports[0].state,
            crate::reporting::ReportState::Pending
        );

        receiver.restart();
        let sender = Sender {
            endpoint: receiver.endpoint.clone(),
            client: reqwest::Client::new(),
        };
        let receipt = sender
            .submit_manual_with(&state, &id, &HttpTransport(&sender.client))
            .await
            .unwrap();
        assert_eq!(receipt.report_id, id);
        assert_eq!(
            serde_json::from_slice::<Receipt>(&first.body).unwrap(),
            receipt
        );
        assert_eq!(receiver.count(), 1);
        assert_eq!(
            state.lock().unwrap().as_mut().unwrap().list().reports[0].state,
            crate::reporting::ReportState::Accepted
        );

        {
            let mut guard = state.lock().unwrap();
            let store = guard.as_mut().unwrap();
            store.set_auto_consent(true).unwrap();
            store
                .record(DiagnosticEvent::new(
                    Component::Host,
                    Stage::Startup,
                    Code::SidecarMissing,
                ))
                .unwrap();
        }
        sender
            .run_auto_due_with(
                &state,
                &HttpTransport(&sender.client),
                chrono::Utc::now().timestamp(),
            )
            .await;
        assert_eq!(receiver.count(), 2);
        state
            .lock()
            .unwrap()
            .as_mut()
            .unwrap()
            .set_auto_consent(false)
            .unwrap();
        sender
            .run_auto_due_with(
                &state,
                &HttpTransport(&sender.client),
                chrono::Utc::now().timestamp() + 120,
            )
            .await;
        assert_eq!(receiver.count(), 2);
    }
}

#[cfg(test)]
mod durable_receipt_tests {
    use super::*;
    use std::{path::PathBuf, sync::Mutex};
    struct BreakStorageAfterAcceptance(PathBuf);
    impl Transport for BreakStorageAfterAcceptance {
        fn send<'a>(&'a self, _endpoint: &'a reqwest::Url, body: &'a [u8]) -> TransportFuture<'a> {
            Box::pin(async move {
                let envelope: super::super::ReportEnvelope = serde_json::from_slice(body).unwrap();
                std::fs::rename(self.0.join("reporting"), self.0.join("saved-reporting")).unwrap();
                std::fs::write(self.0.join("reporting"), b"fixture storage failure").unwrap();
                Ok(WireResponse {
                    status: 201,
                    body: serde_json::to_vec(&Receipt {
                        report_id: envelope.report_id,
                        receipt_id: "b".repeat(32),
                        accepted_at: super::super::now(),
                    })
                    .unwrap(),
                })
            })
        }
    }
    #[tokio::test]
    async fn reporting_manual_success_requires_local_durable_receipt() {
        let root =
            std::env::temp_dir().join(format!("gamja-core-durability-{}", super::super::new_id()));
        let mut store = super::super::ReportStore::open(&root).unwrap();
        let draft = store.create_manual_draft(&[], None).unwrap();
        let state = Mutex::new(Some(store));
        let sender = Sender::loopback_test("http://127.0.0.1:1/reports");
        let result = sender
            .submit_manual_with(
                &state,
                &draft.envelope.report_id,
                &BreakStorageAfterAcceptance(root.clone()),
            )
            .await;
        assert_eq!(result, Err(SendError::Unavailable));
        let durable: super::super::ReportSnapshot = serde_json::from_slice(
            &std::fs::read(root.join("saved-reporting/state.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(durable.reports[0].state, super::super::ReportState::Sending);
        assert!(durable.reports[0].receipt.is_none());
        assert_eq!(
            state.lock().unwrap().as_mut().unwrap().list().reports[0].state,
            super::super::ReportState::Sending
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
