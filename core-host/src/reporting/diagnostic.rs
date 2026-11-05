use serde::{Deserialize, Serialize};

const STAGES: &[&str] = &[
    "turn_setup",
    "context_build",
    "provider_request",
    "provider_result",
    "tool_validation",
    "tool_execution",
    "turn_persist",
];
const AUTH_STAGES: &[&str] = &["auth_remember", "auth_restore"];
const REASONS: &[&str] = &[
    "connect_timeout",
    "read_timeout",
    "timeout",
    "connect_error",
    "network_error",
    "http_status",
    "database_error",
    "permission_denied",
    "not_found",
    "connection_refused",
    "os_error",
    "invalid_tool_arguments",
    "unexpected_error",
];
const AUTH_REMEMBER_REASONS: &[&str] = &[
    "credential_channel_unavailable",
    "credential_response_invalid",
    "credential_request_invalid",
    "keychain_operation_in_progress",
    "keychain_interaction_required",
    "keychain_interaction_state_read_failed",
    "keychain_interaction_disable_failed",
    "keychain_interaction_restore_failed",
    "keychain_no_ui_guard_failed",
    "keychain_interaction_state_uncertain",
    "keychain_query_failed",
    "keychain_query_binding_failed",
    "keychain_store_unavailable",
    "keychain_backend_unsupported",
    "keychain_backend_unavailable",
    "credential_reentry_required",
    "automatic_login_readback_failed",
    "automatic_login_save_uncertain",
    "automatic_login_save_failed",
];
const AUTH_RESTORE_REASONS: &[&str] = &[
    "keychain_operation_in_progress",
    "keychain_interaction_required",
    "keychain_interaction_state_read_failed",
    "keychain_interaction_disable_failed",
    "keychain_interaction_restore_failed",
    "keychain_no_ui_guard_failed",
    "keychain_interaction_state_uncertain",
    "keychain_query_failed",
    "keychain_query_binding_failed",
    "keychain_store_unavailable",
    "keychain_backend_unsupported",
    "keychain_backend_unavailable",
    "credential_reentry_required",
];
const ERROR_CLASSES: &[&str] = &[
    "native_refusal",
    "llm_failure",
    "provider_failure",
    "connection_failure",
    "sqlite_error",
    "database_error",
    "http_error",
    "timeout_error",
    "os_error",
    "type_error",
    "value_error",
    "key_error",
    "index_error",
    "runtime_error",
    "other_error",
];
const AUTH_REMEMBER_ERROR_CLASSES: &[&str] = &[
    "native_os_status",
    "interaction_required",
    "bridge_transport",
    "bridge_protocol",
    "request_validation",
    "store_unavailable",
    "credential_reentry_required",
];
const AUTH_RESTORE_ERROR_CLASSES_LOCKED: &[&str] =
    &["secret_interaction_required", "credential_reentry_required"];
const AUTH_RESTORE_ERROR_CLASSES_UNAVAILABLE: &[&str] = &["secret_store_unavailable"];
const FAILURE_CODES: &[&str] = &[
    "unexpected_error",
    "provider_unavailable",
    "invalid_tool_arguments",
    "provider_not_configured",
    "invalid_trigger",
    "context_input_too_large",
    "context_memory_too_large",
    "memory_context_changed",
    "execution_context_changed",
    "teammate_context_changed",
    "teammate_unavailable",
    "teammate_invalid_request",
    "teammate_delivery_changed",
    "teammate_budget_exhausted",
    "bot_message_context_changed",
    "bot_message_invalid_request",
    "bot_message_delivery_changed",
    "context_too_large",
    "tool_broker_unavailable",
    "mcp_transport_unavailable",
    "tool_budget_exhausted",
    "invalid_continuation",
    "executor_budget_exhausted",
    "invalid_action_result",
    "tool_effect_uncertain",
    "provider_invalid_response",
    "invalid_configuration",
    "missing_model",
    "cancelled",
    "redirect_refused",
    "invalid_credential",
    "rate_limited",
    "provider_rejected",
    "secret_store_unavailable",
    "missing_secret",
    "connection_disabled",
    "storage_unavailable",
    "unreachable",
    "provider_error",
    "invalid_response",
    "unsupported_option",
    "unsupported_model_protocol",
    "desktop_release_failed",
    "native_capability_unavailable",
    "screen_recording_denied",
    "native_lease_unconfirmed",
    "native_response_unconfirmed",
    "native_screenshot_invalid",
    "screenshot_artifact_unavailable",
    "process_launch_unconfirmed",
    "process_guardian_launch_failed",
    "process_executable_not_found",
    "process_executable_not_permitted",
    "process_launch_failed",
    "accessibility_denied",
    "backend_stopped",
    "capture_failed",
    "capture_too_large",
    "desktop_busy",
    "input_failed",
    "invalid_chord",
    "invalid_coordinates",
    "invalid_scroll",
    "native_request_denied",
    "protected_surface",
    "text_limit",
    "unsupported_key",
    "unsupported_native_family",
];
const AUTH_FAILURE_CODES: &[&str] = &["credential_locked", "secret_store_unavailable"];
const AUTH_OPERATIONS: &[&str] = &[
    "sec_item_add",
    "sec_item_update",
    "sec_item_copy_matching",
    "sec_item_delete",
];
const AUTH_CLEANUP: &[&str] = &["success", "failed", "unknown"];

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExceptionSymbol {
    pub module: String,
    #[serde(rename = "class")]
    pub class_name: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StackFrame {
    pub file: String,
    pub function: String,
    pub line: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct McpSyncDiagnostic {
    pub stage: String,
    pub code: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReportDiagnostic {
    pub version: u8,
    pub correlation_id: String,
    pub occurred_at: String,
    pub stage: String,
    pub failure_code: String,
    pub reason: String,
    pub error_class: String,
    #[serde(deserialize_with = "required_nullable")]
    pub room_id: Option<i64>,
    #[serde(deserialize_with = "required_nullable")]
    pub agent_id: Option<i64>,
    #[serde(deserialize_with = "required_nullable")]
    pub connection_id: Option<i64>,
    #[serde(deserialize_with = "required_nullable")]
    pub trigger_event_id: Option<String>,
    #[serde(deserialize_with = "required_nullable")]
    pub action_id: Option<String>,
    pub exception_chain: Vec<ExceptionSymbol>,
    pub frames: Vec<StackFrame>,
    #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "optional_non_null")]
    pub mcp_sync: Option<McpSyncDiagnostic>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "optional_non_null"
    )]
    pub http_status: Option<u16>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "optional_non_null"
    )]
    pub vendor_request_id: Option<String>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "optional_non_null"
    )]
    pub os_status: Option<i32>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "optional_non_null"
    )]
    pub operation: Option<String>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "optional_non_null"
    )]
    pub cleanup: Option<String>,
}

// Context keys are required even when their value is null; optional metadata
// can be omitted but must have the declared type whenever it is present.
fn required_nullable<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

fn optional_non_null<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

fn identifier_with_limit(value: &str, limit: usize) -> bool {
    !value.is_empty()
        && value.len() <= limit
        && value
            .bytes()
            .next()
            .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

fn identifier(value: &str) -> bool {
    identifier_with_limit(value, 64)
}

fn local_id(value: &str) -> bool {
    let compact = value.replace('-', "");
    compact.len() == 32
        && compact
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        && (value.len() == 32
            || (value.len() == 36 && [8, 13, 18, 23].iter().all(|i| value.as_bytes()[*i] == b'-')))
}

impl ReportDiagnostic {
    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1
            || self.correlation_id.len() != 32
            || !self
                .correlation_id
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
            || self.occurred_at.len() > 32
            || !self.occurred_at.ends_with('Z')
            || chrono::DateTime::parse_from_rfc3339(&self.occurred_at).is_err()
        {
            return Err("Invalid diagnostic classification".into());
        }
        let is_auth_stage = AUTH_STAGES.contains(&self.stage.as_str());
        let is_turn_stage = STAGES.contains(&self.stage.as_str());
        if !is_auth_stage && !is_turn_stage {
            return Err("Invalid diagnostic classification".into());
        }
        // Auth stages have different validation rules
        if is_auth_stage {
            return self.validate_auth();
        }
        // Turn stages must not have auth-specific fields
        if self.os_status.is_some() || self.operation.is_some() || self.cleanup.is_some() {
            return Err("Auth fields forbidden on turn stages".into());
        }
        if !FAILURE_CODES.contains(&self.failure_code.as_str())
            || !(REASONS.contains(&self.reason.as_str())
                || FAILURE_CODES.contains(&self.reason.as_str()))
            || !ERROR_CLASSES.contains(&self.error_class.as_str())
        {
            return Err("Invalid diagnostic classification".into());
        }
        if let Some(sync) = &self.mcp_sync {
            if self.failure_code != "mcp_transport_unavailable"
                || !["configure", "bind", "epoch", "channel"].contains(&sync.stage.as_str())
                || !["mcp_configuration_epoch_changed", "mcp_configuration_requires_revoke",
                    "mcp_binding_requires_revoke", "mcp_owner_changed",
                    "mcp_configuration_unavailable", "mcp_binding_changed",
                    "mcp_authority_unavailable", "unavailable"].contains(&sync.code.as_str()) {
                return Err("Invalid MCP diagnostic".into());
            }
        }
        for value in [self.room_id, self.agent_id, self.connection_id] {
            if value.is_some_and(|id| id <= 0) {
                return Err("Invalid diagnostic context".into());
            }
        }
        for value in [&self.trigger_event_id, &self.action_id] {
            if value.as_deref().is_some_and(|id| !local_id(id)) {
                return Err("Invalid diagnostic context".into());
            }
        }
        if self.exception_chain.len() > 3 || self.frames.len() > 4 {
            return Err("Diagnostic detail exceeds bounds".into());
        }
        for symbol in &self.exception_chain {
            let module = symbol.module.as_str();
            if module.len() > 80
                || !module
                    .split('.')
                    .all(|part| identifier_with_limit(part, 80))
                || !(matches!(
                    module,
                    "builtins"
                        | "sqlite3"
                        | "sqlalchemy.exc"
                        | "httpx"
                        | "httpcore"
                        | "ssl"
                        | "socket"
                        | "other"
                ) || module.starts_with("httpx.")
                    || module.starts_with("httpcore.")
                    || module.starts_with("app."))
                || !identifier(&symbol.class_name)
            {
                return Err("Invalid diagnostic exception".into());
            }
        }
        for frame in &self.frames {
            let file = frame
                .file
                .strip_prefix("app/")
                .and_then(|file| file.strip_suffix(".py"));
            let safe_file = file.is_some_and(|file| {
                file.split('/').all(|part| {
                    !part.is_empty()
                        && part != "."
                        && part != ".."
                        && part
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
                })
            });
            let safe_function = !frame.function.is_empty()
                && frame.function.len() <= 64
                && frame
                    .function
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_<>".contains(&b));
            if frame.file.len() > 160
                || !safe_file
                || !safe_function
                || frame.line == 0
                || frame.line > 1_000_000
            {
                return Err("Invalid diagnostic frame".into());
            }
        }
        if self
            .http_status
            .is_some_and(|status| !(100..=599).contains(&status))
            || self.vendor_request_id.as_deref().is_some_and(|id| {
                id.is_empty()
                    || id.len() > 64
                    || !id.as_bytes()[0].is_ascii_alphanumeric()
                    || !id
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            })
        {
            return Err("Invalid diagnostic request metadata".into());
        }
        Ok(())
    }

    fn validate_auth(&self) -> Result<(), String> {
        // Auth stages must have NULL context identifiers and empty chains/frames
        if self.room_id.is_some()
            || self.agent_id.is_some()
            || self.connection_id.is_some()
            || self.trigger_event_id.is_some()
            || self.action_id.is_some()
            || !self.exception_chain.is_empty()
            || !self.frames.is_empty()
            || self.http_status.is_some()
            || self.vendor_request_id.is_some()
            || self.mcp_sync.is_some()
        {
            return Err("Auth diagnostic must have empty context".into());
        }
        // Auth failure codes
        if !AUTH_FAILURE_CODES.contains(&self.failure_code.as_str()) {
            return Err("Invalid auth failure code".into());
        }
        if self.stage == "auth_remember" {
            // auth_remember: cleanup REQUIRED, operation OPTIONAL
            if !AUTH_REMEMBER_REASONS.contains(&self.reason.as_str()) {
                return Err("Invalid auth_remember reason".into());
            }
            if !AUTH_REMEMBER_ERROR_CLASSES.contains(&self.error_class.as_str()) {
                return Err("Invalid auth_remember error_class".into());
            }
            match &self.cleanup {
                Some(c) if AUTH_CLEANUP.contains(&c.as_str()) => {}
                _ => return Err("auth_remember requires valid cleanup".into()),
            }
            if let Some(op) = &self.operation {
                if !AUTH_OPERATIONS.contains(&op.as_str()) {
                    return Err("Invalid auth operation".into());
                }
            }
        } else {
            // auth_restore: cleanup and operation forbidden
            if self.cleanup.is_some() || self.operation.is_some() {
                return Err("auth_restore forbids cleanup and operation".into());
            }
            if !AUTH_RESTORE_REASONS.contains(&self.reason.as_str()) {
                return Err("Invalid auth_restore reason".into());
            }
            let valid_classes = if self.failure_code == "credential_locked" {
                AUTH_RESTORE_ERROR_CLASSES_LOCKED
            } else {
                AUTH_RESTORE_ERROR_CLASSES_UNAVAILABLE
            };
            if !valid_classes.contains(&self.error_class.as_str()) {
                return Err("Invalid auth_restore error_class".into());
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod native_capability_tests {
    use super::*;
    #[test]
    fn mcp_sync_diagnostic_is_closed_and_rejects_untrusted_details() {
        let base = serde_json::json!({
            "version":1,"correlation_id":"a".repeat(32),"occurred_at":"2026-10-30T00:00:00Z",
            "stage":"context_build","failure_code":"mcp_transport_unavailable",
            "reason":"mcp_transport_unavailable","error_class":"llm_failure",
            "room_id":4,"agent_id":7,"connection_id":null,"trigger_event_id":null,"action_id":null,
            "exception_chain":[],"frames":[],
            "mcp_sync":{"stage":"configure","code":"mcp_configuration_epoch_changed"}
        });
        let accepted: ReportDiagnostic = serde_json::from_value(base.clone()).unwrap();
        assert!(accepted.validate().is_ok());
        let mut bad = base.clone(); bad["mcp_sync"]["code"] = serde_json::json!("private text");
        assert!(serde_json::from_value::<ReportDiagnostic>(bad).unwrap().validate().is_err());
        let mut bad = base.clone(); bad["mcp_sync"]["stage"] = serde_json::json!("private");
        assert!(serde_json::from_value::<ReportDiagnostic>(bad).unwrap().validate().is_err());
        let mut bad = base.clone(); bad["mcp_sync"]["secret"] = serde_json::json!("private");
        assert!(serde_json::from_value::<ReportDiagnostic>(bad).is_err());
        let mut bad = base.clone(); bad["mcp_sync"] = serde_json::Value::Null;
        assert!(serde_json::from_value::<ReportDiagnostic>(bad).is_err());
        let mut bad = base; bad["failure_code"] = serde_json::json!("unexpected_error");
        assert!(serde_json::from_value::<ReportDiagnostic>(bad).unwrap().validate().is_err());
    }

    #[test]
    fn profile_model_guard_retains_known_code_without_allowing_arbitrary_codes() {
        let value = serde_json::json!({
            "version":1,"correlation_id":"b".repeat(32),"occurred_at":"2026-10-12T00:00:00Z",
            "stage":"provider_request","failure_code":"unsupported_model_protocol",
            "reason":"unsupported_model_protocol","error_class":"provider_failure",
            "room_id":4,"agent_id":7,"connection_id":2,"trigger_event_id":"c".repeat(32),
            "action_id":null,"exception_chain":[{"module":"app.services.providers.base","class":"ProviderFailure"}],"frames":[]
        });
        let mut diagnostic: ReportDiagnostic = serde_json::from_value(value).unwrap();
        assert!(diagnostic.validate().is_ok());
        diagnostic.failure_code = "private_untrusted_model_error".into();
        assert!(diagnostic.validate().is_err());
    }

    #[test]
    fn missing_native_capability_preserves_fixed_diagnostic_and_rejects_freeform_codes() {
        let value = serde_json::json!({
            "version":1,"correlation_id":"a".repeat(32),"occurred_at":"2026-09-30T00:00:00Z",
            "stage":"tool_execution","failure_code":"native_capability_unavailable","reason":"native_capability_unavailable","error_class":"native_refusal",
            "room_id":4,"agent_id":7,"connection_id":null,"trigger_event_id":null,"action_id":null,"exception_chain":[],"frames":[]
        });
        let diagnostic: ReportDiagnostic = serde_json::from_value(value).unwrap();
        assert!(diagnostic.validate().is_ok());
        let mut invalid = diagnostic.clone();
        invalid.failure_code = "arbitrary_private_exception".into();
        assert!(invalid.validate().is_err());
        invalid = diagnostic;
        invalid.reason = "arbitrary_private_exception".into();
        assert!(invalid.validate().is_err());
    }
}
