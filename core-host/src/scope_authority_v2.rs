//! Experimental isolated state machine. NOT connected to Core or an authorization boundary.
//! A caller must authenticate owner/backend generation, serialize DB admission with Stop,
//! validate the existing approval and proof, supply unforgeable IDs, and own a monotonic clock.
//! Activation requires one shared mutex across issue/revoke/commit for every consumer.
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Scope { pub id: u64, pub epoch: u64 }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum End { Settled, Unknown }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error { Closed, Full, Invalid, Revoked, Expired, Missing, Active }
#[derive(Clone, Debug)]
struct Lease { lineage: Vec<Scope>, expires: u64, cancelled: bool, ended: Option<End> }
#[derive(Default)]
struct Cohort { sealed: bool, revoked: BTreeMap<u64,u64>, leases: BTreeMap<u64,Lease> }
/// Capacity counts live lease and tombstone records. No expiry or LRU evicts them.
pub struct Authority { cohorts: BTreeMap<u64,Cohort>, capacity: usize, highest_generation: u64 }
impl Authority {
    pub fn new(capacity:usize)->Self { Self { cohorts:BTreeMap::new(),capacity,highest_generation:0 } }
    fn used(&self)->usize { self.cohorts.values().map(|c|c.revoked.len()+c.leases.len()).sum() }
    /// A cohort must be opened only after the external admission barrier binds it to a
    /// fresh authenticated backend generation and the current DB epoch snapshot.
    pub fn open(&mut self, generation:u64)->Result<(),Error> {
        if generation<=self.highest_generation {return Err(Error::Invalid)}
        self.highest_generation=generation;self.cohorts.insert(generation,Cohort::default());Ok(())
    }
    pub fn seal(&mut self,generation:u64)->Result<(),Error> {
        self.cohorts.get_mut(&generation).ok_or(Error::Missing)?.sealed=true;Ok(())
    }
    pub fn issue(&mut self,generation:u64,id:u64,lineage:Vec<Scope>,expires:u64,now:u64)->Result<(),Error> {
        let c=self.cohorts.get(&generation).ok_or(Error::Missing)?;
        let mut ids=BTreeSet::new();
        if c.sealed {return Err(Error::Closed)}
        if id==0 || lineage.is_empty() || lineage.len()>8 || expires<=now || c.leases.contains_key(&id)
            || lineage.iter().any(|s|s.id==0 || !ids.insert(s.id)) {return Err(Error::Invalid)}
        if lineage.iter().any(|s|c.revoked.get(&s.id).is_some_and(|epoch|s.epoch<=*epoch)) {return Err(Error::Revoked)}
        if self.used()>=self.capacity {return Err(Error::Full)}
        self.cohorts.get_mut(&generation).unwrap().leases.insert(id,Lease{lineage,expires,cancelled:false,ended:None});Ok(())
    }
    /// Capacity failure must not partially mutate state or be reported as a successful stop.
    /// Integration must stop admission/abort active work if this returns Full.
    pub fn revoke(&mut self,generation:u64,scopes:&[Scope])->Result<Vec<u64>,Error> {
        let c=self.cohorts.get(&generation).ok_or(Error::Missing)?;
        if scopes.is_empty() || scopes.iter().any(|s|s.id==0) {return Err(Error::Invalid)}
        let new=scopes.iter().map(|s|s.id).filter(|id|!c.revoked.contains_key(id)).collect::<BTreeSet<_>>().len();
        if self.used().saturating_add(new)>self.capacity {return Err(Error::Full)}
        let c=self.cohorts.get_mut(&generation).unwrap();
        for s in scopes {c.revoked.entry(s.id).and_modify(|v|*v=(*v).max(s.epoch)).or_insert(s.epoch);}
        let mut affected=Vec::new();
        for (id,lease) in &mut c.leases {
            if lease.lineage.iter().any(|p|scopes.iter().any(|s|s.id==p.id && p.epoch<=s.epoch)) {
                lease.cancelled=true; if lease.ended.is_none(){affected.push(*id)}
            }
        }
        Ok(affected)
    }
    pub fn allowed(&self,generation:u64,id:u64,now:u64)->Result<(),Error> {
        let lease=self.cohorts.get(&generation).and_then(|c|c.leases.get(&id)).ok_or(Error::Missing)?;
        if lease.cancelled {return Err(Error::Revoked)}
        if lease.ended.is_some(){return Err(Error::Closed)}
        if now>=lease.expires {return Err(Error::Expired)}
        Ok(())
    }
    /// Unknown means the worker/effect is not proven quiescent. It cannot be GC'd.
    pub fn end(&mut self,generation:u64,id:u64,end:End)->Result<(),Error> {
        let lease=self.cohorts.get_mut(&generation).and_then(|c|c.leases.get_mut(&id)).ok_or(Error::Missing)?;
        match lease.ended {None=>lease.ended=Some(end),Some(End::Unknown) if end==End::Settled=>lease.ended=Some(end),_=>return Err(Error::Closed)};Ok(())
    }
    /// Call only when the caller has authenticated a closed admission channel. Expiry
    /// alone is insufficient: every worker/effect must be positively settled.
    pub fn collect_sealed(&mut self,generation:u64)->Result<(),Error> {
        let c=self.cohorts.get(&generation).ok_or(Error::Missing)?;
        if !c.sealed{return Err(Error::Closed)}
        if c.leases.values().any(|l|l.ended!=Some(End::Settled)){return Err(Error::Active)}
        self.cohorts.remove(&generation);Ok(())
    }
}
#[cfg(test)] mod tests {
 use super::*;
 fn s(id:u64,epoch:u64)->Scope{Scope{id,epoch}}
 #[test] fn revoke_before_issue_and_idempotent_capacity(){let mut a=Authority::new(2);a.open(1).unwrap();a.revoke(1,&[s(1,1),s(1,1)]).unwrap();assert_eq!(a.issue(1,7,vec![s(1,0)],99,1),Err(Error::Revoked));a.issue(1,7,vec![s(2,0)],99,1).unwrap();assert_eq!(a.revoke(1,&[s(1,1)]),Ok(vec![]));assert_eq!(a.revoke(1,&[s(3,1)]),Err(Error::Full));assert_eq!(a.allowed(1,7,2),Ok(()));}
 #[test] fn stop_cancels_only_descendants_and_unknown_blocks_collection(){let mut a=Authority::new(8);a.open(1).unwrap();a.issue(1,1,vec![s(1,0),s(2,0)],10,0).unwrap();a.issue(1,2,vec![s(1,0),s(3,0)],10,0).unwrap();assert_eq!(a.revoke(1,&[s(2,1)]),Ok(vec![1]));assert_eq!(a.allowed(1,1,1),Err(Error::Revoked));assert_eq!(a.allowed(1,2,1),Ok(()));a.seal(1).unwrap();a.end(1,1,End::Unknown).unwrap();a.end(1,2,End::Settled).unwrap();assert_eq!(a.collect_sealed(1),Err(Error::Active));a.end(1,1,End::Settled).unwrap();assert_eq!(a.collect_sealed(1),Ok(()));assert_eq!(a.allowed(1,1,2),Err(Error::Missing));}
 #[test] fn expiry_does_not_prove_worker_stopped(){let mut a=Authority::new(4);a.open(1).unwrap();a.issue(1,1,vec![s(1,0)],3,0).unwrap();a.seal(1).unwrap();assert_eq!(a.allowed(1,1,3),Err(Error::Expired));assert_eq!(a.collect_sealed(1),Err(Error::Active));a.end(1,1,End::Settled).unwrap();a.collect_sealed(1).unwrap();}
 #[test] fn sealing_blocks_new_issue_but_preserves_existing(){let mut a=Authority::new(8);a.open(1).unwrap();a.issue(1,1,vec![s(1,0)],9,0).unwrap();a.seal(1).unwrap();a.open(2).unwrap();assert_eq!(a.issue(1,2,vec![s(1,0)],9,0),Err(Error::Closed));assert_eq!(a.allowed(1,1,1),Ok(()));assert_eq!(a.issue(2,2,vec![s(1,0)],9,0),Ok(()));}
 #[test] fn collected_generation_cannot_be_reopened(){let mut a=Authority::new(4);a.open(1).unwrap();a.seal(1).unwrap();a.collect_sealed(1).unwrap();assert_eq!(a.open(1),Err(Error::Invalid));a.open(2).unwrap();}

}
