"""Closed public DTOs for cached and explicitly refreshed model catalogs."""

from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


MODEL_ID_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9._:-]*(?:/[A-Za-z0-9_][A-Za-z0-9._:-]*)*$"
MODEL_ID_MAX_LENGTH = 200


def valid_model_id(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= MODEL_ID_MAX_LENGTH
            and re.fullmatch(MODEL_ID_PATTERN, value) is not None)


class ModelRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    config_revision: int = Field(ge=1)
    auth_grant_revision: int = Field(ge=0)


class ModelItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())
    id: str = Field(min_length=1, max_length=MODEL_ID_MAX_LENGTH, pattern=MODEL_ID_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    source: Literal["configured", "discovery", "native_catalog"]


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    observed_at: datetime | None
    stale: bool
    truncated: bool
    models: tuple[ModelItem, ...] = Field(max_length=200)
