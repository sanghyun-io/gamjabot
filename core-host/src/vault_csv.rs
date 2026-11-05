//! Owner-selected, in-memory CSV login parsing; not an import or storage API.
//! The caller must retain secrets in native/Core memory, obtain Owner review,
//! enforce current realm/epoch, and perform independently reconciled writes.
//!
//! Header references: Chrome https://support.google.com/chrome/answer/13068232
//! Firefox https://raw.githubusercontent.com/mozilla/gecko-dev/master/toolkit/components/passwordmgr/LoginExport.sys.mjs
//! Bitwarden https://bitwarden.com/help/condition-bitwarden-import/
//! 1Password https://support.1password.com/export/
//! Apple https://support.apple.com/guide/passwords/mchl35b12625/mac
//! Edge https://support.microsoft.com/en-us/edge/export-passwords-in-microsoft-edge
//! LastPass compatible reader https://github.com/bitwarden/clients/blob/main/libs/importer/src/importers/lastpass/lastpass-csv-importer.ts
//! Vendor documentation does not guarantee every historical export header.

use std::collections::HashSet;
use zeroize::Zeroizing;

pub(crate) const MAX_FILE_BYTES: usize = 5_000_000; // exclusive
pub(crate) const MAX_ROWS: usize = 5_000; // data records; over limit rejects entire file
const MAX_COLUMNS: usize = 64;
const MAX_FIELD_BYTES: usize = 65_536;

// Deliberately no Debug, Clone, or Serialize for structures holding credentials.
pub(crate) struct LoginRow {
    pub row: usize,
    pub website: String,
    pub username: String,
    pub nickname: String,
    pub hint: String,
    pub password: Zeroizing<String>,
}
pub(crate) struct SafeRowError { pub row: usize, pub reason: &'static str }
pub(crate) struct ParsedLogins { pub rows: Vec<LoginRow>, pub errors: Vec<SafeRowError> }
pub(crate) struct ParseError { pub row: usize, pub reason: &'static str }

type Field = Zeroizing<Vec<u8>>;
type Record = Vec<Field>;
fn error(row: usize, reason: &'static str) -> ParseError { ParseError { row, reason } }

// RFC 4180 quoted fields, doubled quotes, CRLF/LF and quoted embedded newlines.
// Any malformed quoting or resource limit aborts the whole file.
fn records(bytes: &[u8]) -> Result<Vec<Record>, ParseError> {
    let mut all = Vec::new();
    let mut row = Vec::new();
    let mut field = Zeroizing::new(Vec::new());
    let (mut i, mut quoted, mut closed, mut at_start) = (0, false, false, true);
    while i < bytes.len() {
        let b = bytes[i];
        if quoted {
            if b == b'"' {
                if bytes.get(i + 1) == Some(&b'"') {
                    field.push(b'"');
                    if field.len() > MAX_FIELD_BYTES { return Err(error(all.len()+1, "field_too_large")); }
                    i += 2; continue;
                }
                quoted = false; closed = true; i += 1; continue;
            }
            field.push(b); i += 1;
        } else if b == b'"' && at_start {
            quoted = true; at_start = false; i += 1;
        } else if b == b',' || b == b'\r' || b == b'\n' {
            row.push(std::mem::replace(&mut field, Zeroizing::new(Vec::new())));
            if row.len() > MAX_COLUMNS { return Err(error(all.len()+1, "too_many_columns")); }
            at_start = true; closed = false;
            if b == b'\r' && bytes.get(i+1) != Some(&b'\n') { return Err(error(all.len()+1, "malformed_csv")); }
            if b != b',' {
                all.push(std::mem::take(&mut row));
                if all.len() > MAX_ROWS + 1 { return Err(error(all.len(), "too_many_rows")); }
                if b == b'\r' { i += 1; }
            }
            i += 1;
        } else {
            if closed || b == b'"' { return Err(error(all.len()+1, "malformed_csv")); }
            field.push(b); at_start = false; i += 1;
        }
        if field.len() > MAX_FIELD_BYTES { return Err(error(all.len()+1, "field_too_large")); }
    }
    if quoted { return Err(error(all.len()+1, "malformed_csv")); }
    if !row.is_empty() || !field.is_empty() || closed {
        row.push(field);
        if row.len() > MAX_COLUMNS { return Err(error(all.len()+1, "too_many_columns")); }
        all.push(row);
    }
    if all.len() > MAX_ROWS + 1 { return Err(error(all.len(), "too_many_rows")); }
    Ok(all)
}
fn str_field(v: &Field) -> &str { std::str::from_utf8(v).unwrap_or("") }
fn index(header: &Record, name: &str) -> Option<usize> {
    header.iter().position(|h| str_field(h).eq_ignore_ascii_case(name))
}
fn one_of(header: &Record, names: &[&str]) -> Option<usize> {
    let matches: Vec<_> = names.iter().filter_map(|n| index(header,n)).collect();
    if matches.len() == 1 { Some(matches[0]) } else { None }
}
fn get(row: &Record, col: Option<usize>) -> &str { col.and_then(|i|row.get(i)).map(str_field).unwrap_or("") }
fn valid_metadata(value: &str, max: usize) -> bool {
    value.len() <= max && !value.chars().any(char::is_control)
}

/// `input` is consumed and zeroized on success and every error path.
/// Every returned password remains zeroizing and is never serialized here.
pub(crate) fn parse_owned(input: Zeroizing<Vec<u8>>) -> Result<ParsedLogins, ParseError> {
    if input.len() >= MAX_FILE_BYTES { return Err(error(0,"file_too_large")); }
    if std::str::from_utf8(&input).is_err() { return Err(error(0,"invalid_utf8")); }
    let bytes = input.strip_prefix(&[0xef,0xbb,0xbf]).unwrap_or(&input);
    let mut parsed = records(bytes)?;
    if parsed.is_empty() { return Err(error(0,"empty_file")); }
    let header = parsed.remove(0);
    if header.is_empty() || header.iter().any(|v| v.is_empty() || v.len() > 128 || !str_field(v).is_ascii()) {
        return Err(error(1,"unsupported_header"));
    }
    const ALLOWED: &[&str] = &["url","website","username","password","name","title",
        "note","notes","otp","otpauth","favorite","archived","tags","type",
        "folder","collections","fields","reprompt","login_uri","login_username",
        "login_password","login_totp","extra","grouping","fav","totp",
        "httprealm","formactionorigin","guid","timecreated","timelastused","timepasswordchanged"];
    if header.iter().any(|h| !ALLOWED.iter().any(|v| str_field(h).eq_ignore_ascii_case(v))) {
        return Err(error(1,"unsupported_header"));
    }
    for (i, a) in header.iter().enumerate() {
        if header[..i].iter().any(|b| str_field(a).eq_ignore_ascii_case(str_field(b))) { return Err(error(1,"unsupported_header")); }
    }
    let bitwarden = index(&header,"login_uri").is_some();
    let (url,username,password) = if bitwarden {
        (index(&header,"login_uri"),index(&header,"login_username"),index(&header,"login_password"))
    } else {
        (one_of(&header,&["url","website"]),index(&header,"username"),index(&header,"password"))
    };
    if url.is_none() || username.is_none() || password.is_none() { return Err(error(1,"unsupported_header")); }
    let name=one_of(&header,&["name","title"]);
    if index(&header,"name").is_some() && index(&header,"title").is_some() { return Err(error(1,"unsupported_header")); }
    let kind=index(&header,"type");
    if bitwarden && kind.is_none() { return Err(error(1,"unsupported_header")); }
    let archived=index(&header,"archived");
    let mut out=ParsedLogins {rows:Vec::new(),errors:Vec::new()};
    let mut seen=HashSet::new();
    for (i,row) in parsed.into_iter().enumerate() {
        let number=i+2;
        let reject = |out: &mut ParsedLogins, reason| out.errors.push(SafeRowError{row:number,reason});
        if row.len()!=header.len() { reject(&mut out,"column_count"); continue; }
        if let Some(kind)=kind {
            let t=get(&row,Some(kind));
            if !t.is_empty() && !t.eq_ignore_ascii_case("login") && !t.eq_ignore_ascii_case("password") {
                reject(&mut out,"unsupported_item_type"); continue;
            }
        }
        if let Some(archived)=archived {
            let a=get(&row,Some(archived));
            if a.eq_ignore_ascii_case("true") || a=="1" { reject(&mut out,"archived"); continue; }
            if !(a.is_empty() || a.eq_ignore_ascii_case("false") || a=="0") { reject(&mut out,"unsupported_item_type"); continue; }
        }
        let raw_url=get(&row,url);
        // LastPass secure notes and form-fill records are not logins.
        if raw_url.eq_ignore_ascii_case("http://sn") { reject(&mut out,"unsupported_item_type"); continue; }
        let website=match crate::vault::login_origin(raw_url) {
            Ok(origin)=>origin, Err(_)=>{reject(&mut out,"invalid_website");continue;}
        };
        let user=get(&row,username);
        let nick=get(&row,name);
        let pass=get(&row,password);
        if user.is_empty() || !crate::vault::valid_username(user) {reject(&mut out,"invalid_username");continue;}
        if !valid_metadata(nick,200) {reject(&mut out,"invalid_nickname");continue;}
        if pass.is_empty() {reject(&mut out,"missing_password");continue;}
        // Match the exact JSON envelope limit used by VaultStore::replace.
        #[derive(serde::Serialize)]
        struct Envelope<'a> { version:u8, kind:&'static str, origin:&'a str, username:&'a str, password:&'a str }
        let mut envelope=Zeroizing::new(Vec::new());
        serde_json::to_writer(&mut *envelope, &Envelope {version:1,kind:"login",origin:&website,username:user,password:pass})
            .map_err(|_|error(number,"invalid_row"))?;
        if envelope.len()>8192 {reject(&mut out,"secret_too_large");continue;}
        if !seen.insert((website.clone(),user.to_owned())) {reject(&mut out,"duplicate");continue;}
        out.rows.push(LoginRow{row:number,website,username:user.to_owned(),nickname:nick.to_owned(),hint:String::new(),password:Zeroizing::new(pass.to_owned())});
    }
    Ok(out)
}
