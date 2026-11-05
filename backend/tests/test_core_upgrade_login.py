"""Real private-channel exchanges against an isolated SQLite candidate."""
import errno
import json
import os
import socket
import struct
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services import core_upgrade_login as channel
from app.services.auth_diagnostics import AuthDiagnosticStore
from app.services.secrets import SecretStoreUnavailable, consume_inherited_socket

GENERATION = "b" * 64
OLD_REF = "a" * 32
OLD_HASH = "c" * 64


class Store:
    def __init__(self):
        self.values = {OLD_REF: "old-fixture-value"}
        self.deleted = []
        self.writes = 0
        self.failure = None
        self.on_put = None
    def put(self, ref, value):
        self.writes += 1
        if self.on_put:
            self.on_put()
        if self.failure:
            raise self.failure
        self.values[ref] = value
    def get_no_ui(self, ref):
        return self.values.get(ref)
    def delete(self, ref):
        self.deleted.append(ref)
        self.values.pop(ref, None)


@pytest.fixture
def candidate(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'candidate.db'}", connect_args={"timeout": 0.01})
    factory = sessionmaker(bind=engine)
    expiry = (datetime.utcnow() + timedelta(days=2)).isoformat(sep=" ")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE users(id INTEGER PRIMARY KEY, is_active INTEGER NOT NULL)"))
        db.execute(text("CREATE TABLE remembered_login(id INTEGER PRIMARY KEY, owner_id INTEGER, secret_ref TEXT, revision INTEGER, expires_at TEXT, secret_hash TEXT)"))
        db.execute(text("INSERT INTO users VALUES (1,1)"))
        db.execute(text("INSERT INTO remembered_login VALUES (1,1,:ref,3,:expiry,:hash)"), {"ref":OLD_REF,"expiry":expiry,"hash":OLD_HASH})
    try:
        yield factory, expiry
    finally:
        engine.dispose()


def read(sock):
    def exact(count):
        value = b""
        while len(value) < count:
            chunk = sock.recv(count-len(value))
            if not chunk:
                raise EOFError
            value += chunk
        return value
    return json.loads(exact(struct.unpack(">I", exact(4))[0]))


def send(sock, value, raw=False):
    payload = value if raw else json.dumps(value).encode()
    sock.sendall(struct.pack(">I",len(payload))+payload)


def exchange(factory, expiry, store, *, alter=None, ack="accepted", disconnect_ack=False, diagnostics=None):
    server, engine = socket.socketpair()
    messages, failures = [], []
    def core():
        try:
            server.settimeout(1)
            begin = read(server)
            messages.append(begin)
            grant = {"version":1,"kind":"grant","nonce":begin["nonce"],"generation":begin["generation"],
                     "owner_id":1,"old_secret_ref":OLD_REF,"revision":3,"expires_at":expiry,
                     "old_secret_hash":OLD_HASH,"deadline_unix_ms":int(time.time()*1000)+30_000}
            if alter:
                grant = alter(grant)
            send(server, grant, raw=isinstance(grant, bytes))
            complete = read(server)
            messages.append(complete)
            if complete["status"] == "committed" and not disconnect_ack:
                send(server,{"version":1,"kind":"ack","nonce":begin["nonce"],"generation":begin["generation"],"status":ack})
        except (EOFError, BrokenPipeError, ConnectionResetError):
            pass
        except BaseException as error:
            failures.append(error)
        finally:
            server.close()
    thread = threading.Thread(target=core)
    thread.start()
    try:
        with factory() as db:
            result = channel.consume_upgrade_channel(db,store,engine,GENERATION,diagnostics)
        return result,messages
    finally:
        engine.close()
        thread.join(2)
        assert not thread.is_alive()
        assert not failures


def row(factory):
    with factory() as db:
        return db.execute(text("SELECT owner_id,secret_ref,revision,expires_at,secret_hash FROM remembered_login")).first()


def test_real_exchange_preserves_owner_expiry_and_old_credential(candidate):
    factory,expiry = candidate
    store = Store()
    result,messages = exchange(factory,expiry,store)
    current = row(factory)
    assert current.owner_id == 1 and current.expires_at == expiry and current.revision == 4
    assert current.secret_ref == result.new_secret_ref and current.secret_ref != OLD_REF
    assert current.secret_hash != OLD_HASH
    assert store.values[OLD_REF] == "old-fixture-value" and not store.deleted
    assert [m["kind"] for m in messages] == ["begin","complete"]
    assert messages[1]["status"] == "committed" and len(messages[0]["nonce"]) == 64
    assert store.writes == 1


@pytest.mark.parametrize("field,value", [
    ("version",True),("version",1.0),("version",2),("kind","begin"),
    ("nonce","d"*64),("generation","d"*64),("owner_id",True),("owner_id",0),
    ("owner_id",2**63),("revision",True),("revision",0),
    ("old_secret_ref","../invalid"),("old_secret_hash","BAD"),
    ("expires_at","invalid"),("expires_at","2030-01-01T00:00:00+00:00"),
    ("deadline_unix_ms",True),("deadline_unix_ms",0),("deadline_unix_ms",2**63),
])
def test_malformed_grant_never_calls_store(candidate,field,value):
    factory,expiry = candidate
    store = Store()
    def alter(grant):
        grant[field] = value
        return grant
    with pytest.raises(channel.ChannelError,match="upgrade_channel_invalid"):
        exchange(factory,expiry,store,alter=alter)
    assert store.writes == 0 and row(factory).secret_ref == OLD_REF


@pytest.mark.parametrize("alter",[
    lambda grant:{**grant,"unexpected":1},
    lambda grant:{k:v for k,v in grant.items() if k!="revision"},
    lambda grant:(json.dumps(grant)[:-1]+',"version":1}').encode(),
    lambda grant:b'{"version":NaN}',
])
def test_unknown_missing_duplicate_nonfinite_grants_rejected(candidate,alter):
    factory,expiry = candidate
    store = Store()
    with pytest.raises(channel.ChannelError):
        exchange(factory,expiry,store,alter=alter)
    assert store.writes == 0 and row(factory).secret_ref == OLD_REF


@pytest.mark.parametrize("field,value",[("owner_id",2),("revision",4),("old_secret_ref","d"*32),("old_secret_hash","e"*64)])
def test_valid_but_drifted_grant_never_calls_store(candidate,field,value):
    factory,expiry = candidate
    store = Store()
    def alter(grant):
        grant[field] = value
        return grant
    with pytest.raises(channel.ChannelError,match="upgrade_state_changed"):
        exchange(factory,expiry,store,alter=alter)
    assert store.writes == 0 and row(factory).secret_ref == OLD_REF


def test_candidate_lock_prevents_hash_only_race(candidate):
    from sqlalchemy.exc import OperationalError
    factory,expiry = candidate
    store = Store()
    attempts = []
    def race():
        with factory() as other:
            with pytest.raises(OperationalError):
                other.execute(text("UPDATE remembered_login SET secret_hash='changed'"))
            attempts.append(True)
    store.on_put = race
    exchange(factory,expiry,store)
    assert attempts == [True] and row(factory).revision == 4


@pytest.mark.parametrize("disconnect,ack",[(True,"accepted"),(False,"denied")])
def test_commit_ack_failure_retains_new_credential_and_candidate(candidate,disconnect,ack):
    factory,expiry = candidate
    store = Store()
    with pytest.raises(channel.ChannelError,match="upgrade_ack_unconfirmed"):
        exchange(factory,expiry,store,disconnect_ack=disconnect,ack=ack)
    current = row(factory)
    assert current.revision == 4 and current.expires_at == expiry
    assert current.secret_ref in store.values and OLD_REF in store.values and not store.deleted


def test_native_failure_retains_actual_detail_privately(candidate,tmp_path):
    factory,expiry = candidate
    store = Store()
    store.failure = SecretStoreUnavailable("keychain_write_failed",-25293,operation="sec_item_add",error_class="native_os_status")
    (tmp_path / "diagnostics").mkdir(mode=0o700)
    diagnostics = AuthDiagnosticStore(str(tmp_path / "diagnostics"))
    with pytest.raises(channel.ChannelError,match="upgrade_native_failed"):
        exchange(factory,expiry,store,diagnostics=diagnostics)
    assert row(factory).secret_ref == OLD_REF and OLD_REF not in store.deleted
    path = tmp_path / "diagnostics" / "auth-diagnostics.json"
    content = path.read_text()
    assert "sec_item_add" in content and "-25293" in content
    assert OLD_REF not in content and OLD_HASH not in content and "old-fixture-value" not in content


def test_diagnostic_failure_does_not_change_native_failure(candidate):
    factory,expiry = candidate
    store = Store()
    store.failure = SecretStoreUnavailable("keychain_write_failed",-25293,operation="sec_item_add")
    class BrokenDiagnostics:
        def put(self,value):
            raise RuntimeError("fixture disk full")
    with pytest.raises(channel.ChannelError,match="upgrade_native_failed"):
        exchange(factory,expiry,store,diagnostics=BrokenDiagnostics())
    assert row(factory).secret_ref == OLD_REF


@pytest.mark.parametrize("payload",[b"",b"x"*8193,b'[]',b'\xff'])
def test_bounded_frame_and_eof(candidate,payload,monkeypatch):
    factory,_ = candidate
    engine,server = socket.socketpair()
    store = Store()
    monkeypatch.setattr(channel,"CHANNEL_DEADLINE_SECONDS",0.02)
    try:
        server.settimeout(0.2)
        if len(payload) > channel.MAX_FRAME_SIZE:
            # The oversized length alone must reject; sending its body first
            # can fill macOS's socket buffer before the reader is started.
            server.sendall(struct.pack(">I",len(payload)))
        else:
            send(server,payload,raw=True)
        server.shutdown(socket.SHUT_WR)
        with factory() as db, pytest.raises(channel.ChannelError):
            channel.consume_upgrade_channel(db,store,engine,GENERATION)
        assert engine.fileno() == -1 and not store.writes
    finally:
        engine.close();server.close()


def test_inherited_socket_takes_ownership_and_is_noninheritable():
    engine,peer = socket.socketpair()
    fd = engine.detach()
    os.set_inheritable(fd,True)
    owned = consume_inherited_socket(fd)
    try:
        assert owned.fileno() == fd and not os.get_inheritable(fd)
    finally:
        owned.close();peer.close()
    with pytest.raises(OSError) as error:
        os.fstat(fd)
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("machine,mode,fields",[
    ("arm64","legacy",{}),("x86_64","core_managed_recovery_required",{}),
    ("arm64","core_managed_recovery_required",{"provider_fd":None}),
    ("arm64","core_managed_recovery_required",{"upgrade_fd":43}),
    ("arm64","core_managed_recovery_required",{"upgrade_fd":True}),
])
def test_invalid_upgrade_launch_rejected_before_channel_open(monkeypatch,machine,mode,fields):
    import app.main as main
    monkeypatch.setattr(main.platform,"machine",lambda:machine)
    values = dict(database_url="sqlite:///:memory:",credential_fd=41,auth_broker_fd=42,provider_fd=43,upgrade_fd=44,provider_recovery_mode=mode)
    values.update(fields)
    with pytest.raises(ValueError):
        main.RuntimeConfig(**values)


def test_upgrade_fd_environment_and_runner_classification(monkeypatch):
    from app.main import RuntimeConfig
    import run
    for name,value in [("GAMJA_CREDENTIAL_FD","41"),("GAMJA_AUTH_BROKER_FD","42"),("GAMJA_PROVIDER_FD","43"),("GAMJA_UPGRADE_FD","44")]:
        monkeypatch.setenv(name,value)
    assert RuntimeConfig.from_environment("sqlite:///:memory:",provider_recovery_mode="core_managed_recovery_required").upgrade_fd == 44
    assert run.provider_recovery_mode({"GAMJA_UPGRADE_FD":"44"},platform_name="win32",machine="AMD64") == "core_managed_recovery_required"
    assert run.provider_recovery_mode({},platform_name="win32",machine="AMD64") == "legacy"


def test_committed_candidate_cannot_replay_original_grant(candidate):
    factory,expiry = candidate
    store = Store()
    exchange(factory,expiry,store)
    with pytest.raises(channel.ChannelError,match="upgrade_state_changed"):
        exchange(factory,expiry,store)
    assert store.writes == 1 and row(factory).revision == 4


@pytest.mark.parametrize("accept",[False,True])
def test_startup_requires_ack_before_scheduler_or_api(candidate,monkeypatch,accept):
    import app.main as main
    from fastapi.testclient import TestClient
    factory,expiry = candidate
    engine = factory.kw["bind"]
    store = Store()
    lifecycle = []
    class Client:
        available = True
        current_generation_fingerprint = GENERATION
        def __init__(self,name):
            self.name = name
        def close(self):
            lifecycle.append(self.name+"-closed")
        def set_poison_callback(self,callback):
            pass
        def call(self,method,namespace,ref,value=None):
            assert namespace == "remembered_login"
            if method == "put":
                return store.put(ref,value)
            if method == "get":
                return store.get_no_ui(ref)
            if method == "delete":
                return store.delete(ref)
            raise AssertionError(method)
    for cls,name in [(main.CoreCredentialClient,"credential"),(main.CoreAuthBrokerClient,"auth"),(main.CoreProviderClient,"provider")]:
        client = Client(name)
        monkeypatch.setattr(cls,"from_fd",lambda *args,client=client,**kwargs:client)
    monkeypatch.setattr(main,"migrate_database",lambda engine:None)
    class Scheduler:
        def __init__(self,*args):
            pass
        async def start(self):
            lifecycle.append("scheduler-started")
        async def close(self):
            lifecycle.append("scheduler-closed")
    monkeypatch.setattr(main,"Scheduler",Scheduler)
    server,child = socket.socketpair()
    fd = child.detach()
    errors = []
    def core():
        try:
            server.settimeout(1)
            begin = read(server)
            send(server,{"version":1,"kind":"grant","nonce":begin["nonce"],"generation":GENERATION,
                         "owner_id":1,"old_secret_ref":OLD_REF,"revision":3,"expires_at":expiry,
                         "old_secret_hash":OLD_HASH,"deadline_unix_ms":int(time.time()*1000)+30_000})
            assert read(server)["status"] == "committed"
            assert "scheduler-started" not in lifecycle
            if accept:
                send(server,{"version":1,"kind":"ack","nonce":begin["nonce"],"generation":GENERATION,"status":"accepted"})
        except BaseException as error:
            errors.append(error)
        finally:
            server.close()
    thread = threading.Thread(target=core)
    thread.start()
    try:
        app = main.create_app(runtime_config=main.RuntimeConfig(
            database_url=str(engine.url),credential_fd=10_041,auth_broker_fd=10_042,provider_fd=10_043,
            upgrade_fd=fd,provider_recovery_mode="core_managed_recovery_required",
            local_session_token="fixture-local-session"))
        if accept:
            with TestClient(app) as client:
                assert client.get("/health",headers={"x-gamja-session":"fixture-local-session"}).status_code == 200
            assert "scheduler-started" in lifecycle
        else:
            with pytest.raises(channel.ChannelError,match="upgrade_ack_unconfirmed"),TestClient(app):
                pass
            assert "scheduler-started" not in lifecycle
        assert set(lifecycle) >= {"credential-closed","auth-closed","provider-closed"}
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        thread.join(2)
        assert not thread.is_alive() and not errors
        server.close()
