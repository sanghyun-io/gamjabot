//! Private launch pipe contract. No credential is emitted on stdout or argv.
use serde::{Deserialize, Serialize};
pub const VERSION: u8 = 1;
#[derive(Deserialize, Serialize)]
pub struct Bootstrap {
    pub version: u8,
    pub key: String,
    pub endpoint: String,
}
#[derive(Deserialize, Serialize)]
pub struct Ready {
    pub version: u8,
    pub endpoint: String,
    pub ready: bool,
}
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SettingsPanel {
    ScreenRecording,
    Accessibility,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PermissionState {
    Granted,
    NotGranted,
    Unsupported,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PermissionStatus {
    pub screen_recording: PermissionState,
    pub accessibility: PermissionState,
}
impl PermissionStatus {
    pub fn unsupported() -> Self {
        Self {
            screen_recording: PermissionState::Unsupported,
            accessibility: PermissionState::Unsupported,
        }
    }
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum OwnerOperation {
    PermissionStatus,
    OpenSettings,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OwnerControl {
    pub version: u8,
    pub request_id: String,
    pub deadline_ms: u64,
    pub operation: OwnerOperation,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub panel: Option<SettingsPanel>,
}
