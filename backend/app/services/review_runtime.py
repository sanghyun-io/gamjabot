"""Non-secret, read-only observations of this executor's owned resources.

Context IDs identify local objects, not an external account or document
version. No browser, process, native call, lock acquisition or effect starts
here. The executor still needs its separate final precondition check.
"""

import time
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from app.services.action_review import Observation, Positive, ReviewChanged

ContextID = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]
Workflow = Annotated[StrictStr, Field(min_length=1, max_length=200)]


class RuntimeTarget(Observation):
    room_id: Positive
    agent_id: Positive
    session_id: Annotated[StrictStr, Field(min_length=36, max_length=36)]
    workflow_id: Workflow | None


class OwnedProcessTarget(RuntimeTarget):
    role: Literal["owned_process"] = "owned_process"
    handle: ContextID
    status: Literal["verified", "unavailable", "unsupported"]
    generation: Annotated[StrictInt, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "verified") != (self.generation is not None):
            raise ValueError("Owned process generation is unavailable")
        return self


class BrowserTarget(RuntimeTarget):
    role: Literal["browser_context"] = "browser_context"
    status: Literal["verified", "absent", "unavailable", "unsupported"]
    context_id: ContextID | None = None
    profile: Literal["temporary_workflow", "persistent_room"] = "temporary_workflow"
    profile_id: ContextID | None = None
    profile_revision: Positive | None = None
    profile_label: Annotated[StrictStr, Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "verified") != (self.context_id is not None):
            raise ValueError("Browser context identity is inconsistent")
        if self.profile == 'persistent_room':
            if self.profile_id is None or self.profile_revision is None:raise ValueError('Profile binding unavailable')
        elif self.profile_id is not None or self.profile_revision is not None or self.profile_label is not None:
            raise ValueError('Temporary browser has no profile binding')
        return self


class VaultProfileTarget(RuntimeTarget):
    role: Literal["vault_profile"] = "vault_profile"
    status: Literal["verified"] = "verified"
    profile_id: ContextID
    profile_revision: Positive
    profile_label: Annotated[StrictStr, Field(max_length=100)]
    profile_origins: list[list[StrictStr | int]]
    account_label: Annotated[StrictStr, Field(max_length=200)]
    item_id: ContextID
    item_revision: Positive
    secret_revision: Positive
    origin: Annotated[StrictStr, Field(min_length=1,max_length=2048)]
    username: Annotated[StrictStr, Field(min_length=1,max_length=512)]
    context_id: ContextID | None = None


class DesktopTarget(RuntimeTarget):
    role: Literal["desktop_channel"] = "desktop_channel"
    # Configured proves adapter identity only, not a successful IPC response.
    status: Literal["configured", "unavailable", "unsupported"]
    context_id: ContextID | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "configured") != (self.context_id is not None):
            raise ValueError("Native adapter identity is inconsistent")
        return self


class VoiceMemoTarget(RuntimeTarget):
    role: Literal["voice_memo"] = "voice_memo"
    status: Literal["configured", "unavailable"]
    core_epoch: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "configured") != (self.core_epoch is not None):
            raise ValueError("Speech Core identity is unavailable")
        return self


class MCPReviewTarget(RuntimeTarget):
    role: Literal["mcp_server"] = "mcp_server"
    status: Literal["configured"] = "configured"
    server_id: Annotated[StrictStr, Field(min_length=36,max_length=36)]
    config_revision: Positive
    binding_revision: Positive
    tool_name: Annotated[StrictStr, Field(min_length=1,max_length=200)]
    schema_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    transport_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


def observe_runtime_target(action, prepared, executor):
    binding = {name: getattr(action, name) for name in ("room_id", "agent_id", "session_id", "workflow_id")}
    if action.family == "synthesize_voice_memo":
        bridge = getattr(executor, 'speech_client', None)
        core_epoch = getattr(bridge, 'core_epoch', None)
        voice = {'id':prepared.raw_arguments.get('voice_id'), 'locale':prepared.raw_arguments.get('locale')}
        if (not getattr(bridge, 'available', False) or not getattr(executor, 'speech_available', False)
                or voice not in getattr(executor, 'speech_voices', []) or not isinstance(core_epoch, str)):
            return VoiceMemoTarget(**binding, status='unavailable')
        return VoiceMemoTarget(**binding, status='configured', core_epoch=core_epoch)
    if action.family == "request_vault_login":
        from app.services.vault_profile_handoff import observed
        return VaultProfileTarget(**binding,**observed(executor,action))
    if action.family == "cancel_process":
        processes = getattr(executor, "processes", None)
        handle = prepared.raw_arguments["process_id"]
        owned = processes.running.get(handle) if processes is not None else None
        if owned is None:
            return OwnedProcessTarget(**binding, handle=handle,
                                      status="unsupported" if processes is None else "unavailable")
        if (owned.handle != handle or any(getattr(owned, name) != value for name, value in binding.items())):
            raise ReviewChanged("Process is not owned by this execution context")
        available = (owned.generation is not None and owned.process.poll() is None
                     and owned.deadline > time.monotonic())
        return OwnedProcessTarget(**binding, handle=handle, status="verified" if available else "unavailable",
                                  generation=owned.generation if available else None)
    if action.family.startswith("browser_"):
        browser = getattr(executor, "browser", None)
        if browser is None:
            return BrowserTarget(**binding, status="unsupported")
        profiles = getattr(browser, 'profiles', None)
        from app.services.resource_binding import freeze_binding
        proof=freeze_binding(getattr(action,'execution_binding',None))
        metadata = profiles.selected(action,proof) if profiles else None
        profile_binding = {'profile':'persistent_room','profile_id':metadata['id'],
                           'profile_revision':metadata['revision'],'profile_label':metadata['account_label'] or metadata['name']} if metadata else {}
        key = (action.room_id, action.agent_id, action.workflow_id or action.action_id)
        session = browser.workflows.get(key)
        if session is None:
            if metadata:
                from app.services.vault_profile_handoff import hold,custody_observed
                if hold(profiles,metadata['id']):
                    custody=custody_observed(profiles,metadata,action)
                    if custody is None:return BrowserTarget(**binding, **profile_binding, status='unavailable')
                    return BrowserTarget(**binding, **profile_binding, status='verified', context_id=custody['context_id'])
                pair=profiles.session(metadata['id'])
                if pair is not None or (metadata['id'] in getattr(profiles,'claims',{}) and not browser.parked_valid(metadata)) or metadata['id'] in getattr(profiles,'transitions',set()):
                    return BrowserTarget(**binding, **profile_binding, status='unavailable')
            return BrowserTarget(**binding, **profile_binding, status="absent")
        if any(getattr(session, name) != binding[name] for name in ("room_id", "agent_id", "workflow_id")):
            raise ReviewChanged("Browser context ownership changed")
        if (getattr(session,'profile_id',None)!=(metadata['id'] if metadata else None)
                or freeze_binding(getattr(session,'execution_binding',None))!=proof
                or getattr(session,'controller',None)!='agent' or getattr(session,'closing',None) is not False
                or session.deadline <= time.monotonic() or session.page.is_closed()
                or not session.authority.active()):
            return BrowserTarget(**binding, **profile_binding, status="unavailable")
        return BrowserTarget(**binding, **profile_binding, status="verified", context_id=session.context_id)
    desktop = getattr(executor, "desktop", None)
    if desktop is None:
        return DesktopTarget(**binding, status="unsupported")
    if desktop.native is None:
        return DesktopTarget(**binding, status="unavailable")
    context_id = getattr(desktop.native, "context_id", None)
    if context_id is None:
        # An injected/older adapter has no observable identity. Do not invent
        # one or mislabel it as a broken native call before any dispatch.
        return DesktopTarget(**binding, status="unsupported")
    return DesktopTarget(**binding, status="configured", context_id=context_id)
