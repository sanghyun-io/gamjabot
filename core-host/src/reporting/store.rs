use super::{
    new_id, now, platform_os_version, Arch, Code, Component, Mode, Os, ReportDiagnostic,
    ReportEnvelope, Stage, Transition,
};
use serde::{Deserialize, Serialize};
#[cfg(unix)]
use std::fs::File;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

const STORE_MAX_BYTES: usize = 65_536;
const STORE_MAX_EVENTS: usize = 256;
const TTL_SECONDS: i64 = 7 * 24 * 60 * 60;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiagnosticEvent {
    pub event_id: String,
    pub occurred_at: String,
    pub component: Component,
    pub stage: Stage,
    pub code: Code,
    pub app_version: String,
    pub build_id: String,
    pub os: Os,
    pub os_version: String,
    pub arch: Arch,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diagnostic: Option<ReportDiagnostic>,
}

impl DiagnosticEvent {
    pub fn new(component: Component, stage: Stage, code: Code) -> Self {
        let app_version = env!("CARGO_PKG_VERSION").to_string();
        let build_id = option_env!("GAMJA_BUILD_ID").unwrap_or("unknown");
        let build_id = if build_id.is_empty()
            || build_id.len() > 64
            || !build_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._+-".contains(&b))
        {
            "unknown".to_string()
        } else {
            build_id.to_string()
        };
        Self {
            event_id: new_id(),
            occurred_at: now(),
            component,
            stage,
            code,
            app_version,
            build_id,
            os: if cfg!(target_os = "windows") {
                Os::Windows
            } else {
                Os::Macos
            },
            os_version: platform_os_version(),
            arch: if cfg!(target_arch = "x86_64") {
                Arch::X86_64
            } else {
                Arch::Aarch64
            },
            diagnostic: None,
        }
    }

    fn envelope(
        &self,
        mode: Mode,
        report_id: String,
        transitions: Vec<Transition>,
        description: Option<String>,
    ) -> ReportEnvelope {
        ReportEnvelope {
            schema_version: 1,
            report_id,
            mode,
            app_version: self.app_version.clone(),
            build_id: self.build_id.clone(),
            os: self.os,
            os_version: self.os_version.clone(),
            arch: self.arch,
            occurred_at: self.occurred_at.clone(),
            component: self.component,
            stage: self.stage,
            code: self.code,
            occurrences: 1,
            transitions,
            diagnostics: self.diagnostic.iter().cloned().collect(),
            description,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubmissionDraft {
    pub envelope: ReportEnvelope,
    body: String,
}

impl SubmissionDraft {
    fn new(envelope: ReportEnvelope) -> Result<Self, String> {
        envelope.validate()?;
        let body = serde_json::to_string(&envelope).map_err(|_| "Invalid report")?;
        Ok(Self { envelope, body })
    }

    pub fn body(&self) -> &str {
        &self.body
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReportState {
    Pending,
    Sending,
    Accepted,
    Rejected,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalReport {
    pub draft: SubmissionDraft,
    pub state: ReportState,
    pub attempts: u16,
    #[serde(default)]
    pub attempt_times: Vec<i64>,
    #[serde(default)]
    pub next_attempt_at: i64,
    #[serde(default)]
    pub receipt: Option<super::Receipt>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReportSnapshot {
    pub events: Vec<DiagnosticEvent>,
    pub reports: Vec<LocalReport>,
    pub auto_reporting_enabled: bool,
    pub consent_generation: u64,
    pub discarded: u64,
    pub discarded_expired: u64,
    pub discarded_capacity: u64,
    #[serde(default)]
    pub auto_attempt_times: Vec<i64>,
}

pub struct ReportStore {
    dir: PathBuf,
    state: ReportSnapshot,
}

impl ReportStore {
    pub fn open(app_data: &Path) -> Result<Self, String> {
        let dir = app_data.join("reporting");
        fs::create_dir_all(&dir).map_err(|_| "Reporting storage unavailable")?;
        private_dir(&dir)?;
        let path = dir.join("state.json");
        let state = match fs::metadata(&path) {
            Ok(meta) if meta.len() as usize <= STORE_MAX_BYTES => fs::read(&path)
                .ok()
                .and_then(|bytes| serde_json::from_slice::<ReportSnapshot>(&bytes).ok())
                .filter(valid_state)
                .unwrap_or_default(),
            _ => ReportSnapshot::default(),
        };
        let mut store = Self { dir, state };
        if store.state.auto_reporting_enabled {
            store.state.consent_generation = store.state.consent_generation.saturating_add(1);
        }
        store.state.auto_reporting_enabled = false;
        store.state.reports.retain(|report| {
            report.draft.envelope.mode == Mode::Manual || report.state == ReportState::Accepted
        });
        for report in &mut store.state.reports {
            if report.state == ReportState::Sending {
                report.state = ReportState::Pending;
            }
        }
        store.expire_and_bound();
        store.persist()?;
        Ok(store)
    }

    pub fn list(&mut self) -> ReportSnapshot {
        let previous = self.state.discarded;
        self.expire_and_bound();
        if self.state.discarded != previous {
            let _ = self.persist();
        }
        self.state.clone()
    }
    pub fn snapshot_consent(&self) -> (bool, u64) {
        (
            self.state.auto_reporting_enabled,
            self.state.consent_generation,
        )
    }

    pub fn record(&mut self, event: DiagnosticEvent) -> Result<(), String> {
        if !valid_event(&event) {
            return Err("Invalid diagnostic event".into());
        }
        self.change(|state| {
            if state.auto_reporting_enabled {
                if let Some(report) = state.reports.iter_mut().rev().find(|report| {
                    let prior = &report.draft.envelope;
                    event.diagnostic.is_none()
                        && prior.diagnostics.is_empty()
                        && report.state == ReportState::Pending
                        && report.attempts == 0
                        && prior.mode == Mode::Automatic
                        && prior.component == event.component
                        && prior.stage == event.stage
                        && prior.code == event.code
                        && prior.build_id == event.build_id
                        && prior.app_version == event.app_version
                        && prior.occurrences < 255
                        && chrono::DateTime::parse_from_rfc3339(&prior.occurred_at).is_ok_and(
                            |first| {
                                chrono::DateTime::parse_from_rfc3339(&event.occurred_at).is_ok_and(
                                    |last| {
                                        let seconds = (last - first).num_seconds();
                                        (0..3600).contains(&seconds)
                                    },
                                )
                            },
                        )
                }) {
                    let mut envelope = report.draft.envelope.clone();
                    envelope.occurrences += 1;
                    report.draft = SubmissionDraft::new(envelope)?;
                    state.events.push(event);
                    return Ok(());
                }
                let draft = SubmissionDraft::new(event.envelope(
                    Mode::Automatic,
                    new_id(),
                    Vec::new(),
                    None,
                ))?;
                state.reports.push(LocalReport {
                    draft,
                    state: ReportState::Pending,
                    attempts: 0,
                    attempt_times: Vec::new(),
                    next_attempt_at: 0,
                    receipt: None,
                });
            }
            state.events.push(event);
            Ok(())
        })
    }

    pub fn set_auto_consent(&mut self, enabled: bool) -> Result<(), String> {
        if !enabled {
            if self.state.auto_reporting_enabled {
                self.state.consent_generation = self.state.consent_generation.saturating_add(1);
            }
            self.state.auto_reporting_enabled = false;
            self.state.reports.retain(|report| {
                report.draft.envelope.mode == Mode::Manual || report.state == ReportState::Accepted
            });
            self.expire_and_bound();
            // Keep revocation in memory even if persistence fails. The next open also starts off.
            return self.persist();
        }
        self.change(|state| {
            if state.auto_reporting_enabled != enabled {
                state.consent_generation = state.consent_generation.saturating_add(1);
                state.auto_reporting_enabled = enabled;
            }
            Ok(())
        })
    }

    pub fn create_manual_draft(
        &mut self,
        selected_event_ids: &[String],
        description: Option<String>,
    ) -> Result<SubmissionDraft, String> {
        let envelope = self.manual_envelope(new_id(), selected_event_ids, description)?;
        let draft = SubmissionDraft::new(envelope)?;
        self.change(|state| {
            state.reports.push(LocalReport {
                draft: draft.clone(),
                state: ReportState::Pending,
                attempts: 0,
                attempt_times: Vec::new(),
                next_attempt_at: 0,
                receipt: None,
            });
            Ok(())
        })?;
        self.get_draft(&draft.envelope.report_id)
            .ok_or_else(|| "Report expired".into())
    }

    pub fn update_manual_draft(
        &mut self,
        report_id: &str,
        selected_event_ids: &[String],
        description: Option<String>,
    ) -> Result<SubmissionDraft, String> {
        let existing = self
            .state
            .reports
            .iter()
            .find(|report| report.draft.envelope.report_id == report_id)
            .ok_or("Report not found")?;
        if is_expired(
            &existing.draft.envelope.occurred_at,
            chrono::Utc::now().timestamp(),
        ) {
            return Err("Report expired".into());
        }
        if existing.draft.envelope.mode != Mode::Manual || existing.attempts != 0 {
            return Err("Report is immutable after first attempt".into());
        }
        let draft = SubmissionDraft::new(self.manual_envelope(
            report_id.to_string(),
            selected_event_ids,
            description,
        )?)?;
        self.change(|state| {
            let report = state
                .reports
                .iter_mut()
                .find(|report| report.draft.envelope.report_id == report_id)
                .ok_or("Report not found")?;
            report.draft = draft.clone();
            Ok(())
        })?;
        self.get_draft(report_id)
            .ok_or_else(|| "Report expired".into())
    }

    pub fn mark_attempt_started(&mut self, report_id: &str) -> Result<SubmissionDraft, String> {
        self.change(|state| {
            let report = state
                .reports
                .iter_mut()
                .find(|report| report.draft.envelope.report_id == report_id)
                .ok_or("Report not found")?;
            if is_expired(
                &report.draft.envelope.occurred_at,
                chrono::Utc::now().timestamp(),
            ) {
                return Err("Report expired".into());
            }
            if report.state != ReportState::Pending {
                return Err("Report cannot be retried".into());
            }
            report.attempts = report.attempts.saturating_add(1);
            report.state = ReportState::Sending;
            Ok(())
        })?;
        self.get_draft(report_id)
            .ok_or_else(|| "Report expired".into())
    }

    pub fn begin_manual_attempt(&mut self, report_id: &str) -> Result<SubmissionDraft, String> {
        if self
            .get_draft(report_id)
            .ok_or("Report not found")?
            .envelope
            .mode
            != Mode::Manual
        {
            return Err("Manual submission required".into());
        }
        self.mark_attempt_started(report_id)
    }

    pub fn due_auto_ids(&mut self, now: i64) -> Vec<String> {
        self.list();
        if !self.state.auto_reporting_enabled || self.auto_attempts_today(now) >= 10 {
            return Vec::new();
        }
        self.state
            .reports
            .iter()
            .filter(|report| {
                report.draft.envelope.mode == Mode::Automatic
                    && report.state == ReportState::Pending
                    && report.next_attempt_at <= now
                    && !is_expired(&report.draft.envelope.occurred_at, now)
                    && report
                        .attempt_times
                        .iter()
                        .filter(|time| **time > now - 86_400)
                        .count()
                        < 3
            })
            .map(|report| report.draft.envelope.report_id.clone())
            .collect()
    }

    fn auto_attempts_today(&self, now: i64) -> usize {
        self.state
            .auto_attempt_times
            .iter()
            .filter(|time| **time > now - 86_400)
            .count()
    }

    pub fn begin_auto_attempt(
        &mut self,
        report_id: &str,
        generation: u64,
        now: i64,
    ) -> Result<SubmissionDraft, String> {
        self.change(|state| {
            if !state.auto_reporting_enabled || state.consent_generation != generation {
                return Err("Automatic reporting disabled".into());
            }
            state.auto_attempt_times.retain(|time| *time > now - 86_400);
            if state.auto_attempt_times.len() >= 10 {
                return Err("Automatic daily limit reached".into());
            }
            let report = state
                .reports
                .iter_mut()
                .find(|report| report.draft.envelope.report_id == report_id)
                .ok_or("Report not found")?;
            if report.draft.envelope.mode != Mode::Automatic
                || report.state != ReportState::Pending
                || is_expired(&report.draft.envelope.occurred_at, now)
                || report.next_attempt_at > now
            {
                return Err("Automatic report not due".into());
            }
            report.attempt_times.retain(|time| *time > now - 86_400);
            if report.attempt_times.len() >= 3 {
                return Err("Automatic report limit reached".into());
            }
            report.attempts = report.attempts.saturating_add(1);
            report.attempt_times.push(now);
            report.next_attempt_at =
                now + retry_delay(&report.draft.envelope.report_id, report.attempts);
            report.state = ReportState::Sending;
            state.auto_attempt_times.push(now);
            Ok(())
        })?;
        self.get_draft(report_id).ok_or("Report expired".into())
    }

    pub fn finish_retryable(
        &mut self,
        report_id: &str,
        generation: Option<u64>,
        now: i64,
    ) -> Result<(), String> {
        self.change(|state| {
            if generation.is_some_and(|expected| {
                expected != state.consent_generation || !state.auto_reporting_enabled
            }) {
                return Err("Consent changed".into());
            }
            let report = state
                .reports
                .iter_mut()
                .find(|report| report.draft.envelope.report_id == report_id)
                .ok_or("Report not found")?;
            if report.state != ReportState::Sending {
                return Err("Report not sending".into());
            }
            report.state = ReportState::Pending;
            if report.draft.envelope.mode == Mode::Automatic {
                report.next_attempt_at = report
                    .next_attempt_at
                    .max(now + retry_delay(&report.draft.envelope.report_id, report.attempts));
            }
            Ok(())
        })
    }

    pub fn finish_accepted(
        &mut self,
        report_id: &str,
        generation: Option<u64>,
        receipt: super::Receipt,
    ) -> Result<(), String> {
        self.finish_terminal(report_id, generation, ReportState::Accepted, Some(receipt))
    }

    pub fn finish_rejected(
        &mut self,
        report_id: &str,
        generation: Option<u64>,
    ) -> Result<(), String> {
        self.finish_terminal(report_id, generation, ReportState::Rejected, None)
    }

    fn finish_terminal(
        &mut self,
        report_id: &str,
        generation: Option<u64>,
        status: ReportState,
        receipt: Option<super::Receipt>,
    ) -> Result<(), String> {
        self.change(|state| {
            if generation.is_some_and(|expected| {
                expected != state.consent_generation || !state.auto_reporting_enabled
            }) {
                return Err("Consent changed".into());
            }
            let report = state
                .reports
                .iter_mut()
                .find(|report| report.draft.envelope.report_id == report_id)
                .ok_or("Report not found")?;
            if report.state != ReportState::Sending {
                return Err("Report not sending".into());
            }
            report.state = status;
            report.receipt = receipt;
            Ok(())
        })
    }

    pub fn get_draft(&self, report_id: &str) -> Option<SubmissionDraft> {
        let now = chrono::Utc::now().timestamp();
        self.state
            .reports
            .iter()
            .find(|report| {
                report.draft.envelope.report_id == report_id
                    && (report.state == ReportState::Accepted
                        || !is_expired(&report.draft.envelope.occurred_at, now))
            })
            .map(|report| report.draft.clone())
    }

    pub fn delete(&mut self, report_id: &str) -> Result<(), String> {
        self.change(|state| {
            state.events.retain(|event| event.event_id != report_id);
            state
                .reports
                .retain(|report| report.draft.envelope.report_id != report_id);
            Ok(())
        })
    }

    pub fn export_report(&self, report_id: &str) -> Result<PathBuf, String> {
        let draft = self.get_draft(report_id).ok_or("Report not found")?;
        let exports = self.dir.join("exports");
        fs::create_dir_all(&exports).map_err(|_| "Reporting export unavailable")?;
        private_dir(&exports)?;
        let path = exports.join(format!("{}.json", draft.envelope.report_id));
        atomic_write(&path, draft.body.as_bytes())?;
        Ok(path)
    }

    fn manual_envelope(
        &self,
        report_id: String,
        ids: &[String],
        description: Option<String>,
    ) -> Result<ReportEnvelope, String> {
        if ids.len() > 16 {
            return Err("Too many selected diagnostics".into());
        }
        let mut selected = Vec::with_capacity(ids.len());
        for id in ids {
            let event = self
                .state
                .events
                .iter()
                .find(|event| &event.event_id == id)
                .ok_or("Diagnostic event not found")?;
            if is_expired(&event.occurred_at, chrono::Utc::now().timestamp()) {
                return Err("Diagnostic event expired".into());
            }
            if selected
                .iter()
                .any(|prior: &&DiagnosticEvent| prior.event_id == event.event_id)
            {
                return Err("Duplicate diagnostic event".into());
            }
            selected.push(event);
        }
        let fallback =
            DiagnosticEvent::new(Component::Host, Stage::Runtime, Code::UnexpectedFailure);
        let first = selected.first().copied().unwrap_or(&fallback);
        let transitions = selected
            .iter()
            .skip(1)
            .map(|event| Transition {
                stage: event.stage,
                code: event.code,
            })
            .collect();
        let mut envelope = first.envelope(Mode::Manual, report_id, transitions, description);
        envelope.diagnostics = selected
            .iter()
            .filter_map(|event| event.diagnostic.clone())
            .collect();
        Ok(envelope)
    }

    fn change(
        &mut self,
        update: impl FnOnce(&mut ReportSnapshot) -> Result<(), String>,
    ) -> Result<(), String> {
        let mut next = self.state.clone();
        update(&mut next)?;
        let old = std::mem::replace(&mut self.state, next);
        self.expire_and_bound();
        if let Err(error) = self.persist() {
            self.state = old;
            return Err(error);
        }
        Ok(())
    }

    fn expire_and_bound(&mut self) {
        let now = chrono::Utc::now().timestamp();
        let before = self.state.events.len() + self.state.reports.len();
        self.state
            .events
            .retain(|event| !is_expired(&event.occurred_at, now));
        self.state.reports.retain(|report| {
            report.state == ReportState::Accepted
                || !is_expired(&report.draft.envelope.occurred_at, now)
        });
        let expired_count = (before - self.state.events.len() - self.state.reports.len()) as u64;
        self.state.discarded = self.state.discarded.saturating_add(expired_count);
        self.state.discarded_expired = self.state.discarded_expired.saturating_add(expired_count);
        while self.state.events.len() > STORE_MAX_EVENTS || state_len(&self.state) > STORE_MAX_BYTES
        {
            if !self.state.events.is_empty() {
                self.state.events.remove(0);
            } else if !self.state.reports.is_empty() {
                self.state.reports.remove(0);
            } else {
                break;
            }
            self.state.discarded = self.state.discarded.saturating_add(1);
            self.state.discarded_capacity = self.state.discarded_capacity.saturating_add(1);
        }
    }

    fn persist(&self) -> Result<(), String> {
        let bytes = serde_json::to_vec(&self.state).map_err(|_| "Reporting state invalid")?;
        if bytes.len() > STORE_MAX_BYTES {
            return Err("Reporting queue full".into());
        }
        atomic_write(&self.dir.join("state.json"), &bytes)
    }
}

fn is_expired(date: &str, now: i64) -> bool {
    chrono::DateTime::parse_from_rfc3339(date).map_or(true, |date| {
        now.saturating_sub(date.timestamp()) > TTL_SECONDS
    })
}

fn retry_delay(report_id: &str, attempts: u16) -> i64 {
    let base = (60_i64.saturating_mul(1 << attempts.saturating_sub(1).min(8))).min(6 * 3600);
    let jitter = report_id.bytes().fold(attempts as u64, |sum, byte| {
        sum.wrapping_mul(31).wrapping_add(byte as u64)
    }) % 31;
    base + jitter as i64
}

fn state_len(state: &ReportSnapshot) -> usize {
    serde_json::to_vec(state).map_or(usize::MAX, |bytes| bytes.len())
}

fn valid_state(state: &ReportSnapshot) -> bool {
    state_len(state) <= STORE_MAX_BYTES
        && state.events.len() <= STORE_MAX_EVENTS
        && state.events.iter().all(valid_event)
        && state.reports.iter().all(|report| {
            report.draft.envelope.validate().is_ok()
                && serde_json::to_string(&report.draft.envelope)
                    .ok()
                    .as_deref()
                    == Some(report.draft.body())
        })
}

fn valid_event(event: &DiagnosticEvent) -> bool {
    event
        .envelope(Mode::Automatic, event.event_id.clone(), Vec::new(), None)
        .validate()
        .is_ok()
}

fn private_dir(path: &Path) -> Result<(), String> {
    if fs::symlink_metadata(path)
        .map_err(|_| "Reporting storage unavailable")?
        .file_type()
        .is_symlink()
    {
        return Err("Reporting storage unavailable".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|_| "Reporting storage unavailable")?;
    }
    Ok(())
}

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let temp = path.with_extension(format!("tmp-{}", new_id()));
    let result = (|| {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&temp)
            .map_err(|_| "Reporting storage unavailable")?;
        file.write_all(bytes)
            .map_err(|_| "Reporting storage unavailable")?;
        file.sync_all()
            .map_err(|_| "Reporting storage unavailable")?;
        replace_atomically(&temp, path)?;
        #[cfg(unix)]
        {
            File::open(path.parent().ok_or("Reporting storage unavailable")?)
                .and_then(|dir| dir.sync_all())
                .map_err(|_| "Reporting storage unavailable")?;
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

#[cfg(not(target_os = "windows"))]
fn replace_atomically(source: &Path, destination: &Path) -> Result<(), String> {
    fs::rename(source, destination).map_err(|_| "Reporting storage unavailable".into())
}

#[cfg(target_os = "windows")]
fn replace_atomically(source: &Path, destination: &Path) -> Result<(), String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let wide = |path: &Path| {
        path.as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect::<Vec<_>>()
    };
    let source = wide(source);
    let destination = wide(destination);
    let result = unsafe {
        MoveFileExW(
            source.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if result == 0 {
        Err("Reporting storage unavailable".into())
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reporting::hex;
    use std::path::PathBuf;

    struct TestDir(PathBuf);
    impl TestDir {
        fn new() -> Self {
            let mut id = [0u8; 16];
            getrandom::fill(&mut id).unwrap();
            let path = std::env::temp_dir().join(format!("gamja-report-test-{}", hex(&id)));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TestDir {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }

    fn event() -> DiagnosticEvent {
        DiagnosticEvent::new(Component::Backend, Stage::Startup, Code::BackendInitFailed)
    }

    fn detailed_event() -> DiagnosticEvent {
        let mut event = DiagnosticEvent::new(
            Component::Backend,
            Stage::Runtime,
            Code::BackendRuntimeFailed,
        );
        event.diagnostic = Some(serde_json::from_value(serde_json::json!({
            "version": 1, "correlation_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "occurred_at": event.occurred_at, "stage": "tool_execution",
            "failure_code": "provider_unavailable", "reason": "unexpected_error", "error_class": "value_error",
            "room_id": 4, "agent_id": 7, "connection_id": 3,
            "trigger_event_id": null, "action_id": null,
            "exception_chain": [{"module": "builtins", "class": "ValueError"}],
            "frames": [{"file": "app/services/scheduler.py", "function": "_run", "line": 382}]
        })).unwrap());
        event
    }

    #[test]
    fn legacy_report_state_round_trips_without_loss() {
        let fixture = include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../backend/tests/fixtures/cli_core/reporting/state.json"
        ));
        let fixture_value: serde_json::Value = serde_json::from_str(fixture).unwrap();
        let mut snapshot: ReportSnapshot = serde_json::from_str(fixture).unwrap();
        assert!(valid_state(&snapshot));
        assert_eq!(serde_json::to_value(&snapshot).unwrap(), fixture_value);

        let occurred_at = crate::reporting::now();
        for event in &mut snapshot.events {
            event.occurred_at = occurred_at.clone();
        }
        for report in &mut snapshot.reports {
            report.draft.envelope.occurred_at = occurred_at.clone();
            report.draft.body = serde_json::to_string(&report.draft.envelope).unwrap();
        }
        let manual = snapshot.reports[0].clone();
        let pending_automatic_id = snapshot.reports[1].draft.envelope.report_id.clone();
        let accepted = snapshot.reports[2].clone();
        let auto_attempt_times = snapshot.auto_attempt_times.clone();

        let dir = TestDir::new();
        std::fs::create_dir(dir.0.join("reporting")).unwrap();
        std::fs::write(
            dir.0.join("reporting/state.json"),
            serde_json::to_vec(&snapshot).unwrap(),
        )
        .unwrap();
        let mut store = ReportStore::open(&dir.0).unwrap();
        assert_eq!(store.snapshot_consent(), (false, 5));
        let reopened = store.list();
        assert_eq!(reopened.reports.len(), 2);
        assert!(reopened
            .reports
            .iter()
            .all(|report| report.draft.envelope.report_id != pending_automatic_id));

        let reopened_manual = reopened
            .reports
            .iter()
            .find(|report| report.draft.envelope.report_id == manual.draft.envelope.report_id)
            .unwrap();
        assert_eq!(reopened_manual.draft.body(), manual.draft.body());
        assert_eq!(reopened_manual.state, ReportState::Pending);
        assert_eq!(reopened_manual.attempts, manual.attempts);
        assert_eq!(reopened_manual.attempt_times, manual.attempt_times);
        assert_eq!(reopened_manual.next_attempt_at, manual.next_attempt_at);

        let reopened_accepted = reopened
            .reports
            .iter()
            .find(|report| report.draft.envelope.report_id == accepted.draft.envelope.report_id)
            .unwrap();
        assert_eq!(reopened_accepted.draft.body(), accepted.draft.body());
        assert_eq!(reopened_accepted.state, ReportState::Accepted);
        assert_eq!(reopened_accepted.attempts, accepted.attempts);
        assert_eq!(reopened_accepted.attempt_times, accepted.attempt_times);
        assert_eq!(reopened_accepted.next_attempt_at, accepted.next_attempt_at);
        assert_eq!(reopened_accepted.receipt, accepted.receipt);
        assert_eq!(reopened.auto_attempt_times, auto_attempt_times);
    }

    #[test]
    fn detailed_failure_survives_manual_preview_and_opted_in_auto_without_coalescing() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        let first = detailed_event();
        store.record(first.clone()).unwrap();
        assert!(store.list().reports.is_empty());
        let manual = store.create_manual_draft(&[first.event_id], None).unwrap();
        assert_eq!(manual.envelope.diagnostics, vec![first.diagnostic.unwrap()]);
        assert!(manual.body().contains("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));
        store.set_auto_consent(true).unwrap();
        let second = detailed_event();
        store.record(second.clone()).unwrap();
        let third = detailed_event();
        store.record(third).unwrap();
        let automatic: Vec<_> = store
            .list()
            .reports
            .into_iter()
            .filter(|report| report.draft.envelope.mode == Mode::Automatic)
            .collect();
        assert_eq!(automatic.len(), 2);
        assert_eq!(
            automatic[0].draft.envelope.diagnostics,
            vec![second.diagnostic.unwrap()]
        );
        assert_eq!(automatic[0].draft.envelope.occurrences, 1);
        store.set_auto_consent(false).unwrap();
        let reports = store.list().reports;
        assert_eq!(reports.len(), 1);
        assert_eq!(reports[0].draft.body(), manual.body());
    }

    #[test]
    fn consent_defaults_off_and_revocation_persists_without_deleting_manual() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        assert_eq!(store.snapshot_consent(), (false, 0));
        let before = event();
        store.record(before).unwrap();
        assert_eq!(store.list().reports.len(), 0);
        store.set_auto_consent(true).unwrap();
        assert_eq!(store.snapshot_consent(), (true, 1));
        let after = event();
        store.record(after.clone()).unwrap();
        assert_eq!(store.list().reports.len(), 1);
        let manual = store
            .create_manual_draft(&[after.event_id], Some("help".into()))
            .unwrap();
        store.set_auto_consent(false).unwrap();
        assert_eq!(store.snapshot_consent(), (false, 2));
        assert_eq!(store.list().reports.len(), 1);
        assert_eq!(
            store.list().reports[0].draft.envelope.report_id,
            manual.envelope.report_id
        );
        let mut reopened = ReportStore::open(&dir.0).unwrap();
        assert_eq!(reopened.snapshot_consent(), (false, 2));
        assert_eq!(reopened.list().reports.len(), 1);
    }

    #[test]
    fn manual_edits_stop_after_first_attempt_and_wire_remains_identical() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        let selected = event();
        store.record(selected.clone()).unwrap();
        let draft = store
            .create_manual_draft(
                std::slice::from_ref(&selected.event_id),
                Some("first".into()),
            )
            .unwrap();
        let updated = store
            .update_manual_draft(
                &draft.envelope.report_id,
                &[selected.event_id],
                Some("second".into()),
            )
            .unwrap();
        let attempted = store
            .mark_attempt_started(&draft.envelope.report_id)
            .unwrap();
        assert_eq!(attempted.body(), updated.body());
        assert!(store
            .update_manual_draft(&draft.envelope.report_id, &[], None)
            .is_err());
        assert_eq!(
            store.get_draft(&draft.envelope.report_id).unwrap().body(),
            attempted.body()
        );
    }

    #[test]
    fn expiry_corruption_and_private_export_fail_closed() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        let selected = event();
        store.record(selected.clone()).unwrap();
        let draft = store
            .create_manual_draft(&[selected.event_id], None)
            .unwrap();
        let export = store.export_report(&draft.envelope.report_id).unwrap();
        assert!(export.starts_with(&dir.0));
        assert_eq!(std::fs::read(export).unwrap(), draft.body().as_bytes());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(dir.0.join("reporting"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
            assert_eq!(
                std::fs::metadata(dir.0.join("reporting/state.json"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        store.set_auto_consent(true).unwrap();
        std::fs::write(dir.0.join("reporting/state.json"), b"{broken").unwrap();
        let mut recovered = ReportStore::open(&dir.0).unwrap();
        assert!(!recovered.snapshot_consent().0);
        assert!(recovered.list().reports.is_empty());
    }

    #[test]
    fn queue_evicts_to_event_and_byte_limits() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        for _ in 0..300 {
            store.record(event()).unwrap();
        }
        assert!(store.list().events.len() <= 256);
        assert!(
            std::fs::metadata(dir.0.join("reporting/state.json"))
                .unwrap()
                .len()
                <= 65536
        );
        assert!(store.list().discarded > 0);
        assert!(store.list().discarded_capacity > 0);
    }

    #[test]
    fn expired_unsent_and_unsafe_metadata_do_not_enter_queue() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        let mut unsafe_event = event();
        unsafe_event.app_version = "/Users/alice/secret".into();
        assert!(store.record(unsafe_event).is_err());
        let mut old = event();
        old.occurred_at = "2020-01-01T00:00:00Z".into();
        store.record(old).unwrap();
        assert!(store.list().events.is_empty());
        assert_eq!(store.list().discarded, 1);
        assert_eq!(store.list().discarded_expired, 1);
    }

    #[test]
    fn an_aged_pending_draft_cannot_be_sent_or_exported_before_pruning() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        let selected = event();
        store.record(selected.clone()).unwrap();
        let draft = store
            .create_manual_draft(
                std::slice::from_ref(&selected.event_id),
                Some("pending".into()),
            )
            .unwrap();
        let aged = &mut store.state.reports[0].draft;
        aged.envelope.occurred_at = "2020-01-01T00:00:00Z".into();
        aged.body = serde_json::to_string(&aged.envelope).unwrap();
        assert!(store.get_draft(&draft.envelope.report_id).is_none());
        assert!(store
            .mark_attempt_started(&draft.envelope.report_id)
            .is_err());
        assert!(store.export_report(&draft.envelope.report_id).is_err());
        store.state.events[0].occurred_at = "2020-01-01T00:00:00Z".into();
        assert!(store
            .create_manual_draft(&[selected.event_id], None)
            .is_err());
    }

    #[test]
    fn an_aged_automatic_draft_cannot_start_after_consent() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        store.set_auto_consent(true).unwrap();
        store.record(event()).unwrap();
        let id = store.state.reports[0].draft.envelope.report_id.clone();
        store.state.reports[0].draft.envelope.occurred_at = "2020-01-01T00:00:00Z".into();
        assert!(store.get_draft(&id).is_none());
        assert!(store.mark_attempt_started(&id).is_err());
        store.set_auto_consent(false).unwrap();
        assert_eq!(store.snapshot_consent(), (false, 2));
        assert!(store.list().reports.is_empty());
    }

    #[test]
    fn automatic_duplicate_coalesces_only_before_first_attempt() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        store.set_auto_consent(true).unwrap();
        store.record(event()).unwrap();
        let first_id = store.list().reports[0].draft.envelope.report_id.clone();
        store.record(event()).unwrap();
        assert_eq!(store.list().reports.len(), 1);
        assert_eq!(store.list().reports[0].draft.envelope.occurrences, 2);
        store.mark_attempt_started(&first_id).unwrap();
        store.record(event()).unwrap();
        assert_eq!(store.list().reports.len(), 2);
    }

    #[test]
    fn automatic_attempt_history_survives_restart_but_pending_report_does_not() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        store.set_auto_consent(true).unwrap();
        store.record(event()).unwrap();
        let (enabled, generation) = store.snapshot_consent();
        assert!(enabled);
        let id = store.list().reports[0].draft.envelope.report_id.clone();
        let now = chrono::Utc::now().timestamp();
        let body = store
            .begin_auto_attempt(&id, generation, now)
            .unwrap()
            .body()
            .to_string();
        store.finish_retryable(&id, Some(generation), now).unwrap();
        assert_eq!(store.get_draft(&id).unwrap().body(), body);
        let mut reopened = ReportStore::open(&dir.0).unwrap();
        assert_eq!(reopened.list().auto_attempt_times.len(), 1);
        assert!(reopened.get_draft(&id).is_none());
        assert!(reopened
            .begin_auto_attempt(&id, generation, now + 1)
            .is_err());
        reopened.set_auto_consent(false).unwrap();
        assert!(reopened
            .begin_auto_attempt(&id, generation, now + 86_400)
            .is_err());
        assert!(reopened.list().reports.is_empty());
    }

    #[test]
    fn automatic_caps_are_persisted_across_restart() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        store.set_auto_consent(true).unwrap();
        let now = chrono::Utc::now().timestamp();
        let generation = store.snapshot_consent().1;
        for component in [Component::Host, Component::Backend, Component::Frontend] {
            for stage in [Stage::Startup, Stage::Ready, Stage::Runtime, Stage::Tool] {
                store
                    .record(DiagnosticEvent::new(
                        component,
                        stage,
                        Code::UnexpectedFailure,
                    ))
                    .unwrap();
            }
        }
        let ids: Vec<_> = store
            .list()
            .reports
            .iter()
            .map(|report| report.draft.envelope.report_id.clone())
            .collect();
        assert_eq!(ids.len(), 12);
        for id in &ids[..10] {
            store.begin_auto_attempt(id, generation, now).unwrap();
            store.finish_retryable(id, Some(generation), now).unwrap();
        }
        let mut reopened = ReportStore::open(&dir.0).unwrap();
        assert!(reopened
            .begin_auto_attempt(&ids[10], generation, now)
            .is_err());
        assert_eq!(reopened.list().auto_attempt_times.len(), 10);
    }

    #[test]
    fn one_auto_report_gets_only_three_attempts_per_rolling_day() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        store.set_auto_consent(true).unwrap();
        store.record(event()).unwrap();
        let id = store.list().reports[0].draft.envelope.report_id.clone();
        let generation = store.snapshot_consent().1;
        let now = chrono::Utc::now().timestamp();
        for attempt in 0..3 {
            store
                .begin_auto_attempt(&id, generation, now + attempt)
                .unwrap();
            store
                .finish_retryable(&id, Some(generation), now + attempt)
                .unwrap();
            store.state.reports[0].next_attempt_at = 0;
        }
        store.persist().unwrap();
        let mut reopened = ReportStore::open(&dir.0).unwrap();
        assert!(reopened
            .begin_auto_attempt(&id, generation, now + 5)
            .is_err());
    }

    #[test]
    fn failed_opt_out_revokes_in_memory_and_restart_stays_off() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        store.set_auto_consent(true).unwrap();
        store.record(event()).unwrap();
        let persisted_dir = store.dir.clone();
        store.dir = dir.0.join("missing-reporting-directory");
        assert!(store.set_auto_consent(false).is_err());
        assert_eq!(store.snapshot_consent(), (false, 2));
        assert!(store
            .due_auto_ids(chrono::Utc::now().timestamp())
            .is_empty());
        assert!(store.list().reports.is_empty());
        store.dir = persisted_dir;
        drop(store);
        let mut reopened = ReportStore::open(&dir.0).unwrap();
        assert_eq!(reopened.snapshot_consent(), (false, 2));
        assert!(reopened.list().reports.is_empty());
    }

    #[test]
    fn automatic_opt_in_expires_at_app_restart() {
        let dir = TestDir::new();
        let mut store = ReportStore::open(&dir.0).unwrap();
        store.set_auto_consent(true).unwrap();
        store.record(event()).unwrap();
        drop(store);
        let mut reopened = ReportStore::open(&dir.0).unwrap();
        assert_eq!(reopened.snapshot_consent(), (false, 2));
        assert!(reopened.list().reports.is_empty());
    }
}
