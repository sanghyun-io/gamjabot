"""Explicit private directory layout; no Bot configuration or execution state."""
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

UUID = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'


class DirectoryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class DirectorySection(DirectoryModel):
    id: StrictStr = Field(pattern=UUID)
    name: StrictStr = Field(min_length=1, max_length=80)


class DirectoryItem(DirectoryModel):
    agent_id: StrictInt = Field(gt=0)
    pinned: StrictBool
    hidden: StrictBool
    section_id: StrictStr | None = Field(pattern=UUID)


class DirectorySnapshot(DirectoryModel):
    sections: list[DirectorySection] = Field(max_length=100)
    items: list[DirectoryItem] = Field(max_length=5000)


class DirectoryWrite(DirectorySnapshot):
    expected_revision: StrictInt = Field(ge=0)
