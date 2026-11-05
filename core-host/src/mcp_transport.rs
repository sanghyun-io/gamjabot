//! MCP wire limits and immutable approval binding. Transport effects must only be
//! reached through the separately authenticated, reviewed Core channel.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::Path;

pub const MAX_FRAME: usize = 1_048_576;
// One schema may occupy at most one eighth of the existing 512 KiB toolset
// budget and must fit the 64 KiB provider-schema bound. Frame limits remain separate.
pub const MAX_SCHEMA: usize = MAX_FRAME / 16;
pub const MAX_TOOLS: usize = 100;

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Transport {
    Http {
        url: String,
    },
    Stdio {
        executable: String,
        argv: Vec<String>,
    },
}
impl Transport {
    pub fn validate_frozen(&self, canonical: &str, digest: &str) -> Result<(), &'static str> {
        self.validate()?;
        let value = serde_json::to_value(self).map_err(|_| "invalid_mcp_transport")?;
        if !is_digest(digest) || canonical_digest(canonical, &value, MAX_FRAME / 4)? != digest {
            return Err("mcp_transport_changed");
        }
        Ok(())
    }
    pub fn validate(&self) -> Result<(), &'static str> {
        match self {
            Self::Http { url } => {
                let parsed = url::Url::parse(url).map_err(|_| "invalid_mcp_url")?;
                if !matches!(parsed.scheme(), "http" | "https")
                    || parsed.host_str().is_none()
                    || !parsed.username().is_empty()
                    || parsed.password().is_some()
                    || parsed.query().is_some()
                    || parsed.fragment().is_some()
                    || url.len() > 2048
                {
                    return Err("invalid_mcp_url");
                }
            }
            Self::Stdio { executable, argv } => {
                if !Path::new(executable).is_absolute()
                    || executable.len() > 4096
                    || executable.contains('\0')
                    || argv.len() > 64
                    || argv.iter().any(|s| s.len() > 4096 || s.contains('\0'))
                {
                    return Err("invalid_mcp_executable");
                }
            }
        }
        Ok(())
    }
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DiscoveryBinding {
    pub owner_id: i64,
    pub owner_epoch: String,
    pub core_epoch: String,
    pub server_id: String,
    pub config_revision: u64,
    pub discovery_id: String,
    pub preview_id: String,
    pub transport_sha256: String,
    pub executable_review: Option<Value>,
}
impl DiscoveryBinding {
    fn valid(&self) -> bool {
        self.owner_id > 0
            && is_digest(&self.owner_epoch)
            && is_digest(&self.core_epoch)
            && !self.server_id.is_empty()
            && self.server_id.len() <= 128
            && self.config_revision > 0
            && !self.discovery_id.is_empty()
            && self.discovery_id.len() <= 128
            && !self.preview_id.is_empty()
            && self.preview_id.len() <= 128
            && is_digest(&self.transport_sha256)
    }
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ToolPermission {
    pub tool_id: String,
    pub tool_name: String,
    pub schema_sha256: String,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScopeEpoch {
    pub scope_id: String,
    pub epoch: u64,
}
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExecutionBinding {
    pub version: u8,
    pub root_scope_id: String,
    pub scope_id: String,
    pub owner_id: i64,
    pub root_room_id: i64,
    pub root_request_event_id: String,
    pub execution_room_id: i64,
    pub agent_id: i64,
    pub run_generation: u64,
    pub job_id: Option<String>,
    pub phase: Option<String>,
    pub context_mode: String,
    pub deadline_at: String,
    pub lineage: Vec<ScopeEpoch>,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TeammateBinding {
    pub handoff_id: String,
    pub phase: String,
}
/// Recipient authority is supplied by the trusted first-party Engine after
/// its canonical checks. This is a local one-use binding, not remote attestation.
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SharedRecipientBinding {
    pub version: u8,
    pub owner_id: i64,
    pub owner_epoch: String,
    pub core_epoch: String,
    pub issuer: String,
    pub subject: String,
    pub team_id: String,
    pub publication_id: String,
    pub session_generation: String,
    pub room_id: i64,
    pub agent_id: i64,
    pub root_event_id: String,
    pub generation: u64,
    pub server_id: String,
    pub config_revision: u64,
    pub binding_revision: u64,
    pub link_id: String,
    pub link_revision: u64,
    pub authority_id: String,
    pub preference_revision: u64,
    pub consent_id: String,
    pub consent_revision: u64,
    pub consent_scope: String,
    pub credential_ref_sha256: String,
    pub action_id: String,
    pub review_id: String,
    pub grant_id: i64,
}
impl SharedRecipientBinding {
    fn valid(&self) -> bool {
        let origin=url::Url::parse(&self.issuer).ok();
        let issuer_ok=origin.is_some_and(|u| {
            self.issuer.len()<=1024 && u.username().is_empty() && u.password().is_none()
                && u.query().is_none() && u.fragment().is_none() && u.path()=="/"
                && u.host_str().is_some_and(|host| u.scheme()=="https" ||
                    (u.scheme()=="http" && matches!(host,"127.0.0.1"|"[::1]"|"::1")))
        });
        self.version==1 && self.owner_id>0 && self.room_id>0 && self.agent_id>0 && self.grant_id>0
            && self.generation>0 && self.config_revision>0 && self.binding_revision>0
            && self.link_revision>0 && self.preference_revision>0 && self.consent_revision>0
            && is_digest(&self.owner_epoch) && is_digest(&self.core_epoch)
            && is_digest(&self.credential_ref_sha256) && issuer_ok
            && [&self.subject,&self.team_id,&self.publication_id,&self.session_generation,
                &self.root_event_id,&self.server_id,&self.link_id,&self.authority_id,
                &self.consent_id,&self.action_id,&self.review_id].iter().all(|v|uuid_string(v))
            && matches!(self.consent_scope.as_str(),"once"|"bot"|"all")
    }
    fn agrees(&self,b:&ApprovalBinding)->bool {
        self.valid() && self.owner_id==b.owner_id && self.owner_epoch==b.owner_epoch
            && self.core_epoch==b.core_epoch && self.agent_id==b.agent_id
            && self.root_event_id==b.root_event_id && self.generation==b.generation
            && self.server_id==b.server_id && self.config_revision==b.config_revision
            && self.binding_revision==b.binding_revision && self.action_id==b.action_id
            && self.review_id==b.review_id && self.grant_id==b.grant_id
    }
}
fn uuid_string(s: &str) -> bool {
    s.len() == 36 && s.bytes().enumerate().all(|(i, b)| {
        if [8, 13, 18, 23].contains(&i) { b == b'-' }
        else { b.is_ascii_digit() || (b'a'..=b'f').contains(&b) }
    })
}
impl ExecutionBinding {
    pub(crate) fn valid(&self, owner: i64, agent: i64, root: &str, generation: u64) -> bool {
        let deadline = chrono::DateTime::parse_from_rfc3339(&self.deadline_at);
        let mut unique = std::collections::HashSet::new();
        self.version == 1 && self.owner_id == owner && self.agent_id == agent
            && self.root_request_event_id == root && self.run_generation == generation
            && self.root_room_id > 0 && self.execution_room_id > 0
            && uuid_string(&self.root_scope_id) && uuid_string(&self.scope_id)
            && uuid_string(&self.root_request_event_id)
            && (self.context_mode == "user_room" || self.context_mode == "delegated")
            && match (&self.job_id, &self.phase) {
                (None, None) => self.context_mode == "user_room",
                (Some(job), Some(phase)) => uuid_string(job) && self.context_mode == "delegated"
                    && (phase == "response" || phase == "return"),
                _ => false,
            }
            && deadline.is_ok_and(|d| d.offset().local_minus_utc() == 0 && d > chrono::Utc::now())
            && !self.lineage.is_empty() && self.lineage.len() <= 8
            && self.lineage.first().is_some_and(|s| s.scope_id == self.root_scope_id)
            && self.lineage.last().is_some_and(|s| s.scope_id == self.scope_id)
            && self.lineage.iter().all(|s| uuid_string(&s.scope_id) && unique.insert(&s.scope_id))
    }
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ApprovalBinding {
    pub owner_id: i64,
    pub owner_epoch: String,
    pub core_epoch: String,
    pub agent_id: i64,
    pub root_event_id: String,
    pub action_id: String,
    pub review_id: String,
    pub grant_id: i64,
    pub generation: u64,
    pub server_id: String,
    pub config_revision: u64,
    pub binding_revision: u64,
    pub tool_id: String,
    pub tool_name: String,
    pub schema_sha256: String,
    pub input_sha256: String,
    pub payload_digest: String,
    pub review_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub execution_binding: Option<ExecutionBinding>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub teammate_binding: Option<TeammateBinding>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub shared_recipient_binding: Option<SharedRecipientBinding>,
}
fn is_digest(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn canonical_digest(raw: &str, expected: &Value, limit: usize) -> Result<String, &'static str> {
    if raw.len() > limit {
        return Err("mcp_frame_too_large");
    }
    let parsed: Value = serde_json::from_str(raw).map_err(|_| "invalid_json")?;
    if &parsed != expected {
        return Err("mcp_canonical_mismatch");
    }
    Ok(format!("{:x}", Sha256::digest(raw.as_bytes())))
}
impl ApprovalBinding {
    pub fn validate(
        &self,
        arguments: &Value,
        schema: &Value,
        arguments_canonical: &str,
        schema_canonical: &str,
    ) -> Result<(), &'static str> {
        if self.owner_id <= 0
            || self.agent_id <= 0
            || self.root_event_id.is_empty()
            || !is_digest(&self.owner_epoch)
            || !is_digest(&self.core_epoch)
            || self.action_id.is_empty()
            || self.action_id.len() > 128
            || self.review_id.is_empty()
            || self.review_id.len() > 128
            || self.grant_id <= 0
            || self.root_event_id.len() > 128
            || self.server_id.is_empty()
            || self.server_id.len() > 128
            || !is_digest(&self.tool_id)
            || self.tool_name.is_empty()
            || self.tool_name.len() > 128
            || self.generation == 0
            || self.config_revision == 0
            || self.binding_revision == 0
            || !is_digest(&self.schema_sha256)
            || !is_digest(&self.input_sha256)
            || !is_digest(&self.payload_digest)
            || !is_digest(&self.review_sha256)
            || !arguments.is_object()
        {
            return Err("invalid_mcp_binding");
        }
        if let Some(scope) = &self.execution_binding {
            if !scope.valid(self.owner_id, self.agent_id, &self.root_event_id, self.generation) {
                return Err("invalid_mcp_scope");
            }
        }
        if let Some(teammate) = &self.teammate_binding {
            if self.execution_binding.as_ref().is_none_or(|s| s.context_mode != "user_room")
                || !uuid_string(&teammate.handoff_id)
                || !matches!(teammate.phase.as_str(), "response" | "return") {
                return Err("invalid_mcp_scope");
            }
        }
        if self.shared_recipient_binding.as_ref().is_some_and(|shared| !shared.agrees(self)) {
            return Err("invalid_mcp_shared_binding");
        }
        validate_schema(schema)?;
        if canonical_digest(schema_canonical, schema, MAX_SCHEMA)? != self.schema_sha256
            || canonical_digest(arguments_canonical, arguments, MAX_FRAME)? != self.input_sha256
        {
            return Err("mcp_binding_changed");
        }
        Ok(())
    }
}
/// External references must never trigger network or filesystem resolution.
pub fn validate_schema(schema: &Value) -> Result<(), &'static str> {
    validate_schema_detail(schema).map_err(|code| if code == "remote_schema_ref" {code} else {"invalid_schema"})
}
fn validate_schema_detail(schema: &Value) -> Result<(), &'static str> {
    if !schema.is_object() {return Err("schema_type");}
    if serde_json::to_vec(schema).map_err(|_| "invalid_schema")?.len() > MAX_SCHEMA {return Err("schema_size");}
    fn walk(v: &Value, depth: usize) -> Result<(), &'static str> {
        if depth > 64 {
            return Err("schema_depth");
        }
        match v {
            Value::Object(m) => {
                for (key, item) in m {
                    if key == "$ref" && !item.as_str().is_some_and(|s| s.starts_with('#')) {
                        return Err("remote_schema_ref");
                    }
                    walk(item, depth + 1)?;
                }
            }
            Value::Array(a) => {
                for item in a {
                    walk(item, depth + 1)?;
                }
            }
            _ => {}
        }
        Ok(())
    }
    walk(schema, 0)?;
    if !crate::mcp_schema::safe_mcp_schema(schema) {return Err("remote_schema_ref");}
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn bound_canonical_bytes_and_remote_refs() {
        let schema = json!({"type":"object"});
        let arguments = json!({"number":1.5});
        let schema_raw = "{\"type\":\"object\"}";
        let args_raw = "{\"number\":1.5}";
        let binding = ApprovalBinding {
            owner_id: 1,
            owner_epoch: "a".repeat(64),
            core_epoch: "b".repeat(64),
            agent_id: 2,
            root_event_id: "root".into(),
            action_id: "action".into(),
            review_id: "review".into(),
            grant_id: 1,
            generation: 1,
            server_id: "server".into(),
            config_revision: 1,
            binding_revision: 1,
            tool_id: "e".repeat(64),
            tool_name: "example".into(),
            schema_sha256: format!("{:x}", Sha256::digest(schema_raw.as_bytes())),
            input_sha256: format!("{:x}", Sha256::digest(args_raw.as_bytes())),
            payload_digest: "c".repeat(64),
            review_sha256: "d".repeat(64),
            execution_binding: None,
            teammate_binding: None,
            shared_recipient_binding: None,
        };
        assert!(binding
            .validate(&arguments, &schema, args_raw, schema_raw)
            .is_ok());
        assert!(binding
            .validate(&json!({"number":2}), &schema, args_raw, schema_raw)
            .is_err());
        assert_eq!(
            validate_schema(&json!({"$ref":"https://example.invalid/schema"})),
            Err("remote_schema_ref")
        );
        assert!(validate_schema(&json!({"$defs":{"value":{"type":"string"}},"$ref":"#/$defs/value"})).is_ok());
    }
}

/// In-process grant state. Only the authenticated Core endpoint may own this;
/// the Engine never receives an API that can issue grants.
mod grant_registry {
    use super::{ApprovalBinding, DiscoveryBinding, SharedRecipientBinding, ToolPermission};
    use std::collections::{HashMap, HashSet};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    };
    use std::time::{Duration, Instant};

    struct Grant {
        binding: ApprovalBinding,
        transport_hash: String,
        expires: Instant,
    }
    struct State {
        configurations: HashMap<(i64, String), (u64, String)>,
        cancelled_roots: HashSet<(i64, String, String, u64)>,
        revoked_scopes: HashMap<(i64, String), u64>,
        closed: bool,
        bindings: HashMap<(i64, String, i64), (u64, Vec<ToolPermission>)>,
        pending: HashMap<String, Grant>,
        discovery_pending: HashMap<String, (DiscoveryBinding, Instant)>,
        discovery_active: HashMap<String, (DiscoveryBinding, Arc<AtomicBool>)>,
        active: HashMap<String, (ApprovalBinding, Arc<AtomicBool>)>,
        shared_agents: HashSet<(i64, i64)>,
        shared_authorities: HashMap<(i64, String, String), SharedRecipientBinding>,
        shared_authorizations: HashMap<(i64, String), (SharedRecipientBinding, Instant)>,
        shared_used_actions: HashSet<(i64, String)>,
        shared_revoked: HashSet<(i64, String, String)>,
        shared_closed: bool,
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        use serde_json::json;
        use sha2::{Digest, Sha256};
        #[test]
        fn repeated_scope_revocation_at_capacity_does_not_close_registry() {
            let registry=GrantRegistry::new();
            {let mut state=registry.0.lock().unwrap();
                for n in 0..16384 {state.revoked_scopes.insert((1,format!("{n:08x}-0000-4000-8000-000000000000")),1);}}
            let old=super::super::ScopeEpoch{scope_id:"00000000-0000-4000-8000-000000000000".into(),epoch:2};
            assert!(registry.revoke_scopes(1,&[old.clone(),old]).is_ok());
            let new=super::super::ScopeEpoch{scope_id:"ffffffff-0000-4000-8000-000000000000".into(),epoch:1};
            assert!(registry.revoke_scopes(1,&[new]).is_err());
        }
        #[test]
        fn nonce_is_once_and_revoke_invalidates_pending_and_active() {
            let registry = GrantRegistry::new();
            let hash = "f".repeat(64);
            registry
                .configure(1, "srv".into(), 1, hash.clone())
                .unwrap();
            registry
                .bind(
                    1,
                    "srv",
                    2,
                    1,
                    vec![ToolPermission {
                        tool_id: "e".repeat(64),
                        tool_name: "tool".into(),
                        schema_sha256: format!("{:x}", Sha256::digest(b"{\"type\":\"object\"}")),
                    }],
                )
                .unwrap();
            let schema = json!({"type":"object"});
            let args = json!({});
            let sc = "{\"type\":\"object\"}";
            let ac = "{}";
            let binding = ApprovalBinding {
                owner_id: 1,
                owner_epoch: "a".repeat(64),
                core_epoch: "b".repeat(64),
                agent_id: 2,
                root_event_id: "root".into(),
                action_id: "act".into(),
                review_id: "review".into(),
                grant_id: 1,
                generation: 1,
                server_id: "srv".into(),
                config_revision: 1,
                binding_revision: 1,
                tool_id: "e".repeat(64),
                tool_name: "tool".into(),
                schema_sha256: format!("{:x}", Sha256::digest(sc.as_bytes())),
                input_sha256: format!("{:x}", Sha256::digest(ac.as_bytes())),
                payload_digest: "c".repeat(64),
                review_sha256: "d".repeat(64),
                execution_binding: None,
                teammate_binding: None,
                shared_recipient_binding: None,
            };
            let mut other_owner = binding.clone();
            other_owner.owner_id = 99;
            assert!(registry
                .issue(other_owner, hash.clone(), &args, &schema, ac, sc)
                .is_err());
            let mut stale_binding = binding.clone();
            stale_binding.binding_revision = 2;
            assert!(registry
                .issue(stale_binding, hash.clone(), &args, &schema, ac, sc)
                .is_err());
            let mut unselected = binding.clone();
            unselected.tool_id = "1".repeat(64);
            assert!(registry
                .issue(unselected, hash.clone(), &args, &schema, ac, sc)
                .is_err());
            // An ancestor Stop invalidates pending grants and signals an active
            // descendant, while an unrelated direct request remains usable.
            let root = "11111111-1111-4111-8111-111111111111";
            let child = "22222222-2222-4222-8222-222222222222";
            let mut delegated = binding.clone();
            delegated.root_event_id = "33333333-3333-4333-8333-333333333333".into();
            delegated.execution_binding = Some(super::super::ExecutionBinding {
                version: 1, root_scope_id: root.into(), scope_id: child.into(), owner_id: 1,
                root_room_id: 3, root_request_event_id: delegated.root_event_id.clone(),
                execution_room_id: 4, agent_id: 2, run_generation: 1,
                job_id: Some("44444444-4444-4444-8444-444444444444".into()),
                phase: Some("response".into()), context_mode: "delegated".into(),
                deadline_at: "2999-01-01T00:00:00+00:00".into(),
                lineage: vec![super::super::ScopeEpoch {scope_id: root.into(), epoch: 4},
                    super::super::ScopeEpoch {scope_id: child.into(), epoch: 2}],
            });
            let active_nonce = registry.issue(delegated.clone(), hash.clone(), &args, &schema, ac, sc).unwrap();
            let pending_nonce = registry.issue(delegated.clone(), hash.clone(), &args, &schema, ac, sc).unwrap();
            let active = registry.consume(&active_nonce, &delegated, &hash).unwrap();
            registry.revoke_scopes(1, &[super::super::ScopeEpoch {scope_id: root.into(), epoch: 5}]).unwrap();
            assert!(active.cancelled.load(Ordering::Acquire));
            assert!(registry.consume(&pending_nonce, &delegated, &hash).is_err());
            assert!(registry.issue(delegated.clone(), hash.clone(), &args, &schema, ac, sc).is_err());
            delegated.execution_binding.as_mut().unwrap().lineage[0].epoch = 6;
            assert!(registry.issue(delegated.clone(), hash.clone(), &args, &schema, ac, sc).is_ok());
            let mut teammate = delegated.clone();
            let scope = teammate.execution_binding.as_mut().unwrap();
            scope.context_mode = "user_room".into();
            scope.job_id = None;
            scope.phase = None;
            teammate.teammate_binding = Some(super::super::TeammateBinding {
                handoff_id: "55555555-5555-4555-8555-555555555555".into(), phase: "response".into(),
            });
            assert!(teammate.validate(&args, &schema, ac, sc).is_ok());
            teammate.teammate_binding.as_mut().unwrap().phase = "invalid".into();
            assert!(teammate.validate(&args, &schema, ac, sc).is_err());
            delegated.agent_id = 9;
            assert!(registry.issue(delegated, hash.clone(), &args, &schema, ac, sc).is_err());
            let first = registry
                .issue(binding.clone(), hash.clone(), &args, &schema, ac, sc)
                .unwrap();
            let second = registry
                .issue(binding.clone(), hash.clone(), &args, &schema, ac, sc)
                .unwrap();
            let permit = registry.consume(&first, &binding, &hash).unwrap();
            assert!(registry.consume(&first, &binding, &hash).is_err());
            registry.cancel(1, "srv", "root", 1).unwrap();
            assert!(permit.cancelled.load(Ordering::Acquire));
            assert!(registry
                .issue(binding.clone(), hash.clone(), &args, &schema, ac, sc)
                .is_err());
            let mut other_root = binding.clone();
            other_root.root_event_id = "other-root".into();
            let third = registry
                .issue(other_root.clone(), hash.clone(), &args, &schema, ac, sc)
                .unwrap();
            let revoke_permit = registry.consume(&third, &other_root, &hash).unwrap();
            registry.revoke(1, "srv").unwrap();
            assert!(revoke_permit.cancelled.load(Ordering::Acquire));
            assert!(permit.cancelled.load(Ordering::Acquire));
            assert!(registry.consume(&second, &binding, &hash).is_err());
            assert!(registry
                .issue(binding, hash, &args, &schema, ac, sc)
                .is_err());
        }
    }

    pub(super) struct GrantRegistry(Mutex<State>);
    pub(super) struct RunPermit {
        pub(super) cancelled: Arc<AtomicBool>,
    }
    impl GrantRegistry {
        pub(super) fn new() -> Self {
            Self(Mutex::new(State {
                configurations: HashMap::new(),
                cancelled_roots: HashSet::new(),
                revoked_scopes: HashMap::new(),
                closed: false,
                bindings: HashMap::new(),
                pending: HashMap::new(),
                discovery_pending: HashMap::new(),
                discovery_active: HashMap::new(),
                active: HashMap::new(),
                shared_agents: HashSet::new(),
                shared_authorities: HashMap::new(),
                shared_authorizations: HashMap::new(),
                shared_used_actions: HashSet::new(),
                shared_revoked: HashSet::new(),
                shared_closed: false,
            }))
        }
        /// Engine has already checked current remote membership and local consent.
        /// Core freezes that first-party assertion for one exact approved action.
        pub(super) fn shared_authorize(&self, shared: SharedRecipientBinding) -> Result<(), &'static str> {
            if !shared.valid() { return Err("invalid_mcp_shared_binding"); }
            let mut state=self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            state.shared_authorizations.retain(|_,(_,expires)| *expires>Instant::now());
            let authority=(shared.owner_id,shared.authority_id.clone(),shared.session_generation.clone());
            let action=(shared.owner_id,shared.action_id.clone());
            if state.closed || state.shared_closed || state.shared_revoked.iter().any(|(owner,id,_)|
                    *owner==shared.owner_id && id==&shared.authority_id)
                || state.shared_used_actions.contains(&action) || state.shared_agents.len()>=16384
                || state.shared_authorities.len()>=16384 || state.shared_authorizations.len()>=128 {
                return Err("mcp_shared_authority_unavailable");
            }
            if state.shared_authorities.iter().any(|((owner,id,_),old)|
                    *owner==shared.owner_id && id==&shared.authority_id && old!=&shared)
                || state.shared_authorizations.get(&action).is_some_and(|(old,_)|old!=&shared) {
                return Err("mcp_shared_authority_changed");
            }
            if state.shared_authorities.contains_key(&authority) {
                // Identical repeats do not extend the original authorization;
                // an expired or consumed authority cannot be recreated.
                return if state.shared_authorizations.get(&action)
                    .is_some_and(|(old,expires)|old==&shared && *expires>Instant::now()) {
                    Ok(())
                } else { Err("mcp_shared_authority_unavailable") };
            }
            let agent=(shared.owner_id,shared.agent_id);
            if state.shared_agents.insert(agent) {
                state.pending.retain(|_,grant| !(grant.binding.owner_id==shared.owner_id
                    && grant.binding.agent_id==shared.agent_id && grant.binding.shared_recipient_binding.is_none()));
                for (binding,cancel) in state.active.values() {
                    if binding.owner_id==shared.owner_id && binding.agent_id==shared.agent_id
                        && binding.shared_recipient_binding.is_none() {cancel.store(true,Ordering::Release);}
                }
            }
            state.shared_authorities.insert(authority,shared.clone());
            state.shared_authorizations.insert(action,(shared,Instant::now()+Duration::from_secs(60)));
            Ok(())
        }
        /// Revocation seals an authority/session for this Core lifetime. A new
        /// consent, preference or credential identity needs a new authority ID.
        pub(super) fn shared_revoke(&self, owner:i64, authority_id:&str, session:&str) -> Result<(), &'static str> {
            if owner<=0 || !super::uuid_string(authority_id) || !super::uuid_string(session) {
                return Err("invalid_mcp_shared_binding");
            }
            let mut state=self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            let key=(owner,authority_id.to_owned(),session.to_owned());
            if !state.shared_revoked.contains(&key) && state.shared_revoked.len()>=16384 {
                state.shared_closed=true;
                state.shared_authorizations.clear();
                state.pending.retain(|_,grant|grant.binding.shared_recipient_binding.is_none());
                for (binding,cancel) in state.active.values() {
                    if binding.shared_recipient_binding.is_some() {cancel.store(true,Ordering::Release);}
                }
                return Err("mcp_shared_authority_unavailable");
            }
            state.shared_revoked.insert(key.clone());
            state.shared_authorizations.retain(|_,(binding,_)| !(binding.owner_id==owner
                && binding.authority_id==authority_id && binding.session_generation==session));
            state.pending.retain(|_,grant| !grant.binding.shared_recipient_binding.as_ref().is_some_and(|binding|
                binding.owner_id==owner && binding.authority_id==authority_id && binding.session_generation==session));
            for (binding,cancel) in state.active.values() {
                if binding.shared_recipient_binding.as_ref().is_some_and(|s|s.owner_id==owner
                    && s.authority_id==authority_id && s.session_generation==session) {
                    cancel.store(true,Ordering::Release);
                }
            }
            Ok(())
        }
        /// Called only after authenticated Owner configuration is committed in Core.
        pub(super) fn configure(
            &self,
            owner: i64,
            server: String,
            revision: u64,
            transport_hash: String,
        ) -> Result<(), &'static str> {
            if owner <= 0
                || server.is_empty()
                || revision == 0
                || !super::is_digest(&transport_hash)
            {
                return Err("invalid_mcp_configuration");
            }
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            let key = (owner, server);
            if let Some(current) = state.configurations.get(&key) {
                return if current == &(revision, transport_hash) {
                    Ok(())
                } else {
                    Err("mcp_configuration_requires_revoke")
                };
            }
            state.configurations.insert(key, (revision, transport_hash));
            Ok(())
        }
        pub(super) fn bind(
            &self,
            owner: i64,
            server: &str,
            agent: i64,
            revision: u64,
            mut allowed_tools: Vec<ToolPermission>,
        ) -> Result<(), &'static str> {
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            if agent <= 0
                || revision == 0
                || allowed_tools.len() > super::MAX_TOOLS
                || allowed_tools.iter().any(|t| {
                    !super::is_digest(&t.tool_id)
                        || !super::is_digest(&t.schema_sha256)
                        || t.tool_name.is_empty()
                        || t.tool_name.len() > 128
                })
                || allowed_tools.iter().enumerate().any(|(i, t)| {
                    allowed_tools[..i]
                        .iter()
                        .any(|p| p.tool_id == t.tool_id || p.tool_name == t.tool_name)
                })
                || !state
                    .configurations
                    .contains_key(&(owner, server.to_owned()))
            {
                return Err("invalid_mcp_binding");
            }
            allowed_tools.sort_by(|a, b| a.tool_id.cmp(&b.tool_id));
            let key = (owner, server.to_owned(), agent);
            if let Some(current) = state.bindings.get(&key) {
                return if current == &(revision, allowed_tools.clone()) {
                    Ok(())
                } else {
                    Err("mcp_binding_requires_revoke")
                };
            }
            state.bindings.insert(key, (revision, allowed_tools));
            Ok(())
        }
        pub(super) fn issue_discovery(
            &self,
            binding: DiscoveryBinding,
        ) -> Result<String, &'static str> {
            if !binding.valid() {
                return Err("invalid_mcp_discovery_binding");
            }
            let mut bytes = [0u8; 32];
            getrandom::fill(&mut bytes).map_err(|_| "mcp_nonce_unavailable")?;
            let nonce: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            if state.closed {
                return Err("mcp_authority_unavailable");
            }
            if state
                .configurations
                .get(&(binding.owner_id, binding.server_id.clone()))
                != Some(&(binding.config_revision, binding.transport_sha256.clone()))
            {
                return Err("mcp_authority_changed");
            }
            state
                .discovery_pending
                .retain(|_, (_, expires)| *expires > Instant::now());
            if state.discovery_pending.len() >= 128
                || state.discovery_active.len() >= 128
                || state.discovery_pending.contains_key(&nonce)
            {
                return Err("mcp_authority_busy");
            }
            state.discovery_pending.insert(
                nonce.clone(),
                (binding, Instant::now() + Duration::from_secs(60)),
            );
            Ok(nonce)
        }
        pub(super) fn consume_discovery(
            &self,
            nonce: &str,
            binding: &DiscoveryBinding,
        ) -> Result<RunPermit, &'static str> {
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            let (saved, expires) = state
                .discovery_pending
                .remove(nonce)
                .ok_or("mcp_grant_unavailable")?;
            if &saved != binding || expires <= Instant::now() {
                return Err("mcp_grant_changed");
            }
            let cancelled = Arc::new(AtomicBool::new(false));
            state
                .discovery_active
                .insert(nonce.into(), (binding.clone(), cancelled.clone()));
            Ok(RunPermit { cancelled })
        }
        pub(super) fn issue(
            &self,
            binding: ApprovalBinding,
            transport_hash: String,
            arguments: &serde_json::Value,
            schema: &serde_json::Value,
            arguments_canonical: &str,
            schema_canonical: &str,
        ) -> Result<String, &'static str> {
            binding.validate(arguments, schema, arguments_canonical, schema_canonical)?;
            if !super::is_digest(&transport_hash) {
                return Err("invalid_mcp_transport_hash");
            }
            let mut bytes = [0u8; 32];
            getrandom::fill(&mut bytes).map_err(|_| "mcp_nonce_unavailable")?;
            let nonce: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            if state.closed {
                return Err("mcp_authority_unavailable");
            }
            if state.cancelled_roots.contains(&(
                binding.owner_id,
                binding.server_id.clone(),
                binding.root_event_id.clone(),
                binding.generation,
            )) {
                return Err("mcp_scope_cancelled");
            }
            if !Self::scope_allowed(&state, &binding) {
                return Err("mcp_scope_cancelled");
            }
            if let Some(shared)=&binding.shared_recipient_binding {
                let action=(binding.owner_id,binding.action_id.clone());
                if state.shared_closed || state.shared_used_actions.len()>=16384
                    || state.shared_used_actions.contains(&action)
                    || !state.shared_authorizations.get(&action).is_some_and(|(saved,expires)|saved==shared && *expires>Instant::now()) {
                    return Err("mcp_shared_authority_unavailable");
                }
            }
            let config = state
                .configurations
                .get(&(binding.owner_id, binding.server_id.clone()));
            let bot = state.bindings.get(&(
                binding.owner_id,
                binding.server_id.clone(),
                binding.agent_id,
            ));
            if config != Some(&(binding.config_revision, transport_hash.clone()))
                || !bot.is_some_and(|(revision, tools)| {
                    *revision == binding.binding_revision
                        && tools.iter().any(|t| {
                            t.tool_id == binding.tool_id
                                && t.tool_name == binding.tool_name
                                && t.schema_sha256 == binding.schema_sha256
                        })
                })
            {
                return Err("mcp_authority_changed");
            }
            state
                .pending
                .retain(|_, grant| grant.expires > Instant::now());
            if state.pending.len() >= 128
                || state.active.len() >= 128
                || state.pending.contains_key(&nonce)
            {
                return Err("mcp_authority_busy");
            }
            if binding.shared_recipient_binding.is_some() {
                let action=(binding.owner_id,binding.action_id.clone());
                state.shared_authorizations.remove(&action);
                state.shared_used_actions.insert(action);
            }
            state.pending.insert(
                nonce.clone(),
                Grant {
                    binding,
                    transport_hash,
                    expires: Instant::now() + Duration::from_secs(60),
                },
            );
            Ok(nonce)
        }
        pub(super) fn consume(
            &self,
            nonce: &str,
            binding: &ApprovalBinding,
            transport_hash: &str,
        ) -> Result<RunPermit, &'static str> {
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            let grant = state.pending.remove(nonce).ok_or("mcp_grant_unavailable")?;
            if grant.expires <= Instant::now()
                || &grant.binding != binding
                || grant.transport_hash != transport_hash
            {
                return Err("mcp_grant_changed");
            }
            if !Self::scope_allowed(&state, binding) {
                return Err("mcp_scope_cancelled");
            }
            let cancelled = Arc::new(AtomicBool::new(false));
            state
                .active
                .insert(nonce.into(), (binding.clone(), cancelled.clone()));
            Ok(RunPermit { cancelled })
        }
        fn scope_allowed(state: &State, binding: &ApprovalBinding) -> bool {
            let shared_ok=match &binding.shared_recipient_binding {
                None => !state.shared_agents.contains(&(binding.owner_id,binding.agent_id)),
                Some(shared) => {
                    let key=(shared.owner_id,shared.authority_id.clone(),shared.session_generation.clone());
                    !state.shared_closed && shared.agrees(binding) && !state.shared_revoked.contains(&key)
                        && state.shared_authorities.get(&key)==Some(shared)
                }
            };
            shared_ok && binding.execution_binding.as_ref().is_none_or(|proof| {
                proof.valid(binding.owner_id, binding.agent_id, &binding.root_event_id, binding.generation)
                    && proof.lineage.iter().all(|entry| state.revoked_scopes
                        .get(&(binding.owner_id, entry.scope_id.clone()))
                        .is_none_or(|epoch| entry.epoch > *epoch))
            })
        }
        pub(super) fn revoke_scopes(&self, owner: i64, scopes: &[super::ScopeEpoch]) -> Result<(), &'static str> {
            if owner <= 0 || scopes.is_empty() || scopes.len() > 1024
                || scopes.iter().any(|s| !super::uuid_string(&s.scope_id)) {
                return Err("invalid_mcp_scope");
            }
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            if state.closed { return Err("mcp_authority_unavailable"); }
            let new_keys=scopes.iter().map(|scope|scope.scope_id.as_str())
                .filter(|id|!state.revoked_scopes.contains_key(&(owner,(*id).to_owned())))
                .collect::<std::collections::HashSet<_>>().len();
            if state.revoked_scopes.len().saturating_add(new_keys) > 16384 {
                state.closed = true;
                return Err("mcp_authority_unavailable");
            }
            for item in scopes {
                state.revoked_scopes.entry((owner, item.scope_id.clone()))
                    .and_modify(|epoch| *epoch = (*epoch).max(item.epoch)).or_insert(item.epoch);
            }
            let affected = |b: &ApprovalBinding| b.owner_id == owner && b.execution_binding.as_ref()
                .is_some_and(|p| p.lineage.iter().any(|v| scopes.iter()
                    .any(|r| r.scope_id == v.scope_id && v.epoch <= r.epoch)));
            state.pending.retain(|_, grant| !affected(&grant.binding));
            for (binding, cancelled) in state.active.values() {
                if affected(binding) { cancelled.store(true, Ordering::Release); }
            }
            Ok(())
        }
        pub(super) fn revoke(&self, owner: i64, server: &str) -> Result<(), &'static str> {
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            let affected: Vec<_> = state.shared_authorities.iter()
                .filter_map(|(key,binding)| if binding.owner_id==owner && binding.server_id==server
                    && !state.shared_revoked.contains(key) {Some(key.clone())} else {None}).collect();
            if state.shared_revoked.len().saturating_add(affected.len())>16384 {
                state.shared_closed=true;
                state.shared_authorizations.clear();
                state.pending.retain(|_,grant|grant.binding.shared_recipient_binding.is_none());
                for (binding,cancel) in state.active.values() {
                    if binding.shared_recipient_binding.is_some() {cancel.store(true,Ordering::Release);}
                }
            } else {
                for key in affected {state.shared_revoked.insert(key);}
                state.shared_authorizations.retain(|_,(binding,_)| !(binding.owner_id==owner && binding.server_id==server));
            }
            state.configurations.remove(&(owner, server.to_owned()));
            state
                .bindings
                .retain(|(o, s, _), _| *o != owner || s != server);
            state.pending.retain(|_, grant| {
                grant.binding.owner_id != owner || grant.binding.server_id != server
            });
            state
                .discovery_pending
                .retain(|_, (binding, _)| binding.owner_id != owner || binding.server_id != server);
            for (binding, cancel) in state.discovery_active.values() {
                if binding.owner_id == owner && binding.server_id == server {
                    cancel.store(true, Ordering::Release);
                }
            }

            for (binding, cancel) in state.active.values() {
                if binding.owner_id == owner && binding.server_id == server {
                    cancel.store(true, Ordering::Release);
                }
            }
            Ok(())
        }
        pub(super) fn cancel(
            &self,
            owner: i64,
            server: &str,
            root: &str,
            generation: u64,
        ) -> Result<(), &'static str> {
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            if state.cancelled_roots.len() >= 16384 {
                state.closed = true;
            } else {
                state.cancelled_roots.insert((
                    owner,
                    server.to_owned(),
                    root.to_owned(),
                    generation,
                ));
            }
            state.pending.retain(|_, grant| {
                !(grant.binding.owner_id == owner
                    && grant.binding.server_id == server
                    && grant.binding.root_event_id == root
                    && grant.binding.generation == generation)
            });
            for (binding, cancel) in state.active.values() {
                if binding.owner_id == owner
                    && binding.server_id == server
                    && binding.root_event_id == root
                    && binding.generation == generation
                {
                    cancel.store(true, Ordering::Release);
                }
            }
            Ok(())
        }
        pub(super) fn finish(&self, nonce: &str) -> Result<(), &'static str> {
            let mut state = self.0.lock().map_err(|_| "mcp_authority_unavailable")?;
            state.active.remove(nonce);
            state.discovery_active.remove(nonce);
            Ok(())
        }
    }
}

// These primitives are deliberately private until the authenticated one-use
// dispatch channel owns their invocation. Returned server data is untrusted.
mod http_wire {
    use super::MAX_FRAME;
    use serde_json::Value;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    use std::time::Duration;

    pub(super) struct HttpWire {
        client: reqwest::Client,
        url: String,
        bearer: Option<zeroize::Zeroizing<String>>,
        session: Option<String>,
    }
    impl HttpWire {
        pub(super) fn new(
            url: String,
            bearer: Option<zeroize::Zeroizing<String>>,
        ) -> Result<Self, &'static str> {
            super::Transport::Http { url: url.clone() }.validate()?;
            if bearer
                .as_ref()
                .is_some_and(|value| value.is_empty() || value.len() > 32768)
            {
                return Err("mcp_secret_invalid");
            }
            let client = reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .no_proxy()
                .connect_timeout(Duration::from_secs(5))
                .timeout(Duration::from_secs(30))
                .build()
                .map_err(|_| "mcp_http_unavailable")?;
            Ok(Self {
                client,
                url,
                bearer,
                session: None,
            })
        }
        pub(super) async fn notify(
            &self,
            message: &Value,
            cancelled: Arc<AtomicBool>,
        ) -> Result<(), &'static str> {
            if cancelled.load(Ordering::Acquire) {
                return Err("mcp_cancelled_uncertain");
            }
            let body = serde_json::to_vec(message).map_err(|_| "mcp_invalid_request")?;
            if body.len() > MAX_FRAME {
                return Err("mcp_frame_too_large");
            }
            let mut request = self
                .client
                .post(&self.url)
                .header("Accept", "application/json, text/event-stream")
                .header("Content-Type", "application/json")
                .header("MCP-Protocol-Version", "2025-06-18")
                .body(body);
            if let Some(session) = &self.session {
                request = request.header("Mcp-Session-Id", session);
            }
            if let Some(secret) = &self.bearer {
                request = request.bearer_auth(secret.as_str());
            }
            let response = tokio::select! { value=request.send()=>value.map_err(|_| "mcp_http_uncertain")?, _=wait_cancel(cancelled)=>return Err("mcp_cancelled_uncertain") };
            if response.status().as_u16() != 202 {
                return Err(if matches!(response.status().as_u16(),401 | 403) {"mcp_http_auth_failed"} else {"mcp_http_failed"});
            }
            Ok(())
        }
        pub(super) async fn request(
            &mut self,
            message: &Value,
            cancelled: Arc<AtomicBool>,
        ) -> Result<Value, &'static str> {
            if cancelled.load(Ordering::Acquire) {
                return Err("mcp_cancelled_uncertain");
            }
            let body = serde_json::to_vec(message).map_err(|_| "mcp_invalid_request")?;
            if body.len() > MAX_FRAME {
                return Err("mcp_frame_too_large");
            }
            let expected = message.get("id").cloned().ok_or("mcp_missing_id")?;
            let mut request = self
                .client
                .post(&self.url)
                .header("Accept", "application/json, text/event-stream")
                .header("Content-Type", "application/json")
                .header("MCP-Protocol-Version", "2025-06-18")
                .body(body);
            if let Some(session) = &self.session {
                request = request.header("Mcp-Session-Id", session);
            }
            if let Some(secret) = &self.bearer {
                request = request.bearer_auth(secret.as_str());
            }
            let mut response = tokio::select! {
                result=request.send()=>result.map_err(|_| "mcp_http_uncertain")?,
                _=wait_cancel(cancelled.clone())=>return Err("mcp_cancelled_uncertain"),
            };
            if !response.status().is_success() {
                return Err(if matches!(response.status().as_u16(),401 | 403) {"mcp_http_auth_failed"} else {"mcp_http_failed"});
            }
            if let Some(raw) = response.headers().get("Mcp-Session-Id") {
                let id = raw.to_str().map_err(|_| "mcp_session_invalid")?;
                if id.is_empty()
                    || id.len() > 256
                    || !id.bytes().all(|b| (0x21..=0x7e).contains(&b))
                {
                    return Err("mcp_session_invalid");
                }
                if self.session.as_ref().is_some_and(|known| known != id) {
                    return Err("mcp_session_changed");
                }
                self.session = Some(id.to_owned());
            }
            let content = response
                .headers()
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .split(';')
                .next()
                .unwrap_or("")
                .trim();
            let stream = match content {
                "application/json" => false,
                "text/event-stream" => true,
                _ => return Err("mcp_http_content_type"),
            };
            let mut buffer = Vec::new();
            loop {
                let next = tokio::select! {
                    result=response.chunk()=>result.map_err(|_| "mcp_http_uncertain")?,
                    _=wait_cancel(cancelled.clone())=>return Err("mcp_cancelled_uncertain"),
                };
                let Some(chunk) = next else {
                    break;
                };
                if buffer.len().saturating_add(chunk.len()) > MAX_FRAME {
                    return Err("mcp_frame_too_large");
                }
                buffer.extend_from_slice(&chunk);
                if stream {
                    if let Some(value) = sse_reply(
                        &buffer,
                        &expected,
                        self.bearer.as_deref().map(|v| v.as_str()),
                    )? {
                        return Ok(value);
                    }
                }
            }
            if stream {
                return Err("mcp_http_uncertain");
            }
            let value: Value =
                serde_json::from_slice(&buffer).map_err(|_| "mcp_invalid_response")?;
            checked(value, &expected, self.bearer.as_deref().map(|s| s.as_str()))
        }
    }
    fn sse_reply(
        buffer: &[u8],
        expected: &Value,
        secret: Option<&str>,
    ) -> Result<Option<Value>, &'static str> {
        let text = match std::str::from_utf8(buffer) {
            Ok(v) => v,
            Err(e) if e.error_len().is_none() => return Ok(None),
            Err(_) => return Err("mcp_invalid_utf8"),
        };
        let normalized = text.replace("\r\n", "\n");
        for event in normalized
            .split("\n\n")
            .take(normalized.matches("\n\n").count())
        {
            let data = event
                .lines()
                .filter_map(|line| {
                    line.strip_prefix("data:")
                        .map(|v| v.strip_prefix(' ').unwrap_or(v))
                })
                .collect::<Vec<_>>()
                .join("\n");
            if data.is_empty() {
                continue;
            }
            let value: Value = serde_json::from_str(&data).map_err(|_| "mcp_invalid_response")?;
            if value.get("id") == Some(expected) {
                return checked(value, expected, secret).map(Some);
            }
            if value.get("id").is_some() {
                return Err("mcp_unexpected_server_request");
            }
        }
        Ok(None)
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        use serde_json::json;
        #[test]
        fn json_sse_frames_and_secret_echo_are_bounded() {
            let id = json!(3);
            let body = br#"data: {"jsonrpc":"2.0","id":3,"result":{"text":"ok"}}

"#;
            assert!(sse_reply(&body[..body.len() - 2], &id, None)
                .unwrap()
                .is_none());
            assert_eq!(
                sse_reply(body, &id, None).unwrap().unwrap()["result"]["text"],
                "ok"
            );
            assert!(checked(
                json!({"jsonrpc":"2.0","id":3,"result":{"text":"synthetic-token"}}),
                &id,
                Some("synthetic-token")
            )
            .is_err());
            assert!(checked(json!({"jsonrpc":"2.0","id":4,"result":{}}), &id, None).is_err());
        }
    }
    fn sensitive(value: &Value, secret: &str) -> bool {
        match value {
            Value::String(s) => s.contains(secret),
            Value::Array(items) => items.iter().any(|v| sensitive(v, secret)),
            Value::Object(map) => map
                .iter()
                .any(|(k, v)| k.contains(secret) || sensitive(v, secret)),
            _ => false,
        }
    }
    fn checked(value: Value, id: &Value, secret: Option<&str>) -> Result<Value, &'static str> {
        if secret.is_some_and(|token| sensitive(&value, token)) {
            return Err("mcp_sensitive_response");
        }
        if value.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
            || value.get("id") != Some(id)
            || value.get("result").is_none()
            || value.get("error").is_some()
        {
            return Err("mcp_rpc_failed");
        }
        Ok(value)
    }
    async fn wait_cancel(cancelled: Arc<AtomicBool>) {
        while !cancelled.load(Ordering::Acquire) {
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }
}

#[cfg(unix)]
mod stdio_wire {
    use super::{Transport, MAX_FRAME};
    use serde_json::Value;
    use std::process::Stdio;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    use std::time::Duration;
    use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
    use tokio::process::{Child, ChildStdin, ChildStdout, Command};

    pub(super) struct StdioWire {
        child: Child,
        input: ChildStdin,
        output: BufReader<ChildStdout>,
        pgid: i32,
    }
    impl StdioWire {
        pub(super) fn start(executable: String, argv: Vec<String>) -> Result<Self, &'static str> {
            use std::os::unix::process::CommandExt;
            Transport::Stdio {
                executable: executable.clone(),
                argv: argv.clone(),
            }
            .validate()?;
            let mut cmd = Command::new(executable);
            cmd.args(argv)
                .env_clear()
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .kill_on_drop(true);
            // This owned group is killed before its leader is reaped, including descendants.
            cmd.as_std_mut().process_group(0);
            let mut child = cmd.spawn().map_err(|_| "mcp_process_unavailable")?;
            let pgid = i32::try_from(child.id().ok_or("mcp_process_unavailable")?)
                .map_err(|_| "mcp_process_unavailable")?;
            let input = child.stdin.take().ok_or("mcp_process_unavailable")?;
            let output = BufReader::new(child.stdout.take().ok_or("mcp_process_unavailable")?);
            Ok(Self {
                child,
                input,
                output,
                pgid,
            })
        }
        pub(super) async fn notify(
            &mut self,
            message: &Value,
            cancelled: Arc<AtomicBool>,
        ) -> Result<(), &'static str> {
            if cancelled.load(Ordering::Acquire) {
                return Err("mcp_cancelled_uncertain");
            }
            let mut data = serde_json::to_vec(message).map_err(|_| "mcp_invalid_request")?;
            if data.len() > MAX_FRAME {
                return Err("mcp_frame_too_large");
            }
            data.push(b'\n');
            tokio::select! { value=self.input.write_all(&data)=>value.map_err(|_| "mcp_process_uncertain")?, _=wait_cancel(cancelled)=>return Err("mcp_cancelled_uncertain") };
            Ok(())
        }
        pub(super) async fn request(
            &mut self,
            message: &Value,
            cancelled: Arc<AtomicBool>,
        ) -> Result<Value, &'static str> {
            if cancelled.load(Ordering::Acquire) {
                return Err("mcp_cancelled_uncertain");
            }
            let mut data = serde_json::to_vec(message).map_err(|_| "mcp_invalid_request")?;
            if data.len() > MAX_FRAME {
                return Err("mcp_frame_too_large");
            }
            let id = message.get("id").cloned().ok_or("mcp_missing_id")?;
            data.push(b'\n');
            tokio::select! { value=self.input.write_all(&data)=>value.map_err(|_| "mcp_process_uncertain")?, _=wait_cancel(cancelled.clone())=>return Err("mcp_cancelled_uncertain") };
            let read = async {
                loop {
                    let mut line = Vec::new();
                    let size = (&mut self.output)
                        .take((MAX_FRAME + 1) as u64)
                        .read_until(b'\n', &mut line)
                        .await
                        .map_err(|_| "mcp_process_uncertain")?;
                    if size == 0 {
                        return Err("mcp_process_uncertain");
                    }
                    if size > MAX_FRAME || !line.ends_with(b"\n") {
                        return Err("mcp_frame_too_large");
                    }
                    let value: Value =
                        serde_json::from_slice(&line).map_err(|_| "mcp_invalid_response")?;
                    if value.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
                        return Err("mcp_invalid_response");
                    }
                    if value.get("id") == Some(&id) {
                        if value.get("result").is_none() || value.get("error").is_some() {
                            return Err("mcp_rpc_failed");
                        }
                        return Ok(value);
                    }
                    if value.get("id").is_some() {
                        return Err("mcp_unexpected_server_request");
                    }
                }
            };
            tokio::select! {
                result=tokio::time::timeout(Duration::from_secs(30), read)=>result.map_err(|_| "mcp_process_uncertain")?,
                _=wait_cancel(cancelled)=>Err("mcp_cancelled_uncertain"),
            }
        }
    }
    impl Drop for StdioWire {
        fn drop(&mut self) {
            // No wait/try_wait occurs before this kill: a terminated leader is
            // still unreaped and therefore its process-group id cannot be reused.
            unsafe {
                libc::kill(-self.pgid, libc::SIGKILL);
            }
            let _ = self.child.start_kill();
        }
    }
    async fn wait_cancel(cancelled: Arc<AtomicBool>) {
        while !cancelled.load(Ordering::Acquire) {
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }
}

#[cfg(unix)]
mod protocol_wire {
    use super::{
        http_wire::HttpWire, stdio_wire::StdioWire, ApprovalBinding, Transport, MAX_FRAME,
        MAX_TOOLS,
    };
    use serde_json::{json, Value};
    use std::collections::HashSet;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };

    enum Wire {
        Http(HttpWire),
        Stdio(StdioWire),
        #[cfg(test)]
        Script(ScriptWire),
    }
    #[cfg(test)]
    struct ScriptWire {
        calls: Arc<std::sync::Mutex<Vec<String>>>,
        replies: std::collections::VecDeque<(&'static str, Value)>,
    }

    impl Wire {
        async fn request(
            &mut self,
            message: &Value,
            cancel: Arc<AtomicBool>,
        ) -> Result<Value, &'static str> {
            match self {
                Self::Http(v) => v.request(message, cancel).await,
                Self::Stdio(v) => v.request(message, cancel).await,
                #[cfg(test)]
                Self::Script(v) => {
                    if cancel.load(Ordering::Acquire) {
                        return Err("mcp_cancelled_uncertain");
                    }
                    let method = message
                        .get("method")
                        .and_then(Value::as_str)
                        .ok_or("mcp_invalid_request")?;
                    let (expected, result) =
                        v.replies.pop_front().ok_or("mcp_unexpected_script")?;
                    if method != expected {
                        return Err("mcp_unexpected_script");
                    }
                    v.calls
                        .lock()
                        .map_err(|_| "mcp_unexpected_script")?
                        .push(method.to_owned());
                    Ok(json!({"jsonrpc":"2.0","id":message["id"],"result":result}))
                }
            }
        }
        async fn notify(
            &mut self,
            message: &Value,
            cancel: Arc<AtomicBool>,
        ) -> Result<(), &'static str> {
            match self {
                Self::Http(v) => v.notify(message, cancel).await,
                Self::Stdio(v) => v.notify(message, cancel).await,
                #[cfg(test)]
                Self::Script(v) => {
                    if cancel.load(Ordering::Acquire) {
                        return Err("mcp_cancelled_uncertain");
                    }
                    let method = message
                        .get("method")
                        .and_then(Value::as_str)
                        .ok_or("mcp_invalid_request")?;
                    v.calls
                        .lock()
                        .map_err(|_| "mcp_unexpected_script")?
                        .push(method.to_owned());
                    Ok(())
                }
            }
        }
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        use sha2::{Digest, Sha256};
        #[tokio::test]
        async fn scripted_initialize_list_and_call_without_external_io() {
            let calls = Arc::new(std::sync::Mutex::new(Vec::new()));
            let schema = json!({"type":"object"});
            let args = json!({"value":3});
            let responses = std::collections::VecDeque::from(vec![
                (
                    "initialize",
                    json!({"protocolVersion":"2025-06-18","capabilities":{"tools":{}}}),
                ),
                (
                    "tools/list",
                    json!({"tools":[{"name":"sample","inputSchema":schema}]}),
                ),
                (
                    "tools/call",
                    json!({"content":[{"type":"text","text":"synthetic"}]}),
                ),
            ]);
            let wire = Wire::Script(ScriptWire {
                calls: calls.clone(),
                replies: responses,
            });
            let mut session = Session::open_wire(wire, Arc::new(AtomicBool::new(false)))
                .await
                .unwrap();
            let sc = serde_json::to_string(&schema).unwrap();
            let ac = serde_json::to_string(&args).unwrap();
            let binding = ApprovalBinding {
                owner_id: 1,
                owner_epoch: "a".repeat(64),
                core_epoch: "b".repeat(64),
                agent_id: 2,
                root_event_id: "root".into(),
                action_id: "action".into(),
                review_id: "review".into(),
                grant_id: 1,
                generation: 1,
                server_id: "server".into(),
                config_revision: 1,
                binding_revision: 1,
                tool_id: "e".repeat(64),
                tool_name: "sample".into(),
                schema_sha256: format!("{:x}", Sha256::digest(sc.as_bytes())),
                input_sha256: format!("{:x}", Sha256::digest(ac.as_bytes())),
                payload_digest: "c".repeat(64),
                review_sha256: "d".repeat(64),
                execution_binding: None,
                teammate_binding: None,
                shared_recipient_binding: None,
            };
            let result = session
                .invoke(&binding, &schema, &args, &sc, &ac)
                .await
                .unwrap();
            assert_eq!(result["content"][0]["text"], "synthetic");
            assert_eq!(
                *calls.lock().unwrap(),
                vec![
                    "initialize",
                    "notifications/initialized",
                    "tools/list",
                    "tools/call"
                ]
            );
        }
    }

    /// The caller must consume an Owner-approved discovery or action nonce
    /// before constructing this session. Never call directly from Engine input.
    pub(super) struct Session {
        wire: Wire,
        next_id: u64,
        cancel: Arc<AtomicBool>,
    }
    impl Session {
        pub(super) async fn open(
            transport: Transport,
            bearer: Option<zeroize::Zeroizing<String>>,
            cancel: Arc<AtomicBool>,
        ) -> Result<Self, &'static str> {
            if cancel.load(Ordering::Acquire) {
                return Err("mcp_cancelled_uncertain");
            }
            let wire = match transport {
                Transport::Http { url } => Wire::Http(HttpWire::new(url, bearer)?),
                Transport::Stdio { executable, argv } => {
                    if bearer.is_some() {
                        return Err("mcp_stdio_credential_unsupported");
                    }
                    Wire::Stdio(StdioWire::start(executable, argv)?)
                }
            };
            Self::open_wire(wire, cancel).await
        }
        async fn open_wire(wire: Wire, cancel: Arc<AtomicBool>) -> Result<Self, &'static str> {
            let mut session = Self {
                wire,
                next_id: 1,
                cancel,
            };
            let init=session.rpc("initialize",json!({"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"gamja-core","version":"1"}})).await?;
            let value = init.get("result").ok_or("mcp_invalid_initialize")?;
            if value.get("protocolVersion").and_then(Value::as_str) != Some("2025-06-18")
                || value
                    .get("capabilities")
                    .and_then(|x| x.get("tools"))
                    .is_none()
            {
                return Err("mcp_protocol_unsupported");
            }
            session
                .wire
                .notify(
                    &json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
                    session.cancel.clone(),
                )
                .await.map_err(|code| match code {
                    "mcp_http_auth_failed" => "mcp_initialized_auth_failed",
                    "mcp_http_failed" => "mcp_initialized_http_failed",
                    _ => "mcp_initialized_failed",
                })?;
            Ok(session)
        }
        async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, &'static str> {
            let id = self.next_id;
            self.next_id = self.next_id.checked_add(1).ok_or("mcp_request_limit")?;
            self.wire
                .request(
                    &json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}),
                    self.cancel.clone(),
                )
                .await
        }
        pub(super) async fn tools(&mut self) -> Result<Vec<Value>, &'static str> {
            let mut tools = Vec::new();
            let mut cursor: Option<String> = None;
            let mut cursors = HashSet::new();
            let mut names = HashSet::new();
            for _ in 0..8 {
                let params = if let Some(c) = &cursor {
                    json!({"cursor":c})
                } else {
                    json!({})
                };
                let response = self.rpc("tools/list", params).await?;
                let result = response
                    .get("result")
                    .and_then(Value::as_object)
                    .ok_or("mcp_invalid_tools")?;
                let rows = result
                    .get("tools")
                    .and_then(Value::as_array)
                    .ok_or("mcp_invalid_tools")?;
                for tool in rows {
                    let name = tool
                        .get("name")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty() && s.len() <= 128)
                        .ok_or("mcp_invalid_tools")?;
                    if !names.insert(name.to_owned()) || tools.len() >= MAX_TOOLS {
                        return Err("mcp_invalid_tools");
                    }
                    super::validate_schema_detail(tool.get("inputSchema").ok_or("mcp_invalid_tools")?)?;
                    tools.push(tool.clone());
                    if serde_json::to_vec(&tools)
                        .map_err(|_| "mcp_invalid_tools")?
                        .len()
                        > MAX_FRAME / 2
                    {
                        return Err("mcp_tools_too_large");
                    }
                }
                match result.get("nextCursor") {
                    None | Some(Value::Null) => return Ok(tools),
                    Some(Value::String(next))
                        if !next.is_empty()
                            && next.len() <= 1024
                            && cursors.insert(next.clone()) =>
                    {
                        cursor = Some(next.clone())
                    }
                    _ => return Err("mcp_invalid_tools"),
                }
            }
            Err("mcp_page_limit")
        }
        pub(super) async fn invoke(
            &mut self,
            binding: &ApprovalBinding,
            schema: &Value,
            arguments: &Value,
            schema_canonical: &str,
            arguments_canonical: &str,
        ) -> Result<Value, &'static str> {
            binding.validate(arguments, schema, arguments_canonical, schema_canonical)?;
            let tools = self.tools().await?;
            let matching: Vec<_> = tools
                .iter()
                .filter(|tool| tool.get("name").and_then(Value::as_str) == Some(&binding.tool_name))
                .collect();
            if matching.len() != 1 || matching[0].get("inputSchema") != Some(schema) {
                return Err("mcp_schema_changed");
            }
            let response = self
                .rpc(
                    "tools/call",
                    json!({"name":binding.tool_name,"arguments":arguments}),
                )
                .await?;
            let result = response
                .get("result")
                .cloned()
                .ok_or("mcp_invalid_response")?;
            if serde_json::to_vec(&result)
                .map_err(|_| "mcp_invalid_response")?
                .len()
                > MAX_FRAME / 2
            {
                return Err("mcp_result_too_large");
            }
            Ok(result)
        }
    }
}

#[cfg(unix)]
pub(crate) mod private_channel {
    use super::{
        grant_registry::GrantRegistry, protocol_wire::Session, ApprovalBinding, DiscoveryBinding,
        Transport, MAX_FRAME,
    };
    use crate::auth_broker::{AuthBroker, BackendGeneration, SyntheticAuthTransport};
    use crate::credential::CredentialBackend;
    use serde::Deserialize;
    use serde_json::{json, Value};
    use sha2::Digest;
    use std::collections::HashMap;
    use std::io::{Read, Write};
    use std::os::unix::net::UnixStream;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    };

    #[derive(Clone, Deserialize, PartialEq, Eq)]
    #[serde(deny_unknown_fields)]
    struct Config {
        owner_id: i64,
        owner_epoch: String,
        core_epoch: String,
        server_id: String,
        config_revision: u64,
        transport: Transport,
        transport_canonical: String,
        transport_sha256: String,
        credential_ref: Option<String>,
    }
    impl Config {
        fn only_owner_epoch_changed(&self, incoming: &Self) -> bool {
            if self.owner_epoch == incoming.owner_epoch { return false; }
            let mut old = self.clone();
            old.owner_epoch = incoming.owner_epoch.clone();
            old == *incoming
        }
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Bind {
        owner_id: i64,
        owner_epoch: String,
        core_epoch: String,
        server_id: String,
        config_revision: u64,
        agent_id: i64,
        binding_revision: u64,
        enabled: bool,
        allowed_tools: Vec<super::ToolPermission>,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Revocation {
        owner_id: i64,
        owner_epoch: String,
        core_epoch: String,
        server_id: String,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ScopeRevocation {
        owner_id: i64,
        owner_epoch: String,
        core_epoch: String,
        scopes: Vec<super::ScopeEpoch>,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct SharedAuthorization {
        binding: super::SharedRecipientBinding,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct SharedRevocation {
        owner_id: i64,
        owner_epoch: String,
        core_epoch: String,
        authority_id: String,
        session_generation: String,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct CredentialStatus {
        owner_id: i64,
        owner_epoch: String,
        core_epoch: String,
        server_id: String,
        config_revision: u64,
        credential_ref: String,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Cancel {
        owner_id: i64,
        owner_epoch: String,
        core_epoch: String,
        server_id: String,
        root_event_id: String,
        generation: u64,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Action {
        binding: ApprovalBinding,
        transport_sha256: String,
        schema: Value,
        arguments: Value,
        schema_canonical: String,
        arguments_canonical: String,
        nonce: Option<String>,
        review: Value,
        review_canonical: String,
        action_payload_canonical: String,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Envelope {
        version: u8,
        request_id: u64,
        operation: String,
        payload: Value,
    }
    pub(crate) struct State {
        #[cfg(all(target_os="macos",target_arch="aarch64"))]
        slack: Option<Arc<crate::slack_transport::Manager>>,
        registry: GrantRegistry,
        speech: Arc<crate::speech_memo::Registry>,
        configurations: Mutex<HashMap<(i64, String), Config>>,
        oauth: Arc<crate::notion_oauth::Manager>,
    }
    pub(crate) trait SecretOwner: Send + Sync {
        fn oauth_begin(&self, server_id:&str, expected_revision:u64, operation_id:&str) -> Result<Value, &'static str>;
        fn oauth_status(&self, server_id:&str, expected_revision:u64, operation_id:&str) -> Result<Value, &'static str>;
        fn oauth_cancel(&self, server_id:&str, expected_revision:u64, operation_id:&str) -> Result<Value, &'static str>;
        fn status(
            &self,
            server_id: &str,
            expected_revision: u64,
            operation_id: &str,
        ) -> Result<Option<String>, &'static str>;
        fn store(
            &self,
            server_id: &str,
            expected_revision: u64,
            operation_id: &str,
            secret: zeroize::Zeroizing<Vec<u8>>,
        ) -> Result<String, &'static str>;
    }
    struct OwnerHandle<B: CredentialBackend, T: SyntheticAuthTransport> {
        state: Arc<State>,
        broker: Arc<Mutex<AuthBroker<B, T>>>,
    }
    fn oauth_config(state:&State, server:&str, revision:u64) -> Result<Config, &'static str> {
        let entries=state.configurations.lock().map_err(|_| "mcp_authority_unavailable")?;
        let mut hits=entries.values().filter(|c| c.server_id==server && c.config_revision==revision);
        let config=hits.next().ok_or("mcp_configuration_changed")?.clone();
        if hits.next().is_some() || !matches!(&config.transport, Transport::Http{url} if url==crate::notion_oauth::ENDPOINT) {return Err("mcp_configuration_changed");}
        Ok(config)
    }
    fn oauth_guard<B: CredentialBackend, T: SyntheticAuthTransport>(state:Arc<State>, broker:Arc<Mutex<AuthBroker<B,T>>>, config:Config) -> Arc<dyn Fn()->bool + Send + Sync> {
        Arc::new(move || state.configurations.lock().ok().is_some_and(|entries| entries.get(&(config.owner_id,config.server_id.clone()))==Some(&config)) && owner_ok(&broker,config.owner_id,&config.owner_epoch,&config.core_epoch))
    }
    fn oauth_store<B: CredentialBackend, T: SyntheticAuthTransport>(state:Arc<State>, broker:Arc<Mutex<AuthBroker<B,T>>>, config:Config, reference:String) -> Arc<dyn Fn(&[u8])->Result<(), &'static str> + Send + Sync> {
        Arc::new(move |data| {
            let entries=state.configurations.lock().map_err(|_| "mcp_authority_unavailable")?;
            if entries.get(&(config.owner_id,config.server_id.clone()))!=Some(&config) {return Err("mcp_configuration_changed");}
            let mut broker=broker.lock().map_err(|_| "mcp_owner_unavailable")?;
            if !broker.mcp_owner_valid(config.owner_id as u64,&config.owner_epoch,&config.core_epoch) {return Err("mcp_owner_changed");}
            broker.mcp_secret_put(config.owner_id as u64,&config.owner_epoch,&config.server_id,&reference,&zeroize::Zeroizing::new(data.to_vec()))
        })
    }
    impl<B: CredentialBackend, T: SyntheticAuthTransport> SecretOwner for OwnerHandle<B, T> {
        fn oauth_begin(&self, server:&str, revision:u64, operation:&str) -> Result<Value,&'static str> {
            let config=oauth_config(&self.state,server,revision)?;
            let reference=format!("mcp-oauth:{}:{}:{}",config.owner_id,server,operation);
            let reuse = {
                let mut broker=self.broker.lock().map_err(|_| "mcp_owner_unavailable")?;
                if !broker.mcp_owner_valid(config.owner_id as u64,&config.owner_epoch,&config.core_epoch) {return Err("mcp_owner_changed");}
                if broker.mcp_secret_get(config.owner_id as u64,&config.owner_epoch,server,&reference)?.is_some() {return Err("mcp_oauth_operation_conflict");}
                if let Some(old)=config.credential_ref.as_deref().filter(|r|r.starts_with("mcp-oauth:")) {
                    let data=broker.mcp_secret_get(config.owner_id as u64,&config.owner_epoch,server,old)?.ok_or("mcp_oauth_invalid")?;
                    let bundle=serde_json::from_slice::<crate::notion_oauth::Bundle>(&data).map_err(|_| "mcp_oauth_invalid")?;
                    Some(crate::notion_oauth::Reuse::from_bundle(&bundle,config.owner_id,server)?)
                } else {None}
            };
            let guard=oauth_guard(self.state.clone(),self.broker.clone(),config.clone());
            let store=oauth_store(self.state.clone(),self.broker.clone(),config.clone(),reference.clone());
            self.state.oauth.begin(server.into(),revision,operation.into(),config.owner_id,reference,reuse,guard,store)
        }
        fn oauth_status(&self, server:&str, revision:u64, operation:&str) -> Result<Value,&'static str> {
            let config=oauth_config(&self.state,server,revision)?;
            if !owner_ok(&self.broker,config.owner_id,&config.owner_epoch,&config.core_epoch) {return Err("mcp_owner_changed");}
            self.state.oauth.status(server,revision,operation,false)
        }
        fn oauth_cancel(&self, server:&str, revision:u64, operation:&str) -> Result<Value,&'static str> {
            let config=oauth_config(&self.state,server,revision)?;
            if !owner_ok(&self.broker,config.owner_id,&config.owner_epoch,&config.core_epoch) {return Err("mcp_owner_changed");}
            self.state.oauth.status(server,revision,operation,true)
        }
        fn status(
            &self,
            server_id: &str,
            expected_revision: u64,
            operation_id: &str,
        ) -> Result<Option<String>, &'static str> {
            if operation_id.len() != 32
                || !operation_id
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err("mcp_operation_invalid");
            }
            let entries = self
                .state
                .configurations
                .lock()
                .map_err(|_| "mcp_authority_unavailable")?;
            let hits: Vec<_> = entries
                .values()
                .filter(|c| c.server_id == server_id && c.config_revision == expected_revision)
                .collect();
            if hits.len() != 1 {
                return Err("mcp_configuration_changed");
            }
            let config = hits[0];
            let reference = format!("mcp:{}:{}:{}", config.owner_id, server_id, operation_id);
            let mut broker = self.broker.lock().map_err(|_| "mcp_owner_unavailable")?;
            if !broker.mcp_owner_valid(
                config.owner_id as u64,
                &config.owner_epoch,
                &config.core_epoch,
            ) {
                return Err("mcp_owner_changed");
            }
            Ok(broker
                .mcp_secret_get(
                    config.owner_id as u64,
                    &config.owner_epoch,
                    server_id,
                    &reference,
                )?
                .map(|_| reference))
        }
        fn store(
            &self,
            server_id: &str,
            expected_revision: u64,
            operation_id: &str,
            secret: zeroize::Zeroizing<Vec<u8>>,
        ) -> Result<String, &'static str> {
            if operation_id.len() != 32
                || !operation_id
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err("mcp_operation_invalid");
            }
            let entries = self
                .state
                .configurations
                .lock()
                .map_err(|_| "mcp_authority_unavailable")?;
            let hits: Vec<_> = entries
                .values()
                .filter(|c| c.server_id == server_id && c.config_revision == expected_revision)
                .collect();
            if hits.len() != 1 {
                return Err("mcp_configuration_changed");
            }
            let config = hits[0];
            let reference = format!("mcp:{}:{}:{}", config.owner_id, server_id, operation_id);
            let mut broker = self.broker.lock().map_err(|_| "mcp_owner_unavailable")?;
            if !broker.mcp_owner_valid(
                config.owner_id as u64,
                &config.owner_epoch,
                &config.core_epoch,
            ) {
                return Err("mcp_owner_changed");
            }
            if let Some(existing) = broker.mcp_secret_get(
                config.owner_id as u64,
                &config.owner_epoch,
                server_id,
                &reference,
            )? {
                if existing.as_slice() != secret.as_slice() {
                    return Err("mcp_operation_conflict");
                }
            } else {
                broker.mcp_secret_put(
                    config.owner_id as u64,
                    &config.owner_epoch,
                    server_id,
                    &reference,
                    &secret,
                )?;
            }
            Ok(reference)
        }
    }
    pub(crate) fn new_owner<B: CredentialBackend, T: SyntheticAuthTransport>(
        broker: Arc<Mutex<AuthBroker<B, T>>>,
    ) -> (Arc<State>, Arc<dyn SecretOwner>) {
        new_owner_at(broker,None)
    }
    pub(crate) fn new_owner_at<B: CredentialBackend, T: SyntheticAuthTransport>(broker: Arc<Mutex<AuthBroker<B,T>>>, _base:Option<std::path::PathBuf>) -> (Arc<State>,Arc<dyn SecretOwner>) {
        let state = Arc::new(State {
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            slack:_base.map(crate::slack_transport::Manager::new),
            registry: GrantRegistry::new(),
            speech: crate::speech_memo::Registry::new(),
            configurations: Mutex::new(HashMap::new()),
            oauth: Arc::new(crate::notion_oauth::Manager::default()),
        });
        let owner = Arc::new(OwnerHandle {
            state: state.clone(),
            broker,
        });
        (state, owner)
    }
    fn owner_ok<B: CredentialBackend, T: SyntheticAuthTransport>(
        broker: &Arc<Mutex<AuthBroker<B, T>>>,
        owner: i64,
        epoch: &str,
        core: &str,
    ) -> bool {
        owner > 0
            && broker
                .lock()
                .ok()
                .is_some_and(|b| b.mcp_owner_valid(owner as u64, epoch, core))
    }
    fn shared_credential_matches(config:&Config, shared:&super::SharedRecipientBinding)->bool {
        config.owner_id==shared.owner_id && config.owner_epoch==shared.owner_epoch
            && config.core_epoch==shared.core_epoch && config.server_id==shared.server_id
            && config.config_revision==shared.config_revision
            && config.credential_ref.as_ref().is_some_and(|reference|
                format!("{:x}",super::Sha256::digest(reference.as_bytes()))==shared.credential_ref_sha256)
    }
    // A wedged authority lock must cancel speech rather than trap the native
    // renderer's periodic cancellation check behind another channel command.
    fn speech_owner_ok<B: CredentialBackend, T: SyntheticAuthTransport>(
        broker: &Arc<Mutex<AuthBroker<B, T>>>, owner: i64, epoch: &str, core: &str,
    ) -> bool {
        if owner <= 0 { return false; }
        let started=std::time::Instant::now();
        loop {
            match broker.try_lock() {
                Ok(b) => return b.mcp_owner_valid(owner as u64, epoch, core),
                Err(std::sync::TryLockError::Poisoned(_)) => return false,
                Err(std::sync::TryLockError::WouldBlock) if started.elapsed()<std::time::Duration::from_millis(100) => {
                    std::thread::sleep(std::time::Duration::from_millis(2));
                }
                Err(_) => return false,
            }
        }
    }
    fn frame(stream: &mut UnixStream, value: &Value) -> Result<(), ()> {
        let data = serde_json::to_vec(value).map_err(|_| ())?;
        if data.is_empty() || data.len() > MAX_FRAME {
            return Err(());
        }
        stream
            .write_all(&(data.len() as u32).to_be_bytes())
            .and_then(|_| stream.write_all(&data))
            .map_err(|_| ())
    }
    fn read(stream: &mut UnixStream) -> Result<Option<Envelope>, ()> {
        let mut header = [0u8; 4];
        match stream.read(&mut header[..1]) {
            Ok(0) => return Ok(None),
            Ok(1) => {}
            _ => return Err(()),
        }
        stream.read_exact(&mut header[1..]).map_err(|_| ())?;
        let size = u32::from_be_bytes(header) as usize;
        if size == 0 || size > MAX_FRAME {
            return Err(());
        }
        let mut bytes = zeroize::Zeroizing::new(vec![0u8; size]);
        stream.read_exact(&mut bytes).map_err(|_| ())?;
        serde_json::from_slice(&bytes).map(Some).map_err(|_| ())
    }
    fn fail(code: &'static str) -> Value {
        json!({"state":"failed","code":code})
    }
    fn parse<T: for<'a> Deserialize<'a>>(mut value: Value) -> Result<T, &'static str> {
        let object = value.as_object_mut().ok_or("mcp_invalid_control")?;
        if object.remove("version") != Some(json!(1)) {
            return Err("mcp_invalid_control");
        }
        serde_json::from_value(value).map_err(|_| "mcp_invalid_control")
    }
    fn verify_review(action: &Action) -> Result<(), &'static str> {
        if super::canonical_digest(&action.review_canonical, &action.review, 65536)?
            != action.binding.review_sha256
        {
            return Err("mcp_review_changed");
        }
        let a = action.review.get("action").ok_or("mcp_review_changed")?;
        let m = a.get("mcp_binding").ok_or("mcp_review_changed")?;
        let scope = serde_json::to_value(&action.binding.execution_binding).map_err(|_| "mcp_review_changed")?;
        let teammate = serde_json::to_value(&action.binding.teammate_binding).map_err(|_| "mcp_review_changed")?;
        // Action/review/grant IDs may be allocated after the review snapshot.
        // They are bound separately to the one-shot authorization and nonce;
        // all recipient, consent and credential fields remain in the review digest.
        let shared = match &action.binding.shared_recipient_binding {
            None => Value::Null,
            Some(binding) => {
                let mut value=serde_json::to_value(binding).map_err(|_| "mcp_review_changed")?;
                let object=value.as_object_mut().ok_or("mcp_review_changed")?;
                object.remove("action_id"); object.remove("review_id"); object.remove("grant_id");
                value
            }
        };
        if a.get("execution_binding").unwrap_or(&Value::Null) != &scope
            || m.get("teammate_binding").unwrap_or(&Value::Null) != &teammate
            || m.get("shared_recipient_binding").unwrap_or(&Value::Null) != &shared {
            return Err("mcp_review_changed");
        }
        let authority = action.review.get("authority").ok_or("mcp_review_changed")?;
        let proposer = authority.get("proposer").ok_or("mcp_review_changed")?;
        let b = &action.binding;
        let targets = action
            .review
            .get("targets")
            .and_then(Value::as_array)
            .ok_or("mcp_review_changed")?;
        let matches: Vec<_> = targets
            .iter()
            .filter(|v| v.get("role").and_then(Value::as_str) == Some("mcp_server"))
            .collect();
        if matches.len() != 1 {
            return Err("mcp_review_changed");
        }
        let target = matches[0];
        if let Some(recipient)=&b.shared_recipient_binding {
            if a.get("room_id").and_then(Value::as_i64)!=Some(recipient.room_id)
                || authority.get("room_id").and_then(Value::as_i64)!=Some(recipient.room_id)
                || proposer.get("room_id").and_then(Value::as_i64)!=Some(recipient.room_id)
                || target.get("room_id").and_then(Value::as_i64)!=Some(recipient.room_id) {
                return Err("mcp_review_changed");
            }
        }
        if target.get("status").and_then(Value::as_str) != Some("configured")
            || target.get("server_id").and_then(Value::as_str) != Some(b.server_id.as_str())
            || target.get("agent_id").and_then(Value::as_i64) != Some(b.agent_id)
            || target.get("config_revision").and_then(Value::as_u64) != Some(b.config_revision)
            || target.get("binding_revision").and_then(Value::as_u64) != Some(b.binding_revision)
            || target.get("tool_name").and_then(Value::as_str) != Some(b.tool_name.as_str())
            || target.get("schema_sha256").and_then(Value::as_str) != Some(b.schema_sha256.as_str())
            || target.get("transport_sha256").and_then(Value::as_str)
                != Some(action.transport_sha256.as_str())
        {
            return Err("mcp_review_changed");
        }
        let digest_value = json!({"binding":m,"arguments":action.arguments});
        if super::canonical_digest(&action.action_payload_canonical, &digest_value, MAX_FRAME)?
            != b.payload_digest
        {
            return Err("mcp_review_changed");
        }
        if authority.get("owner_id").and_then(Value::as_i64) != Some(b.owner_id)
            || authority.get("agent_id").and_then(Value::as_i64) != Some(b.agent_id)
            || proposer.get("owner_id").and_then(Value::as_i64) != Some(b.owner_id)
            || proposer.get("owner_epoch").and_then(Value::as_str) != Some(b.owner_epoch.as_str())
            || authority.get("os_user_status").and_then(Value::as_str) != Some("verified")
            || authority.get("euid").and_then(Value::as_u64)
                != Some(unsafe { libc::geteuid() } as u64)
        {
            return Err("mcp_review_changed");
        }
        if a.get("payload_digest").and_then(Value::as_str) != Some(b.payload_digest.as_str())
            || a.get("family").and_then(Value::as_str) != Some("mcp_tool_call")
            || a.get("action_id").and_then(Value::as_str) != Some(b.action_id.as_str())
            || a.get("agent_id").and_then(Value::as_i64) != Some(b.agent_id)
            || a.get("trigger_event_id").and_then(Value::as_str) != Some(b.root_event_id.as_str())
            || m.get("owner_id").and_then(Value::as_i64) != Some(b.owner_id)
            || m.get("agent_id").and_then(Value::as_i64) != Some(b.agent_id)
            || m.get("root_event_id").and_then(Value::as_str) != Some(b.root_event_id.as_str())
            || m.get("generation").and_then(Value::as_u64) != Some(b.generation)
            || m.get("server_id").and_then(Value::as_str) != Some(b.server_id.as_str())
            || m.get("config_revision").and_then(Value::as_u64) != Some(b.config_revision)
            || m.get("binding_revision").and_then(Value::as_u64) != Some(b.binding_revision)
            || m.get("tool_id").and_then(Value::as_str) != Some(b.tool_id.as_str())
            || m.get("tool_name").and_then(Value::as_str) != Some(b.tool_name.as_str())
            || m.get("schema_sha256").and_then(Value::as_str) != Some(b.schema_sha256.as_str())
            || m.get("input_sha256").and_then(Value::as_str) != Some(b.input_sha256.as_str())
            || m.get("transport_sha256").and_then(Value::as_str)
                != Some(action.transport_sha256.as_str())
        {
            return Err("mcp_review_changed");
        }
        Ok(())
    }
    fn command<B: CredentialBackend, T: SyntheticAuthTransport>(
        state: &Arc<State>,
        broker: &Arc<Mutex<AuthBroker<B, T>>>,
        operation: &str,
        payload: Value,
    ) -> Result<Value, &'static str> {
        match operation {
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            "slack_configure" | "slack_status" | "slack_poll" => state.slack.as_ref().ok_or("slack_unavailable")?.command(broker.clone(),operation,payload),
            "shared_capabilities" => {
                let _: serde_json::Map<String,Value> = parse(payload)?;
                Ok(json!({"state":"completed","result":{"version":1,"shared_recipient_mcp":true}}))
            }
            "shared_authorize" => {
                let item: SharedAuthorization = parse(payload)?;
                let shared=&item.binding;
                if !owner_ok(broker,shared.owner_id,&shared.owner_epoch,&shared.core_epoch) {
                    return Err("mcp_owner_changed");
                }
                let entries=state.configurations.lock().map_err(|_| "mcp_authority_unavailable")?;
                let config=entries.get(&(shared.owner_id,shared.server_id.clone())).ok_or("mcp_configuration_unavailable")?;
                if !shared_credential_matches(config,shared) {return Err("mcp_shared_credential_changed");}
                state.registry.shared_authorize(item.binding)?;
                Ok(json!({"state":"authorized"}))
            }
            "shared_revoke" => {
                let item: SharedRevocation = parse(payload)?;
                if !owner_ok(broker,item.owner_id,&item.owner_epoch,&item.core_epoch) {return Err("mcp_owner_changed");}
                state.registry.shared_revoke(item.owner_id,&item.authority_id,&item.session_generation)?;
                Ok(json!({"state":"revoked"}))
            }
            "configure" => {
                let config: Config = parse(payload)?;
                if !owner_ok(
                    broker,
                    config.owner_id,
                    &config.owner_epoch,
                    &config.core_epoch,
                ) {
                    return Err("mcp_owner_changed");
                }
                config
                    .transport
                    .validate_frozen(&config.transport_canonical, &config.transport_sha256)?;
                let mut entries = state
                    .configurations
                    .lock()
                    .map_err(|_| "mcp_authority_unavailable")?;
                if let Some(old) = entries.get(&(config.owner_id, config.server_id.clone())) {
                    if old != &config {
                        return Err(if old.only_owner_epoch_changed(&config) {
                            "mcp_configuration_epoch_changed"
                        } else {
                            "mcp_configuration_requires_revoke"
                        });
                    }
                }
                state.registry.configure(
                    config.owner_id,
                    config.server_id.clone(),
                    config.config_revision,
                    config.transport_sha256.clone(),
                )?;
                entries.insert((config.owner_id, config.server_id.clone()), config);
                Ok(json!({"state":"configured"}))
            }
            "bind" => {
                let bind: Bind = parse(payload)?;
                if !owner_ok(broker, bind.owner_id, &bind.owner_epoch, &bind.core_epoch) {
                    return Err("mcp_owner_changed");
                }
                let entries = state
                    .configurations
                    .lock()
                    .map_err(|_| "mcp_authority_unavailable")?;
                let config = entries
                    .get(&(bind.owner_id, bind.server_id.clone()))
                    .ok_or("mcp_configuration_unavailable")?;
                if config.config_revision != bind.config_revision || !bind.enabled {
                    return Err("mcp_binding_changed");
                }
                state.registry.bind(
                    bind.owner_id,
                    &bind.server_id,
                    bind.agent_id,
                    bind.binding_revision,
                    bind.allowed_tools,
                )?;
                Ok(json!({"state":"bound"}))
            }
            "revoke" => {
                let item: Revocation = parse(payload)?;
                if !owner_ok(broker, item.owner_id, &item.owner_epoch, &item.core_epoch) {
                    return Err("mcp_owner_changed");
                }
                let mut entries = state
                    .configurations
                    .lock()
                    .map_err(|_| "mcp_authority_unavailable")?;
                state.registry.revoke(item.owner_id, &item.server_id)?;
                entries.remove(&(item.owner_id, item.server_id));
                Ok(json!({"state":"revoked"}))
            }
            "scope_revoke" => {
                let item: ScopeRevocation = parse(payload)?;
                if !owner_ok(broker, item.owner_id, &item.owner_epoch, &item.core_epoch) {
                    return Err("mcp_owner_changed");
                }
                // Hold the broker mutex while both registries are updated. A
                // custody park takes the same mutex for its final commit, so
                // there is one ordering point for both consumers. Registry
                // methods do not call back into the broker while locked.
                let revoke_result={
                    let mut auth=broker.lock().map_err(|_| "mcp_authority_unavailable")?;
                    if !auth.mcp_owner_valid(item.owner_id as u64,&item.owner_epoch,&item.core_epoch) {
                        Err("mcp_owner_changed")
                    } else {
                        state.registry.revoke_scopes(item.owner_id,&item.scopes).and_then(|()|
                            auth.custody_revoke_scopes(item.owner_id as u64,&item.owner_epoch,&item.core_epoch,&item.scopes))
                    }
                };
                if let Err(code)=revoke_result {
                    state.speech.shutdown();
                    return Err(code);
                }
                if let Err(code)=state.speech.revoke_scopes(item.owner_id, &item.scopes) {
                    state.speech.shutdown();
                    return Err(code);
                }
                Ok(json!({"state":"revoked"}))
            }
            "speech_capabilities" => {
                let _: serde_json::Map<String,Value> = parse(payload)?;
                let voices=crate::speech_memo::voices();
                Ok(json!({"state":"completed","result":{"available":crate::speech_memo::available() && !voices.is_empty(),"format":"pcm_s16le_22050_mono_wav","voices":voices}}))
            }
            "speech_issue" => {
                let request: crate::speech_memo::Request = parse(payload)?;
                let b=&request.binding;
                if !crate::speech_memo::available() || !speech_owner_ok(broker,b.owner_id,&b.owner_epoch,&b.core_epoch) {return Err("speech_unavailable")}
                state.speech.issue(request)
            }
            "speech_cancel" => {
                let request: crate::speech_memo::Cancel = parse(payload)?;
                if !speech_owner_ok(broker,request.owner_id,&request.owner_epoch,&request.core_epoch) {return Err("speech_owner_changed")}
                state.speech.cancel(request)?;
                Ok(json!({"state":"completed","result":{"cancel_requested":true}}))
            }
            "speech_read" => {
                let request: crate::speech_memo::ReadRequest = parse(payload)?;
                if !speech_owner_ok(broker,request.owner_id,&request.owner_epoch,&request.core_epoch) {return Err("speech_owner_changed")}
                let owner=request.owner_id; let epoch=request.owner_epoch.clone(); let core=request.core_epoch.clone();
                let result=state.speech.read(request,||speech_owner_ok(broker,owner,&epoch,&core))?;
                Ok(json!({"state":"completed","result":result}))
            }
            "credential_status" => {
                let item: CredentialStatus = parse(payload)?;
                if !owner_ok(broker, item.owner_id, &item.owner_epoch, &item.core_epoch) {
                    return Err("mcp_owner_changed");
                }
                let entries = state
                    .configurations
                    .lock()
                    .map_err(|_| "mcp_authority_unavailable")?;
                let config = entries
                    .get(&(item.owner_id, item.server_id.clone()))
                    .ok_or("mcp_configuration_unavailable")?;
                if config.config_revision != item.config_revision {
                    return Err("mcp_configuration_changed");
                }
                let value = broker
                    .lock()
                    .map_err(|_| "mcp_owner_unavailable")?
                    .mcp_secret_get(item.owner_id as u64, &item.owner_epoch, &item.server_id, &item.credential_ref)?;
                let found = if item.credential_ref.starts_with("mcp-oauth:") {
                    matches!(&config.transport, Transport::Http{url} if url==crate::notion_oauth::ENDPOINT)
                        && value.as_ref().and_then(|v| serde_json::from_slice::<crate::notion_oauth::Bundle>(v).ok()).is_some_and(|b| b.valid(item.owner_id,&item.server_id))
                } else { value.is_some() };
                Ok(json!({"state":if found {"present"} else {"missing"}}))
            }
            "cancel" => {
                let item: Cancel = parse(payload)?;
                if !owner_ok(broker, item.owner_id, &item.owner_epoch, &item.core_epoch) {
                    return Err("mcp_owner_changed");
                }
                state.registry.cancel(
                    item.owner_id,
                    &item.server_id,
                    &item.root_event_id,
                    item.generation,
                )?;
                Ok(json!({"state":"cancel_requested"}))
            }
            "issue" => {
                let action: Action = parse(payload)?;
                verify_review(&action)?;
                if action.nonce.is_some()
                    || !owner_ok(
                        broker,
                        action.binding.owner_id,
                        &action.binding.owner_epoch,
                        &action.binding.core_epoch,
                    )
                {
                    return Err("mcp_owner_changed");
                }
                let _entries = state
                    .configurations
                    .lock()
                    .map_err(|_| "mcp_authority_unavailable")?;
                if let Some(shared)=&action.binding.shared_recipient_binding {
                    let config=_entries.get(&(shared.owner_id,shared.server_id.clone())).ok_or("mcp_configuration_unavailable")?;
                    if !shared_credential_matches(config,shared) {return Err("mcp_shared_credential_changed");}
                }
                let nonce = state.registry.issue(
                    action.binding,
                    action.transport_sha256,
                    &action.arguments,
                    &action.schema,
                    &action.arguments_canonical,
                    &action.schema_canonical,
                )?;
                Ok(json!({"state":"issued","nonce":nonce}))
            }
            _ => Err("mcp_invalid_operation"),
        }
    }
    // Fixed discovery categories never include provider text, tool data or credentials.
    fn discovery_error(stage:&str, code:&str) -> &'static str {
        match (stage,code) {
            ("credential", "mcp_oauth_reauthorization_required") => "mcp_disc_credential_reauth_uncertain",
            ("credential", "mcp_configuration_changed" | "mcp_owner_changed") => "mcp_disc_owner_changed_uncertain",
            ("credential", _) => "mcp_disc_credential_uncertain",
            ("initialize", "mcp_initialized_auth_failed") => "mcp_disc_initialized_auth_uncertain",
            ("initialize", "mcp_initialized_http_failed") => "mcp_disc_initialized_http_uncertain",
            ("initialize", "mcp_initialized_failed") => "mcp_disc_initialized_uncertain",
            ("initialize", "mcp_protocol_unsupported" | "mcp_invalid_initialize") => "mcp_disc_protocol_uncertain",
            ("initialize", "mcp_http_auth_failed") => "mcp_disc_init_auth_uncertain",
            ("initialize", "mcp_http_failed") => "mcp_disc_init_http_uncertain",
            ("initialize", "mcp_http_content_type" | "mcp_invalid_response" | "mcp_rpc_failed" | "mcp_session_invalid" | "mcp_session_changed") => "mcp_disc_init_response_uncertain",
            ("initialize", _) => "mcp_disc_initialize_uncertain",
            ("list", "schema_type") => "mcp_disc_schema_type_uncertain",
            ("list", "schema_size") => "mcp_disc_schema_size_uncertain",
            ("list", "schema_depth") => "mcp_disc_schema_depth_uncertain",
            ("list", "remote_schema_ref") => "mcp_disc_schema_ref_uncertain",
            ("list", "invalid_schema") => "mcp_disc_schema_uncertain",
            ("list", "mcp_frame_too_large" | "mcp_tools_too_large" | "mcp_page_limit") => "mcp_disc_list_limit_uncertain",
            ("list", "mcp_http_auth_failed") => "mcp_disc_list_auth_uncertain",
            ("list", "mcp_http_failed") => "mcp_disc_list_http_uncertain",
            ("list", "mcp_http_content_type" | "mcp_invalid_response" | "mcp_invalid_tools" | "mcp_rpc_failed") => "mcp_disc_list_response_uncertain",
            ("list", _) => "mcp_disc_list_uncertain",
            _ => "mcp_operation_uncertain",
        }
    }
    fn bounded_worker_reply(response:Value, request_id:u64, discovery:bool) -> Value {
        if serde_json::to_vec(&response).map_or(true,|data|data.len()>MAX_FRAME) {
            json!({"version":1,"request_id":request_id,"state":"uncertain","code":if discovery {"mcp_disc_frame_uncertain"} else {"mcp_operation_uncertain"}})
        } else {response}
    }
    fn worker<B: CredentialBackend, T: SyntheticAuthTransport>(
        state: Arc<State>,
        broker: Arc<Mutex<AuthBroker<B, T>>>,
        operation: &str,
        payload: Value,
    ) -> Result<Value, &'static str> {
        let (config, nonce, permit, action, discovery_review) = if operation == "start" {
            let action: Action = parse(payload)?;
            verify_review(&action)?;
            let nonce = action.nonce.clone().ok_or("mcp_grant_unavailable")?;
            if !owner_ok(
                &broker,
                action.binding.owner_id,
                &action.binding.owner_epoch,
                &action.binding.core_epoch,
            ) {
                return Err("mcp_owner_changed");
            }
            let entries = state
                .configurations
                .lock()
                .map_err(|_| "mcp_authority_unavailable")?;
            let config = entries
                .get(&(action.binding.owner_id, action.binding.server_id.clone()))
                .ok_or("mcp_configuration_unavailable")?
                .clone();
            if let Some(shared)=&action.binding.shared_recipient_binding {
                if !shared_credential_matches(&config,shared) {return Err("mcp_shared_credential_changed");}
            }
            let permit =
                state
                    .registry
                    .consume(&nonce, &action.binding, &action.transport_sha256)?;
            (config, nonce, permit, Some(action), None)
        } else {
            let binding: DiscoveryBinding = parse(payload)?;
            if !owner_ok(
                &broker,
                binding.owner_id,
                &binding.owner_epoch,
                &binding.core_epoch,
            ) {
                return Err("mcp_owner_changed");
            }
            let entries = state
                .configurations
                .lock()
                .map_err(|_| "mcp_authority_unavailable")?;
            let config = entries
                .get(&(binding.owner_id, binding.server_id.clone()))
                .ok_or("mcp_configuration_unavailable")?
                .clone();
            let nonce = state.registry.issue_discovery(binding.clone())?;
            let permit = state.registry.consume_discovery(&nonce, &binding)?;
            let review = binding.executable_review.clone();
            (config, nonce, permit, None, review)
        };
        // Stop an in-flight session if the authenticated Owner/Core epoch changes.
        let done = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let watch_done = done.clone();
        let watch_cancel = permit.cancelled.clone();
        let watch_broker = broker.clone();
        let watch_owner = config.owner_id;
        let watch_owner_epoch = config.owner_epoch.clone();
        let watch_core_epoch = config.core_epoch.clone();
        let watcher = std::thread::spawn(move || {
            while !watch_done.load(Ordering::Acquire) {
                if !owner_ok(
                    &watch_broker,
                    watch_owner,
                    &watch_owner_epoch,
                    &watch_core_epoch,
                ) {
                    watch_cancel.store(true, Ordering::Release);
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(25));
            }
        });
        let result = (|| -> Result<Value, &'static str> {
            let shared_action=action.as_ref().is_some_and(|value|value.binding.shared_recipient_binding.is_some());
            if shared_action && permit.cancelled.load(Ordering::Acquire) {return Err("mcp_operation_uncertain");}
            if let Transport::Stdio { executable, .. } = &config.transport {
                let proof = if let Some(action) = &action {
                    let targets = action
                        .review
                        .get("targets")
                        .and_then(Value::as_array)
                        .ok_or("mcp_review_changed")?;
                    let found: Vec<_> = targets
                        .iter()
                        .filter(|v| v.get("role").and_then(Value::as_str) == Some("executable"))
                        .collect();
                    if found.len() != 1 {
                        return Err("mcp_review_changed");
                    }
                    found[0]
                } else {
                    discovery_review.as_ref().ok_or("mcp_review_changed")?
                };
                super::executable_review::verify(executable, proof)?;
            }
            let secret = (|| -> Result<Option<zeroize::Zeroizing<String>>, &'static str> {
            Ok(if let Some(reference) = &config.credential_ref {
                if reference.starts_with("mcp-oauth:") {
                    if !matches!(&config.transport, Transport::Http{url} if url==crate::notion_oauth::ENDPOINT) {return Err("mcp_oauth_invalid");}
                    let _refresh=state.oauth.refresh_lock.lock().map_err(|_| "mcp_oauth_unavailable")?;
                    let original_guard=oauth_guard(state.clone(),broker.clone(),config.clone());
                    let guard: Arc<dyn Fn()->bool + Send + Sync> = if shared_action {
                        let cancel=permit.cancelled.clone();
                        Arc::new(move || !cancel.load(Ordering::Acquire) && original_guard())
                    } else { original_guard };
                    let store=oauth_store(state.clone(),broker.clone(),config.clone(),reference.clone());
                    if !(guard)() {return Err("mcp_configuration_changed");}
                    let bytes=broker.lock().map_err(|_| "mcp_owner_unavailable")?.mcp_secret_get(config.owner_id as u64,&config.owner_epoch,&config.server_id,reference)?.ok_or("mcp_secret_unavailable")?;
                    let bundle=serde_json::from_slice::<crate::notion_oauth::Bundle>(&bytes).map_err(|_| "mcp_oauth_invalid")?;
                    Some(crate::notion_oauth::access(bundle,config.owner_id,&config.server_id,&guard,&store)?)
                } else {
                    let bytes=broker.lock().map_err(|_| "mcp_owner_unavailable")?.mcp_secret_get(config.owner_id as u64,&config.owner_epoch,&config.server_id,reference)?.ok_or("mcp_secret_unavailable")?;
                    let value=String::from_utf8(bytes.to_vec()).map_err(|_| "mcp_secret_invalid")?;
                    Some(zeroize::Zeroizing::new(value))
                }
            } else { None })
            })().map_err(|code| if operation=="discover" {discovery_error("credential",code)} else {code})?;
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|_| "mcp_runtime_unavailable")?;
            runtime.block_on(async {
                let mut session =
                    Session::open(config.transport, secret, permit.cancelled.clone()).await.map_err(|code| if operation=="discover" {discovery_error("initialize",code)} else {"mcp_operation_uncertain"})?;
                if let Some(action) = action {
                    session
                        .invoke(
                            &action.binding,
                            &action.schema,
                            &action.arguments,
                            &action.schema_canonical,
                            &action.arguments_canonical,
                        )
                        .await.map_err(|_| "mcp_operation_uncertain")
                } else {
                    let tools = session.tools().await.map_err(|code| discovery_error("list",code))?;
                    let mut public=Vec::new();
                    for tool in tools {
                        let schema=tool.get("inputSchema").ok_or("mcp_invalid_tools")?.clone();
                        let canonical=serde_json::to_string(&schema).map_err(|_| "mcp_invalid_tools")?;
                        let digest=format!("{:x}",sha2::Sha256::digest(canonical.as_bytes()));
                        let description=match tool.get("description") {None|Some(Value::Null)=>"",Some(Value::String(s)) if s.len()<=4000=>s.as_str(),_=>return Err("mcp_invalid_tools")};
                        public.push(json!({"name":tool.get("name"),"description":description,"input_schema":schema,"schema_canonical":canonical,"schema_sha256":digest}));
                    }
                    Ok(json!({"tools":public}))
                }
            })
        })();
        done.store(true, Ordering::Release);
        let _ = watcher.join();
        let cancelled = permit.cancelled.load(Ordering::Acquire);
        let _ = state.registry.finish(&nonce);
        if cancelled {
            Err("mcp_operation_uncertain")
        } else {
            result
        }
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        use crate::auth_broker::UnavailableAuthTransport;
        use crate::credential::{CredentialBackendError, MCP_SERVICE};
        use sha2::{Digest, Sha256};
        use std::collections::HashMap;
        #[derive(Default)]
        struct MockSecrets(HashMap<(String, String), Vec<u8>>);
        impl CredentialBackend for MockSecrets {
            fn get(&mut self, s: &str, a: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
                Ok(self.0.get(&(s.into(), a.into())).cloned())
            }
            fn put(&mut self, s: &str, a: &str, v: &[u8]) -> Result<(), CredentialBackendError> {
                self.0.insert((s.into(), a.into()), v.to_vec());
                Ok(())
            }
            fn delete(&mut self, s: &str, a: &str) -> Result<(), CredentialBackendError> {
                self.0.remove(&(s.into(), a.into()));
                Ok(())
            }
        }
        fn receive(socket: &mut UnixStream) -> Value {
            let mut len = [0; 4];
            socket.read_exact(&mut len).unwrap();
            let mut raw = vec![0; u32::from_be_bytes(len) as usize];
            socket.read_exact(&mut raw).unwrap();
            serde_json::from_slice(&raw).unwrap()
        }
        fn call(socket: &mut UnixStream, id: u64, operation: &str, payload: Value) -> Value {
            frame(
                socket,
                &json!({"version":1,"request_id":id,"operation":operation,"payload":payload}),
            )
            .unwrap();
            let result = receive(socket);
            assert_eq!(result["request_id"], id);
            result
        }
        fn digest(v: &str) -> String {
            format!("{:x}", Sha256::digest(v.as_bytes()))
        }
        #[cfg(all(target_os="macos",target_arch="aarch64"))]
        #[test]
        fn custody_park_and_scope_revoke_channel_are_linearized() {
            use std::sync::{atomic::Ordering,mpsc};
            use std::time::Duration;
            let root="11111111-1111-4111-8111-111111111111";
            let old="22222222-2222-4222-8222-222222222222";
            let next="44444444-4444-4444-8444-444444444444";
            let make=|scope:&str| -> super::super::ExecutionBinding {serde_json::from_value(json!({
                "version":1,"root_scope_id":root,"scope_id":scope,"owner_id":7,"root_room_id":1,
                "root_request_event_id":"33333333-3333-4333-8333-333333333333","execution_room_id":1,
                "agent_id":8,"run_generation":1,"job_id":null,"phase":null,"context_mode":"user_room",
                "deadline_at":"2099-01-01T00:00:00Z","lineage":[{"scope_id":root,"epoch":0},{"scope_id":scope,"epoch":0}]
            })).unwrap()};
            let mut auth=AuthBroker::new(MockSecrets::default(),UnavailableAuthTransport);
            let generation=auth.attach_backend(&mut Vec::new()).unwrap();
            let epoch=auth.bind_owner(7).unwrap().current_owner_epoch.unwrap();
            let core=generation.fingerprint().to_owned();
            let old_proof=Some(make(old));let next_proof=Some(make(next));
            let old_cancel=auth.test_custody_scope("handoff",old_proof.clone());
            let broker=Arc::new(Mutex::new(auth));let (state,_)=new_owner(broker.clone());
            let mut held=broker.lock().unwrap();
            let (entered_tx,entered_rx)=mpsc::channel();let (done_tx,done_rx)=mpsc::channel();
            let thread_state=state.clone();let thread_broker=broker.clone();let epoch2=epoch.clone();let core2=core.clone();
            let worker=std::thread::spawn(move || {
                entered_tx.send(()).unwrap();
                let result=command(&thread_state,&thread_broker,"scope_revoke",json!({"version":1,"owner_id":7,
                    "owner_epoch":epoch2,"core_epoch":core2,"scopes":[{"scope_id":old,"epoch":1}]}));
                done_tx.send(result).unwrap();
            });
            entered_rx.recv().unwrap();assert!(done_rx.recv_timeout(Duration::from_millis(30)).is_err());
            assert!(held.test_custody_park_commit(7,"handoff",&old_proof));
            drop(held);assert_eq!(done_rx.recv_timeout(Duration::from_secs(2)).unwrap().unwrap()["state"],"revoked");
            worker.join().unwrap();assert!(!old_cancel.load(Ordering::SeqCst));
            let next_cancel=broker.lock().unwrap().test_custody_scope("handoff",next_proof.clone());
            assert_eq!(command(&state,&broker,"scope_revoke",json!({"version":1,"owner_id":7,"owner_epoch":epoch,
                "core_epoch":core,"scopes":[{"scope_id":next,"epoch":1}]})).unwrap()["state"],"revoked");
            assert!(next_cancel.load(Ordering::SeqCst));
            assert!(!broker.lock().unwrap().test_custody_park_commit(7,"handoff",&next_proof));
        }
        #[test]
        fn discovery_schema_reasons_preserve_bounds_and_local_refs() {
            assert_eq!(super::super::validate_schema_detail(&json!([])),Err("schema_type"));
            let larger=json!({"type":"object","description":"x".repeat(20_000)});
            assert!(super::super::validate_schema_detail(&larger).is_ok());
            let canonical=serde_json::to_string(&larger).unwrap();
            assert!(super::super::canonical_digest(&canonical,&larger,super::super::MAX_SCHEMA).is_ok());
            let empty=json!({"type":"object","description":""});
            let overhead=serde_json::to_vec(&empty).unwrap().len();
            let at_limit=json!({"type":"object","description":"x".repeat(super::super::MAX_SCHEMA-overhead)});
            assert_eq!(serde_json::to_vec(&at_limit).unwrap().len(),super::super::MAX_SCHEMA);
            assert!(super::super::validate_schema_detail(&at_limit).is_ok());
            assert_eq!(super::super::validate_schema_detail(&json!({"type":"object","description":"x".repeat(super::super::MAX_SCHEMA)})),Err("schema_size"));
            let mut deep=json!(true); for _ in 0..65 {deep=json!({"x":deep});}
            assert_eq!(super::super::validate_schema_detail(&deep),Err("schema_depth"));
            assert_eq!(super::super::validate_schema_detail(&json!({"$ref":"https://example.invalid"})),Err("remote_schema_ref"));
            assert_eq!(super::super::validate_schema_detail(&json!({"$ref":{"x":1}})),Err("remote_schema_ref"));
            assert!(super::super::validate_schema_detail(&json!({"type":"object","$defs":{"x":{"type":"string"}},"properties":{"v":{"$ref":"#/$defs/x"}}})).is_ok());
            for (code,expected) in [("schema_type","mcp_disc_schema_type_uncertain"),("schema_size","mcp_disc_schema_size_uncertain"),("schema_depth","mcp_disc_schema_depth_uncertain"),("remote_schema_ref","mcp_disc_schema_ref_uncertain")] {assert_eq!(discovery_error("list",code),expected);}
        }

        #[test]
        fn oversized_discovery_reply_becomes_small_uncertain_without_closing_channel() {
            let oversized=json!({"version":1,"request_id":4,"state":"completed","result":{"tools":"x".repeat(MAX_FRAME)}});
            let safe=bounded_worker_reply(oversized,4,true);
            assert_eq!(safe["state"],"uncertain"); assert_eq!(safe["code"],"mcp_disc_frame_uncertain");
            assert!(serde_json::to_vec(&safe).unwrap().len()<1024);
            assert_eq!(discovery_error("initialize","mcp_initialized_http_failed"),"mcp_disc_initialized_http_uncertain");
            assert_eq!(discovery_error("list","remote_schema_ref"),"mcp_disc_schema_ref_uncertain");
            assert_eq!(discovery_error("credential","secret material"),"mcp_disc_credential_uncertain");
        }

        #[test]
        fn configure_epoch_only_change_is_distinct_from_configuration_change() {
            let config = Config {
                owner_id: 7, owner_epoch: "a".repeat(64), core_epoch: "b".repeat(64),
                server_id: "server".into(), config_revision: 1,
                transport: Transport::Http { url: "http://127.0.0.1:1234/mcp".into() },
                transport_canonical: "canonical".into(), transport_sha256: "c".repeat(64),
                credential_ref: None,
            };
            assert!(!config.only_owner_epoch_changed(&config));
            let mut next = config.clone(); next.owner_epoch = "d".repeat(64);
            assert!(config.only_owner_epoch_changed(&next));
            next.config_revision += 1;
            assert!(!config.only_owner_epoch_changed(&next));
            next = config.clone(); next.owner_epoch = "d".repeat(64); next.core_epoch = "e".repeat(64);
            assert!(!config.only_owner_epoch_changed(&next));
            next = config.clone(); next.owner_epoch = "d".repeat(64); next.credential_ref = Some("ref".into());
            assert!(!config.only_owner_epoch_changed(&next));
            next = config.clone(); next.owner_epoch = "d".repeat(64); next.transport_sha256 = "f".repeat(64);
            assert!(!config.only_owner_epoch_changed(&next));
        }

        #[test]
        fn inherited_channel_binds_selection_review_secret_and_revocation_without_external_effect()
        {
            let mut auth = AuthBroker::new(MockSecrets::default(), UnavailableAuthTransport);
            let generation = auth.attach_backend(&mut Vec::new()).unwrap();
            let epoch = auth.bind_owner(7).unwrap().current_owner_epoch.unwrap();
            let core = generation.fingerprint().to_owned();
            let broker = Arc::new(Mutex::new(auth));
            let (state, owner) = new_owner(broker.clone());
            let (mut client, server) = UnixStream::pair().unwrap();
            let thread = std::thread::spawn(move || serve(server, broker, generation, state));
            assert_eq!(receive(&mut client)["core_epoch"], core);
            let server_id = "12345678-1234-4123-8123-123456789abc";
            let transport = json!({"type":"http","url":"http://127.0.0.1/mcp"});
            let transport_canonical = serde_json::to_string(&transport).unwrap();
            let transport_hash = digest(&transport_canonical);
            let identity = json!({"owner_id":7,"owner_epoch":epoch,"core_epoch":core});
            let mut config = identity.clone();
            config.as_object_mut().unwrap().extend(json!({"version":1,"server_id":server_id,"config_revision":1,"transport":transport,"transport_canonical":transport_canonical,"transport_sha256":transport_hash,"credential_ref":null}).as_object().unwrap().clone());
            assert_eq!(
                call(&mut client, 1, "configure", config)["state"],
                "configured"
            );
            let op = "a".repeat(32);
            let stored = owner
                .store(
                    server_id,
                    1,
                    &op,
                    zeroize::Zeroizing::new(b"synthetic-secret".to_vec()),
                )
                .unwrap();
            assert!(stored.starts_with("mcp:7:"));
            assert_eq!(
                owner.status(server_id, 1, &op).unwrap(),
                Some(stored.clone())
            );
            assert!(owner
                .store(
                    server_id,
                    1,
                    &op,
                    zeroize::Zeroizing::new(b"different".to_vec())
                )
                .is_err());
            let mut status = identity.clone();
            status.as_object_mut().unwrap().extend(json!({"version":1,"server_id":server_id,"config_revision":1,"credential_ref":stored}).as_object().unwrap().clone());
            let status = call(&mut client, 2, "credential_status", status);
            assert_eq!(status["state"], "present");
            assert!(!status.to_string().contains("synthetic-secret"));
            let schema = json!({"type":"object"});
            let args = json!({});
            let sc = serde_json::to_string(&schema).unwrap();
            let ac = serde_json::to_string(&args).unwrap();
            let sh = digest(&sc);
            let ah = digest(&ac);
            let tid = "c".repeat(64);
            let mut bind = identity.clone();
            bind.as_object_mut().unwrap().extend(json!({"version":1,"server_id":server_id,"config_revision":1,"agent_id":8,"binding_revision":1,"enabled":true,"allowed_tools":[{"tool_id":tid,"tool_name":"read","schema_sha256":sh}]}).as_object().unwrap().clone());
            assert_eq!(call(&mut client, 3, "bind", bind)["state"], "bound");
            let proof = json!({"owner_id":7,"agent_id":8,"root_event_id":"root","generation":1,"server_id":server_id,"config_revision":1,"binding_revision":1,"tool_id":tid,"tool_name":"read","schema_sha256":sh,"input_sha256":ah,"transport_sha256":transport_hash,"transport_type":"http"});
            let action_canonical =
                serde_json::to_string(&json!({"binding":proof,"arguments":args})).unwrap();
            let action_hash = digest(&action_canonical);
            let review = json!({"action":{"family":"mcp_tool_call","action_id":"action","agent_id":8,"trigger_event_id":"root","payload_digest":action_hash,"mcp_binding":proof},"authority":{"owner_id":7,"agent_id":8,"os_user_status":"verified","euid":unsafe{libc::geteuid()},"proposer":{"owner_id":7,"owner_epoch":epoch}},"targets":[{"role":"mcp_server","status":"configured","server_id":server_id,"agent_id":8,"config_revision":1,"binding_revision":1,"tool_name":"read","schema_sha256":sh,"transport_sha256":transport_hash}]});
            let review_canonical = serde_json::to_string(&review).unwrap();
            let review_hash = digest(&review_canonical);
            let binding = json!({"owner_id":7,"owner_epoch":epoch,"core_epoch":core,"agent_id":8,"root_event_id":"root","generation":1,"server_id":server_id,"config_revision":1,"binding_revision":1,"tool_id":tid,"tool_name":"read","schema_sha256":sh,"input_sha256":ah,"action_id":"action","review_id":"review","grant_id":1,"payload_digest":action_hash,"review_sha256":review_hash});
            let mut request = json!({"version":1,"binding":binding,"transport_sha256":transport_hash,"schema":schema,"arguments":args,"schema_canonical":sc,"arguments_canonical":ac,"review":review,"review_canonical":review_canonical,"action_payload_canonical":action_canonical});
            let issued = call(&mut client, 4, "issue", request.clone());
            assert_eq!(issued["state"], "issued");
            let nonce = issued["nonce"].as_str().unwrap().to_owned();
            let mut scope_revoke = identity.clone();
            scope_revoke.as_object_mut().unwrap().extend(json!({"version":1,"scopes":[{
                "scope_id":"11111111-1111-4111-8111-111111111111","epoch":1
            }]}).as_object().unwrap().clone());
            assert_eq!(call(&mut client, 5, "scope_revoke", scope_revoke)["state"], "revoked");
            let mut revoke = identity;
            revoke.as_object_mut().unwrap().extend(
                json!({"version":1,"server_id":server_id})
                    .as_object()
                    .unwrap()
                    .clone(),
            );
            assert_eq!(call(&mut client, 6, "revoke", revoke)["state"], "revoked");
            request["nonce"] = json!(nonce);
            let rejected = call(&mut client, 7, "start", request);
            assert_eq!(rejected["state"], "failed");
            assert!(owner.status(server_id, 1, &op).is_err());
            drop(client);
            assert!(thread.join().unwrap().is_ok());
            let _ = MCP_SERVICE;
        }
    }
    pub(crate) fn serve<B: CredentialBackend, T: SyntheticAuthTransport>(
        mut stream: UnixStream,
        broker: Arc<Mutex<AuthBroker<B, T>>>,
        generation: BackendGeneration,
        state: Arc<State>,
    ) -> Result<(), ()> {
        frame(
            &mut stream,
            &json!({"version":1,"core_epoch":generation.fingerprint(),"max_frame_bytes":MAX_FRAME,"max_in_flight":4}),
        )?;
        let writer = Arc::new(Mutex::new(stream.try_clone().map_err(|_| ())?));
        let active = Arc::new(AtomicUsize::new(0));
        let mut last = 0u64;
        let status = (|| -> Result<(), ()> {
            while let Some(req) = read(&mut stream)? {
                if req.version != 1 || req.request_id <= last {
                    return Err(());
                }
                last = req.request_id;
                if matches!(req.operation.as_str(), "start" | "discover" | "speech_start" | "slack_ack" | "slack_reply") {
                    if active.fetch_add(1, Ordering::AcqRel) >= 4 {
                        active.fetch_sub(1, Ordering::AcqRel);
                        frame(
                            &mut *writer.lock().map_err(|_| ())?,
                            &json!({"version":1,"request_id":req.request_id,"state":"failed","code":"mcp_capacity"}),
                        )?;
                        continue;
                    }
                    let state = state.clone();
                    let broker = broker.clone();
                    let writer = writer.clone();
                    let active = active.clone();
                    std::thread::spawn(move || {
                        #[cfg(all(target_os="macos",target_arch="aarch64"))]
                        if matches!(req.operation.as_str(),"slack_ack"|"slack_reply") {
                            let result=state.slack.as_ref().ok_or("slack_unavailable").and_then(|m|m.command(broker.clone(),&req.operation,req.payload));
                            let mut response=result.unwrap_or_else(fail).as_object().cloned().unwrap_or_default();
                            response.insert("version".into(),json!(1));response.insert("request_id".into(),json!(req.request_id));
                            if let Ok(mut out)=writer.lock(){if frame(&mut out,&Value::Object(response)).is_err(){let _=out.shutdown(std::net::Shutdown::Both);}}
                            active.fetch_sub(1,Ordering::AcqRel);return;
                        }
                        let result = if req.operation == "speech_start" {
                            match parse::<crate::speech_memo::Request>(req.payload) {
                                Ok(request) => {
                                    let binding=request.binding.clone();
                                    state.speech.start(request, || speech_owner_ok(&broker,binding.owner_id,&binding.owner_epoch,&binding.core_epoch))
                                }
                                Err(code) => Err(code),
                            }
                        } else { worker(state, broker, &req.operation, req.payload) };
                        let response = match result {
                            Ok(result) => {
                                let is_tool_error = req.operation == "start"
                                    && result.get("isError") == Some(&Value::Bool(true));
                                if is_tool_error {
                                    json!({"version":1,"request_id":req.request_id,"state":"failed","code":"mcp_tool_error","result":result})
                                } else {
                                    json!({"version":1,"request_id":req.request_id,"state":"completed","result":result})
                                }
                            }
                            Err(code) => {
                                let status = if code.ends_with("uncertain") {
                                    "uncertain"
                                } else {
                                    "failed"
                                };
                                json!({"version":1,"request_id":req.request_id,"state":status,"code":code})
                            }
                        };
                        let response=bounded_worker_reply(response,req.request_id,req.operation=="discover");
                        if let Ok(mut out) = writer.lock() {
                            if frame(&mut out, &response).is_err() {
                                let _ = out.shutdown(std::net::Shutdown::Both);
                            }
                        }
                        active.fetch_sub(1, Ordering::AcqRel);
                    });
                } else {
                    let result =
                        command(&state, &broker, &req.operation, req.payload).unwrap_or_else(fail);
                    let mut reply = result.as_object().cloned().ok_or(())?;
                    reply.insert("version".into(), json!(1));
                    reply.insert("request_id".into(), json!(req.request_id));
                    frame(&mut *writer.lock().map_err(|_| ())?, &Value::Object(reply))?;
                }
            }
            Ok(())
        })();
        if let Ok(entries) = state.configurations.lock() {
            for ((owner, server), _) in entries.iter() {
                let _ = state.registry.revoke(*owner, server);
            }
        }
        state.speech.shutdown();
        #[cfg(all(target_os="macos",target_arch="aarch64"))]
        if let Some(slack)=state.slack.as_ref(){slack.shutdown();}
        status
    }
}

#[cfg(unix)]
mod executable_review {
    use serde::Deserialize;
    use std::ffi::CString;
    use std::fs::{File, Metadata};
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::fs::MetadataExt;
    use std::path::{Component, Path};

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Parent {
        component: String,
        dev: u64,
        ino: u64,
        mode: u64,
        uid: u64,
        gid: u64,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Leaf {
        dev: u64,
        ino: u64,
        mode: u64,
        uid: u64,
        gid: u64,
        size: u64,
        mtime_ns: i64,
        ctime_ns: i64,
    }
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Target {
        role: String,
        path: String,
        status: String,
        parents: Vec<Parent>,
        leaf: Leaf,
    }
    fn parent_matches(m: &Metadata, p: &Parent) -> bool {
        (
            m.dev(),
            m.ino(),
            u64::from(m.mode()),
            u64::from(m.uid()),
            u64::from(m.gid()),
        ) == (p.dev, p.ino, p.mode, p.uid, p.gid)
    }
    fn stamp(seconds: i64, nanos: i64) -> Option<i64> {
        seconds.checked_mul(1_000_000_000)?.checked_add(nanos)
    }
    /// Re-observe each parent and leaf through no-follow descriptors immediately
    /// before launch. This is not a content CAS or a guarantee after exec begins.
    pub(super) fn verify(path: &str, value: &serde_json::Value) -> Result<(), &'static str> {
        let target: Target =
            serde_json::from_value(value.clone()).map_err(|_| "mcp_review_changed")?;
        if target.role != "executable"
            || target.status != "verified"
            || target.path != path
            || path.contains("//")
            || !Path::new(path).is_absolute()
        {
            return Err("mcp_review_changed");
        }
        let mut parts = Vec::new();
        for component in Path::new(path).components() {
            match component {
                Component::RootDir => {}
                Component::Normal(v) => parts.push(v),
                _ => return Err("mcp_review_changed"),
            }
        }
        if parts.is_empty() || parts.len() > 128 || target.parents.len() != parts.len() {
            return Err("mcp_review_changed");
        }
        let mut dir = File::open("/").map_err(|_| "mcp_review_changed")?;
        if target.parents[0].component != "/"
            || !parent_matches(
                &dir.metadata().map_err(|_| "mcp_review_changed")?,
                &target.parents[0],
            )
        {
            return Err("mcp_review_changed");
        }
        for (index, part) in parts.iter().enumerate().take(parts.len() - 1) {
            let name = part.to_str().ok_or("mcp_review_changed")?;
            let expected = &target.parents[index + 1];
            if expected.component != name {
                return Err("mcp_review_changed");
            }
            let name = CString::new(name).map_err(|_| "mcp_review_changed")?;
            let fd = unsafe {
                libc::openat(
                    dir.as_raw_fd(),
                    name.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err("mcp_review_changed");
            }
            let next = unsafe { File::from_raw_fd(fd) };
            if !parent_matches(
                &next.metadata().map_err(|_| "mcp_review_changed")?,
                expected,
            ) {
                return Err("mcp_review_changed");
            }
            dir = next;
        }
        let name = CString::new(
            parts
                .last()
                .and_then(|x| x.to_str())
                .ok_or("mcp_review_changed")?,
        )
        .map_err(|_| "mcp_review_changed")?;
        let fd = unsafe {
            libc::openat(
                dir.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err("mcp_review_changed");
        }
        let leaf = unsafe { File::from_raw_fd(fd) };
        let m = leaf.metadata().map_err(|_| "mcp_review_changed")?;
        let e = target.leaf;
        if !m.is_file()
            || (
                m.dev(),
                m.ino(),
                u64::from(m.mode()),
                u64::from(m.uid()),
                u64::from(m.gid()),
                m.size(),
            ) != (e.dev, e.ino, e.mode, e.uid, e.gid, e.size)
            || stamp(m.mtime(), m.mtime_nsec()) != Some(e.mtime_ns)
            || stamp(m.ctime(), m.ctime_nsec()) != Some(e.ctime_ns)
        {
            return Err("mcp_review_changed");
        }
        Ok(())
    }
}
