//! Shared by the actual platform helper and the legacy Tauri desktop host.
//! Scope proofs confine an existing broker grant; they never issue permission.
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

pub const CAPABILITY: &str = "root_scope_revocation_v1";

fn canonical_id(value: &str) -> bool {
    value.len() == 36 && value.bytes().enumerate().all(|(i,b)| {
        if matches!(i,8|13|18|23) { b == b'-' } else { b.is_ascii_digit() || (b'a'..=b'f').contains(&b) }
    })
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScopeEpoch { pub scope_id: String, pub epoch: u64 }

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScopeProof {
    #[serde(default = "one")]
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
fn one() -> u8 { 1 }

impl ScopeProof {
    pub fn valid_for(&self, room: i64, agent: i64) -> bool {
        let mut seen=HashSet::new();
        self.version == 1 && self.owner_id > 0 && self.root_room_id > 0
            && self.execution_room_id == room && self.agent_id == agent
            && canonical_id(&self.root_scope_id) && canonical_id(&self.scope_id)
            && canonical_id(&self.root_request_event_id)
            && self.lineage.len() > 0 && self.lineage.len() <= 8
            && self.lineage.first().is_some_and(|s| s.scope_id == self.root_scope_id)
            && self.lineage.last().is_some_and(|s| s.scope_id == self.scope_id)
            && self.lineage.iter().all(|s| canonical_id(&s.scope_id) && seen.insert(&s.scope_id))
            && matches!(self.context_mode.as_str(), "delegated"|"user_room")
            && (self.job_id.is_some() == self.phase.is_some())
            && self.job_id.as_ref().map_or(true, |id| canonical_id(id))
            && self.phase.as_deref().map_or(true, |p| matches!(p,"response"|"return"))
            && !self.deadline_at.is_empty() && self.deadline_at.len() <= 64
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScopeControl {
    pub version: u8,
    pub request_id: String,
    pub deadline_ms: u64,
    pub operation: String,
    #[serde(default)]
    pub revocations: Vec<ScopeEpoch>,
}
impl ScopeControl {
    pub fn valid(&self, now: u64) -> bool {
        self.version == 1 && self.deadline_ms >= now && self.deadline_ms <= now+15_000
            && self.request_id.len() == 32 && self.request_id.bytes().all(|b| b.is_ascii_hexdigit())
            && (self.operation == "scope_capabilities" && self.revocations.is_empty()
                || self.operation == "scope_revoke" && !self.revocations.is_empty()
                   && self.revocations.len() <= 1024
                   && self.revocations.iter().all(|s| canonical_id(&s.scope_id)))
    }
}

#[derive(Default)]
pub struct ResourceScopes { revoked: HashMap<String,u64> }
impl ResourceScopes {
    pub fn allows(&self, proof: &ScopeProof) -> bool {
        proof.lineage.iter().all(|item| self.revoked.get(&item.scope_id).map_or(true,|epoch|item.epoch > *epoch))
    }
    pub fn revoke(&mut self, items: &[ScopeEpoch]) {
        for item in items {
            self.revoked.entry(item.scope_id.clone()).and_modify(|epoch|*epoch=(*epoch).max(item.epoch)).or_insert(item.epoch);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ancestor_tombstone_survives_without_a_lease_and_preserves_sibling() {
        let root="10000000-0000-4000-8000-000000000000";
        let b="20000000-0000-4000-8000-000000000000";
        let c="30000000-0000-4000-8000-000000000000";
        let d="40000000-0000-4000-8000-000000000000";
        let proof=|ids:&[&str]| ScopeProof { version:1,root_scope_id:root.into(),scope_id:ids.last().unwrap().to_string(),
            owner_id:1,root_room_id:1,root_request_event_id:root.into(),execution_room_id:2,agent_id:2,
            run_generation:1,job_id:Some(c.into()),phase:Some("response".into()),context_mode:"delegated".into(),
            deadline_at:"2030-01-01T00:00:00+00:00".into(),lineage:ids.iter().map(|id|ScopeEpoch{scope_id:(*id).into(),epoch:0}).collect() };
        let mut scopes=ResourceScopes::default();
        scopes.revoke(&[ScopeEpoch{scope_id:b.into(),epoch:1}]);
        assert!(!scopes.allows(&proof(&[root,b,c])));
        assert!(scopes.allows(&proof(&[root,d])));
        let mut scopes=ResourceScopes::default();
        scopes.revoke(&[ScopeEpoch{scope_id:c.into(),epoch:1}]);
        assert!(scopes.allows(&proof(&[root,b]))); // B return belongs to its parent scope.
    }
}
