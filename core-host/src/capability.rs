use crate::{platform, EndpointFile, Result, MAX_CONTROL_BYTES};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};

pub fn random_hex() -> Result<String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| "random_unavailable")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
pub fn read(app_data: &Path) -> Result<EndpointFile> {
    platform::verify_directory(app_data)?;
    let mut file = platform::open_private(&app_data.join("owner-cli.json"))?;
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take((MAX_CONTROL_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| "endpoint_unavailable")?;
    let text = std::str::from_utf8(&bytes).map_err(|_| "invalid_endpoint")?;
    EndpointFile::from_json(text).map_err(|_| "invalid_endpoint")
}
/// Atomic replace happens only after locks are acquired and control is listening.
pub struct PublishedEndpoint {
    path: PathBuf,
    capability: String,
}
impl PublishedEndpoint {
    pub fn publish(app_data: &Path, endpoint: &EndpointFile) -> Result<Self> {
        endpoint.validate().map_err(|_| "invalid_endpoint")?;
        platform::verify_directory(app_data)?;
        let path = app_data.join("owner-cli.json");
        if fs::symlink_metadata(&path).is_ok() {
            let _ = platform::open_private(&path)?;
        }
        let temporary = app_data.join(format!(".owner-cli-{}.tmp", random_hex()?));
        let write: Result<()> = (|| {
            let mut file = platform::create_private(&temporary)?;
            let bytes = serde_json::to_vec(endpoint).map_err(|_| "invalid_endpoint")?;
            file.write_all(&bytes)
                .and_then(|_| file.sync_all())
                .map_err(|_| "endpoint_unavailable")?;
            fs::rename(&temporary, &path).map_err(|_| "endpoint_unavailable")?;
            Ok(())
        })();
        if write.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        write?;
        Ok(Self {
            path,
            capability: endpoint.capability.clone(),
        })
    }
}
impl Drop for PublishedEndpoint {
    fn drop(&mut self) {
        if self
            .path
            .parent()
            .and_then(|p| read(p).ok())
            .is_some_and(|e| e.capability == self.capability)
        {
            let _ = fs::remove_file(&self.path);
        }
    }
}
