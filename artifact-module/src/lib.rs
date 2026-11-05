//! Native screenshot artifact protocol and frozen-engine metadata verification.

mod engine;
mod protocol;
#[cfg(unix)]
mod storage;

pub use engine::{
    EngineAuth, EngineClient, VerifiedArtifact, VerifiedArtifactPage, MAX_EVENT_RESPONSE_BYTES,
};
pub use protocol::{
    read_frame, write_frame, ActionId, ArtifactError, ArtifactListItem, ArtifactListResponse,
    ArtifactListState, ArtifactOperation, ArtifactRequest, ArtifactResponse, RequestAuth, RequestId,
    ResponseHeader, ResponseState, LIST_MEDIA_TYPE, MAX_BODY_BYTES, MAX_HEADER_BYTES,
    MAX_LIST_BODY_BYTES, MAX_PIXELS, MAX_REQUEST_BYTES,
};
#[cfg(unix)]
pub use storage::{ArtifactState, ArtifactStore, DeleteOutcome, VerifiedPng};
