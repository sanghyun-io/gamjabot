//! Reporting belongs to the supervisor, independently of the engine or attached clients.
use super::{
    sender::{SendError, Sender},
    *,
};
use crate::ControlRequest;
use serde_json::{json, Value};
use std::{
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::Duration,
};

pub struct Reporting {
    state: Arc<ReportingState>,
    sender: Option<Arc<Sender>>,
    stop: Arc<AtomicBool>,
    worker: Option<thread::JoinHandle<()>>,
}

impl Reporting {
    pub fn open(data: &Path) -> Self {
        let state = Arc::new(ReportingState::new(ReportStore::open(data).ok()));
        let sender = Sender::production().map(Arc::new);
        Self::from_parts(state, sender)
    }

    fn from_parts(state: Arc<ReportingState>, sender: Option<Arc<Sender>>) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let worker = sender.clone().and_then(|sender| {
            let state = state.clone();
            let halt = stop.clone();
            thread::Builder::new()
                .name("gamja-report-sender".into())
                .spawn(move || {
                    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                    else {
                        return;
                    };
                    runtime.block_on(async move {
                        while !halt.load(Ordering::Acquire) {
                            let send = sender.run_auto_due(&state, chrono::Utc::now().timestamp());
                            tokio::pin!(send);
                            loop {
                                tokio::select! {
                                    _ = &mut send => break,
                                    _ = tokio::time::sleep(Duration::from_millis(100)) => {
                                        if halt.load(Ordering::Acquire) { return }
                                    }
                                }
                            }
                            for _ in 0..300 {
                                if halt.load(Ordering::Acquire) {
                                    return;
                                }
                                tokio::time::sleep(Duration::from_millis(100)).await;
                            }
                        }
                    });
                    runtime.shutdown_timeout(Duration::from_millis(200));
                })
                .ok()
        });
        Self {
            state,
            sender,
            stop,
            worker,
        }
    }

    pub fn state(&self) -> Arc<ReportingState> {
        self.state.clone()
    }

    pub fn record(&self, component: Component, stage: Stage, code: Code) {
        record(&self.state, DiagnosticEvent::new(component, stage, code));
    }

    pub fn handle(&self, request: ControlRequest) -> (u16, Value) {
        if request.validate().is_err() {
            return (400, json!({"code":"invalid_request"}));
        }
        if let ControlRequest::ReportSubmit { report_id } = request {
            let Some(sender) = &self.sender else {
                return (503, json!({"code":"submission_unavailable"}));
            };
            let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            else {
                return (503, json!({"code":"reporting_unavailable"}));
            };
            return match runtime.block_on(sender.submit_manual(&self.state, &report_id)) {
                Ok(receipt) => (200, json!(receipt)),
                Err(error) => (
                    503,
                    json!({"code":match error { SendError::Unavailable => "reporting_unavailable", SendError::Retryable => "submission_pending", SendError::Rejected => "report_rejected" }}),
                ),
            };
        }
        let Ok(mut guard) = self.state.lock() else {
            return (503, json!({"code":"reporting_unavailable"}));
        };
        let Some(store) = guard.as_mut() else {
            return (503, json!({"code":"reporting_unavailable"}));
        };
        let result: Result<Value, String> = match request {
            ControlRequest::ReportList {} => Ok(json!(store.list())),
            ControlRequest::ReportCreate {
                event_ids,
                description,
            } => store
                .create_manual_draft(&event_ids, description)
                .map(|draft| json!(draft)),
            ControlRequest::ReportUpdate {
                report_id,
                event_ids,
                description,
            } => store
                .update_manual_draft(&report_id, &event_ids, description)
                .map(|draft| json!(draft)),
            ControlRequest::ReportDelete { report_id } => {
                store.delete(&report_id).map(|_| Value::Null)
            }
            ControlRequest::ReportExport { report_id } => store
                .export_report(&report_id)
                .map(|path| json!(path.to_string_lossy())),
            ControlRequest::ReportConsent { enabled } => store
                .set_auto_consent(enabled)
                .map(|_| json!(store.snapshot_consent())),
            ControlRequest::ReportReceipt { report_id } => store
                .list()
                .reports
                .into_iter()
                .find(|report| report.draft.envelope.report_id == report_id)
                .ok_or_else(|| "Report not found".to_string())
                .map(|report| json!(report.receipt)),
            ControlRequest::ReportStatus {} => {
                let (auto_reporting_enabled, consent_generation) = store.snapshot_consent();
                Ok(json!(ReportStatus {
                    auto_reporting_enabled,
                    consent_generation,
                    submission_available: self.sender.is_some()
                }))
            }
            ControlRequest::ReportFrontendFailure {} => store
                .record(DiagnosticEvent::new(
                    Component::Frontend,
                    Stage::Render,
                    Code::FrontendRenderFailed,
                ))
                .map(|_| Value::Null),
            _ => return (400, json!({"code":"invalid_request"})),
        };
        match result {
            Ok(value) => (200, value),
            Err(_) => (400, json!({"code":"report_operation_failed"})),
        }
    }
}

pub(crate) fn record(state: &ReportingState, event: DiagnosticEvent) {
    if let Ok(mut guard) = state.lock() {
        if let Some(store) = guard.as_mut() {
            let _ = store.record(event);
        }
    }
}

impl Drop for Reporting {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        io::{Read, Write},
        net::TcpListener,
        time::Instant,
    };

    struct TestRoot(std::path::PathBuf);
    impl TestRoot {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("gamja-core-report-{}", new_id()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn reporting_sender_outlives_disposable_gui_and_persists_receipt() {
        let root = TestRoot::new();
        let mut store = ReportStore::open(&root.0).unwrap();
        store.set_auto_consent(true).unwrap();
        store
            .record(DiagnosticEvent::new(
                Component::Frontend,
                Stage::Render,
                Code::FrontendRenderFailed,
            ))
            .unwrap();
        let state = Arc::new(ReportingState::new(Some(store)));
        let disposable_gui = state.clone();
        let gate = state.lock().unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!(
            "http://127.0.0.1:{}/reports",
            listener.local_addr().unwrap().port()
        );
        let receiver = thread::spawn(move || {
            let end = Instant::now() + Duration::from_secs(5);
            let mut socket = loop {
                if let Ok((socket, _)) = listener.accept() {
                    break socket;
                }
                assert!(Instant::now() < end, "core sender stopped with GUI client");
                thread::sleep(Duration::from_millis(10));
            };
            socket
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut headers = Vec::new();
            let mut byte = [0];
            while !headers.ends_with(b"\r\n\r\n") {
                socket.read_exact(&mut byte).unwrap();
                headers.push(byte[0]);
                assert!(headers.len() < 8192);
            }
            let headers = String::from_utf8(headers).unwrap();
            let length: usize = headers
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("content-length:")
                        .map(|value| value.trim().parse().unwrap())
                })
                .unwrap();
            let mut body = vec![0; length];
            socket.read_exact(&mut body).unwrap();
            let envelope: ReportEnvelope = serde_json::from_slice(&body).unwrap();
            assert_eq!(envelope.mode, Mode::Automatic);
            assert!(envelope.description.is_none());
            let receipt = Receipt {
                report_id: envelope.report_id,
                receipt_id: "b".repeat(32),
                accepted_at: now(),
            };
            let body = serde_json::to_vec(&receipt).unwrap();
            write!(socket,"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len()).unwrap();
            socket.write_all(&body).unwrap();
            receipt
        });
        let owner =
            Reporting::from_parts(state.clone(), Some(Arc::new(Sender::loopback_test(&url))));
        drop(disposable_gui);
        drop(gate);
        let receipt = receiver.join().unwrap();
        let end = Instant::now() + Duration::from_secs(5);
        loop {
            let snapshot: ReportSnapshot =
                serde_json::from_slice(&fs::read(root.0.join("reporting/state.json")).unwrap())
                    .unwrap();
            if snapshot.reports[0].receipt.as_ref() == Some(&receipt) {
                break;
            }
            assert!(Instant::now() < end, "receipt never became durable");
            thread::sleep(Duration::from_millis(10));
        }
        drop(owner);
        let snapshot = ReportStore::open(&root.0).unwrap().list();
        assert_eq!(snapshot.reports[0].receipt, Some(receipt));
        assert_eq!(snapshot.reports[0].state, ReportState::Accepted);
        assert!(!snapshot.auto_reporting_enabled);
    }
}
