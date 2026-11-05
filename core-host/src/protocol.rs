pub use gamja_platform_helper::protocol::{PermissionState, PermissionStatus, SettingsPanel};
use serde::{Deserialize, Serialize};
use url::Url;

pub const ENDPOINT_FILE_VERSION: u16 = 1;
pub const CORE_PROTOCOL_VERSION: u16 = 1;
pub const ENGINE_PROTOCOL_VERSION: u16 = 1;
pub const CAPABILITY_HEX_LEN: usize = 64;
pub const REPORT_ID_HEX_LEN: usize = 32;
pub const MAX_CONTROL_BYTES: usize = 4096;
// 256 selected event IDs plus 1000 JSON-escaped description characters.
pub const MAX_REPORT_CONTROL_BYTES: usize = 16_384;
pub const MAX_REPORT_EVENT_IDS: usize = 256;
pub const MAX_REPORT_DESCRIPTION_CHARS: usize = 1000;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EndpointFile {
    pub version: u16,
    pub api_url: String,
    pub control_url: String,
    pub capability: String,
    pub core_version: u16,
    pub engine_version: u16,
}

impl EndpointFile {
    pub fn from_json(value: &str) -> Result<Self, String> {
        if value.len() > MAX_CONTROL_BYTES {
            return Err("Endpoint file is too large".into());
        }
        let endpoint: Self =
            serde_json::from_str(value).map_err(|_| "Invalid endpoint file".to_string())?;
        endpoint.validate()?;
        Ok(endpoint)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.version != ENDPOINT_FILE_VERSION
            || self.core_version != CORE_PROTOCOL_VERSION
            || self.engine_version != ENGINE_PROTOCOL_VERSION
        {
            return Err("Unsupported endpoint protocol".into());
        }
        validate_loopback_url(&self.api_url)?;
        validate_loopback_url(&self.control_url)?;
        if !is_lower_hex(&self.capability, CAPABILITY_HEX_LEN) {
            return Err("Invalid endpoint capability".into());
        }
        Ok(())
    }
}

fn validate_loopback_url(value: &str) -> Result<(), String> {
    let parsed = Url::parse(value).map_err(|_| "Invalid endpoint URL".to_string())?;
    let port = parsed
        .port_or_known_default()
        .ok_or_else(|| "Invalid endpoint URL".to_string())?;
    let canonical = format!("http://127.0.0.1:{port}");
    if parsed.scheme() != "http"
        || parsed.host_str() != Some("127.0.0.1")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || value != canonical
    {
        return Err("Endpoint URL must be bare loopback HTTP".into());
    }
    Ok(())
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[derive(Clone, PartialEq, Eq)]
pub struct OwnerSecret(zeroize::Zeroizing<String>);
impl OwnerSecret { pub fn new(value:String)->Self {Self(zeroize::Zeroizing::new(value))} pub(crate) fn as_str(&self)->&str {self.0.as_str()} }
impl std::fmt::Debug for OwnerSecret { fn fmt(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result {f.write_str("<private>")} }
impl serde::Serialize for OwnerSecret {fn serialize<S:serde::Serializer>(&self,s:S)->std::result::Result<S::Ok,S::Error>{s.serialize_str(self.as_str())}}
impl<'de> serde::Deserialize<'de> for OwnerSecret {fn deserialize<D:serde::Deserializer<'de>>(d:D)->std::result::Result<Self,D::Error>{String::deserialize(d).map(Self::new)}}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum ControlRequest {
    Health {},
    CreateUiSession {},
    Stop {},
    SlackCredentialStatus { owner_id:u64 },
    SlackCredentialStore { owner_id:u64, owner_epoch:String, core_epoch:String, account_id:String, expected_revision:u64, operation_id:String, app_token:OwnerSecret, bot_token:OwnerSecret },
    SlackCredentialDelete { owner_id:u64, owner_epoch:String, core_epoch:String, account_id:String, expected_revision:u64, operation_id:String },
    McpSecretStore { server_id:String, expected_revision:u64, operation_id:String, secret:OwnerSecret },
    McpSecretStatus { server_id:String, expected_revision:u64, operation_id:String },
    McpOAuthBegin { server_id:String, expected_revision:u64, operation_id:String },
    McpOAuthStatus { server_id:String, expected_revision:u64, operation_id:String },
    McpOAuthCancel { server_id:String, expected_revision:u64, operation_id:String },
    NativePermissionStatus {},
    NativeOpenSettings {
        panel: SettingsPanel,
    },
    ReportList {},
    ReportFrontendFailure {},
    ReportCreate {
        event_ids: Vec<String>,
        description: Option<String>,
    },
    ReportUpdate {
        report_id: String,
        event_ids: Vec<String>,
        description: Option<String>,
    },
    ReportDelete {
        report_id: String,
    },
    ReportExport {
        report_id: String,
    },
    ReportConsent {
        enabled: bool,
    },
    ReportStatus {},
    ReportReceipt {
        report_id: String,
    },
    ReportSubmit {
        report_id: String,
    },
}

impl ControlRequest {
    pub fn from_json(value: &[u8]) -> Result<Self, String> {
        if value.len() > MAX_REPORT_CONTROL_BYTES {
            return Err("Control request is too large".into());
        }
        let request: Self =
            serde_json::from_slice(value).map_err(|_| "Invalid control request".to_string())?;
        request.validate()?;
        Ok(request)
    }

    pub fn validate(&self) -> Result<(), String> {
        if let Self::McpSecretStore{server_id,expected_revision,operation_id,secret} = self {
            if !valid_mcp_secret_metadata(server_id,*expected_revision,operation_id) || secret.as_str().is_empty() || secret.as_str().len()>32768 {return Err("Invalid MCP secret request".into());}
        }
        if let Self::McpSecretStatus{server_id,expected_revision,operation_id} = self {
            if !valid_mcp_secret_metadata(server_id,*expected_revision,operation_id) {return Err("Invalid MCP secret request".into());}
        }
        if let Self::McpOAuthBegin{server_id,expected_revision,operation_id} | Self::McpOAuthStatus{server_id,expected_revision,operation_id} | Self::McpOAuthCancel{server_id,expected_revision,operation_id} = self {
            if !valid_mcp_secret_metadata(server_id,*expected_revision,operation_id) {return Err("Invalid MCP OAuth request".into());}
        }
        let (report_id, event_ids, description) = match self {
            Self::ReportCreate {
                event_ids,
                description,
            } => (None, Some(event_ids), description.as_deref()),
            Self::ReportUpdate {
                report_id,
                event_ids,
                description,
            } => (Some(report_id), Some(event_ids), description.as_deref()),
            Self::ReportDelete { report_id }
            | Self::ReportExport { report_id }
            | Self::ReportSubmit { report_id }
            | Self::ReportReceipt { report_id } => (Some(report_id), None, None),
            _ => (None, None, None),
        };
        if report_id.is_some_and(|value| !is_lower_hex(value, REPORT_ID_HEX_LEN)) {
            return Err("Invalid report identity".into());
        }
        if event_ids.is_some_and(|values| {
            values.len() > MAX_REPORT_EVENT_IDS
                || values
                    .iter()
                    .any(|value| !is_lower_hex(value, REPORT_ID_HEX_LEN))
        }) {
            return Err("Invalid report event selection".into());
        }
        if description.is_some_and(|value| value.chars().count() > MAX_REPORT_DESCRIPTION_CHARS) {
            return Err("Report description is too long".into());
        }
        if serde_json::to_vec(self).map_or(true, |value| zeroize::Zeroizing::new(value).len() > MAX_REPORT_CONTROL_BYTES) {
            return Err("Control request is too large".into());
        }
        Ok(())
    }
}

fn valid_mcp_secret_metadata(server:&str, revision:u64, operation:&str)->bool {
    revision>0 && is_lower_hex(operation,32) && server.len()==36 && server.bytes().enumerate().all(|(i,b)|if matches!(i,8|13|18|23){b==b'-'}else{b.is_ascii_digit()||(b'a'..=b'f').contains(&b)})
}

#[cfg(test)]
mod protocol_tests {
    use super::*;
    use serde_json::json;

    fn valid_endpoint() -> serde_json::Value {
        json!({
            "version": 1,
            "api_url": "http://127.0.0.1:41001",
            "control_url": "http://127.0.0.1:41002",
            "capability": "ab".repeat(32),
            "core_version": 1,
            "engine_version": 1
        })
    }

    #[test]
    fn endpoint_rejects_unknown_fields_and_non_loopback_urls() {
        let endpoint = EndpointFile::from_json(&valid_endpoint().to_string()).unwrap();
        assert_eq!(endpoint.version, ENDPOINT_FILE_VERSION);
        assert_eq!(endpoint.core_version, CORE_PROTOCOL_VERSION);
        assert_eq!(endpoint.engine_version, ENGINE_PROTOCOL_VERSION);
        assert_eq!(endpoint.capability.len(), CAPABILITY_HEX_LEN);
        let mut default_port = valid_endpoint();
        default_port["api_url"] = json!("http://127.0.0.1:80");
        EndpointFile::from_json(&default_port.to_string()).unwrap();

        let mut invalid = Vec::new();
        let mut unknown = valid_endpoint();
        unknown["session_token"] = json!("private-webview-secret");
        invalid.push(unknown);
        for field in ["api_url", "control_url"] {
            for url in [
                "https://127.0.0.1:41001",
                "http://localhost:41001",
                "http://192.0.2.1:41001",
                "http://owner@127.0.0.1:41001",
                "http://127.0.0.1",
                "http://127.0.0.1:041001",
                "http://127.0.0.1:41001/",
                "http://127.0.0.1:41001/path",
                "http://127.0.0.1:41001?secret=value",
                "http://127.0.0.1:41001#fragment",
            ] {
                let mut value = valid_endpoint();
                value[field] = json!(url);
                invalid.push(value);
            }
        }
        for capability in ["aa".repeat(31), "AA".repeat(32), "gg".repeat(32)] {
            let mut value = valid_endpoint();
            value["capability"] = json!(capability);
            invalid.push(value);
        }
        for field in ["version", "core_version", "engine_version"] {
            let mut value = valid_endpoint();
            value[field] = json!(2);
            invalid.push(value);
        }

        for value in invalid {
            assert!(
                EndpointFile::from_json(&value.to_string()).is_err(),
                "accepted {value}"
            );
        }
    }

    #[test]
    fn control_protocol_has_bounded_private_operations() {
        assert_eq!(
            serde_json::from_value::<ControlRequest>(json!({"operation": "health"})).unwrap(),
            ControlRequest::Health {}
        );
        assert_eq!(
            serde_json::from_value::<ControlRequest>(json!({"operation": "create_ui_session"}))
                .unwrap(),
            ControlRequest::CreateUiSession {}
        );
        assert_eq!(
            serde_json::from_value::<ControlRequest>(json!({"operation": "stop"})).unwrap(),
            ControlRequest::Stop {}
        );
        assert!(serde_json::from_value::<ControlRequest>(
            json!({"operation": "health", "capability": "must-stay-in-header"})
        )
        .is_err());
        for request in [
            json!({
                "operation": "report_create",
                "event_ids": ["1".repeat(REPORT_ID_HEX_LEN)],
                "description": null
            }),
            json!({
                "operation": "report_update",
                "report_id": "2".repeat(REPORT_ID_HEX_LEN),
                "event_ids": ["1".repeat(REPORT_ID_HEX_LEN)],
                "description": null
            }),
            json!({"operation": "report_delete", "report_id": "2".repeat(REPORT_ID_HEX_LEN)}),
            json!({"operation": "report_export", "report_id": "2".repeat(REPORT_ID_HEX_LEN)}),
            json!({"operation": "report_submit", "report_id": "2".repeat(REPORT_ID_HEX_LEN)}),
        ] {
            ControlRequest::from_json(&serde_json::to_vec(&request).unwrap()).unwrap();
        }
        assert!(serde_json::from_value::<ControlRequest>(json!({
            "operation": "report_create",
            "event_ids": vec!["a".repeat(REPORT_ID_HEX_LEN); MAX_REPORT_EVENT_IDS + 1],
            "description": null
        }))
        .unwrap()
        .validate()
        .is_err());
        assert!(serde_json::from_value::<ControlRequest>(json!({
            "operation": "report_create",
            "event_ids": ["a".repeat(REPORT_ID_HEX_LEN)],
            "description": "x".repeat(MAX_REPORT_DESCRIPTION_CHARS + 1)
        }))
        .unwrap()
        .validate()
        .is_err());
    }
    #[test]
    fn native_owner_controls_accept_only_typed_fixed_settings() {
        for value in [
            json!({"operation":"native_permission_status"}),
            json!({"operation":"native_open_settings","panel":"screen_recording"}),
            json!({"operation":"native_open_settings","panel":"accessibility"}),
        ] {
            ControlRequest::from_json(&serde_json::to_vec(&value).unwrap()).unwrap();
        }
        for value in [
            json!({"operation":"native_permission_status","family":"screenshot"}),
            json!({"operation":"native_open_settings","panel":"arbitrary_url"}),
            json!({"operation":"native_open_settings","panel":"screen_recording","url":"file:///private"}),
        ] {
            assert!(ControlRequest::from_json(&serde_json::to_vec(&value).unwrap()).is_err());
        }
    }
}

#[cfg(test)]
mod reporting_bounds_tests {
    use super::*;
    #[test]
    fn reporting_maximum_request_is_bounded_without_truncation() {
        let request = ControlRequest::ReportUpdate {
            report_id: "a".repeat(32),
            event_ids: vec!["b".repeat(32); 256],
            description: Some("\u{0001}".repeat(1000)),
        };
        let bytes = serde_json::to_vec(&request).unwrap();
        assert!(bytes.len() > 4096);
        assert_eq!(ControlRequest::from_json(&bytes).unwrap(), request);
        assert!(ControlRequest::from_json(&vec![b' '; 16385]).is_err());
        assert!(ControlRequest::ReportCreate {
            event_ids: vec![],
            description: Some("description only".into())
        }
        .validate()
        .is_ok());
    }
}
