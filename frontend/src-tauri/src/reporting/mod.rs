mod diagnostic;
pub mod sender;
mod store;

pub use diagnostic::*;
use serde::{Deserialize, Serialize};
pub use store::*;

pub const WIRE_MAX_BYTES: usize = 4096;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Receipt {
    pub report_id: String,
    pub receipt_id: String,
    pub accepted_at: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Manual,
    Automatic,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Os {
    Macos,
    Windows,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Arch {
    Aarch64,
    X86_64,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Component {
    Host,
    Backend,
    Frontend,
    Native,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    Startup,
    Ready,
    Runtime,
    Tool,
    Render,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Code {
    SidecarMissing,
    SidecarSpawnFailed,
    BackendInitFailed,
    BackendNotReady,
    SidecarExited,
    BackendRuntimeFailed,
    FrontendRenderFailed,
    NativeActionFailed,
    UnexpectedFailure,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Transition {
    pub stage: Stage,
    pub code: Code,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReportEnvelope {
    pub schema_version: u8,
    pub report_id: String,
    pub mode: Mode,
    pub app_version: String,
    pub build_id: String,
    pub os: Os,
    pub os_version: String,
    pub arch: Arch,
    pub occurred_at: String,
    pub component: Component,
    pub stage: Stage,
    pub code: Code,
    pub occurrences: u8,
    pub transitions: Vec<Transition>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub diagnostics: Vec<ReportDiagnostic>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
}

impl ReportEnvelope {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1
            || self.report_id.len() != 32
            || !self.report_id.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err("Invalid report identity".into());
        }
        for value in [&self.app_version, &self.build_id] {
            if value.is_empty()
                || value.len() > 64
                || !value
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._+-".contains(&b))
            {
                return Err("Invalid build metadata".into());
            }
        }
        if self.os_version != normalize_os_version(&self.os_version) {
            return Err("Invalid OS version".into());
        }
        if self.occurred_at.len() > 32
            || !self.occurred_at.ends_with('Z')
            || chrono::DateTime::parse_from_rfc3339(&self.occurred_at).is_err()
            || self.occurrences == 0
            || self.transitions.len() > 16
            || self.diagnostics.len() > 16
        {
            return Err("Invalid diagnostic metadata".into());
        }
        for diagnostic in &self.diagnostics {
            diagnostic.validate()?;
        }
        match (&self.mode, &self.description) {
            (Mode::Automatic, Some(_)) => return Err("Automatic description forbidden".into()),
            (Mode::Manual, Some(text)) if text.chars().count() > 1000 => {
                return Err("Description too long".into())
            }
            _ => {}
        }
        if serde_json::to_vec(self)
            .map_err(|_| "Invalid report")?
            .len()
            > WIRE_MAX_BYTES
        {
            return Err("Report exceeds wire limit".into());
        }
        Ok(())
    }
}

pub fn normalize_os_version(value: &str) -> String {
    if value.is_empty()
        || value.len() > 32
        || value.starts_with('.')
        || value.ends_with('.')
        || value
            .split('.')
            .any(|part| part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()))
    {
        "unknown".into()
    } else {
        value.into()
    }
}

#[cfg(target_os = "macos")]
pub(crate) fn platform_os_version() -> String {
    let mut bytes = [0u8; 33];
    let mut len = bytes.len();
    let result = unsafe {
        libc::sysctlbyname(
            b"kern.osproductversion\0".as_ptr().cast(),
            bytes.as_mut_ptr().cast(),
            &mut len,
            std::ptr::null_mut(),
            0,
        )
    };
    if result != 0 || len == 0 || len > bytes.len() {
        return "unknown".into();
    }
    let end = bytes[..len]
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(len);
    std::str::from_utf8(&bytes[..end])
        .map(normalize_os_version)
        .unwrap_or_else(|_| "unknown".into())
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn platform_os_version() -> String {
    "unknown".into()
}

pub(crate) fn new_id() -> String {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).expect("OS random source unavailable");
    hex(&bytes)
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 15) as usize] as char);
    }
    output
}

pub(crate) fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

pub(crate) fn record_typed_failure(
    app: &tauri::AppHandle,
    component: Component,
    stage: Stage,
    code: Code,
) -> Result<(), String> {
    record_diagnostic_failure(app, component, stage, code, None)
}

pub(crate) fn record_diagnostic_failure(
    app: &tauri::AppHandle,
    component: Component,
    stage: Stage,
    code: Code,
    diagnostic: Option<ReportDiagnostic>,
) -> Result<(), String> {
    use tauri::Manager;
    let state = app.state::<crate::ReportingState>();
    let mut guard = state.lock().map_err(|_| "Reporting storage unavailable")?;
    let mut event = DiagnosticEvent::new(component, stage, code);
    event.diagnostic = diagnostic;
    guard
        .as_mut()
        .ok_or("Reporting storage unavailable")?
        .record(event)
}

#[derive(Serialize)]
pub struct ReportStatus {
    pub auto_reporting_enabled: bool,
    pub consent_generation: u64,
    pub submission_available: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_fixture_is_wire_bounded_and_invalid_fixture_is_rejected() {
        let valid: serde_json::Value =
            serde_json::from_str(include_str!("../../../../reporting/fixtures/valid.json"))
                .unwrap();
        let invalid: serde_json::Value =
            serde_json::from_str(include_str!("../../../../reporting/fixtures/invalid.json"))
                .unwrap();
        assert!(valid
            .as_array()
            .unwrap()
            .iter()
            .any(|value| serde_json::to_vec(value).unwrap().len() >= 4000));
        let near_limit: ReportEnvelope =
            serde_json::from_value(valid.as_array().unwrap().last().unwrap().clone()).unwrap();
        assert_eq!(serde_json::to_vec(&near_limit).unwrap().len(), 4095);
        let over_limit: ReportEnvelope =
            serde_json::from_value(invalid.as_array().unwrap().last().unwrap().clone()).unwrap();
        assert_eq!(serde_json::to_vec(&over_limit).unwrap().len(), 4097);
        for value in valid.as_array().unwrap() {
            assert!(serde_json::to_vec(value).unwrap().len() <= 4096);
            assert!(serde_json::from_value::<ReportEnvelope>(value.clone())
                .unwrap()
                .validate()
                .is_ok());
        }
        for value in invalid.as_array().unwrap() {
            assert!(serde_json::from_value::<ReportEnvelope>(value.clone())
                .map_or(true, |report| report.validate().is_err()));
        }
    }

    #[test]
    fn os_version_accepts_only_numeric_dotted_values() {
        assert_eq!(normalize_os_version("14.7.1"), "14.7.1");
        assert_eq!(normalize_os_version("14.7.1 user=alice"), "unknown");
        assert_eq!(normalize_os_version(""), "unknown");
        assert_eq!(normalize_os_version(&"1".repeat(33)), "unknown");
    }

    #[test]
    fn multibyte_manual_text_is_rejected_at_wire_limit() {
        let values: Vec<ReportEnvelope> =
            serde_json::from_str(include_str!("../../../../reporting/fixtures/valid.json"))
                .unwrap();
        let mut report = values[0].clone();
        report.description = Some("😀".repeat(1000));
        assert_eq!(report.description.as_ref().unwrap().chars().count(), 1000);
        assert_eq!(report.validate().unwrap_err(), "Report exceeds wire limit");
    }
}
