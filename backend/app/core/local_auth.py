"""Per-launch desktop credential and browser origin boundary."""
import hashlib
import hmac

from fastapi import HTTPException, Request, WebSocket


ALLOWED_ORIGINS = frozenset({
    "tauri://localhost",
    "http://tauri.localhost",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
})


def _valid_token(received: str | None, expected: str | None) -> bool:
    return bool(received and expected and hmac.compare_digest(received, expected))


def _valid_cli_capability(received: str | None, expected_hash: str | None) -> bool:
    if (not received or not expected_hash or len(received) != 64
            or any(char not in "0123456789abcdef" for char in received)):
        return False
    actual = hashlib.sha256(received.encode("ascii")).hexdigest()
    return hmac.compare_digest(actual, expected_hash)


def require_local_session(request: Request) -> None:
    origin = request.headers.get("origin")
    capability = request.headers.get("x-gamja-cli-capability")
    if capability:
        # A capability file is an owner-shell boundary, never a browser one.
        if origin is not None or not _valid_cli_capability(capability, request.app.state.cli_capability_hash):
            raise HTTPException(status_code=401, detail="Local session required")
        return
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin denied")
    if not _valid_token(request.headers.get("x-gamja-session"), request.app.state.local_session_token):
        raise HTTPException(status_code=401, detail="Local session required")


def authorize_ws(websocket: WebSocket) -> bool:
    if websocket.headers.get("origin") not in ALLOWED_ORIGINS:
        return False
    protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    local = [item.removeprefix("gamja.local.") for item in protocols if item.startswith("gamja.local.")]
    return len(local) == 1 and _valid_token(local[0], websocket.app.state.local_session_token)
