// Reuse the canonical diagnostic contract from core-host.
// This avoids duplicating the type definition and ensures auth stage support.
pub use gamja_core_host::reporting::{ExceptionSymbol, ReportDiagnostic, StackFrame};

#[cfg(test)]
mod native_capability_tests {
    use super::*;
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

#[cfg(test)]
mod canonical_auth_contract_tests {
    use super::*;

    #[test]
    fn valid_auth_remember_accepts_all_three_optional_auth_fields() {
        let value = serde_json::json!({
            "version": 1,
            "correlation_id": "a".repeat(32),
            "occurred_at": "2026-09-30T00:00:00Z",
            "stage": "auth_remember",
            "failure_code": "credential_locked",
            "reason": "keychain_query_failed",
            "error_class": "native_os_status",
            "room_id": null,
            "agent_id": null,
            "connection_id": null,
            "trigger_event_id": null,
            "action_id": null,
            "exception_chain": [],
            "frames": [],
            "os_status": -25299,
            "operation": "sec_item_copy_matching",
            "cleanup": "success"
        });
        let diagnostic: ReportDiagnostic = serde_json::from_value(value).unwrap();
        assert!(diagnostic.validate().is_ok());
        assert_eq!(diagnostic.os_status, Some(-25299));
        assert_eq!(diagnostic.operation.as_deref(), Some("sec_item_copy_matching"));
        assert_eq!(diagnostic.cleanup.as_deref(), Some("success"));
    }

    #[test]
    fn valid_auth_restore_accepts_os_status_only() {
        let value = serde_json::json!({
            "version": 1,
            "correlation_id": "a".repeat(32),
            "occurred_at": "2026-09-30T00:00:00Z",
            "stage": "auth_restore",
            "failure_code": "secret_store_unavailable",
            "reason": "keychain_backend_unavailable",
            "error_class": "secret_store_unavailable",
            "room_id": null,
            "agent_id": null,
            "connection_id": null,
            "trigger_event_id": null,
            "action_id": null,
            "exception_chain": [],
            "frames": [],
            "os_status": -25300
        });
        let diagnostic: ReportDiagnostic = serde_json::from_value(value).unwrap();
        assert!(diagnostic.validate().is_ok());
        assert_eq!(diagnostic.os_status, Some(-25300));
        assert!(diagnostic.operation.is_none());
        assert!(diagnostic.cleanup.is_none());
    }

    #[test]
    fn auth_remember_rejects_invalid_cleanup() {
        let value = serde_json::json!({
            "version": 1,
            "correlation_id": "a".repeat(32),
            "occurred_at": "2026-09-30T00:00:00Z",
            "stage": "auth_remember",
            "failure_code": "credential_locked",
            "reason": "keychain_query_failed",
            "error_class": "native_os_status",
            "room_id": null,
            "agent_id": null,
            "connection_id": null,
            "trigger_event_id": null,
            "action_id": null,
            "exception_chain": [],
            "frames": [],
            "cleanup": "invalid_cleanup"
        });
        let diagnostic: ReportDiagnostic = serde_json::from_value(value).unwrap();
        assert!(diagnostic.validate().is_err());
    }

    #[test]
    fn auth_restore_rejects_cleanup_and_operation() {
        let value = serde_json::json!({
            "version": 1,
            "correlation_id": "a".repeat(32),
            "occurred_at": "2026-09-30T00:00:00Z",
            "stage": "auth_restore",
            "failure_code": "secret_store_unavailable",
            "reason": "keychain_backend_unavailable",
            "error_class": "secret_store_unavailable",
            "room_id": null,
            "agent_id": null,
            "connection_id": null,
            "trigger_event_id": null,
            "action_id": null,
            "exception_chain": [],
            "frames": [],
            "cleanup": "success"
        });
        let diagnostic: ReportDiagnostic = serde_json::from_value(value).unwrap();
        assert!(diagnostic.validate().is_err());
    }

    #[test]
    fn auth_diagnostic_rejects_non_null_context() {
        let value = serde_json::json!({
            "version": 1,
            "correlation_id": "a".repeat(32),
            "occurred_at": "2026-09-30T00:00:00Z",
            "stage": "auth_remember",
            "failure_code": "credential_locked",
            "reason": "keychain_query_failed",
            "error_class": "native_os_status",
            "room_id": 4,
            "agent_id": null,
            "connection_id": null,
            "trigger_event_id": null,
            "action_id": null,
            "exception_chain": [],
            "frames": [],
            "cleanup": "success"
        });
        let diagnostic: ReportDiagnostic = serde_json::from_value(value).unwrap();
        assert!(diagnostic.validate().is_err());
    }

    #[test]
    fn legacy_turn_stage_rejects_auth_fields() {
        let value = serde_json::json!({
            "version": 1,
            "correlation_id": "a".repeat(32),
            "occurred_at": "2026-09-30T00:00:00Z",
            "stage": "tool_execution",
            "failure_code": "provider_unavailable",
            "reason": "unexpected_error",
            "error_class": "value_error",
            "room_id": 4,
            "agent_id": 7,
            "connection_id": null,
            "trigger_event_id": null,
            "action_id": null,
            "exception_chain": [],
            "frames": [],
            "os_status": -25299
        });
        let diagnostic: ReportDiagnostic = serde_json::from_value(value).unwrap();
        assert!(diagnostic.validate().is_err());
    }
}
