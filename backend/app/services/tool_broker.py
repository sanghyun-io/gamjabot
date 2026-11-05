"""The sole consent and executor boundary for scheduler tool calls."""

import asyncio
import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from contextlib import nullcontext
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, ValidationError
from app.services.mcp_schema import validate_mcp_meta,validate_mcp_arguments
from referencing.exceptions import Unresolvable
from sqlalchemy import update, text as sql_text

from app.models.collaboration import RoomEvent, RoomGrant, ToolAction
from app.services.events import ActorRef, append_event, stage_event
from app.services.grants import GrantRef, ReviewRequired, broad_state, claim, matches, now, OPAQUE_ACTIONS
from app.services.file_preconditions import FilePostconditions, validate_file_postconditions
from app.services.review_execution import (bound_authority, canonical_review_id, execution_supported,
                                            grant_lifetime_valid, session_review_supported)
from app.services.llm_service import LLMFailure
from app.services.diagnostics import emit_detailed_diagnostic
from app.services.diagnostic_detail import make_diagnostic
from app.services.action_review import ProposerBinding, ReviewChanged, observe_authority
from app.services.review_snapshot import (
    StoredTarget, capture_review, retained_review, same_review_observations, snapshot_value,
)


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


S = {"type": "string", "minLength": 1, "maxLength": 8192}
C = {"type": "integer", "minimum": -100000, "maximum": 100000}
W = {"workflow_id": {"type": "string", "minLength": 1, "maxLength": 200}}
TOOL_SCHEMAS = [
    {"name":"synthesize_voice_memo", "description":"Propose a local spoken memo using an installed generic system voice; Owner approval is required. This creates an audio file, not live playback.", "parameters":_schema({"text":{"type":"string","minLength":1,"maxLength":4000}, "voice_id":{"type":"string","minLength":1,"maxLength":200}, "locale":{"type":"string","minLength":1,"maxLength":64}, "rate_milli":{"type":"integer","minimum":1,"maximum":1000}}, ("text","voice_id","locale","rate_milli"))},
    {"name": "start_process", "description": "Run an owned process. The executable must be an absolute path; bare command names are rejected. No shell or PATH lookup is used.", "parameters": _schema({"executable": S, "argv": {"type": "array", "items": S, "maxItems": 128}, "cwd": S,
        "background_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        "env": {"type": "object", "maxProperties": 32, "additionalProperties": {"type": "string", "maxLength": 8192}}, **W}, ("executable",))},
    {"name": "cancel_process", "parameters": _schema({"process_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}, **W}, ("process_id",))},
    {"name": "read_file", "parameters": _schema({"path": S}, ("path",))},
    {"name": "write_file", "parameters": _schema({"path": S, "content": {"type": "string", "maxLength": 1000000}}, ("path", "content"))},
    {"name": "move_file", "parameters": _schema({"source": S, "destination": S}, ("source", "destination"))},
    {"name": "delete_file", "parameters": _schema({"path": S}, ("path",))},
    {"name": "request_vault_login", "description": "Request one Owner-reviewed login handoff for this Bot persistent browser profile. No password is returned.", "parameters": _schema({"item_id": {"type":"string","pattern":"^[0-9a-f]{32}$"}, **W}, ("item_id",))},
    {"name": "browser_navigate", "parameters": _schema({"url": S, **W}, ("url",))},
    {"name": "browser_screenshot", "description": "Capture the reviewed URL in the Bot dedicated managed Chromium page, never the OS desktop or user Chrome; creates a room-owned PNG. Image bytes are available to a supported model only after explicit Owner consent for this action.", "parameters": _schema({"url": S, **W}, ("url",))},
    {"name": "browser_inspect", "parameters": _schema({"url": S, **W}, ("url",))},
    {"name": "browser_interact", "description": "In a held Vault browser profile, click requires a standard CSS selector matching exactly one visible enabled element and dispatches a synthetic click, not a trusted pointer event. Fill and submit are unavailable there. Do not automatically retry or use a fallback after an uncertain effect.", "parameters": _schema({"url": S, "selector": S, "operation": {"enum": ["click", "fill", "submit"]}, "text": {"type": "string", "maxLength": 100000}, **W}, ("url", "selector", "operation"))},
    {"name": "screenshot", "description": "Capture the desktop. On macOS this is the main display, rendered at one image pixel per logical display coordinate. The image remains a local artifact unless the Owner explicitly consents to sending it to the model for this action.", "parameters": _schema({**W})},
    {"name": "pointer_click", "description": "Click at x,y. On macOS coordinates are logical units from the main display's upper-left; screenshot pixels map one-to-one, and offsets outside it can reach other displays.", "parameters": _schema({"x": C, "y": C, **W}, ("x", "y"))},
    {"name": "pointer_move", "description": "Move to x,y. On macOS coordinates are logical units from the main display's upper-left; screenshot pixels map one-to-one, and offsets outside it can reach other displays.", "parameters": _schema({"x": C, "y": C, **W}, ("x", "y"))},
    {"name": "pointer_scroll", "description": "Scroll at x,y. On macOS coordinates are logical units from the main display's upper-left; screenshot pixels map one-to-one, and offsets outside it can reach other displays.", "parameters": _schema({"x": C, "y": C, "delta": {"type": "integer", "minimum": -100000, "maximum": 100000}, **W}, ("x", "y", "delta"))},
    {"name": "keyboard_text", "parameters": _schema({"text": {"type": "string", "minLength": 1, "maxLength": 100000}, **W}, ("text",))},
    {"name": "keyboard_chord", "parameters": _schema({"keys": {"type": "array", "items": S, "minItems": 1, "maxItems": 8}, **W}, ("keys",))},
]
SCHEMAS = {entry["name"]: {**entry["parameters"], "properties": {
    key: value for key, value in entry["parameters"]["properties"].items() if key != "workflow_id"}}
    for entry in TOOL_SCHEMAS}


@dataclass(frozen=True)
class PreparedAction:
    family: str
    target: str
    scope_target: str
    expected_effect: str
    payload_digest: str
    public_arguments: dict
    raw_arguments: dict
    mcp_binding: dict | None = None


@dataclass(frozen=True)
class ActionResult:
    state: str
    text: str
    executor_seconds: float = 0.0
    safe_text: str = ""
    screenshot_ref: str | None = None
    image_bytes: bytes | None = None
    image_mime: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    sensitivity: str = "normal"
    error_code: str | None = None
    diagnostic: dict | None = None
    file_postconditions: FilePostconditions | None = None
    voice_memo_data: tuple | None = None


TERMINAL = frozenset({"completed", "failed", "denied", "cancelled", "uncertain"})


def stored_result(action: ToolAction, text: str, sensitivity: str = "normal") -> str:
    """Keep useful observations; redact credential-shaped material before persistence."""
    if sensitivity == "sensitive" or (action.family == "read_file" and re.search(
            r"(?:^|[/\\])(?:\.env(?:\..*)?|id_(?:rsa|ed25519)|[^/\\]+\.(?:pem|key))$", action.target or "", re.I)):
        return "[sensitive result redacted]"
    bounded = str(text)[:16000]
    bounded = re.sub(r"-----BEGIN [^-]+PRIVATE KEY-----.*?-----END [^-]+PRIVATE KEY-----",
                     "[private key redacted]", bounded, flags=re.S)
    bounded = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", bounded)
    bounded = re.sub(r"(?i)(['\"]?\b(?:api[_-]?key|access[_-]?token|token|password|secret|client[_-]?secret)\b['\"]?\s*[:=]\s*['\"]?)[^'\"\s,;}]+",
                     r"\1[redacted]", bounded)
    bounded = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", bounded)
    return bounded


def stage_action_result(db, action_id: str, state: str, text: str = "", *,
                        screenshot_ref: str | None = None, sensitivity: str = "normal",
                        image_mime: str | None = None, image_width: int | None = None,
                        image_height: int | None = None, error_code: str | None = None,
                        diagnostic: dict | None = None, generated_file: dict | None = None, voice_memo: dict | None = None):
    """Stage a terminal action and its canonical result event."""
    if state not in TERMINAL:
        raise ValueError("Invalid terminal action state")
    action = db.get(ToolAction, action_id)
    if action is None:
        return None
    key = f"tool-result:{action_id}"
    prior = db.query(RoomEvent).filter_by(room_id=action.room_id, idempotency_key=key).first()
    if prior:
        return prior
    if action.state in TERMINAL:
        state = action.state
        observation = dict(action.observation or {"call_id": action.call_id, "state": state,
                                                  "text": text or "[prior result unavailable]", "screenshot_ref": None})
        observation["text"] = stored_result(action, observation.get("text", ""))
    else:
        safe_ref = (screenshot_ref if isinstance(screenshot_ref, str) and re.fullmatch(
            r"[A-Za-z0-9_./-]{1,256}", screenshot_ref) and ".." not in screenshot_ref else None)
        if action.family in {"screenshot", "browser_screenshot"} and safe_ref != f"rooms/{action.room_id}/screenshots/{action.action_id}.png":
            safe_ref = None
        observation = {"call_id": action.call_id, "state": state,
                       "text": stored_result(action, text, sensitivity),
                       "screenshot_ref": safe_ref}
        if safe_ref and image_mime in ("image/png", "image/jpeg") and isinstance(image_width, int) and isinstance(image_height, int):
            observation.update(image_mime=image_mime, image_width=image_width, image_height=image_height)
        if error_code and re.fullmatch(r"[a-z_]{1,64}", error_code):
            observation["error_code"] = error_code
        if diagnostic is not None:
            observation["diagnostic"] = diagnostic
        if generated_file is not None:
            observation["generated_file"] = generated_file
        if voice_memo is not None:
            observation["voice_memo"] = voice_memo
    if action.family == 'mcp_tool_call':
        from app.models.mcp import MCPOwnerDraft
        draft=db.get(MCPOwnerDraft,action_id)
        if draft and draft.applied_review_id==action.review_id:
            observation['owner_edited']=True
    action.state, action.completed_at = state, now()
    action.observation=({**observation,'text':'Owner-edited action result is available to the Owner.'}
                        if observation.get('owner_edited') is True else observation)
    payload = {**observation, "agent_id": action.agent_id, "action_id": action.action_id,
               "trigger_event_id": action.trigger_event_id}
    if canonical_review_id(action.review_id):
        payload["review_id"] = action.review_id
    return stage_event(db, action.room_id, ActorRef("tool", None), "tool_result", payload,
                       key, allow_inactive=True)


def record_action_result(db, action_id: str, state: str, text: str = "", **kwargs):
    """Commit terminal action and its one canonical room event together."""
    try:
        result = stage_action_result(db, action_id, state, text, **kwargs)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def _path(value):
    if "\x00" in value:
        raise ValueError
    return os.path.realpath(os.path.abspath(os.path.expanduser(value)))


def _url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    public = urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))
    # Query and fragment can contain credentials. A digest distinguishes exact
    # destinations without persisting those values in approval/activity state.
    if parsed.query or parsed.fragment:
        fingerprint = hashlib.sha256(value.encode()).hexdigest()[:16]
        public += f" [parameters sha256:{fingerprint}]"
    return public


def _public_mcp_arguments(value, key=''):
    """Redact credential-shaped fields in the approval preview; do not reject ordinary input."""
    if re.search(r'(?i)(password|secret|token|api[_-]?key|authorization|credential)',key):return '[redacted]'
    if isinstance(value,dict):return {str(k):_public_mcp_arguments(v,str(k)) for k,v in value.items()}
    if isinstance(value,list):return [_public_mcp_arguments(v) for v in value]
    if isinstance(value,str):return re.sub(r'(?i)Bearer\s+[A-Za-z0-9._~+/=-]+','Bearer [redacted]',value)
    return value

def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def prepare(family: str, arguments: dict, workflow_id: str | None = None) -> PreparedAction:
    schema = SCHEMAS.get(family)
    if (schema is None or not isinstance(arguments, dict)
            or (workflow_id is not None and (not isinstance(workflow_id, str) or len(workflow_id) > 200))):
        raise ValueError("Unsupported tool action")
    if (family == "screenshot" or family.startswith(("pointer_", "keyboard_"))) and not workflow_id:
        raise ValueError("Desktop action requires a workflow ID")
    try:
        Draft202012Validator(schema).validate(arguments)
        encoded = json.dumps(arguments, allow_nan=False, ensure_ascii=False)
        if len(encoded) > 1000000:
            raise ValueError
        args = json.loads(encoded)
        if family in ("read_file", "write_file", "delete_file"):
            scope_target = _path(args["path"])
            args["path"] = scope_target
            effect = {"read_file": "Read file", "write_file": f"Write {len(args.get('content', '').encode())} bytes to file; a private local copy may be retained",
                      "delete_file": "Delete file"}[family]
            target = scope_target
        elif family == "move_file":
            target = f"{_path(args['source'])} → {_path(args['destination'])}"
            scope_target = target
            args["source"], args["destination"] = _path(args["source"]), _path(args["destination"])
            effect = "Move file"
        elif family == "synthesize_voice_memo":
            if not args['text'].strip() or len(args['text'].encode('utf-8')) > 4000:
                raise ValueError("Voice transcript is too long or empty")
            target = scope_target = "local:voice-memo"
            effect = "Create one local audio memo from the reviewed text and selected system voice."
        elif family == "start_process":
            if not os.path.isabs(args["executable"]):
                raise ValueError("Process executable must be absolute")
            args["executable"] = _path(args["executable"])
            if "cwd" in args:
                args["cwd"] = _path(args["cwd"])
            from app.services.tools.process import child_environment
            child_environment(args.get("env"))
            scope_target = args["executable"]
            target = f"{scope_target} [invocation sha256:{_fingerprint({'argv': args.get('argv', []), 'cwd': args.get('cwd'), 'env': args.get('env'), 'background_seconds': args.get('background_seconds')})}]"
            effect = f"Run process with local user authority; {len(args.get('argv', []))} arguments"
            if args.get("background_seconds"):
                effect += f"; owned background lease up to {args['background_seconds']} seconds after start"
        elif family == "cancel_process":
            target, effect = f"process:{args['process_id']}", "Cancel process"
            scope_target = target
        elif family == "request_vault_login":
            target = scope_target = "vault-login:" + args["item_id"]
            effect = "Request Owner-controlled login in this Bot browser profile; no credentials are shown to the Bot."
        elif family.startswith("browser_"):
            target = _url(args["url"])
            parsed = urlsplit(args["url"])
            scope_target = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            if family == "browser_interact":
                target += f" [interaction sha256:{_fingerprint({'selector': args['selector'], 'operation': args['operation'], 'text': args.get('text')})}]"
            effect = {"browser_navigate": "Navigate browser", "browser_inspect": "Inspect page",
                      "browser_interact": f"Browser {args.get('operation', '')}", "browser_screenshot": "Capture browser page"}[family]
        elif family == "keyboard_text":
            target, effect = f"desktop:keyboard [text sha256:{_fingerprint(args['text'])}]", f"Type {len(args['text'])} characters"
            scope_target = "desktop:keyboard"
        elif family == "keyboard_chord":
            target, effect = f"desktop:keyboard [chord sha256:{_fingerprint(args['keys'])}]", "Press keyboard chord"
            scope_target = "desktop:keyboard"
        elif family == "screenshot":
            target, effect = "desktop:screen", "Capture screenshot"
            scope_target = target
        else:
            target, effect = f"desktop:pointer@{args['x']},{args['y']}", family.replace("_", " ").capitalize()
            scope_target = "desktop:pointer"
        digest = hashlib.sha256(json.dumps({"family": family, "workflow_id": workflow_id, "arguments": args},
                                            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if family == "write_file":
            target += f" [payload sha256:{digest[:16]}]"
        public = {key: value for key, value in args.items() if key not in {"content", "text", "argv", "keys", "selector", "url", "env"}}
        if "url" in args:
            public["url"] = target
        if "path" in public:
            public["path"] = args["path"]
        if "source" in public:
            public["source"], public["destination"] = _path(args["source"]), _path(args["destination"])
        if "executable" in public:
            public["executable"] = args["executable"]
        return PreparedAction(family, target, scope_target, effect, digest, public, args)
    except (ValidationError, ValueError, TypeError, OverflowError):
        raise ValueError("Invalid tool action") from None


class ToolBroker:
    # Core speech issue/start/read is not yet wired. Do not offer a tool that
    # cannot complete with the approved authority and artifact contract.
    tool_schemas = [item for item in TOOL_SCHEMAS if item['name'] != 'synthesize_voice_memo']
    requires_verified_tools = True
    requires_proposer_binding = True
    owns_results = True

    def __init__(self, sessions, executor=None, *, owner_epochs=None):
        self.sessions = sessions
        self.executor = executor
        self.session_id = str(uuid4())
        self._previews = {}
        self._proposers = {}
        self._memory_bindings = {}
        self._draft_applied = {}
        self.owner_epochs = owner_epochs
        self.shared_app_state = None

    def prepare(self, family: str, arguments: dict, workflow_id: str | None = None) -> PreparedAction:
        return prepare(family, arguments, workflow_id)

    def prepare_mcp(self, db, *, owner_id, agent_id, room_id, root_id, generation, meta, arguments, execution_binding=None, teammate_binding=None, shared_binding=None):
        from app.services import mcp_registry, mcp_execution
        mcp_execution.require_origin(db,owner_id=owner_id,room_id=room_id,agent_id=agent_id,root_id=root_id,
            generation=generation,execution_binding=execution_binding,teammate_binding=teammate_binding)
        from app.models.shared_text_runtime import SharedTextRoom
        from sqlalchemy import or_
        shared=db.query(SharedTextRoom).filter(or_(SharedTextRoom.room_id==room_id,SharedTextRoom.agent_id==agent_id)).first()
        if shared:
            if not isinstance(shared_binding,dict) or self.shared_app_state is None:raise ValueError('Shared recipient binding required')
            from app.services.shared_connector import require_review_binding
            try:require_review_binding(db,self.shared_app_state,shared_binding,meta)
            except Exception:raise ValueError('Shared recipient authority changed') from None
        elif shared_binding is not None:raise ValueError('Unexpected shared recipient binding')
        try:server,tool=mcp_registry.current_tool(db,owner_id,agent_id,meta)
        except Exception:raise ValueError("MCP binding changed") from None
        if not isinstance(arguments,dict):raise ValueError("Invalid MCP arguments")
        try:
            validate_mcp_meta(tool.input_schema);validate_mcp_arguments(tool.input_schema,arguments)
            raw=json.loads(mcp_registry.canonical(arguments))
            if len(mcp_registry.canonical(raw).encode())>32000:raise ValueError()
        except (ValidationError,ValueError,TypeError,Unresolvable,RecursionError):raise ValueError("Invalid MCP arguments") from None
        transport=server.transport;transport_hash=mcp_registry.digest(transport);input_hash=mcp_registry.digest(raw)
        proof={**meta,'owner_id':owner_id,'agent_id':agent_id,'root_event_id':root_id,'generation':generation,
               'transport_sha256':transport_hash,'input_sha256':input_hash,'transport_type':transport['type']}
        if teammate_binding is not None:proof['teammate_binding']=dict(teammate_binding)
        if shared_binding is not None:proof['shared_recipient_binding']=dict(shared_binding)
        if transport['type']=='stdio':proof['executable']=transport['executable']
        target=server.name+' | '+mcp_registry.canonical(transport)+' | '+tool.name
        digest=mcp_registry.digest({'binding':proof,'arguments':raw})
        return PreparedAction('mcp_tool_call',target,target,'Invoke an external MCP tool; external effects require this one-time approval.',
                              digest,{'argument_keys':sorted(raw),'arguments_sha256':input_hash},raw,proof)

    def _prepare_retained(self,db,action,prepared,owner_id):
        if action.family!='mcp_tool_call':return prepare(action.family,prepared.raw_arguments,action.workflow_id)
        proof=prepared.mcp_binding
        if not isinstance(proof,dict) or action.mcp_binding!=proof:raise ValueError("MCP binding changed")
        keys=('server_id','config_revision','binding_revision','tool_id','tool_name','schema_sha256')
        meta={key:proof[key] for key in keys}
        return self.prepare_mcp(db,owner_id=owner_id,agent_id=action.agent_id,room_id=action.room_id,
            root_id=action.trigger_event_id,generation=proof['generation'],meta=meta,arguments=prepared.raw_arguments,
            execution_binding=action.execution_binding,teammate_binding=proof.get('teammate_binding'),
            shared_binding=proof.get('shared_recipient_binding'))

    def retain(self, action_id: str, prepared: PreparedAction, *, proposer: ProposerBinding | None = None) -> None:
        if proposer is not None:
            if not isinstance(proposer, ProposerBinding):
                raise ReviewChanged("Trusted proposer binding is missing")
            original = self._proposers.get(action_id)
            if original is not None and original != proposer:
                raise ReviewChanged("Original proposer cannot be replaced")
            self._proposers[action_id] = proposer
        self._previews[action_id] = prepared

    def bind_memory(self, action_id, digest, execution_origin, *, mcp_catalog=None):
        if not isinstance(digest,str) or len(digest)!=64:
            raise ReviewChanged("Memory binding is unavailable")
        if mcp_catalog is not None and (not isinstance(mcp_catalog,tuple) or len(mcp_catalog)!=3
                or not isinstance(mcp_catalog[0],str) or not re.fullmatch(r'[a-f0-9]{64}',mcp_catalog[0])
                or not isinstance(mcp_catalog[2],str) or not re.fullmatch(r'[a-f0-9]{64}',mcp_catalog[2])):
            raise ReviewChanged("MCP catalog binding is unavailable")
        self._memory_bindings[action_id]=(digest,execution_origin,mcp_catalog)

    def _require_memory_binding(self, db, action):
        binding=self._memory_bindings.get(action.action_id)
        if binding is None:
            return  # Legacy externally created actions retain their old review contract.
        from app.services.memory_context import snapshot
        shared=(action.mcp_binding or {}).get('shared_recipient_binding') if isinstance(action.mcp_binding,dict) else None
        try:
            if shared:
                from app.services import shared_text_runtime,shared_private_memory
                if self.shared_app_state is None:raise ValueError()
                turn=shared_text_runtime.verify(self.shared_app_state,db,action.room_id,action.agent_id,action.trigger_event_id,shared['generation'])
                current=shared_private_memory.current_snapshot(db,self.shared_app_state,turn)
            else:current=snapshot(db,action.room_id,action.agent_id,execution_origin=binding[1])
        except Exception as error:
            raise ReviewChanged("Memory context changed") from error
        if current['digest']!=binding[0]:
            raise ReviewChanged("Memory context changed")
        if binding[2] is not None:
            from app.services import mcp_registry,shared_connector
            expected,bridge,core_epoch=binding[2];proposer=self._proposers.get(action.action_id)
            try:
                if (not proposer or getattr(self,'mcp_client',None) is not bridge or getattr(bridge,'available',False) is not True
                        or getattr(bridge,'core_epoch',None)!=core_epoch
                        or mcp_registry.digest(shared_connector.eligible_offers(db,self.shared_app_state,turn) if shared else mcp_registry.offered(db,proposer.owner_id,action.agent_id))!=expected):
                    raise ReviewChanged("MCP catalog changed")
            except ReviewChanged:raise
            except Exception:raise ReviewChanged("MCP catalog changed") from None

    def _review_identity(self, db, action, owner_id):
        prepared = self._previews.get(action.action_id)
        proposer = self._proposers.get(action.action_id)
        if (prepared is None or proposer is None or proposer.owner_id != owner_id
                or action.session_id != self.session_id):
            raise ReviewChanged("Original review context is unavailable")
        try:
            self._require_memory_binding(db,action)
            from app.services.owner_routines import require_action_current
            require_action_current(db,action)
            from app.services.request_retry import tool_retry_requires_once
            tool_retry_requires_once(db, action)
            from app.services.execution_scopes import root_scope
            if (root_scope(db, action.trigger_event_id) is not None and not action.execution_binding
                    and action.state != 'started'):
                raise ReviewChanged('Scoped action is missing its original task binding')
            if action.execution_binding:
                from app.services.execution_scopes import require_binding
                require_binding(db, action.execution_binding)
            original = retained_review(action)
            if original.authority.proposer != proposer:
                raise ReviewChanged("Original review proposer changed")
            current = self._prepare_retained(db,action,prepared,owner_id)
            if current != prepared or action.arguments != prepared.public_arguments:
                raise ReviewChanged("Original tool payload changed")
            authority = observe_authority(db, room_id=action.room_id, agent_id=action.agent_id,
                session_id=self.session_id, proposer=proposer, owner_epochs=self.owner_epochs)
            if authority != original.authority:
                raise ReviewChanged("Execution authority changed")
            return original, authority, prepared
        except (ValueError, TypeError) as exc:
            raise ReviewChanged("Trusted review changed") from exc

    def _review_observations(self, db, action, owner_id):
        original, authority, prepared = self._review_identity(db, action, owner_id)
        return original, capture_review(action, prepared, authority, self.executor), prepared

    def execution_review(self, db, action):
        proposer = self._proposers.get(action.action_id)
        if proposer is None:
            raise ReviewChanged("Original execution proposer is unavailable")
        original, latest, _ = self._review_observations(db, action, proposer.owner_id)
        if not same_review_observations(original, latest):
            raise ReviewChanged("Reviewed target changed before start")
        execution_supported(original)
        return original

    def require_review(self, db, action, *, owner_id, review_id):
        """Compare the inspected token and current observations; never refresh it."""
        if action.family == 'mcp_tool_call':
            from app.models.mcp import MCPOwnerDraft
            draft=db.get(MCPOwnerDraft,action.action_id)
            if draft and draft.pending:raise ReviewChanged('Apply or clear the Owner draft before approval')
        try:
            canonical = str(UUID(review_id)) == review_id and UUID(review_id).version == 4
        except (ValueError, TypeError, AttributeError):
            canonical = False
        if (not canonical or review_id != action.review_id or action.state != "waiting"
                or action.approved_at is not None or action.approval_owner_id is not None
                or action.grant_id is not None or action.approval_scope is not None):
            raise ReviewChanged("Inspect this exact pending review before approving")
        original, latest, _ = self._review_observations(db, action, owner_id)
        if not same_review_observations(original, latest):
            raise ReviewChanged("Reviewed target changed; inspect it again")
        return original

    def preview(self, action: ToolAction, *, owner_id: int) -> dict | None:
        """Read the exact retained review; only unapproved file versions refresh.

        This is not an approval. Legacy/restarted previews cannot be rebuilt
        from public arguments or blessed with the current connection identity.
        """
        prepared = self._previews.get(action.action_id)
        proposer = self._proposers.get(action.action_id)
        if prepared is None or proposer is None:
            return None
        with self.sessions() as db:
            # A preview refresh must not race an approval into replacing its
            # token. This lock only serializes SQLite rows, not filesystem I/O.
            if db.bind.dialect.name == "sqlite":
                db.execute(sql_text("BEGIN IMMEDIATE"))
            saved = db.get(ToolAction, action.action_id)
            if (saved is None or saved.room_id != action.room_id or saved.agent_id != action.agent_id
                    or saved.session_id != self.session_id or saved.state not in ("waiting", "approved")
                    or proposer.owner_id != owner_id):
                return None
            original, latest, prepared = self._review_observations(db, saved, owner_id)
            if not same_review_observations(original, latest):
                if (saved.state != "waiting" or saved.approved_at is not None
                        or saved.approval_owner_id is not None
                        or saved.grant_id is not None or saved.approval_scope is not None
                        or not all(isinstance(t, StoredTarget) for t in original.targets + latest.targets)):
                    raise ReviewChanged("Approved or runtime context changed")
                previous_id = saved.review_id
                refreshed_id = str(uuid4())
                count = db.execute(update(ToolAction).where(
                    ToolAction.action_id == saved.action_id, ToolAction.state == "waiting",
                    ToolAction.session_id == self.session_id, ToolAction.review_id == previous_id,
                    ToolAction.approved_at.is_(None), ToolAction.approval_owner_id.is_(None),
                    ToolAction.grant_id.is_(None), ToolAction.approval_scope.is_(None),
                ).values(review_id=refreshed_id, review_snapshot=snapshot_value(latest))
                    .execution_options(synchronize_session=False)).rowcount
                if count != 1:
                    raise ReviewChanged("Review was decided while refreshing")
                saved.review_id = refreshed_id
                original = latest
            review_id = saved.review_id
            send_image_to_model = saved.send_image_to_model
            from app.services.execution_scopes import public_binding
            scope_details = public_binding(db, saved)
            from app.services.owner_routines import public_action_binding
            scope_details.update(public_action_binding(db,saved))
            from app.services.request_retry import tool_retry_requires_once
            retry_once = tool_retry_requires_once(db, saved)
            db.commit()
        action = original.action
        opaque = action.family in OPAQUE_ACTIONS
        if action.family == 'synthesize_voice_memo':
            scope = 'Allow this exact text and voice to be rendered once on this Mac.'
        elif action.family == 'request_vault_login':
            scope = 'Allow this exact reviewed login handoff once. The Owner controls the browser and decides whether to sign in.'
        elif action.family == 'mcp_tool_call':
            scope = "Allow this exact MCP invocation once. External accounts and effects are not verified."
        elif opaque:
            scope = "Allow once within 60 seconds, or explicitly allow this family and target in this room, Agent and workflow until this session ends or permission is revoked. External accounts and document versions are not verified."
        elif action.family == "start_process":
            scope = "Allow this executable with different arguments in this room and Agent session. Executable and cwd version changes require another review. Program-internal effects and external accounts are not verified."
        elif action.family == "write_file":
            scope = "This Agent may write different content to this exact file in this room session."
        elif action.family.startswith(("pointer_", "keyboard_")):
            scope = "This input family may take different steps in the named desktop workflow for this room and Agent session."
        elif action.family.startswith("browser_"):
            scope = "Allow this browser family at this origin in this room and Agent session. Temporary context identity does not verify external accounts or document versions."
        else:
            scope = "This Agent may repeat this family on this resource in this room session."
        delegated = bool(action.execution_binding and action.execution_binding.get('context_mode') == 'delegated')
        if delegated:
            scope = 'Allow this exact reviewed delegated action once. No room-session or other Bot permission is inherited. External accounts and document versions remain unverified.'
        elif retry_once:
            scope = 'Allow this exact reviewed action once. No previous room-session permission is inherited. External accounts and document versions remain unverified.'
        return {"action_id": action.action_id, "review_id": review_id, **scope_details,
                "approval_policy": {"scopes": ["once", "room_session"] if not delegated and not retry_once and session_review_supported(original) else ["once"],
                    "unverified_context_required": opaque, "start_within_seconds": 60 if opaque else None},
                "send_image_to_model": send_image_to_model, "review": snapshot_value(original), "family": action.family, "target": action.target,
                "scope_target": action.scope_target, "scope_description": scope,
                "expected_effect": action.expected_effect, "workflow_id": action.workflow_id,
                "arguments": _public_mcp_arguments(prepared.raw_arguments) if action.family == "mcp_tool_call" else json.loads(json.dumps(prepared.raw_arguments)),
                "mcp_binding": prepared.mcp_binding, "payload_digest": prepared.payload_digest}

    def _active(self, action_id, ref: GrantRef) -> bool:
        prepared=self._previews.get(action_id)
        shared=bool(prepared and isinstance(prepared.mcp_binding,dict) and prepared.mcp_binding.get('shared_recipient_binding'))
        guard=self.shared_app_state.owner_recovery_lock if shared and self.shared_app_state is not None else nullcontext()
        with guard, self.sessions() as db:
            action = db.get(ToolAction, action_id)
            if (action is None or action.state != "started" or action.session_id != self.session_id
                    or ref.review is None or action.review_id != ref.review_id):
                return False
            try:
                original, _, _ = self._review_identity(db, action, ref.review.authority.owner_id)
                if not same_review_observations(original, ref.review):
                    return False
            except ReviewChanged:
                return False
            if ref.scope == "broad":
                from app.models.models import Conversation
                from app.models.collaboration import BroadMode
                room = db.get(Conversation, action.room_id)
                mode = db.get(BroadMode, room.user_id) if room else None
                return bool(action.family not in OPAQUE_ACTIONS and room and room.is_active
                            and mode and ref.broad_revision is not None
                            and mode.updated_at == ref.broad_revision and mode.expires_at == ref.expires_at
                            and broad_state(db, room.user_id, self.session_id)["enabled"])
            grant = db.get(RoomGrant, ref.grant_id)
            scope = grant.scope if grant is not None else None
            return bool(grant and grant.decision == ("consumed" if ref.scope == "once" else "approved")
                        and grant.expires_at == ref.expires_at
                        and grant_lifetime_valid(scope, grant.expires_at, session_id=self.session_id, clock=now())
                        and grant.room_id == ref.room_id and grant.agent_id == ref.agent_id
                        and bound_authority(scope, ref.review) and scope.get("kind") == ref.scope
                        and scope.get("approval_review_id") == ref.approval_review_id
                        and scope.get("session_id") == ref.session_id
                        and (action.family not in OPAQUE_ACTIONS
                             or scope.get("acknowledge_unverified_context") is True)
                        and (ref.scope != "room_session" or scope.get("workflow_id") == action.workflow_id)
                        and (ref.scope != "once" or (scope.get("action_id") == action.action_id
                             and scope.get("payload_digest") == action.payload_digest))
                        and tuple(scope.get("families", ())) == ref.families
                        and tuple(scope.get("targets", ())) == ref.targets)

    def _finish_file(self, action_id, state, text, ref, postconditions, sensitivity, write_payload=None):
        """Settle trusted file evidence and reusable pins in one transaction."""
        capture = None
        store = getattr(self.executor, "generated_files", None)
        with self.sessions() as db:
            try:
                if db.bind.dialect.name == "sqlite":
                    db.execute(sql_text("BEGIN IMMEDIATE"))
                saved = db.get(ToolAction, action_id)
                if saved is None:
                    return None
                prior = db.query(RoomEvent).filter_by(room_id=saved.room_id,
                    idempotency_key=f"tool-result:{action_id}").first()
                if prior is not None:
                    db.expunge(prior)
                    return prior  # Never advance the same action twice.
                advanced = False
                verified = None
                try:
                    if (saved.state != "started" or saved.session_id != ref.session_id
                            or saved.review_id != ref.review_id or saved.grant_id != ref.grant_id
                            or saved.approval_scope != ref.scope):
                        raise ReviewChanged("File execution binding changed")
                    original, _, _ = self._review_identity(db, saved, ref.review.authority.owner_id)
                    if not same_review_observations(original, ref.review):
                        raise ReviewChanged("Original file review changed")
                    verified = validate_file_postconditions(postconditions, saved, ref)
                    if ref.scope == "room_session":
                        grant = db.get(RoomGrant, ref.grant_id)
                        if (grant is None or not matches(grant, saved, ref.session_id, review=ref.review)
                                or grant.expires_at != ref.expires_at
                                or grant.scope.get("approval_review_id") != ref.approval_review_id
                                or tuple(grant.scope.get("families", ())) != ref.families
                                or tuple(grant.scope.get("targets", ())) != ref.targets):
                            raise ReviewChanged("Original file grant changed")
                        old = grant.scope
                        new = {**old, "target_pins": [t.model_dump(mode="json") for t in verified.after_targets]}
                        count = db.execute(update(RoomGrant).where(
                            RoomGrant.id == ref.grant_id, RoomGrant.decision == "approved",
                            RoomGrant.expires_at == ref.expires_at, RoomGrant.scope == old,
                        ).values(scope=new).execution_options(synchronize_session=False)).rowcount
                        if count != 1:
                            raise ReviewChanged("File grant pins changed while settling")
                        advanced = True
                    elif ref.scope == "once":
                        grant = db.get(RoomGrant, ref.grant_id)
                        if (grant is None or grant.decision != "consumed" or grant.expires_at != ref.expires_at
                                or grant.expires_at <= now() or not bound_authority(grant.scope, ref.review)
                                or grant.room_id != ref.room_id or grant.agent_id != ref.agent_id
                                or grant.scope.get("kind") != "once"
                                or grant.scope.get("session_id") != ref.session_id
                                or grant.scope.get("action_id") != action_id
                                or grant.scope.get("payload_digest") != saved.payload_digest
                                or tuple(grant.scope.get("families", ())) != ref.families
                                or tuple(grant.scope.get("targets", ())) != ref.targets
                                or grant.scope.get("approval_review_id") != ref.approval_review_id):
                            raise ReviewChanged("File once grant changed")
                    elif ref.scope != "broad" or not broad_state(db, ref.review.authority.owner_id,
                                                                 ref.session_id)["enabled"]:
                        raise ReviewChanged("File authority was withdrawn")
                except (ReviewChanged, TypeError, AttributeError, ValueError):
                    # Namespace effects cannot be rolled back by SQLite. Do
                    # not advance pins or return success after a failed CAS.
                    state, text = "uncertain", "File effect could not be verified when recording its result."
                    verified = None
                    advanced = False
                generated_file = None
                if (verified is not None and saved.family == "write_file" and sensitivity == "normal"
                        and store is not None and isinstance(write_payload, dict)):
                    from app.services.generated_files import validate_reference
                    try:
                        capture = store.capture(ref.review.authority.owner_id, saved.room_id, action_id,
                                                write_payload.get("path"), write_payload.get("content"))
                        if capture is not None and validate_reference(capture.public, saved.room_id, action_id):
                            generated_file = capture.public
                        elif capture is not None:
                            store.discard(capture); capture = None
                    except Exception:
                        capture = None
                event = stage_action_result(db, action_id, state, text, sensitivity=sensitivity,
                    generated_file=generated_file,
                    error_code=None if verified is not None else "file_postcondition_unconfirmed")
                if verified is not None:
                    stage_event(db, saved.room_id, ActorRef("system", None), "activity",
                        {"action_id": action_id, "review_id": ref.review_id,
                         "status": "file_effect_verified", "check": verified.check,
                         "content_CAS": verified.content_CAS, "grant_id": ref.grant_id,
                         "authority_source": ref.scope, "pins_advanced": advanced,
                         "post_targets": [t.model_dump(mode="json") for t in verified.after_targets]},
                        f"file-review-result:{action_id}")
                # stage_event has flushed this row. Detach before commit so
                # returning its payload cannot trigger an expired ORM read
                # after the session has closed.
                if event is not None:
                    db.expunge(event)
                db.commit()
                return event
            except BaseException:
                db.rollback()
                if capture is not None and capture.created:
                    try: store.discard(capture)
                    except Exception: pass
                raise

    def _finish_voice(self, action_id, text, ref, voice_memo_data):
        """Commit a verified Core audio result only while its original grant remains live."""
        from app.models.collaboration import RoomAgentRun
        meta, audio = voice_memo_data
        store = getattr(self.executor, 'voice_memos', None)
        if store is None or ref.scope != 'once' or not self._active(action_id, ref):
            raise ReviewChanged('Voice memo approval is unavailable')
        created = False
        with self.sessions() as db:
            try:
                db.execute(sql_text('BEGIN IMMEDIATE'))
                saved = db.get(ToolAction, action_id)
                grant = db.get(RoomGrant, ref.grant_id)
                run = db.get(RoomAgentRun, (ref.room_id, ref.agent_id))
                if (saved is None or saved.state != 'started' or saved.family != 'synthesize_voice_memo'
                        or saved.review_id != ref.review_id or saved.grant_id != ref.grant_id
                        or run is None or run.generation != ref.run_generation or run.paused or run.state == 'stopped'
                        or run.trigger_event_id != saved.trigger_event_id or grant is None or grant.decision != 'consumed'
                        or grant.expires_at != ref.expires_at or not grant_lifetime_valid(grant.scope, grant.expires_at,
                            session_id=self.session_id, clock=now())
                        or not bound_authority(grant.scope, ref.review)
                        or grant.scope.get('action_id') != action_id or grant.scope.get('payload_digest') != saved.payload_digest):
                    raise ReviewChanged('Voice memo authority changed')
                original, _, _ = self._review_identity(db, saved, ref.review.authority.owner_id)
                if not same_review_observations(original, ref.review): raise ReviewChanged('Voice memo review changed')
                if (meta.get('owner_id') != ref.review.authority.owner_id or meta.get('room_id') != saved.room_id
                        or meta.get('agent_id') != saved.agent_id or meta.get('trigger_event_id') != saved.trigger_event_id
                        or meta.get('action_id') != saved.action_id or meta.get('review_id') != saved.review_id
                        or meta.get('grant_id') != saved.grant_id or meta.get('generation') != run.generation):
                    raise ReviewChanged('Voice memo result identity changed')
                stored, created = store.capture(meta, audio)
                event = stage_action_result(db, action_id, 'completed', text, voice_memo=stored)
                if event is None: raise ReviewChanged('Voice memo result unavailable')
                db.expunge(event); db.commit(); return event
            except BaseException:
                db.rollback()
                if created:
                    try: store.delete(meta['owner_id'], meta['room_id'], meta['action_id'], meta)
                    except Exception: pass
                raise

    def _finish(self, action_id, state, text="", *, screenshot_ref=None, sensitivity="normal",
                image_mime=None, image_width=None, image_height=None, error_code=None, diagnostic=None,
                ref=None, file_postconditions=None, write_payload=None, voice_memo_data=None):
        if ref is not None and state == 'completed' and voice_memo_data is not None:
            event = self._finish_voice(action_id, text, ref, voice_memo_data)
            self._previews.pop(action_id, None)
            self._proposers.pop(action_id, None)
            self._memory_bindings.pop(action_id, None)
            return event
        # Internal synthetic/custom executors can report ordinary results but
        # can never advance pins without typed evidence from the file executor.
        if ref is not None and state == "completed" and file_postconditions is not None:
            event = self._finish_file(action_id, state, text, ref, file_postconditions, sensitivity, write_payload)
            self._previews.pop(action_id, None)
            self._draft_applied.pop(action_id, None)
            self._proposers.pop(action_id, None)
            self._memory_bindings.pop(action_id, None)
            return event
        with self.sessions() as db:
            try:
                event = stage_action_result(db, action_id, state, text,
                                         screenshot_ref=screenshot_ref, sensitivity=sensitivity,
                                         image_mime=image_mime, image_width=image_width,
                                         image_height=image_height, error_code=error_code,
                                         diagnostic=diagnostic)
                persisted = event is not None and diagnostic is not None and event.payload.get("diagnostic") == diagnostic
                if event is not None:
                    db.expunge(event)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        self._previews.pop(action_id, None)
        self._draft_applied.pop(action_id, None)
        self._proposers.pop(action_id, None)
        self._memory_bindings.pop(action_id, None)
        if persisted and event.payload.get('state') != 'completed':
            emit_detailed_diagnostic(diagnostic)
        return event

    def finalize_interrupted(self, action_id: str) -> None:
        with self.sessions() as db:
            action = db.get(ToolAction, action_id)
            state = "uncertain" if action and action.state == "started" else "cancelled"
        self._finish(action_id, state, "Action interrupted; effect is unknown." if state == "uncertain" else "Action cancelled.")

    async def execute(self, action: ToolAction, grant: GrantRef | int | None,
                      cancel: asyncio.Event, budget) -> ActionResult:
        action_id = action.action_id
        if action.execution_binding and action.family != 'mcp_tool_call':
            prepare_scoped = getattr(self.executor, 'prepare_scoped_execution', None)
            if prepare_scoped is not None:
                await prepare_scoped()
            if not getattr(self.executor, 'scoped_execution_ready', False):
                self._finish(action_id, 'denied', 'Scoped execution is unavailable.', error_code='scoped_execution_unavailable')
                return ActionResult('denied', 'Scoped execution is unavailable.', error_code='scoped_execution_unavailable')
        raw = getattr(action, "_raw_arguments", action.arguments)
        try:
            if action.family == 'mcp_tool_call':
                retained=self._previews.get(action_id);proposer=self._proposers.get(action_id)
                if retained is None or proposer is None:raise ValueError("MCP review unavailable")
                shared=bool(isinstance(retained.mcp_binding,dict) and retained.mcp_binding.get('shared_recipient_binding'))
                guard=self.shared_app_state.owner_recovery_lock if shared and self.shared_app_state is not None else nullcontext()
                with guard, self.sessions() as mcp_db:prepared=self._prepare_retained(mcp_db,action,retained,proposer.owner_id)
            else:prepared = prepare(action.family, raw, action.workflow_id)
        except (ValueError,TypeError,KeyError):
            self._finish(action_id, "denied", "Tool action was invalid.")
            return ActionResult("denied", "Tool action was invalid.", safe_text="Tool action was invalid.")
        with self.sessions() as db:
            saved = db.get(ToolAction, action_id)
            if (saved is None or saved.room_id != action.room_id or saved.agent_id != action.agent_id
                    or saved.session_id != self.session_id):
                return ActionResult("denied", "Tool action changed.", safe_text="Tool action changed.")
            if saved.state in TERMINAL or saved.state == "started":
                return ActionResult("denied", "Action is already settled or running.",
                                    safe_text="Action is already settled or running.")
            changed = (saved.target != prepared.target or saved.scope_target != prepared.scope_target
                    or saved.payload_digest != prepared.payload_digest
                    or saved.expected_effect != prepared.expected_effect
                    or saved.arguments != prepared.public_arguments)
            if not changed and saved.state == "pending":
                saved.state = "waiting"
                saved.requested_at = now()
                db.commit()
        if changed:
            self._finish(action_id, "denied", "Tool action changed after consent snapshot.")
            return ActionResult("denied", "Tool action changed.", safe_text="Tool action changed.")
        started = False
        try:
            if action.family.startswith('browser_') and hasattr(self.executor,'wait_browser_profile'):
                await self.executor.wait_browser_profile(action,cancel)
            while not cancel.is_set():
                shared=bool(isinstance(prepared.mcp_binding,dict) and prepared.mcp_binding.get('shared_recipient_binding'))
                guard=self.shared_app_state.owner_recovery_lock if shared and self.shared_app_state is not None else nullcontext()
                with guard, self.sessions() as db:
                    if db.bind.dialect.name == "sqlite":
                        db.execute(sql_text("BEGIN IMMEDIATE"))
                    saved = db.get(ToolAction, action_id)
                    if saved is None:
                        raise ReviewChanged("Execution action is unavailable")
                    self._require_memory_binding(db,saved)
                    if saved.family == 'mcp_tool_call' and saved.payload_digest != prepared.payload_digest:
                        from app.models.mcp import MCPOwnerDraft
                        draft=db.get(MCPOwnerDraft,action_id);retained=self._previews.get(action_id)
                        if (not draft or not retained or draft.applied_review_id!=saved.review_id
                                or self._draft_applied.get(action_id)!=(saved.review_id,saved.payload_digest)
                                or retained.payload_digest!=saved.payload_digest):
                            raise ReviewChanged('Edited MCP review is unavailable')
                        original,latest,current=self._review_observations(db,saved,draft.owner_id)
                        if not same_review_observations(original,latest) or current!=retained:
                            raise ReviewChanged('Edited MCP review changed')
                        prepared=retained;action.mcp_binding=copy.deepcopy(saved.mcp_binding)
                    if saved.state == "denied":
                        db.rollback()
                        self._finish(action_id, "denied", "Owner denied this action.")
                        return ActionResult("denied", "Owner denied this action.", safe_text="Owner denied this action.")
                    if saved.state in ("cancelled", "uncertain"):
                        db.rollback()
                        self._finish(action_id, saved.state, "Action was stopped.")
                        return ActionResult(saved.state, "Action was stopped.", safe_text="Action was stopped.")
                    if saved.state not in ("waiting", "approved"):
                        return ActionResult("denied", "Action is already settled or running.",
                                            safe_text="Action is already settled or running.")
                    selected = grant.grant_id if isinstance(grant, GrantRef) else grant
                    selected = selected or saved.grant_id
                    try:
                        ref = claim(db, saved, self.session_id, selected, broker=self,
                                    require_action_review=bool(getattr(action, '_resume_requires_review', False)))
                    except ReviewRequired:
                        db.commit()
                        grant = None  # The old explicit once grant was revoked, never consumed.
                        continue
                    if ref:
                        from app.services.execution_scopes import reserve_executor
                        reservation = reserve_executor(db, saved)
                        if reservation == 0:
                            db.rollback()  # Includes the one-time grant claim.
                            await asyncio.sleep(.05)
                            continue
                        if reservation is not None:
                            budget._remaining = min(budget._remaining, reservation / 1000)
                        changed = db.execute(update(ToolAction).where(
                            ToolAction.action_id == action_id,
                            ToolAction.state.in_(("waiting", "approved")),
                            ToolAction.payload_digest == prepared.payload_digest,
                            ToolAction.review_id == ref.review_id,
                            ToolAction.session_id == self.session_id,
                            ToolAction.started_at.is_(None),
                        ).values(state="started", started_at=now())
                            .execution_options(synchronize_session=False)).rowcount
                        if changed != 1:
                            db.rollback()
                            return ActionResult("denied", "Action was already claimed.",
                                                safe_text="Action was already claimed.")
                        stage_event(db, saved.room_id, ActorRef("system", None), "activity",
                            {"action_id": action_id, "status": "started", "review_id": ref.review_id,
                             "authority_source": ref.scope, "grant_id": ref.grant_id},
                            f"tool-start:{action_id}")
                        db.commit()
                        started = True
                        break
                    if selected is not None:
                        db.rollback()
                        self._finish(action_id, "denied", "Grant does not cover this action.")
                        return ActionResult("denied", "Grant does not cover this action.",
                                            safe_text="Grant does not cover this action.")
                await asyncio.sleep(.05)
            if not started:
                self._finish(action_id, "cancelled", "Action was stopped.")
                return ActionResult("cancelled", "Action was stopped.", safe_text="Action was stopped.")
            if self.executor is None:
                self._finish(action_id, "failed", "Local executor is unavailable.")
                return ActionResult("failed", "Local executor is unavailable.", safe_text="Local executor is unavailable.")
            # The detached object carries the exact validated input. Only its
            # redacted form was saved, so consent snapshots never store text.
            action.arguments = json.loads(json.dumps(prepared.raw_arguments))
            action.target, action.expected_effect = prepared.target, prepared.expected_effect
            action.grant_id, action.approval_scope = ref.grant_id, ref.scope
            if action.family == 'synthesize_voice_memo':
                async def invoke_speech():
                    from app.services.voice_memo_execution import execute
                    bridge = getattr(self.executor, 'speech_client', None)
                    with self.sessions() as speech_db:
                        saved = speech_db.get(ToolAction, action_id)
                        review = self.execution_review(speech_db, saved)
                    meta, audio = await execute(bridge, action, ref, prepared, review, cancel,
                                                lambda: self._active(action_id, ref))
                    return ActionResult('completed', 'The Owner already approved this action once. The voice memo file was created. No approval is pending for this action. This action did not start automatic playback; the Owner can choose Play.', voice_memo_data=(meta, audio))
                task = asyncio.create_task(budget.run(invoke_speech()))
            elif action.family == 'mcp_tool_call':
                shared_revoke_attempted=False;shared_revoke_ok=False
                async def invoke_mcp():
                    nonlocal shared_revoke_attempted,shared_revoke_ok
                    from app.services import mcp_registry
                    bridge=getattr(self,'mcp_client',None)
                    if (bridge is None or getattr(bridge,'available',False) is not True
                            or not isinstance(getattr(bridge,'core_epoch',None),str)
                            or not re.fullmatch(r'[a-f0-9]{64}',bridge.core_epoch)
                            or ref.scope!='once' or ref.grant_id is None):
                        return ActionResult('denied','MCP connection is unavailable.',safe_text='MCP connection is unavailable.')
                    shared_prepared=bool(isinstance(prepared.mcp_binding,dict) and prepared.mcp_binding.get('shared_recipient_binding'))
                    guard=self.shared_app_state.owner_recovery_lock if shared_prepared and self.shared_app_state is not None else nullcontext()
                    with guard, self.sessions() as mcp_db:
                        saved=mcp_db.get(ToolAction,action_id);proposer=self._proposers.get(action_id)
                        original=self.execution_review(mcp_db,saved)
                        if self._prepare_retained(mcp_db,saved,prepared,proposer.owner_id) != prepared:
                            raise ValueError('MCP origin changed before issue')
                        proof=prepared.mcp_binding
                        if not proposer or not proposer.owner_epoch or not re.fullmatch(r'[a-f0-9]{64}',proposer.owner_epoch):
                            return ActionResult('denied','MCP Owner binding unavailable.',safe_text='MCP Owner binding unavailable.')
                        keys=('server_id','config_revision','binding_revision','tool_id','tool_name','schema_sha256')
                        server,tool=mcp_registry.current_tool(mcp_db,proposer.owner_id,action.agent_id,{k:proof[k] for k in keys})
                        payload={'version':1,'binding':{**{k:proof[k] for k in ('owner_id','agent_id','root_event_id','generation','server_id','config_revision','binding_revision','tool_id','tool_name','schema_sha256','input_sha256')},
                            'action_id':action_id,'review_id':ref.review_id,'grant_id':ref.grant_id,'owner_epoch':proposer.owner_epoch,'core_epoch':bridge.core_epoch},
                            'transport_sha256':proof['transport_sha256'],'schema':tool.input_schema,'arguments':prepared.raw_arguments,
                            'schema_canonical':tool.schema_canonical,'arguments_canonical':mcp_registry.canonical(prepared.raw_arguments),
                            'review':snapshot_value(original),'review_canonical':mcp_registry.canonical(snapshot_value(original)),
                            'action_payload_canonical':mcp_registry.canonical({'binding':proof,'arguments':prepared.raw_arguments})}
                        if saved.execution_binding is not None:
                            payload['binding']['execution_binding']=saved.execution_binding
                        if proof.get('teammate_binding') is not None:
                            payload['binding']['teammate_binding']=proof['teammate_binding']
                        payload['binding']['payload_digest']=prepared.payload_digest
                        payload['binding']['review_sha256']=mcp_registry.digest(snapshot_value(original))
                    shared=proof.get('shared_recipient_binding')
                    if shared:
                        from app.services import shared_connector
                        from app.models.shared_connector import SharedConnectorRequest
                        if self.shared_app_state is None:raise ReviewChanged('Shared recipient unavailable')
                        with self.shared_app_state.owner_recovery_lock:
                            with self.sessions() as consent_db:
                                try:
                                    consent_db.execute(sql_text('BEGIN IMMEDIATE'))
                                    row=shared_connector.require_review_binding(consent_db,self.shared_app_state,shared,{k:proof[k] for k in keys})
                                    if row.state!='allowed' or row.action_id!=action_id:raise ReviewChanged('Shared account consent changed')
                                    row.state='consumed';consent_db.commit()
                                except BaseException:consent_db.rollback();raise
                        full_shared={**shared,'action_id':action_id,'review_id':ref.review_id,'grant_id':ref.grant_id}
                        payload['binding']['shared_recipient_binding']=full_shared
                    try:
                        if shared:
                            shared_revoke_attempted=True
                            authorization=await bridge.shared_authorize({'version':1,'binding':full_shared})
                            if not isinstance(authorization,dict) or authorization.get('state')!='authorized':
                                return ActionResult('denied','Shared account authorization unavailable.',safe_text='Shared account authorization unavailable.')
                            if cancel.is_set() or not await asyncio.to_thread(self._active,action_id,ref):
                                return ActionResult('cancelled','Shared account authorization changed.',safe_text='Shared account authorization changed.')
                        issued=await bridge.issue(payload)
                        nonce=issued.get('nonce') if isinstance(issued,dict) and issued.get('state')=='issued' else None
                        if not isinstance(nonce,str) or not re.fullmatch(r'[a-f0-9]{64}',nonce):
                            return ActionResult('denied','MCP approval could not be bound.',safe_text='MCP approval could not be bound.')
                        if cancel.is_set() or not self._active(action_id,ref):
                            await bridge.cancel({'version':1,'owner_id':proof['owner_id'],'owner_epoch':proposer.owner_epoch,
                                'core_epoch':bridge.core_epoch,'server_id':proof['server_id'],
                                'root_event_id':proof['root_event_id'],'generation':proof['generation']})
                            return ActionResult('cancelled','MCP action stopped before start.',safe_text='MCP action stopped before start.')
                        response=await bridge.start({**payload,'nonce':nonce})
                    except BaseException:
                        try:await bridge.cancel({'version':1,'owner_id':proof['owner_id'],'owner_epoch':proposer.owner_epoch,
                            'core_epoch':bridge.core_epoch,'server_id':proof['server_id'],
                            'root_event_id':proof['root_event_id'],'generation':proof['generation']})
                        except Exception:pass
                        raise
                    finally:
                        if shared:
                            try:
                                revoked=await asyncio.wait_for(asyncio.shield(bridge.shared_revoke({'version':1,'owner_id':shared['owner_id'],'owner_epoch':shared['owner_epoch'],
                                    'core_epoch':shared['core_epoch'],'authority_id':shared['authority_id'],'session_generation':shared['session_generation']})),3)
                                shared_revoke_ok=isinstance(revoked,dict) and revoked.get('state')=='revoked'
                                if not shared_revoke_ok:raise ReviewChanged('Shared authority revocation unconfirmed')
                            except Exception:raise ReviewChanged('Shared authority revocation unconfirmed') from None
                    state=response.get('state') if isinstance(response,dict) else None
                    if state not in ('completed','failed','cancelled','uncertain'):state='uncertain'
                    result=response.get('result') if isinstance(response,dict) else None
                    text=json.dumps(result,ensure_ascii=False,allow_nan=False)[:16000] if result is not None else 'MCP action '+state
                    return ActionResult(state,text,safe_text=text)
                task = asyncio.create_task(budget.run(invoke_mcp()))
            else:
                task = asyncio.create_task(budget.run(self.executor(action, ref, cancel)))
            try:
                shared_check=bool(isinstance(prepared.mcp_binding,dict) and prepared.mcp_binding.get('shared_recipient_binding'))
                last_shared_check=0.0;shared_active=True
                while not task.done():
                    if shared_check and monotonic()-last_shared_check>=1.0:
                        shared_active=await asyncio.to_thread(self._active,action_id,ref);last_shared_check=monotonic()
                    if cancel.is_set() or (not shared_active if shared_check else not self._active(action_id,ref)):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        self._finish(action_id, "uncertain", "Action stopped after start; effect is unknown.")
                        return ActionResult("uncertain", "Action stopped after start; effect is unknown.",
                                            budget.consumed_seconds, "Action stopped after start; effect is unknown.")
                    await asyncio.sleep(.05)
                result = await task
                if cancel.is_set() or not self._active(action_id, ref):
                    self._finish(action_id, "uncertain", "Action stopped after start; effect is unknown.")
                    return ActionResult("uncertain", "Action stopped after start; effect is unknown.",
                                        budget.consumed_seconds, "Action stopped after start; effect is unknown.")
                if isinstance(result, ActionResult):
                    if action.family in {"screenshot", "browser_screenshot"} and result.state == "completed":
                        expected_ref = f"rooms/{action.room_id}/screenshots/{action.action_id}.png"
                        if (result.screenshot_ref != expected_ref or result.image_mime != "image/png"
                                or not result.image_bytes or result.image_bytes[:8] != b"\x89PNG\r\n\x1a\n"
                                or not isinstance(result.image_width, int) or not isinstance(result.image_height, int)
                                or result.image_width < 1 or result.image_height < 1):
                            raise ValueError("Screenshot observation is not room-owned")
                    safe = stored_result(action, result.text, result.sensitivity)
                    outcome = ActionResult(result.state, result.text, max(result.executor_seconds, budget.consumed_seconds),
                                           safe,
                                           result.screenshot_ref, result.image_bytes, result.image_mime,
                                           result.image_width, result.image_height, result.sensitivity,
                                           result.error_code, result.diagnostic, result.file_postconditions, result.voice_memo_data)
                else:
                    raw_text = str(result)[:16000]
                    outcome = ActionResult("completed", raw_text, budget.consumed_seconds,
                                           stored_result(action, raw_text))
                if outcome.state not in ("completed", "failed", "cancelled", "uncertain"):
                    raise ValueError("Invalid executor result")
                event = self._finish(action_id, outcome.state, outcome.text,
                             screenshot_ref=outcome.screenshot_ref, sensitivity=outcome.sensitivity,
                             image_mime=outcome.image_mime, image_width=outcome.image_width,
                             image_height=outcome.image_height, error_code=outcome.error_code,
                             diagnostic=outcome.diagnostic, ref=ref,
                             file_postconditions=outcome.file_postconditions,
                             write_payload=prepared.raw_arguments if action.family == "write_file" else None,
                             voice_memo_data=outcome.voice_memo_data)
                if event is None:
                    return replace(outcome, state="uncertain", text="Action result could not be recorded.",
                                   safe_text="Action result could not be recorded.", file_postconditions=None)
                if event.payload.get("state") != outcome.state:
                    return replace(outcome, state=event.payload["state"], text=event.payload["text"],
                                   safe_text=("Owner-edited action result is available to the Owner."
                                              if event.payload.get('owner_edited') is True else event.payload["text"]), file_postconditions=None)
                if event.payload.get('owner_edited') is True:
                    outcome=replace(outcome,safe_text='Owner-edited action result is available to the Owner.')
                if outcome.voice_memo_data is not None: outcome = replace(outcome, voice_memo_data=None)
                return outcome
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            self._finish(action_id, "uncertain" if started else "cancelled", "Action interrupted.")
            raise
        except LLMFailure:
            self._finish(action_id, "uncertain" if started else "cancelled", "Executor time budget exhausted.")
            raise
        except ReviewChanged:
            state = "uncertain" if started else "denied"
            self._finish(action_id, state, "Action review changed; a new review is required.",
                         error_code="review_changed")
            return ActionResult(state, "Action review changed; a new review is required.",
                                error_code="review_changed")
        except Exception as error:
            diagnostic = make_diagnostic(error, stage="tool_execution", failure_code="unexpected_error",
                                         room_id=action.room_id, agent_id=action.agent_id, connection_id=None,
                                         trigger_event_id=action.trigger_event_id, action_id=action_id)
            self._finish(action_id, "uncertain" if started else "failed", "Action failed.",
                         diagnostic=diagnostic)
            return ActionResult("uncertain" if started else "failed", "Action failed.",
                                budget.consumed_seconds, "Action failed.", diagnostic=diagnostic)
        finally:
            if action.family=='mcp_tool_call' and isinstance(prepared.mcp_binding,dict) and prepared.mcp_binding.get('shared_recipient_binding') and self.shared_app_state is not None:
                from app.models.shared_connector import SharedConnectorRequest,SharedConnectorLink
                with self.shared_app_state.owner_recovery_lock:
                    with self.sessions() as shared_db:
                        try:
                            shared_db.execute(sql_text('BEGIN IMMEDIATE'))
                            row=shared_db.query(SharedConnectorRequest).filter_by(action_id=action_id).first()
                            if row and row.state in ('allowed','consumed'):
                                confirmed=(not locals().get('shared_revoke_attempted',False) or locals().get('shared_revoke_ok',False))
                                row.state='revoked' if confirmed else 'uncertain'
                                if row.state=='uncertain':
                                    link=shared_db.get(SharedConnectorLink,row.link_id)
                                    if link:link.state='uncertain'
                            shared_db.commit()
                        except Exception:shared_db.rollback()
            with self.sessions() as db:
                saved = db.get(ToolAction, action_id)
                if saved and saved.execution_binding:
                    from app.services.execution_scopes import settle_executor
                    settle_executor(db, saved, budget.consumed_seconds, uncertain=saved.state == 'uncertain')
                    db.commit()
