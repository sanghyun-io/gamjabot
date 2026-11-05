use serde::Serialize;

/// The only native bootstrap data exposed to the webview.
///
/// Both platform owners keep this wire shape stable while their runtime
/// implementations remain target-specific.
#[derive(Clone, Debug, Serialize)]
pub struct LocalEndpoint {
    pub base_url: String,
    pub session_token: String,
}

#[cfg(test)]
mod tests {
    #[test]
    fn endpoint_wire_shape_is_unchanged() {
        let value = serde_json::to_value(super::LocalEndpoint {
            base_url: "http://127.0.0.1:41001".into(),
            session_token: "fixture-session".into(),
        })
        .unwrap();
        assert_eq!(
            value,
            serde_json::json!({
                "base_url": "http://127.0.0.1:41001",
                "session_token": "fixture-session"
            })
        );
    }
}
