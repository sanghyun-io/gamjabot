"""Only explicit reference selections cross the wire; authors/content are server derived."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceRef(ResultModel):
    event_id: StrictStr | None = Field(default=None, pattern=UUID_PATTERN)
    url: StrictStr | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def one_reference(self):
        if (self.event_id is None) == (self.url is None):
            raise ValueError("Choose exactly one evidence reference")
        return self


class ResultSave(ResultModel):
    source_event_id: StrictStr = Field(pattern=UUID_PATTERN)
    kind: Literal["text", "link", "file"]
    title: StrictStr = Field(min_length=1, max_length=200)
    url: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=16)


class ResultVersionSave(ResultSave):
    expected_current_version: StrictInt = Field(ge=1)


class ResultRevisionRequest(ResultModel):
    expected_current_version: StrictInt = Field(ge=1)
    instruction: StrictStr = Field(min_length=1, max_length=4000)


class ResultStateWrite(ResultModel):
    expected_state_revision: StrictInt = Field(ge=1)
    state: Literal["draft", "reviewing", "final"]


class ResultReviewWrite(ResultModel):
    review_event_id: StrictStr = Field(pattern=UUID_PATTERN)


class ResultLinkReportWrite(ResultModel):
    expected_state_revision: StrictInt = Field(ge=1)
    report: Literal["unchecked", "reported_bad"]


class PageRename(ResultModel):
    expected_current_version: StrictInt = Field(ge=1)
    title: StrictStr = Field(min_length=1, max_length=200)
