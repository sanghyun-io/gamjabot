"""Private owner-authority and provider-auth client for the inherited Core FD."""

from __future__ import annotations

import json
import re
import socket
import struct
import time
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Mapping

from app.services.secrets import consume_inherited_socket


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_ORDINARY_REFERENCE = re.compile(
    r"[1-9][0-9]*:[1-9][0-9]*:(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
)
_KINDS = frozenset(("auth", "disconnect", "grant_replace", "same_grant_refresh"))
_FRAME_TYPES = frozenset((
    "auth_start", "auth_status", "auth_cancel", "credential_prepare",
    "credential_commit", "credential_discard", "credential_refresh",
))
_DISCARD_ACTIONS = frozenset((
    "inspect", "restore_old", "discard_prepared", "cleanup_committed",
    "acknowledge_terminal",
))
_AUTH_STATES = frozenset((
    "prepared", "awaiting_user", "polling", "committing", "recovering",
    "restored", "succeeded", "cancelled", "expired", "interrupted", "failed",
))
_FINITE_CODES = frozenset((
    "owner_mismatch", "owner_epoch_unavailable", "owner_epoch_changed",
    "configuration_changed", "auth_method_unavailable", "callback_mismatch",
    "device_slow_down", "authorization_expired", "credential_store_unavailable",
    "credential_recovery_required", "credential_cleanup_unconfirmed",
    "operation_cancelled", "operation_interrupted", "internal_error",
))
_MATERIAL_STATES = frozenset(("recovery", "prepared", "recovery_and_prepared", "none"))
_RECOVERY_STATES = frozenset(("recovering", "restored", "cleaned", "absent", "failed"))
_NATIVE_ENROLLMENT_FIELDS = {
    "azure_cli": ("azure-openai", frozenset(("provider", "resource_endpoint", "api_version"))),
    "aws_profile": ("amazon-bedrock", frozenset(("provider", "profile_name", "region"))),
    "vertex_adc": ("google-vertex", frozenset(("provider", "project_id", "location"))),
}


class CoreAuthBrokerUnavailable(Exception):
    """The private attachment is invalid or its request result is ambiguous."""


@dataclass(frozen=True)
class AuthPublicState:
    """Closed result that may cross an API, CLI, or GUI boundary."""

    version: int
    operation_id: str
    state: str
    code: str | None = None
    authorization_url: str | None = None
    verification_uri: str | None = None
    user_code: str | None = None
    poll_interval: int | None = None

    def safe_dict(self) -> dict:
        return {key: value for key, value in (
            ("version", self.version),
            ("operation_id", self.operation_id),
            ("state", self.state),
            ("code", self.code),
            ("authorization_url", self.authorization_url),
            ("verification_uri", self.verification_uri),
            ("user_code", self.user_code),
            ("poll_interval", self.poll_interval),
        ) if value is not None}


@dataclass(frozen=True)
class RecoveryPublicState:
    """Closed recovery result without the private binding fingerprint."""

    operation_id: str
    state: str
    material_state: str
    receipt_state: str
    code: str | None = None

    def safe_dict(self) -> dict:
        result = {
            "operation_id": self.operation_id,
            "state": self.state,
            "material_state": self.material_state,
            "receipt_state": self.receipt_state,
        }
        if self.code is not None:
            result["code"] = self.code
        return result


class OwnerBinding:
    """Private Core authority that cannot be encoded or rendered with its values."""

    __slots__ = ("_owner_id", "_owner_epoch")

    def __init__(self, owner_id: int, owner_epoch: str):
        object.__setattr__(self, "_owner_id", owner_id)
        object.__setattr__(self, "_owner_epoch", owner_epoch)

    def __setattr__(self, _name, _value):
        raise AttributeError("OwnerBinding is immutable")

    @property
    def owner_id(self) -> int:
        return self._owner_id

    @property
    def owner_epoch(self) -> str:
        return self._owner_epoch

    def __eq__(self, other) -> bool:
        return (isinstance(other, OwnerBinding)
                and self.owner_id == other.owner_id
                and self.owner_epoch == other.owner_epoch)

    def __hash__(self) -> int:
        return hash((self.owner_id, self.owner_epoch))

    def __repr__(self) -> str:
        return "<OwnerBinding private>"


class AuthPrivateReply:
    """Private credential envelope with an explicit public projection."""

    __slots__ = ("_public", "_prepared_ref", "_commit_handle")

    def __init__(self, public: AuthPublicState, prepared_ref: str | None = None,
                 commit_handle: str | None = None):
        object.__setattr__(self, "_public", public)
        object.__setattr__(self, "_prepared_ref", prepared_ref)
        object.__setattr__(self, "_commit_handle", commit_handle)

    def __setattr__(self, _name, _value):
        raise AttributeError("AuthPrivateReply is immutable")

    @property
    def public(self) -> AuthPublicState:
        return self._public

    @property
    def prepared_ref(self) -> str | None:
        return self._prepared_ref

    @property
    def commit_handle(self) -> str | None:
        return self._commit_handle

    def safe_projection(self) -> AuthPublicState:
        return self.public

    def __repr__(self) -> str:
        return "<AuthPrivateReply private>"


class RecoveryBindingSummary:
    """Core-verified private authority for exact DB post-state checks."""

    __slots__ = (
        "_provider_id", "_auth_method_id", "_config_revision",
        "_auth_grant_revision", "_public_config_fingerprint",
    )

    def __init__(self, *, provider_id: str, auth_method_id: str,
                 config_revision: int, auth_grant_revision: int,
                 public_config_fingerprint: str):
        object.__setattr__(self, "_provider_id", provider_id)
        object.__setattr__(self, "_auth_method_id", auth_method_id)
        object.__setattr__(self, "_config_revision", config_revision)
        object.__setattr__(self, "_auth_grant_revision", auth_grant_revision)
        object.__setattr__(self, "_public_config_fingerprint", public_config_fingerprint)

    def __setattr__(self, _name, _value):
        raise AttributeError("RecoveryBindingSummary is immutable")

    provider_id = property(lambda self: self._provider_id)
    auth_method_id = property(lambda self: self._auth_method_id)
    config_revision = property(lambda self: self._config_revision)
    auth_grant_revision = property(lambda self: self._auth_grant_revision)
    public_config_fingerprint = property(lambda self: self._public_config_fingerprint)

    def __repr__(self) -> str:
        return "<RecoveryBindingSummary private>"


class RecoveryInspectReply:
    """Private selector envelope with an explicit public projection."""

    __slots__ = ("_public", "_binding_fingerprint", "_binding_summary")

    def __init__(self, operation_id: str, state: str, material_state: str,
                 receipt_state: str, binding_fingerprint: str | None = None,
                 binding_summary=None,
                 code: str | None = None):
        object.__setattr__(self, "_public", RecoveryPublicState(
            operation_id=operation_id,
            state=state,
            material_state=material_state,
            receipt_state=receipt_state,
            code=code,
        ))
        object.__setattr__(self, "_binding_fingerprint", binding_fingerprint)
        object.__setattr__(self, "_binding_summary", binding_summary)

    def __setattr__(self, _name, _value):
        raise AttributeError("RecoveryInspectReply is immutable")

    @property
    def operation_id(self) -> str:
        return self._public.operation_id

    @property
    def state(self) -> str:
        return self._public.state

    @property
    def material_state(self) -> str:
        return self._public.material_state

    @property
    def receipt_state(self) -> str:
        return self._public.receipt_state

    @property
    def binding_fingerprint(self) -> str | None:
        return self._binding_fingerprint

    @property
    def binding_summary(self):
        return self._binding_summary

    @property
    def code(self) -> str | None:
        return self._public.code

    def safe_projection(self) -> RecoveryPublicState:
        return self._public

    def safe_dict(self) -> dict:
        return self.safe_projection().safe_dict()

    def __repr__(self) -> str:
        return "<RecoveryInspectReply private>"


class CoreAuthBrokerClient:
    _MAX_FRAME = 65_536
    _TIMEOUT_SECONDS = 5.0

    def __init__(self, channel: socket.socket):
        self._channel = channel
        self._channel.settimeout(self._TIMEOUT_SECONDS)
        self._lock = RLock()
        self._failed = False
        self._poison_callback: Callable[[], None] | None = None
        try:
            handshake = self._read_message(time.monotonic() + self._TIMEOUT_SECONDS)
            if (not isinstance(handshake, dict)
                    or set(handshake) != {"version", "nonce", "current_generation_fingerprint"}
                    or not _version_one(handshake.get("version"))
                    or not _hex64(handshake.get("nonce"))
                    or not _hex64(handshake.get("current_generation_fingerprint"))):
                raise ValueError
            self._generation = {
                "nonce": handshake["nonce"],
                "current_generation_fingerprint": handshake["current_generation_fingerprint"],
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError, struct.error):
            self._poison()
            raise CoreAuthBrokerUnavailable("auth_broker_handshake_invalid") from None

    @classmethod
    def from_fd(cls, descriptor: int) -> "CoreAuthBrokerClient":
        try:
            return cls(consume_inherited_socket(descriptor))
        except CoreAuthBrokerUnavailable:
            raise
        except OSError:
            raise CoreAuthBrokerUnavailable("auth_broker_channel_unavailable") from None

    @property
    def available(self) -> bool:
        with self._lock:
            return not self._failed

    @property
    def current_generation_fingerprint(self) -> str:
        """Return only the private generation hash from the accepted handshake."""
        with self._lock:
            if self._failed:
                raise CoreAuthBrokerUnavailable("auth_broker_channel_unavailable")
            return self._generation["current_generation_fingerprint"]

    def set_poison_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._poison_callback = callback
            failed = self._failed
        if failed:
            callback()

    def owner_bind(self, owner_id: int) -> OwnerBinding:
        if not _positive_u64(owner_id):
            raise ValueError("owner_id")
        response = self._exchange({"type": "owner_bind", "version": 1, "owner_id": owner_id})
        if (not isinstance(response, dict)
                or set(response) != {"version", "state", "owner_id", "current_owner_epoch"}
                or not _version_one(response.get("version")) or response.get("state") != "bound"
                or not _positive_u64(response.get("owner_id"))
                or response.get("owner_id") != owner_id
                or not _hex64(response.get("current_owner_epoch"))):
            self._reject("auth_broker_response_invalid")
        return OwnerBinding(owner_id=owner_id, owner_epoch=response["current_owner_epoch"])

    def operation_allocate(self, owner_id: int, current_owner_epoch: str) -> str:
        """Ask Core RNG for an opaque operation ID under the exact owner bind."""
        if not _positive_u64(owner_id) or not _hex64(current_owner_epoch):
            raise ValueError("owner binding")
        response = self._exchange({
            "type": "operation_allocate", "version": 1,
            "owner_id": owner_id, "current_owner_epoch": current_owner_epoch,
        })
        if (not isinstance(response, dict)
                or set(response) != {"version", "state", "operation_id"}
                or not _version_one(response.get("version"))
                or response.get("state") != "allocated"
                or not _hex64(response.get("operation_id"))):
            self._reject("auth_broker_response_invalid")
        return response["operation_id"]

    def vault_realm_bind(self, owner_id: int, current_owner_epoch: str,
                         vault_account_id: str) -> bool:
        """Attest non-secret account identity over the inherited Engine pipe."""
        if (not _positive_u64(owner_id) or not _hex64(current_owner_epoch)
                or not isinstance(vault_account_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", vault_account_id) is None):
            raise ValueError("vault account binding")
        response = self._exchange({
            "type": "vault_realm_bind", "version": 1, "owner_id": owner_id,
            "current_owner_epoch": current_owner_epoch,
            "vault_account_id": vault_account_id,
        })
        if (not isinstance(response, dict) or set(response) != {"version", "state"}
                or not _version_one(response.get("version"))
                or response.get("state") not in {"bound", "unavailable"}):
            return False
        # An unavailable Vault is a valid reply; it must not poison the shared
        # provider/authentication channel or rotate the existing Owner epoch.
        return response["state"] == "bound"

    def vault_metadata_status(self, owner_id: int, current_owner_epoch: str,
                              item_id: str, website: str, username: str) -> dict:
        """Read only non-secret status from Core; values never cross this pipe."""
        unavailable = {"version": 1, "status": "unavailable", "revision": None, "command_id": None}
        if (not _positive_u64(owner_id) or not _hex64(current_owner_epoch)
                or not isinstance(item_id, str) or re.fullmatch(r"[0-9a-f]{32}", item_id) is None
                or not isinstance(website, str) or not website or len(website.encode("utf-8")) > 4096
                or not isinstance(username, str) or len(username.encode("utf-8")) > 512):
            return unavailable
        response = self._exchange({
            "type": "vault_metadata_status", "version": 1,
            "owner_id": owner_id, "current_owner_epoch": current_owner_epoch,
            "item_id": item_id, "website": website, "username": username,
        })
        if (not isinstance(response, dict)
                or set(response) != {"version", "status", "revision", "command_id"}
                or not _version_one(response.get("version"))
                or response.get("status") not in {
                    "present", "missing", "deleted", "metadata_mismatch", "unavailable"
                }):
            return unavailable
        revision, command = response["revision"], response["command_id"]
        if response["status"] in {"missing", "unavailable"}:
            if revision is not None or command is not None:
                return unavailable
        elif (not _positive_u64(revision) or not isinstance(command, str)
              or re.fullmatch(r"[0-9a-f]{32}", command) is None):
            return unavailable
        # A malformed Vault DTO cannot become a secret/value response and does
        # not invalidate unrelated provider/auth operations on the shared pipe.
        return dict(response)

    def vault_card_metadata_status(self, owner_id: int, current_owner_epoch: str,
                                   item_id: str, cardholder: str, expiry_month: int,
                                   expiry_year: int, billing: dict, last4: str | None) -> dict:
        """Read a derived card mask and status, never a payment credential."""
        unavailable = {"version": 1, "status": "unavailable", "revision": None,
                       "command_id": None, "last4": None}
        fields = {"address_line1", "address_line2", "city", "region", "postal_code", "country"}
        if (not _positive_u64(owner_id) or not _hex64(current_owner_epoch)
                or not isinstance(item_id, str) or re.fullmatch(r"[0-9a-f]{32}", item_id) is None
                or not isinstance(cardholder, str) or not cardholder.strip()
                or type(expiry_month) is not int or not 1 <= expiry_month <= 12
                or type(expiry_year) is not int or not 2000 <= expiry_year <= 9999
                or not isinstance(billing, dict) or set(billing) != fields
                or any(not isinstance(value, str) for value in billing.values())
                or (last4 is not None and (not isinstance(last4, str)
                                          or re.fullmatch(r"[0-9]{4}", last4) is None))):
            return unavailable
        try:
            if len(cardholder.encode("utf-8")) > 200 or any(len(v.encode("utf-8")) > 512 for v in billing.values()):
                return unavailable
        except UnicodeError:
            return unavailable
        response = self._exchange({
            "type": "vault_card_metadata_status", "version": 1,
            "owner_id": owner_id, "current_owner_epoch": current_owner_epoch,
            "item_id": item_id, "cardholder": cardholder,
            "expiry_month": expiry_month, "expiry_year": expiry_year,
            "billing": dict(billing), "last4": last4,
        })
        if (not isinstance(response, dict)
                or set(response) != {"version", "status", "revision", "command_id", "last4"}
                or not _version_one(response.get("version"))
                or response.get("status") not in {"present", "missing", "deleted", "metadata_mismatch", "unavailable"}):
            return unavailable
        revision, command, mask = response["revision"], response["command_id"], response["last4"]
        if response["status"] in {"missing", "unavailable"}:
            if revision is not None or command is not None or mask is not None:
                return unavailable
        else:
            if (not _positive_u64(revision) or not isinstance(command, str)
                    or re.fullmatch(r"[0-9a-f]{32}", command) is None):
                return unavailable
            if response["status"] == "present":
                if not isinstance(mask, str) or re.fullmatch(r"[0-9]{4}", mask) is None:
                    return unavailable
            elif mask is not None:
                return unavailable
        return dict(response)

    def vault_profile_prepare(self, owner_id: int, current_owner_epoch: str, grant: dict,
                              profile_metadata_sha256: str, record_sha256: str,
                              proxy_port: int, proxy_secret: str) -> dict:
        handoff_id=grant.get("handoff_id") if isinstance(grant,dict) else None
        unavailable={"version":1,"status":"unavailable","handoff_id":handoff_id}
        if (not _positive_u64(owner_id) or not _hex64(current_owner_epoch)
                or not isinstance(handoff_id,str) or re.fullmatch(r"[0-9a-f]{32}",handoff_id) is None
                or not _hex64(profile_metadata_sha256) or not _hex64(record_sha256)
                or type(proxy_port) is not int or not 1<=proxy_port<=65535
                or not isinstance(proxy_secret,str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}",proxy_secret) is None):return unavailable
        response=self._exchange({"type":"vault_profile_prepare","version":1,"owner_id":owner_id,
            "current_owner_epoch":current_owner_epoch,"grant":grant,"profile_metadata_sha256":profile_metadata_sha256,
            "record_sha256":record_sha256,"proxy_port":proxy_port,"proxy_secret":proxy_secret})
        if (not isinstance(response,dict) or set(response)!={"version","status","handoff_id"}
                or not _version_one(response.get("version")) or response.get("handoff_id")!=handoff_id
                or response.get("status") not in ("prepared","unavailable")):return unavailable
        return response

    def vault_custody(self, operation: str, owner_id: int, current_owner_epoch: str, payload: dict) -> dict:
        data=payload if isinstance(payload,dict) else {};handoff_id=data.get('handoff_id')
        fallback=({'version':1,'status':'unavailable','handoff_id':handoff_id,'custody_id':None}
            if operation=='seal' else {'version':1,'status':'unavailable','handoff_id':handoff_id,'profile_id':None,'profile_revision':None,'custody_id':None,'review_context_id':None})
        if operation=='capture_chunk':fallback={'version':1,'status':'unavailable','capture_id':data.get('capture_id'),'index':data.get('index'),'chunks':0,'data':''}
        if operation=='capture_release':fallback={'version':1,'status':'unavailable','capture_id':data.get('capture_id')}
        if operation in ('borrow','call_start','call_poll'):
            fallback={'version':1,'status':'unknown','borrow_id':data.get('borrow_id'),'action_id':data.get('action_id'),'text':''} if operation!='borrow' else {'version':1,'status':'unavailable','handoff_id':handoff_id,'custody_id':None,'borrow_id':None,'context_id':None}
        fields={
            'seal':{'handoff_id','profile_id','profile_revision','profile_metadata_sha256','record_sha256'},
            'status':{'handoff_id'},
            'borrow':{'handoff_id','profile_id','profile_revision','profile_metadata_sha256','record_sha256','custody_id','borrow_id','review_context_id','workflow_id','room_id','agent_id','action_id','review_id','root_request_event_id','run_generation','execution_binding_sha256','execution_binding','context_mode','origin','scope','operation','url','selector','url_sha256','payload_digest','call_id','expires_at'},
            'call_start':{'handoff_id','custody_id','borrow_id','action_id','operation','url','selector','url_sha256'},
            'call_poll':{'handoff_id','custody_id','borrow_id','action_id'},
            'capture_chunk':{'handoff_id','custody_id','borrow_id','action_id','context_id','capture_id','index'},
            'capture_release':{'handoff_id','custody_id','borrow_id','action_id','context_id','capture_id'},
            'end':{'handoff_id','custody_id','borrow_id','context_id'},
            'park':{'handoff_id','custody_id','context_id','room_id','agent_id','root_request_event_id','run_generation','workflow_id'}}
        if (operation not in fields or not isinstance(payload,dict) or set(payload)!=fields[operation] or not _positive_u64(owner_id)
                or not _hex64(current_owner_epoch) or not isinstance(handoff_id,str)
                or re.fullmatch(r'[0-9a-f]{32}',handoff_id) is None):return fallback
        if operation in ('capture_chunk','capture_release'):
            if (any(not isinstance(payload.get(k),str) or re.fullmatch(r'[0-9a-f]{32}',payload[k]) is None
                    for k in ('custody_id','borrow_id','context_id','capture_id'))
                    or not isinstance(payload.get('action_id'),str)
                    or (operation=='capture_chunk' and (type(payload.get('index')) is not int or not 0<=payload['index']<417))):return fallback
        response=self._exchange({'type':'vault_custody_'+operation,'version':1,'owner_id':owner_id,
                                 'current_owner_epoch':current_owner_epoch,**payload})
        if not isinstance(response,dict) or set(response)!=set(fallback) or not _version_one(response.get('version')):return fallback
        if operation=='seal':
            if response.get('handoff_id')!=handoff_id or response.get('status') not in ('sealed','unavailable'):return fallback
            if response['status']=='sealed' and (not isinstance(response.get('custody_id'),str) or re.fullmatch(r'[0-9a-f]{32}',response['custody_id']) is None):return fallback
            return response if response['status']=='sealed' else fallback
        if operation=='borrow':
            if response.get('handoff_id')!=handoff_id or response.get('status') not in ('borrowed','changed','unavailable'):return fallback
            if response['status']=='borrowed':
                if (response.get('custody_id')!=payload.get('custody_id') or response.get('borrow_id')!=payload.get('borrow_id')
                        or not isinstance(response.get('context_id'),str) or re.fullmatch(r'[0-9a-f]{32}',response['context_id']) is None
                        or response['context_id']!=payload.get('review_context_id')):return fallback
            elif response.get('custody_id') is not None or response.get('borrow_id') is not None or response.get('context_id') is not None:return fallback
            return response
        if operation in ('call_start','call_poll'):
            try:
                if (response.get('borrow_id')!=payload.get('borrow_id') or response.get('action_id')!=payload.get('action_id')
                        or response.get('status') not in (('pending','failed','unknown') if operation=='call_start' else ('pending','completed','failed','unknown')) or not isinstance(response.get('text'),str)
                        or len(response['text'].encode('utf-8'))>12000 or response['status']!='completed' and response['text']!=''):return fallback
            except UnicodeError:return fallback
            return response
        if operation=='capture_chunk':
            if (response.get('capture_id')!=payload.get('capture_id') or type(response.get('index')) is not int or response.get('index')!=payload.get('index')
                    or response.get('status')!='ok' or type(response.get('chunks')) is not int
                    or not 1<=response['chunks']<=417 or not isinstance(response.get('data'),str)
                    or len(response['data'])>32000):return fallback
            return response
        if operation=='capture_release':
            return response if response.get('capture_id')==payload.get('capture_id') and response.get('status')=='released' else fallback
        if response.get('handoff_id')!=handoff_id or response.get('status') not in ('parked','borrowed','unknown','unavailable'):return fallback
        if response['status']=='unavailable':return fallback
        if (not isinstance(response.get('profile_id'),str) or re.fullmatch(r'[0-9a-f]{32}',response['profile_id']) is None
                or not _positive_u64(response.get('profile_revision')) or not isinstance(response.get('custody_id'),str)
                or re.fullmatch(r'[0-9a-f]{32}',response['custody_id']) is None):return fallback
        if response['status'] in ('parked','borrowed'):
            if not isinstance(response.get('review_context_id'),str) or re.fullmatch(r'[0-9a-f]{32}',response['review_context_id']) is None:return fallback
        elif response.get('review_context_id') is not None:return fallback
        return response

    def vault_profile_query(self, operation: str, owner_id: int, current_owner_epoch: str, handoff_id: str) -> dict:
        unavailable={"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":None,"profile_revision":None,"fill_status":"unknown"}
        if (operation not in ("vault_profile_status","vault_profile_revoke") or not _positive_u64(owner_id)
                or not _hex64(current_owner_epoch) or not isinstance(handoff_id,str)
                or re.fullmatch(r"[0-9a-f]{32}",handoff_id) is None):return unavailable
        response=self._exchange({"type":operation,"version":1,"owner_id":owner_id,"current_owner_epoch":current_owner_epoch,"handoff_id":handoff_id})
        if (not isinstance(response,dict) or set(response)!=set(unavailable) or not _version_one(response.get("version"))
                or response.get("handoff_id")!=handoff_id or response.get("status") not in ("prepared","active","closed","unknown","unavailable")
                or response.get("fill_status") not in ("not_started","filled","filled_password_only","not_filled","unknown","cancelled")):return unavailable
        if response['status']=="unavailable":return unavailable
        if (not isinstance(response.get("profile_id"),str) or re.fullmatch(r"[0-9a-f]{32}",response['profile_id']) is None
                or not _positive_u64(response.get("profile_revision"))):return unavailable
        return response

    def vault_profile_recover(self, owner_id: int, current_owner_epoch: str, handoff_id: str,
                              profile_id: str, profile_metadata_sha256: str, record_sha256: str,
                              pendingjournal_sha256: str | None) -> dict:
        unavailable={"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":None,"profile_revision":None,"fill_status":"unknown"}
        if (not _positive_u64(owner_id) or not _hex64(current_owner_epoch)
                or not isinstance(handoff_id,str) or re.fullmatch(r"[0-9a-f]{32}",handoff_id) is None
                or not isinstance(profile_id,str) or re.fullmatch(r"[0-9a-f]{32}",profile_id) is None
                or not _hex64(profile_metadata_sha256) or not _hex64(record_sha256)
                or pendingjournal_sha256 is not None and not _hex64(pendingjournal_sha256)):return unavailable
        response=self._exchange({"type":"vault_profile_recover","version":1,"owner_id":owner_id,
            "current_owner_epoch":current_owner_epoch,"handoff_id":handoff_id,"profile_id":profile_id,
            "profile_metadata_sha256":profile_metadata_sha256,"record_sha256":record_sha256,
            "pendingjournal_sha256":pendingjournal_sha256})
        if (not isinstance(response,dict) or set(response)!=set(unavailable) or not _version_one(response.get("version"))
                or response.get('handoff_id')!=handoff_id or response.get('status')!='recovered'
                or response.get('profile_id')!=profile_id or not _positive_u64(response.get('profile_revision'))
                or response.get('fill_status')!='unknown'):return unavailable
        return response

    def vault_csv_bind(self, owner_id: int, current_owner_epoch: str,
                       draft_id: str, row: int, item_id: str, website: str,
                       username: str, reservation_command: str) -> dict:
        """Bind a committed metadata reservation to an in-memory Core CSV row."""
        unavailable = {"version": 1, "state": "unavailable"}
        identifiers = (draft_id, item_id, reservation_command)
        if (not _positive_u64(owner_id) or not _hex64(current_owner_epoch)
                or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None
                       for value in identifiers)
                or type(row) is not int or not 2 <= row <= 5001
                or not isinstance(website, str) or not website or len(website.encode("utf-8")) > 4096
                or not isinstance(username, str) or len(username.encode("utf-8")) > 512):
            return unavailable
        response = self._exchange({
            "type": "vault_csv_bind", "version": 1,
            "owner_id": owner_id, "current_owner_epoch": current_owner_epoch,
            "draft_id": draft_id, "row": row, "item_id": item_id,
            "website": website, "username": username,
            "reservation_command": reservation_command,
        })
        if (not isinstance(response, dict) or set(response) != {"version", "state"}
                or not _version_one(response.get("version"))
                or response.get("state") not in {"bound", "unavailable"}):
            return unavailable
        return {"version": 1, "state": response["state"]}

    def owner_unbind(self, expected: OwnerBinding) -> str:
        if (not isinstance(expected, OwnerBinding) or not _positive_u64(expected.owner_id)
                or not _hex64(expected.owner_epoch)):
            raise ValueError("expected owner binding")
        response = self._exchange({
            "type": "owner_unbind", "version": 1,
            "expected_owner_id": expected.owner_id,
            "expected_owner_epoch": expected.owner_epoch,
        })
        if (not isinstance(response, dict) or set(response) != {"version", "state"}
                or not _version_one(response.get("version"))
                or response.get("state") not in {"unbound", "already_unbound", "mismatch_preserved"}):
            self._reject("auth_broker_response_invalid")
        return response["state"]

    def auth_start(self, binding: Mapping,
                   native_enrollment: Mapping | None = None) -> AuthPublicState:
        validated = _stable_binding(binding)
        native = _native_enrollment(validated, native_enrollment)
        extra = {} if native is None else {"native_enrollment": native}
        return self._exact("auth_start", validated, extra).safe_projection()

    def auth_status(self, binding: Mapping) -> AuthPublicState:
        return self._exact("auth_status", binding).safe_projection()

    def auth_cancel(self, binding: Mapping) -> AuthPublicState:
        return self._exact("auth_cancel", binding).safe_projection()

    def credential_prepare(self, binding: Mapping,
                           exact_old_reference: str | None = None, *,
                           direct_secret: str | None = None) -> AuthPrivateReply:
        validated = _stable_binding(binding)
        extra = {}
        if exact_old_reference is not None:
            if not _ORDINARY_REFERENCE.fullmatch(exact_old_reference):
                raise ValueError("exact_old_reference")
            extra["exact_old_reference"] = exact_old_reference
        if direct_secret is not None:
            if (not isinstance(direct_secret, str) or not direct_secret
                    or validated["provider_id"] != "azure-openai"
                    or validated["auth_method_id"] != "api_key"
                    or validated["kind"] not in {"auth", "grant_replace"}
                    or (validated["kind"] == "auth") != (exact_old_reference is None)):
                raise ValueError("direct_secret")
            extra["direct_secret"] = direct_secret
        return self._exact("credential_prepare", validated, extra)

    def credential_commit(self, binding: Mapping) -> AuthPrivateReply:
        return self._exact("credential_commit", binding)

    def credential_refresh(self, binding: Mapping) -> AuthPrivateReply:
        return self._exact("credential_refresh", binding)

    def credential_discard_exact(self, binding: Mapping, action: str, *,
                                 commit_handle: str | None = None,
                                 adopted_prepared_ref: str | None = None) -> AuthPrivateReply:
        validated = _stable_binding(binding)
        request = self._discard_request(
            {"exact_binding": validated}, action, commit_handle, adopted_prepared_ref)
        return self._auth_reply(self._exchange(request), validated["operation_id"])

    def credential_discard_recovery(self, selector: Mapping, action: str, *,
                                    binding_fingerprint: str | None = None,
                                    commit_handle: str | None = None,
                                    adopted_prepared_ref: str | None = None) -> RecoveryInspectReply:
        validated = _recovery_selector(selector)
        if action == "inspect":
            if binding_fingerprint is not None:
                raise ValueError("inspect must not include binding_fingerprint")
        elif action in _DISCARD_ACTIONS:
            if not _hex64(binding_fingerprint):
                raise ValueError("binding_fingerprint")
        else:
            raise ValueError("discard action")
        target = {"recovery_selector": validated}
        if binding_fingerprint is not None:
            target["binding_fingerprint"] = binding_fingerprint
        request = self._discard_request(target, action, commit_handle, adopted_prepared_ref)
        return self._recovery_reply(self._exchange(request), validated["operation_id"])

    def close(self) -> None:
        self._poison()

    def _exact(self, frame_type: str, binding: Mapping,
               extra: Mapping | None = None) -> AuthPrivateReply:
        if frame_type not in _FRAME_TYPES or frame_type == "credential_discard":
            raise ValueError("auth frame type")
        validated = _stable_binding(binding)
        request = {"type": frame_type, "binding": validated,
                   "generation": dict(self._generation)}
        if extra:
            request.update(extra)
        return self._auth_reply(self._exchange(request), validated["operation_id"])

    def _discard_request(self, target: dict, action: str, commit_handle: str | None,
                         adopted_prepared_ref: str | None) -> dict:
        if action not in _DISCARD_ACTIONS:
            raise ValueError("discard action")
        request = {"type": "credential_discard", "target": target,
                   "generation": dict(self._generation), "action": action}
        if commit_handle is not None:
            if not _hex64(commit_handle):
                raise ValueError("commit_handle")
            request["commit_handle"] = commit_handle
        if adopted_prepared_ref is not None:
            if not _ORDINARY_REFERENCE.fullmatch(adopted_prepared_ref):
                raise ValueError("adopted_prepared_ref")
            request["adopted_prepared_ref"] = adopted_prepared_ref
        return request

    def _exchange(self, request: dict) -> dict:
        encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode()
        if not encoded or len(encoded) > self._MAX_FRAME:
            raise ValueError("auth frame size")
        with self._lock:
            if self._failed:
                raise CoreAuthBrokerUnavailable("auth_broker_channel_unavailable")
            deadline = time.monotonic() + self._TIMEOUT_SECONDS
            try:
                self._channel.settimeout(max(0.001, deadline - time.monotonic()))
                self._channel.sendall(struct.pack(">I", len(encoded)) + encoded)
                response = self._read_message(deadline)
                self._channel.settimeout(self._TIMEOUT_SECONDS)
            except (OSError, ValueError, TypeError, json.JSONDecodeError, struct.error):
                self._poison()
                raise CoreAuthBrokerUnavailable("auth_broker_channel_unavailable") from None
            if not isinstance(response, dict):
                self._reject("auth_broker_response_invalid")
            return response

    def _read_message(self, deadline: float):
        size = struct.unpack(">I", self._read_exact(4, deadline))[0]
        if size == 0 or size > self._MAX_FRAME:
            raise ValueError
        return json.loads(self._read_exact(size, deadline))

    def _read_exact(self, size: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError("auth broker timeout")
            self._channel.settimeout(remaining)
            chunk = self._channel.recv(size - len(result))
            if not chunk:
                raise OSError("auth broker EOF")
            result.extend(chunk)
        return bytes(result)

    def _auth_reply(self, response: dict, operation_id: str) -> AuthPrivateReply:
        allowed = {"version", "operation_id", "state", "code", "authorization_url",
                   "verification_uri", "user_code", "poll_interval",
                   "prepared_ref", "commit_handle"}
        required = {"version", "operation_id", "state"}
        if (set(response) - allowed or not required.issubset(response)
                or not _version_one(response.get("version"))
                or response.get("operation_id") != operation_id
                or response.get("state") not in _AUTH_STATES):
            self._reject("auth_broker_response_invalid")
        code = response.get("code")
        if code is not None and code not in _FINITE_CODES:
            self._reject("auth_broker_response_invalid")
        for field in ("authorization_url", "verification_uri", "user_code"):
            value = response.get(field)
            if value is not None and (not isinstance(value, str) or not value or len(value.encode()) > 4096):
                self._reject("auth_broker_response_invalid")
        interval = response.get("poll_interval")
        if interval is not None and not _positive_u64(interval):
            self._reject("auth_broker_response_invalid")
        prepared = response.get("prepared_ref")
        handle = response.get("commit_handle")
        if prepared is not None and (not isinstance(prepared, str)
                                     or _ORDINARY_REFERENCE.fullmatch(prepared) is None):
            self._reject("auth_broker_response_invalid")
        if handle is not None and not _hex64(handle):
            self._reject("auth_broker_response_invalid")
        public = AuthPublicState(
            version=response["version"],
            operation_id=response["operation_id"],
            state=response["state"],
            code=code,
            authorization_url=response.get("authorization_url"),
            verification_uri=response.get("verification_uri"),
            user_code=response.get("user_code"),
            poll_interval=interval,
        )
        return AuthPrivateReply(public=public, prepared_ref=prepared, commit_handle=handle)

    def _recovery_reply(self, response: dict, operation_id: str) -> RecoveryInspectReply:
        allowed = {"operation_id", "state", "material_state", "receipt_state",
                   "binding_fingerprint", "binding_summary", "code"}
        required = {"operation_id", "state", "material_state", "receipt_state"}
        fingerprint = response.get("binding_fingerprint")
        summary = response.get("binding_summary")
        code = response.get("code")
        if (set(response) - allowed or not required.issubset(response)
                or response.get("operation_id") != operation_id
                or response.get("state") not in _RECOVERY_STATES
                or response.get("material_state") not in _MATERIAL_STATES
                or response.get("receipt_state") not in {"present", "none"}
                or (fingerprint is not None and not _hex64(fingerprint))
                or (summary is not None and (not isinstance(summary, Mapping)
                    or set(summary) != {"provider_id", "auth_method_id", "config_revision",
                                        "auth_grant_revision", "public_config_fingerprint"}
                    or not isinstance(summary["provider_id"], str)
                    or _IDENTIFIER.fullmatch(summary["provider_id"]) is None
                    or not isinstance(summary["auth_method_id"], str)
                    or _IDENTIFIER.fullmatch(summary["auth_method_id"]) is None
                    or not _positive_u64(summary["config_revision"])
                    or not _u64(summary["auth_grant_revision"])
                    or not _hex64(summary["public_config_fingerprint"])))
                or (code is not None and code not in _FINITE_CODES)):
            self._reject("auth_broker_response_invalid")
        return RecoveryInspectReply(
            operation_id=operation_id, state=response["state"],
            material_state=response["material_state"], receipt_state=response["receipt_state"],
            binding_fingerprint=fingerprint,
            binding_summary=(None if summary is None else RecoveryBindingSummary(**summary)),
            code=code,
        )

    def _reject(self, reason: str):
        self._poison()
        raise CoreAuthBrokerUnavailable(reason)

    def _poison(self) -> None:
        callback = None
        with self._lock:
            if not self._failed:
                self._failed = True
                callback = self._poison_callback
                try:
                    self._channel.close()
                except OSError:
                    pass
        if callback is not None:
            callback()


class TrustedOwnerEpochProvider:
    """Publishes only Core-acknowledged epochs for canonical DB owner IDs."""

    def __init__(self, client: CoreAuthBrokerClient | None, credential_cache=None):
        self._client = client
        self._cache = credential_cache
        self._state_lock = RLock()
        self._control_lock = RLock()
        self._current: OwnerBinding | None = None
        self._vault_binding: tuple[int, str, str, bool] | None = None
        self._poisoned = client is None or not client.available
        if client is not None:
            client.set_poison_callback(self.channel_lost)

    def set_credential_cache(self, credential_cache) -> None:
        with self._state_lock:
            self._cache = credential_cache

    def current(self, owner_id: int) -> str | None:
        with self._state_lock:
            if (self._poisoned or not _positive_u64(owner_id) or self._current is None
                    or self._current.owner_id != owner_id or self._client is None
                    or not self._client.available):
                return None
            return self._current.owner_epoch

    def snapshot(self) -> OwnerBinding | None:
        with self._state_lock:
            return self._current

    def bind(self, owner_id: int) -> OwnerBinding | None:
        if not _positive_u64(owner_id):
            return None
        with self._control_lock:
            with self._state_lock:
                if self._poisoned or self._client is None:
                    return None
                self._current = None
                self._vault_binding = None
                cache = self._cache
            if cache is not None:
                cache.clear_all()
            try:
                binding = self._client.owner_bind(owner_id)
            except CoreAuthBrokerUnavailable:
                self.channel_lost()
                return None
            with self._state_lock:
                if self._poisoned or not self._client.available:
                    return None
                self._current = binding
            return binding

    def attest_vault_account(self, owner_id: int, vault_account_id: str,
                             *, force: bool = False) -> bool:
        if (not _positive_u64(owner_id) or not isinstance(vault_account_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", vault_account_id) is None):
            return False
        with self._control_lock:
            with self._state_lock:
                binding = self._current
                if (self._poisoned or self._client is None or binding is None
                        or binding.owner_id != owner_id):
                    return False
                key = (owner_id, binding.owner_epoch, vault_account_id)
                if not force and self._vault_binding is not None and self._vault_binding[:3] == key:
                    return self._vault_binding[3]
            try:
                accepted = self._client.vault_realm_bind(*key)
            except CoreAuthBrokerUnavailable:
                self.channel_lost()
                return False
            with self._state_lock:
                if self._poisoned or self._current != binding or not self._client.available:
                    return False
                self._vault_binding = (*key, accepted)
            return accepted

    def vault_metadata_status(self, owner_id: int, item_id: str,
                              website: str, username: str) -> dict:
        """Project status only while the attested account and Owner stay bound."""
        unavailable = {"version": 1, "status": "unavailable", "revision": None, "command_id": None}
        if not _positive_u64(owner_id):
            return unavailable
        with self._control_lock:
            with self._state_lock:
                binding = self._current
                attestation = self._vault_binding
                client = self._client
                if (self._poisoned or client is None or not client.available
                        or binding is None or binding.owner_id != owner_id
                        or attestation is None or not attestation[3]
                        or attestation[:2] != (owner_id, binding.owner_epoch)):
                    return unavailable
            try:
                result = client.vault_metadata_status(
                    owner_id, binding.owner_epoch, item_id, website, username)
            except CoreAuthBrokerUnavailable:
                self.channel_lost()
                return unavailable
            with self._state_lock:
                if (self._poisoned or not client.available
                        or self._current != binding or self._vault_binding != attestation):
                    return unavailable
            return result

    def vault_card_metadata_status(self, owner_id: int, item_id: str,
                                   cardholder: str, expiry_month: int, expiry_year: int,
                                   billing: dict, last4: str | None) -> dict:
        unavailable = {"version": 1, "status": "unavailable", "revision": None,
                       "command_id": None, "last4": None}
        if not _positive_u64(owner_id):
            return unavailable
        with self._control_lock:
            with self._state_lock:
                binding, attestation, client = self._current, self._vault_binding, self._client
                if (self._poisoned or client is None or not client.available
                        or binding is None or binding.owner_id != owner_id
                        or attestation is None or not attestation[3]
                        or attestation[:2] != (owner_id, binding.owner_epoch)):
                    return unavailable
            try:
                result = client.vault_card_metadata_status(
                    owner_id, binding.owner_epoch, item_id, cardholder,
                    expiry_month, expiry_year, billing, last4)
            except CoreAuthBrokerUnavailable:
                self.channel_lost()
                return unavailable
            with self._state_lock:
                if (self._poisoned or not client.available
                        or self._current != binding or self._vault_binding != attestation):
                    return unavailable
            return result

    def vault_csv_bind(self, owner_id: int, draft_id: str, row: int,
                       item_id: str, website: str, username: str,
                       reservation_command: str) -> dict:
        unavailable = {"version": 1, "state": "unavailable"}
        if not _positive_u64(owner_id):
            return unavailable
        with self._control_lock:
            with self._state_lock:
                binding, attestation, client = self._current, self._vault_binding, self._client
                if (self._poisoned or client is None or not client.available
                        or binding is None or binding.owner_id != owner_id
                        or attestation is None or not attestation[3]
                        or attestation[:2] != (owner_id, binding.owner_epoch)):
                    return unavailable
            try:
                result = client.vault_csv_bind(
                    owner_id, binding.owner_epoch, draft_id, row, item_id,
                    website, username, reservation_command)
            except CoreAuthBrokerUnavailable:
                self.channel_lost()
                return unavailable
            with self._state_lock:
                if (self._poisoned or not client.available
                        or self._current != binding or self._vault_binding != attestation):
                    return unavailable
            return result

    def _vault_profile_call(self, owner_id: int, operation: str, handoff_id: str, **kwargs) -> dict:
        unavailable=({"version":1,"status":"unavailable","handoff_id":handoff_id} if operation=="prepare" else
            {"version":1,"status":"unavailable","handoff_id":handoff_id,"profile_id":None,"profile_revision":None,"fill_status":"unknown"})
        if not _positive_u64(owner_id):return unavailable
        with self._control_lock:
            with self._state_lock:
                binding,attestation,client=self._current,self._vault_binding,self._client
                if (self._poisoned or client is None or not client.available or binding is None or binding.owner_id!=owner_id
                        or attestation is None or not attestation[3] or attestation[:2]!=(owner_id,binding.owner_epoch)):return unavailable
            try:
                if operation=="prepare":result=client.vault_profile_prepare(owner_id,binding.owner_epoch,**kwargs)
                elif operation=="recover":result=client.vault_profile_recover(owner_id,binding.owner_epoch,handoff_id,**kwargs)
                else:result=client.vault_profile_query("vault_profile_"+operation,owner_id,binding.owner_epoch,handoff_id)
            except CoreAuthBrokerUnavailable:
                self.channel_lost();return unavailable
            with self._state_lock:
                if self._poisoned or not client.available or self._current!=binding or self._vault_binding!=attestation:return unavailable
            return result

    def vault_profile_prepare(self, owner_id: int, grant: dict, profile_metadata_sha256: str, record_sha256: str, proxy_port: int, proxy_secret: str) -> dict:
        return self._vault_profile_call(owner_id,"prepare",grant.get("handoff_id"),grant=grant,profile_metadata_sha256=profile_metadata_sha256,record_sha256=record_sha256,proxy_port=proxy_port,proxy_secret=proxy_secret)

    def vault_profile_status(self, owner_id: int, handoff_id: str) -> dict:
        return self._vault_profile_call(owner_id,"status",handoff_id)

    def vault_profile_revoke(self, owner_id: int, handoff_id: str) -> dict:
        return self._vault_profile_call(owner_id,"revoke",handoff_id)

    def vault_profile_recover(self, owner_id: int, handoff_id: str, profile_id: str,
                              profile_metadata_sha256: str, record_sha256: str,
                              pendingjournal_sha256: str | None) -> dict:
        return self._vault_profile_call(owner_id,"recover",handoff_id,profile_id=profile_id,
            profile_metadata_sha256=profile_metadata_sha256,record_sha256=record_sha256,
            pendingjournal_sha256=pendingjournal_sha256)

    def vault_custody(self, owner_id: int, operation: str, payload: dict) -> dict:
        data=payload if isinstance(payload,dict) else {};handoff_id=data.get('handoff_id')
        fallback=({'version':1,'status':'unavailable','handoff_id':handoff_id,'custody_id':None}
            if operation=='seal' else {'version':1,'status':'unavailable','handoff_id':handoff_id,'profile_id':None,'profile_revision':None,'custody_id':None,'review_context_id':None})
        if operation=='borrow':fallback={'version':1,'status':'unavailable','handoff_id':handoff_id,'custody_id':None,'borrow_id':None,'context_id':None}
        if operation in ('call_start','call_poll'):fallback={'version':1,'status':'unknown','borrow_id':data.get('borrow_id'),'action_id':data.get('action_id'),'text':''}
        if operation=='capture_chunk':fallback={'version':1,'status':'unavailable','capture_id':data.get('capture_id'),'index':data.get('index'),'chunks':0,'data':''}
        if operation=='capture_release':fallback={'version':1,'status':'unavailable','capture_id':data.get('capture_id')}
        if not _positive_u64(owner_id):return fallback
        with self._control_lock:
            with self._state_lock:
                binding,attestation,client=self._current,self._vault_binding,self._client
                if (self._poisoned or client is None or not client.available or binding is None or binding.owner_id!=owner_id
                        or attestation is None or not attestation[3] or attestation[:2]!=(owner_id,binding.owner_epoch)):return fallback
            try:result=client.vault_custody(operation,owner_id,binding.owner_epoch,payload)
            except CoreAuthBrokerUnavailable:
                self.channel_lost();return fallback
            with self._state_lock:
                if self._poisoned or not client.available or self._current!=binding or self._vault_binding!=attestation:return fallback
            return result

    def compare_and_unbind(self, expected: OwnerBinding | None) -> str:
        if expected is None:
            return "already_unbound"
        with self._control_lock:
            with self._state_lock:
                if self._poisoned or self._client is None:
                    raise CoreAuthBrokerUnavailable("auth_broker_channel_unavailable")
            try:
                state = self._client.owner_unbind(expected)
            except CoreAuthBrokerUnavailable:
                self.channel_lost()
                raise
            with self._state_lock:
                if self._current == expected:
                    self._current = None
                    self._vault_binding = None
            return state

    def channel_lost(self) -> None:
        with self._state_lock:
            self._poisoned = True
            self._current = None
            self._vault_binding = None
            cache = self._cache
        if cache is not None:
            cache.clear_all()


def _hex64(value) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _version_one(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


def _positive_u64(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2 ** 64


def _u64(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2 ** 64


def _stable_binding(value: Mapping) -> dict:
    keys = {
        "version", "operation_id", "owner_id", "connection_id", "kind",
        "intent_fingerprint", "before_state_fingerprint", "revoke_requested",
        "expires_at", "owner_epoch", "provider_id", "auth_method_id",
        "config_revision", "auth_grant_revision", "public_config_fingerprint",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("stable binding")
    result = dict(value)
    if (not _version_one(result["version"]) or not _hex64(result["operation_id"])
            or not _positive_u64(result["owner_id"])
            or not _positive_u64(result["connection_id"])
            or result["kind"] not in _KINDS
            or not _hex64(result["intent_fingerprint"])
            or not _hex64(result["before_state_fingerprint"])
            or not isinstance(result["revoke_requested"], bool)
            or not isinstance(result["expires_at"], int) or isinstance(result["expires_at"], bool)
            or not -(2 ** 63) <= result["expires_at"] < 2 ** 63
            or not _hex64(result["owner_epoch"])
            or not isinstance(result["provider_id"], str)
            or _IDENTIFIER.fullmatch(result["provider_id"]) is None
            or not isinstance(result["auth_method_id"], str)
            or _IDENTIFIER.fullmatch(result["auth_method_id"]) is None
            or not _positive_u64(result["config_revision"])
            or not _u64(result["auth_grant_revision"])
            or not _hex64(result["public_config_fingerprint"])):
        raise ValueError("stable binding")
    return result


def _native_enrollment(binding: Mapping, value: Mapping | None) -> dict | None:
    if binding["auth_method_id"] != "native_chain":
        if value is not None:
            raise ValueError("native enrollment")
        return None
    if not isinstance(value, Mapping):
        raise ValueError("native enrollment")
    result = dict(value)
    selected = _NATIVE_ENROLLMENT_FIELDS.get(result.get("provider"))
    if (selected is None or set(result) != selected[1]
            or binding["provider_id"] != selected[0]
            or any(not isinstance(item, str) or not item or item != item.strip()
                   or len(item.encode("utf-8")) > 2048
                   for item in result.values())):
        raise ValueError("native enrollment")
    return result


def _recovery_selector(value: Mapping) -> dict:
    keys = {"operation_id", "owner_id", "connection_id", "kind",
            "intent_fingerprint", "before_state_fingerprint", "revoke_requested"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("recovery selector")
    result = dict(value)
    if (not _hex64(result["operation_id"]) or not _positive_u64(result["owner_id"])
            or not _positive_u64(result["connection_id"]) or result["kind"] not in _KINDS
            or not _hex64(result["intent_fingerprint"])
            or not _hex64(result["before_state_fingerprint"])
            or not isinstance(result["revoke_requested"], bool)):
        raise ValueError("recovery selector")
    return result
