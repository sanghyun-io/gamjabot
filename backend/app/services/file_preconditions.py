"""Internal file-effect evidence, never a caller-supplied execution grant."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.services.action_review import Observation, ReviewChanged, observe_file_target
from app.services.review_snapshot import CanonicalID, ReviewSnapshot, StoredTarget


class FilePostconditions(Observation):
    action_id: CanonicalID
    review_id: CanonicalID
    family: Literal["read_file", "write_file", "delete_file", "move_file"]
    before_targets: Annotated[tuple[StoredTarget, ...], Field(min_length=1, max_length=2)]
    after_targets: Annotated[tuple[StoredTarget, ...], Field(min_length=1, max_length=2)]
    check: Literal["fd_namespace_conditional"] = "fd_namespace_conditional"
    content_CAS: Literal["unsupported"] = "unsupported"

    @field_validator("action_id", "review_id")
    @classmethod
    def canonical(cls, value, info):
        parsed = UUID(value)
        if str(parsed) != value or (info.field_name == "review_id" and parsed.version != 4):
            raise ValueError("Invalid file effect identity")
        return value

    @model_validator(mode="after")
    def transitions(self):
        expected = ("source", "destination") if self.family == "move_file" else ("path",)
        if (tuple(t.role for t in self.before_targets) != expected
                or tuple(t.role for t in self.after_targets) != expected):
            raise ValueError("File effect roles changed")
        for before, after in zip(self.before_targets, self.after_targets):
            if (before.path != after.path or before.parents != after.parents
                    or before.status not in ("verified", "absent")
                    or after.status not in ("verified", "absent")):
                raise ValueError("File effect namespace changed")
        before, after = self.before_targets, self.after_targets
        if self.family == "read_file" and (before[0].status != "verified" or before != after):
            raise ValueError("Read version changed")
        if self.family == "write_file" and after[0].status != "verified":
            raise ValueError("Write result is missing")
        if self.family == "delete_file" and (before[0].status != "verified" or after[0].status != "absent"):
            raise ValueError("Delete transition changed")
        if self.family == "move_file":
            same = before[0].path == before[1].path
            if (before[0].status != "verified" or (same and before != after)
                    or (not same and (after[0].status != "absent" or after[1].status != "verified"))):
                raise ValueError("Move transition changed")
        return self


def validate_file_postconditions(value, action, ref, *, observe=True):
    """Validate internal typed evidence and its still-live, bounded metadata.

    This recheck is not a filesystem/database atomic content-version CAS.
    Only the executor that held and checked the file descriptors creates it.
    """
    try:
        if not isinstance(value, FilePostconditions) or not isinstance(ref.review, ReviewSnapshot):
            raise ValueError("Missing internal file evidence")
        raw = value.model_dump_json()
        if len(raw.encode()) > 65_536:
            raise ValueError("File evidence is too large")
        result = FilePostconditions.model_validate_json(raw)
        if (result.action_id != action.action_id or result.review_id != action.review_id
                or result.review_id != ref.review_id or result.family != action.family
                or result.before_targets != ref.review.targets
                or ref.review.action.action_id != action.action_id):
            raise ValueError("File evidence belongs to another execution")
        if observe:
            for after in result.after_targets:
                current = observe_file_target(after.path, after.role)
                if StoredTarget.model_validate_json(current.model_dump_json()) != after:
                    raise ValueError("File result changed before settlement")
        return result
    except (ValueError, TypeError, AttributeError) as error:
        raise ReviewChanged("File effect postconditions are unconfirmed") from error
