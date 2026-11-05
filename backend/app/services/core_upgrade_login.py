"""Consume one inherited, Core-authenticated upgrade grant before API startup.

No public authentication route, saved old secret, file ticket or OS credential API.
Only the verified Core parent supplies this socket to its bound signed engine.
"""
from datetime import datetime
import json
import re
import secrets
import socket
import struct
import time
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from app.services.upgrade_remembered_login import ExpectedState, UpgradeFailed, upgrade_remembered_login

MAX_FRAME_SIZE = 8192
CHANNEL_DEADLINE_SECONDS = 10.0
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_GRANT_KEYS = {"version", "kind", "nonce", "generation", "owner_id", "old_secret_ref", "revision", "expires_at", "old_secret_hash", "deadline_unix_ms"}
_ACK_KEYS = {"version", "kind", "nonce", "generation", "status"}

class ChannelError(RuntimeError):
    def __init__(self, code="upgrade_channel_invalid"):
        self.code = code if code in {"upgrade_channel_invalid", "upgrade_channel_unavailable", "upgrade_ack_unconfirmed", "upgrade_native_failed", "upgrade_state_changed"} else "upgrade_channel_invalid"
        super().__init__(self.code)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ChannelError()
        out[key] = value
    return out


def _budget(deadline):
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise ChannelError("upgrade_channel_unavailable")
    return remaining


def _read_exact(sock, length, deadline):
    data = bytearray()
    try:
        while len(data) < length:
            sock.settimeout(_budget(deadline))
            chunk = sock.recv(length-len(data))
            if not chunk:
                raise ChannelError("upgrade_channel_unavailable")
            data.extend(chunk)
        return data
    except (OSError, ChannelError):
        data[:] = b"\0"*len(data)
        raise ChannelError("upgrade_channel_unavailable") from None


def _read_frame(sock, deadline):
    length = struct.unpack(">I", _read_exact(sock, 4, deadline))[0]
    if not 0 < length <= MAX_FRAME_SIZE:
        raise ChannelError()
    raw = _read_exact(sock, length, deadline)
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ChannelError()))
        if type(value) is not dict:
            raise ChannelError()
        return value
    except (UnicodeError, ValueError, TypeError):
        raise ChannelError() from None
    finally:
        raw[:] = b"\0"*len(raw)


def _write_frame(sock, value, deadline):
    raw = bytearray(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())
    try:
        if not 0 < len(raw) <= MAX_FRAME_SIZE:
            raise ChannelError()
        sock.settimeout(_budget(deadline))
        sock.sendall(struct.pack(">I", len(raw))+raw)
    except OSError:
        raise ChannelError("upgrade_channel_unavailable") from None
    finally:
        raw[:] = b"\0"*len(raw)


def _matches(msg, keys, kind, nonce, generation):
    return (type(msg) is dict and set(msg) == keys and type(msg.get("version")) is int
            and msg["version"] == 1 and msg.get("kind") == kind
            and msg.get("nonce") == nonce and msg.get("generation") == generation)


def _grant(msg, nonce, generation):
    if not _matches(msg, _GRANT_KEYS, "grant", nonce, generation):
        raise ChannelError()
    for name in ("owner_id", "revision"):
        if type(msg[name]) is not int or not 0 < msg[name] < 2**63:
            raise ChannelError()
    if type(msg["old_secret_hash"]) is not str or not _HEX64.fullmatch(msg["old_secret_hash"]):
        raise ChannelError()
    if type(msg["expires_at"]) is not str or len(msg["expires_at"]) > 64:
        raise ChannelError()
    try:
        expires = datetime.fromisoformat(msg["expires_at"])
        expected = ExpectedState(msg["owner_id"], msg["old_secret_ref"], msg["revision"], expires)
    except (ValueError, TypeError, UpgradeFailed):
        raise ChannelError() from None
    limit = msg["deadline_unix_ms"]
    if type(limit) is not int or not time.time()*1000 < limit <= time.time()*1000+60_000:
        raise ChannelError()
    return expected


def _record_native(diagnostics, error):
    raw = getattr(error, "_diagnostic", {})
    if diagnostics is None or "native_reason" not in raw:
        return
    cls = raw.get("error_class")
    record = {"correlation_id": raw["correlation_id"], "stage": "auth_remember",
              "code": "credential_locked" if cls in {"interaction_required", "credential_reentry_required"} else "secret_store_unavailable",
              "reason": raw["native_reason"], "error_class": cls, "cleanup": raw.get("cleanup", "unknown")}
    for key in ("operation", "os_status"):
        if key in raw:
            record[key] = raw[key]
    # Existing store validates every field; no new diagnostic allowance is added.
    try:
        diagnostics.put(record)
    except Exception:
        # A diagnostic storage failure must not change the native outcome.
        pass


def consume_upgrade_channel(db, store, sock, auth_broker_fingerprint, diagnostics=None):
    """A consumed socket cannot be replayed; ack loss preserves committed candidate data."""
    deadline = time.monotonic()+CHANNEL_DEADLINE_SECONDS
    nonce = secrets.token_hex(32)
    generation = auth_broker_fingerprint
    committed = False
    begin_sent = False
    try:
        if (type(generation) is not str or not _HEX64.fullmatch(generation)
                or sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM
                or db.in_transaction()):
            raise ChannelError()
        _write_frame(sock, {"version":1,"kind":"begin","nonce":nonce,"generation":generation}, deadline)
        begin_sent = True
        msg = _read_frame(sock, deadline)
        expected = _grant(msg, nonce, generation)
        # An isolated candidate only: hold the write lock across validation and
        # re-enrollment so a hash-only race cannot invalidate the source proof.
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.execute(text("SELECT r.owner_id,r.secret_ref,r.revision,r.expires_at,r.secret_hash,u.is_active FROM remembered_login r JOIN users u ON u.id=r.owner_id WHERE r.id=1")).first()
        if (row is None or row.owner_id != expected.owner_id or row.secret_ref != expected.old_secret_ref
                or row.revision != expected.revision or row.expires_at != msg["expires_at"]
                or row.secret_hash != msg["old_secret_hash"] or row.is_active != 1):
            raise ChannelError("upgrade_state_changed")
        result = upgrade_remembered_login(db, store, expected)
        committed = True
        _write_frame(sock, {"version":1,"kind":"complete","nonce":nonce,"generation":generation,"status":"committed"}, deadline)
        ack = _read_frame(sock, deadline)
        if not _matches(ack, _ACK_KEYS, "ack", nonce, generation) or ack["status"] != "accepted":
            raise ChannelError("upgrade_ack_unconfirmed")
        return result
    except UpgradeFailed as error:
        _record_native(diagnostics, error)
        raise ChannelError("upgrade_native_failed") from None
    except HTTPException:
        raise ChannelError("upgrade_state_changed") from None
    except SQLAlchemyError:
        raise ChannelError("upgrade_ack_unconfirmed" if committed else "upgrade_state_changed") from None
    except (OSError, ValueError, TypeError):
        raise ChannelError("upgrade_ack_unconfirmed" if committed else "upgrade_channel_unavailable") from None
    except ChannelError:
        if committed:
            raise ChannelError("upgrade_ack_unconfirmed") from None
        raise
    finally:
        if not committed:
            try:
                db.rollback()
            except Exception:
                pass
        if not committed and begin_sent:
            try:
                _write_frame(sock, {"version":1,"kind":"complete","nonce":nonce,"generation":generation,"status":"failed"}, deadline)
            except (ChannelError, OSError):
                pass
        sock.close()
