//! Owner-only fixed metadata failure stage; no request data or errors.
use serde_json::Value;
use std::{fs::{File,OpenOptions},io::{Read,Write},os::{fd::AsRawFd,unix::fs::{MetadataExt,OpenOptionsExt}},path::Path};
#[derive(Clone,Copy)]
pub(crate) enum MetadataFailure {SnapshotRejected,HelperStart,Install,Read,WorkerTimeout,WorkerSpawn,WorkerDisconnected,Inflight,StatusMissing,StatusDeleted,StatusMetadataMismatch,StatusUnavailable,StatusInvalid}
pub(crate) fn record_failure(directory:&Path,stage:MetadataFailure) {
    use std::os::fd::FromRawFd;
    let _=(|| -> std::io::Result<()> {
        if crate::platform::verify_directory(directory).is_err() || directory.canonicalize().ok().as_deref()!=Some(directory) {return Ok(());}
        let dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(directory)?;
        let dm=dir.metadata()?;
        if !dm.is_dir() || dm.uid()!=unsafe{libc::geteuid()} || dm.mode()&0o777!=0o700 {return Ok(());}
        let target=b"vault-metadata-last-failure.json\0";let temp=b".vault-metadata-last-failure.tmp\0";
        let d=dir.as_raw_fd();
        let existing=unsafe{libc::openat(d,target.as_ptr().cast(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
        let mut previous=None;
        if existing>=0 {
            let mut f=unsafe{File::from_raw_fd(existing)};let m=f.metadata()?;
            if !m.is_file() || m.uid()!=unsafe{libc::geteuid()} || m.mode()&0o777!=0o600 || m.nlink()!=1 || m.len()>1024 {return Ok(());}
            let mut data=Vec::new();std::io::Read::by_ref(&mut f).take(1025).read_to_end(&mut data)?;
            let parsed:Value=match serde_json::from_slice(&data){Ok(v)=>v,Err(_)=>return Ok(())};
            let valid=parsed.as_object().is_some_and(|o|o.len()==2 && o.get("version").and_then(Value::as_u64)==Some(1) && o.get("stage").and_then(Value::as_str).is_some_and(|v|matches!(v,"snapshot_rejected"|"helper_start"|"install"|"read"|"worker_timeout"|"worker_spawn"|"worker_disconnected"|"inflight"|"status_missing"|"status_deleted"|"status_metadata_mismatch"|"status_unavailable"|"status_invalid")));
            if !valid {return Ok(());}
            previous=Some((m.dev(),m.ino()));
        } else if std::io::Error::last_os_error().kind()!=std::io::ErrorKind::NotFound {return Ok(());}
        let fd=unsafe{libc::openat(d,temp.as_ptr().cast(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_NOFOLLOW|libc::O_CLOEXEC,0o600)};
        if fd<0 {return Ok(());}let mut f=unsafe{File::from_raw_fd(fd)};let initial=f.metadata()?;
        let code=match stage {MetadataFailure::SnapshotRejected=>"snapshot_rejected",MetadataFailure::HelperStart=>"helper_start",MetadataFailure::Install=>"install",MetadataFailure::Read=>"read",MetadataFailure::WorkerTimeout=>"worker_timeout",MetadataFailure::WorkerSpawn=>"worker_spawn",MetadataFailure::WorkerDisconnected=>"worker_disconnected",MetadataFailure::Inflight=>"inflight",MetadataFailure::StatusMissing=>"status_missing",MetadataFailure::StatusDeleted=>"status_deleted",MetadataFailure::StatusMetadataMismatch=>"status_metadata_mismatch",MetadataFailure::StatusUnavailable=>"status_unavailable",MetadataFailure::StatusInvalid=>"status_invalid"};
        let result=(|| -> std::io::Result<()> {
            write!(f,"{{\"version\":1,\"stage\":\"{}\"}}",code)?;f.sync_all()?;
            let current=std::fs::symlink_metadata(directory)?;
            if !current.is_dir() || current.dev()!=dm.dev() || current.ino()!=dm.ino() || current.uid()!=dm.uid() || current.mode()&0o777!=0o700 {return Err(std::io::ErrorKind::Other.into());}
            let mut st=std::mem::MaybeUninit::<libc::stat>::uninit();
            let exists=unsafe{libc::fstatat(d,target.as_ptr().cast(),st.as_mut_ptr(),libc::AT_SYMLINK_NOFOLLOW)};
            if exists==0 {let st=unsafe{st.assume_init()};if previous!=Some((st.st_dev as u64,st.st_ino)) || st.st_mode & libc::S_IFMT!=libc::S_IFREG || st.st_uid!=unsafe{libc::geteuid()} || st.st_mode&0o777!=0o600 || st.st_nlink!=1 || st.st_size>1024 {return Err(std::io::ErrorKind::Other.into());}}
            else if previous.is_some() || std::io::Error::last_os_error().kind()!=std::io::ErrorKind::NotFound {return Err(std::io::ErrorKind::Other.into());}
            if unsafe{libc::renameat(d,temp.as_ptr().cast(),d,target.as_ptr().cast())}!=0{return Err(std::io::Error::last_os_error());}Ok(())
        })();
        if result.is_err() {let mut st=std::mem::MaybeUninit::<libc::stat>::uninit();if unsafe{libc::fstatat(d,temp.as_ptr().cast(),st.as_mut_ptr(),libc::AT_SYMLINK_NOFOLLOW)}==0 {let st=unsafe{st.assume_init()};if st.st_dev as u64==initial.dev() && st.st_ino==initial.ino() {unsafe{libc::unlinkat(d,temp.as_ptr().cast(),0);}}}}
        result
    })();
}


// Diagnostic classification only: never alters the returned status.
pub(crate) fn classify(version:u8,status:&str,revision:Option<u64>,command:Option<&str>,card_last4:Option<Option<&str>>)->Option<MetadataFailure> {
    use MetadataFailure as F;
    let empty=revision.is_none() && command.is_none();
    let populated=revision.is_some_and(|n|n>0) && command.is_some_and(|s|s.len()==32 && s.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c)));
    let no_last4=card_last4.is_none_or(|v|v.is_none());
    let valid_last4=card_last4.is_none_or(|v|v.is_some_and(|s|s.len()==4 && s.bytes().all(|b|b.is_ascii_digit())));
    if version!=1 {return Some(F::StatusInvalid);}
    match status {
        "present" if populated && valid_last4=>None,
        "missing" if empty && no_last4=>Some(F::StatusMissing),
        "deleted" if populated && no_last4=>Some(F::StatusDeleted),
        "metadata_mismatch" if populated && no_last4=>Some(F::StatusMetadataMismatch),
        "unavailable" if empty && no_last4=>Some(F::StatusUnavailable),
        _=>Some(F::StatusInvalid),
    }
}
