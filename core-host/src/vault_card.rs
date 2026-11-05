//! Card metadata and write-only secret envelope. No public secret accessor.
use serde::{Deserialize,Serialize};
use zeroize::Zeroizing;

#[derive(Clone,Deserialize,Serialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub struct CardBilling {pub address_line1:String,pub address_line2:String,pub city:String,pub region:String,pub postal_code:String,pub country:String}
#[derive(Clone,Deserialize,Serialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub struct CardMetadata {
    pub cardholder:String,
    pub expiry_month:u8,
    pub expiry_year:u16,
    pub billing:CardBilling,
    /// None is allowed for a newly reserved item before the first secret write.
    pub last4:Option<String>,
}
impl CardMetadata {
    pub(crate) fn valid(&self,allow_empty:bool)->bool {
        fn text(s:&str,max:usize)->bool {s.len()<=max && !s.chars().any(char::is_control)}
        !self.cardholder.trim().is_empty() && text(&self.cardholder,200) && [&self.billing.address_line1,&self.billing.address_line2,&self.billing.city,&self.billing.region,&self.billing.postal_code,&self.billing.country].iter().all(|s|text(s,512))
            && (1..=12).contains(&self.expiry_month) && (2000..=9999).contains(&self.expiry_year)
            && match self.last4.as_deref() {None=>allow_empty,Some(v)=>v.len()==4 && v.bytes().all(|b|b.is_ascii_digit())}
    }
    pub(crate) fn matches_expected(&self,expected:&Self)->bool {
        self.cardholder==expected.cardholder && self.expiry_month==expected.expiry_month && self.expiry_year==expected.expiry_year
            && self.billing==expected.billing && (expected.last4.is_none() || self.last4==expected.last4)
    }
}
// Secret-bearing structs deliberately implement no Debug, Clone or Serialize.
pub struct NativeCardSecret { pan:crate::OwnerSecret, cvv:crate::OwnerSecret }
impl NativeCardSecret {
    pub fn from_parts(pan:crate::OwnerSecret,cvv:crate::OwnerSecret)->Self {Self{pan,cvv}}
    pub(crate) fn pan(&self)->&str {self.pan.as_str()}
    pub(crate) fn cvv(&self)->&str {self.cvv.as_str()}
}
#[derive(Deserialize)] #[serde(deny_unknown_fields)]
pub(crate) struct CardInput { pub pan:Zeroizing<String>,pub cvv:Zeroizing<String> }
#[derive(Serialize)]
pub(crate) struct CardWrite<'a> { pub version:u8,pub kind:&'static str,pub metadata:&'a CardMetadata,pub pan:&'a str,pub cvv:&'a str }
#[derive(Deserialize)] #[serde(deny_unknown_fields)]
pub(crate) struct CardRead {pub version:u8,pub kind:String,pub metadata:CardMetadata,pub pan:Zeroizing<String>,pub cvv:Zeroizing<String>}
pub(crate) fn valid_pan(pan:&str)->bool {(12..=19).contains(&pan.len()) && pan.bytes().all(|b|b.is_ascii_digit())}
pub(crate) fn normalize_pan(pan:&str)->Option<Zeroizing<String>> {
    if !pan.bytes().all(|b|b.is_ascii_digit() || b==b' ' || b==b'-') {return None;}
    let mut normalized=Zeroizing::new(String::new());
    for b in pan.bytes() {if b.is_ascii_digit() {normalized.push(b as char);}}
    if valid_pan(&normalized) {Some(normalized)} else {None}
}
pub(crate) fn valid_cvv(cvv:&str)->bool {cvv.is_empty() || ((3..=4).contains(&cvv.len()) && cvv.bytes().all(|b|b.is_ascii_digit()))}
pub(crate) fn last4(pan:&str)->Option<&str> {if valid_pan(pan) {pan.get(pan.len()-4..)} else {None}}

#[derive(Serialize)]
pub(crate) struct CardStatus {pub version:u8,pub status:&'static str,pub revision:Option<u64>,pub command_id:Option<String>,pub last4:Option<String>}

#[derive(Serialize)]
pub(crate) struct CardMetadataStatus {pub version:u8,pub status:&'static str,pub revision:Option<u64>,pub command_id:Option<String>,pub last4:Option<String>}
impl CardMetadataStatus {pub(crate) fn unavailable()->Self {Self{version:1,status:"unavailable",revision:None,command_id:None,last4:None}}}
