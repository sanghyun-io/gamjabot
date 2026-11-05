from types import SimpleNamespace
import json
import socket
import struct
import threading


class NativeCall:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


def test_source_macos_store_cannot_create_production_keychain_items(monkeypatch):
    import pytest
    from app.services import secrets

    monkeypatch.setattr(secrets.sys, 'platform', 'darwin')
    monkeypatch.delattr(secrets.sys, 'frozen', raising=False)
    with pytest.raises(secrets.SecretStoreUnavailable, match='credential_core_required'):
        secrets.PlatformSecretStore()


def test_core_store_uses_fixed_namespace_and_noninteractive_reads():
    from app.services.secrets import CoreSecretStore

    class FakeCore:
        def __init__(self):
            self.calls = []

        def call(self, operation, namespace, reference, value=None):
            self.calls.append((operation, namespace, reference, value))
            return 'saved-value' if operation == 'get' else None

    core = FakeCore()
    store = CoreSecretStore(core, 'provider')

    store.put('1:2:ref', 'new-value')
    assert store.get('1:2:ref') == 'saved-value'
    assert store.get_no_ui('1:2:ref') == 'saved-value'
    store.delete('1:2:ref')
    assert core.calls == [
        ('put', 'provider', '1:2:ref', 'new-value'),
        ('get', 'provider', '1:2:ref', None),
        ('get', 'provider', '1:2:ref', None),
        ('delete', 'provider', '1:2:ref', None),
    ]


def test_core_store_maps_redacted_reentry_without_secret_text():
    import pytest
    from app.services.secrets import CoreSecretStore, SecretReentryRequired

    class FailingCore:
        def call(self, *_args, **_kwargs):
            raise SecretReentryRequired()

    store = CoreSecretStore(FailingCore(), 'remembered_login')
    with pytest.raises(SecretReentryRequired) as raised:
        store.get_no_ui('current')
    assert str(raised.value) == 'credential_reentry_required'


def test_inherited_core_client_uses_bounded_typed_frames():
    from app.services.secrets import CoreCredentialClient

    client_socket, core_socket = socket.socketpair()
    observed = {}

    def core():
        length = struct.unpack('>I', core_socket.recv(4))[0]
        body = core_socket.recv(length)
        observed.update(json.loads(body))
        response = json.dumps({'status': 'ok', 'value': 'synthetic-result'}).encode()
        core_socket.sendall(struct.pack('>I', len(response)) + response)

    worker = threading.Thread(target=core)
    worker.start()
    client = CoreCredentialClient(client_socket)
    try:
        assert client.call('get', 'provider', '1:2:' + '0' * 32) == 'synthetic-result'
    finally:
        client.close()
        core_socket.close()
        worker.join(2)
    assert observed == {'version': 1, 'operation': 'get', 'namespace': 'provider',
                        'reference': '1:2:' + '0' * 32}


def test_inherited_core_client_maps_missing_and_rejects_untyped_failures():
    import pytest
    from app.services.secrets import (CoreCredentialClient, SecretInteractionRequired,
                                      SecretReentryRequired, SecretStoreUnavailable)

    def exchange(response):
        client_socket, core_socket = socket.socketpair()

        def core():
            length = struct.unpack('>I', core_socket.recv(4))[0]
            core_socket.recv(length)
            encoded = json.dumps(response).encode()
            core_socket.sendall(struct.pack('>I', len(encoded)) + encoded)

        worker = threading.Thread(target=core)
        worker.start()
        client = CoreCredentialClient(client_socket)
        return client, core_socket, worker

    client, peer, worker = exchange({'status': 'missing'})
    try:
        with pytest.raises(SecretReentryRequired):
            client.call('get', 'remembered_login', 'current')
    finally:
        client.close(); peer.close(); worker.join(2)

    client, peer, worker = exchange({'status': 'failed', 'detail': 'sentinel-secret'})
    try:
        with pytest.raises(SecretStoreUnavailable) as raised:
            client.call('get', 'provider', '1:2:' + '0' * 32)
        assert 'sentinel-secret' not in str(raised.value)
    finally:
        client.close(); peer.close(); worker.join(2)

    client, peer, worker = exchange({'status': 'interaction_required', 'operation': 'sec_item_copy_matching',
                                     'error_class': 'interaction_required', 'os_status': -25308})
    try:
        with pytest.raises(SecretInteractionRequired) as raised:
            client.call('get', 'provider', '1:2:' + '0' * 32)
        assert raised.value.reason == 'keychain_interaction_required'
        assert raised.value.os_status == -25308
        assert raised.value.operation == 'sec_item_copy_matching'
        assert raised.value.error_class == 'interaction_required'
    finally:
        client.close(); peer.close(); worker.join(2)

    client, peer, worker = exchange({'status': 'failed', 'operation': 'sec_item_copy_matching',
                                     'error_class': 'native_os_status', 'os_status': -25293})
    try:
        with pytest.raises(SecretStoreUnavailable) as raised:
            client.call('get', 'provider', '1:2:' + '0' * 32)
        assert raised.value.reason == 'keychain_query_failed'
        assert raised.value.os_status == -25293
        assert raised.value.operation == 'sec_item_copy_matching'
        assert raised.value.error_class == 'native_os_status'
    finally:
        client.close(); peer.close(); worker.join(2)


def test_inherited_core_client_distinguishes_native_failure_from_bridge_protocol_failure():
    import pytest
    from app.services.secrets import CoreCredentialClient, SecretStoreUnavailable

    def invoke(response):
        client_socket, core_socket = socket.socketpair()
        def core():
            length = struct.unpack('>I', core_socket.recv(4))[0]
            core_socket.recv(length)
            encoded = json.dumps(response).encode()
            core_socket.sendall(struct.pack('>I', len(encoded)) + encoded)
        worker = threading.Thread(target=core)
        worker.start()
        client = CoreCredentialClient(client_socket)
        try:
            with pytest.raises(SecretStoreUnavailable) as raised:
                client.call('put', 'provider', '1:2:' + '0' * 32, 'sentinel-value')
            return raised.value
        finally:
            client.close(); core_socket.close(); worker.join(2)

    native = invoke({'status': 'failed', 'operation': 'sec_item_update',
                     'error_class': 'native_os_status', 'os_status': -25293})
    assert (native.operation, native.error_class, native.os_status) == (
        'sec_item_update', 'native_os_status', -25293)
    malformed = invoke({'status': 'failed', 'operation': 'sec_item_update',
                        'error_class': 'sentinel-secret', 'os_status': -25293})
    assert malformed.reason == 'credential_response_invalid'
    assert malformed.error_class == 'bridge_protocol'
    assert malformed.operation is None and malformed.os_status is None
    assert 'sentinel-secret' not in str(malformed)


def test_inherited_core_client_rejects_arbitrary_credential_selectors_before_io():
    import pytest
    from app.services.secrets import CoreCredentialClient, SecretStoreUnavailable

    client_socket, core_socket = socket.socketpair()
    client = CoreCredentialClient(client_socket)
    try:
        with pytest.raises(SecretStoreUnavailable, match='credential_request_invalid'):
            client.call('get', 'provider', 'arbitrary-selector')
        with pytest.raises(SecretStoreUnavailable, match='credential_request_invalid'):
            client.call('delete', 'remembered_login', 'someone-else')
        core_socket.setblocking(False)
        with pytest.raises(BlockingIOError):
            core_socket.recv(1)
    finally:
        client.close()
        core_socket.close()


def test_inherited_core_client_accepts_only_legacy_or_uuid_remembered_references():
    import pytest
    from app.services.secrets import CoreCredentialClient, SecretStoreUnavailable

    for invalid in ('someone-else', 'A' * 32, 'a' * 31, 'a' * 33, '../' + 'a' * 32):
        client_socket, core_socket = socket.socketpair()
        client = CoreCredentialClient(client_socket)
        try:
            with pytest.raises(SecretStoreUnavailable, match='credential_request_invalid'):
                client.call('get', 'remembered_login', invalid)
        finally:
            client.close()
            core_socket.close()

    client_socket, core_socket = socket.socketpair()
    client = CoreCredentialClient(client_socket)
    core_socket.close()
    try:
        with pytest.raises(SecretStoreUnavailable, match='credential_channel_unavailable'):
            client.call('get', 'remembered_login', 'a' * 32)
    finally:
        client.close()


def test_inherited_core_client_times_out_with_a_redacted_channel_error():
    import pytest
    from app.services.secrets import CoreCredentialClient, SecretStoreUnavailable

    client_socket, core_socket = socket.socketpair()
    client = CoreCredentialClient(client_socket)
    assert client_socket.gettimeout() == 5.0
    client._TIMEOUT_SECONDS = 0.05

    def drip_response():
        try:
            length = struct.unpack('>I', core_socket.recv(4))[0]
            core_socket.recv(length)
            core_socket.sendall(struct.pack('>I', 32))
            for _ in range(16):
                core_socket.sendall(b'x')
                __import__('time').sleep(0.02)
        except OSError:
            pass

    worker = threading.Thread(target=drip_response)
    worker.start()
    try:
        with pytest.raises(SecretStoreUnavailable) as raised:
            client.call('get', 'provider', '1:2:' + '0' * 32)
        assert str(raised.value) == 'credential_channel_unavailable'
    finally:
        client.close()
        core_socket.close()
        worker.join(1)


def test_timed_out_core_channel_is_closed_before_a_late_response_can_cross_requests():
    import pytest
    from app.services.secrets import CoreCredentialClient, SecretStoreUnavailable

    client_socket, core_socket = socket.socketpair()
    client = CoreCredentialClient(client_socket)
    client._TIMEOUT_SECONDS = 0.03
    observed = {'second_request': False, 'late_send': False}

    def delayed_core():
        try:
            length = struct.unpack('>I', core_socket.recv(4))[0]
            core_socket.recv(length)
            __import__('time').sleep(0.06)
            body = json.dumps({'status': 'ok', 'value': 'late-secret-for-request-a'}).encode()
            core_socket.sendall(struct.pack('>I', len(body)) + body)
            observed['late_send'] = True
            core_socket.settimeout(0.1)
            observed['second_request'] = bool(core_socket.recv(1))
        except (OSError, TimeoutError):
            pass

    worker = threading.Thread(target=delayed_core)
    worker.start()
    try:
        with pytest.raises(SecretStoreUnavailable, match='credential_channel_unavailable'):
            client.call('get', 'provider', '1:2:' + 'a' * 32)
        __import__('time').sleep(0.08)
        with pytest.raises(SecretStoreUnavailable, match='credential_channel_unavailable') as second:
            client.call('get', 'provider', '1:3:' + 'b' * 32)
        assert 'late-secret-for-request-a' not in str(second.value)
    finally:
        client.close()
        core_socket.close()
        worker.join(1)
    assert observed['second_request'] is False


def test_inherited_descriptor_is_closed_when_socket_construction_fails(monkeypatch):
    import pytest
    from app.services import secrets

    closed = []
    monkeypatch.setattr(secrets.os, 'set_inheritable', lambda *_: None)
    monkeypatch.setattr(secrets.socket, 'socket', lambda **_kwargs: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(secrets.os, 'close', closed.append)

    with pytest.raises(secrets.SecretStoreUnavailable, match='credential_channel_unavailable'):
        secrets.CoreCredentialClient.from_fd(44)
    assert closed == [44]


def synthetic_legacy_ui(monkeypatch, secrets, api, *, restore_status=0):
    state = {'allowed': 1, 'calls': []}

    def get_allowed(output):
        output._obj.value = state['allowed']
        state['calls'].append(('get', state['allowed']))
        return 0

    def set_allowed(value):
        requested = value.value
        state['calls'].append(('set', requested))
        if (len([call for call in state['calls'] if call[0] == 'set']) == 2
                and restore_status != 0):
            return restore_status
        state['allowed'] = requested
        return 0

    monkeypatch.setattr(api, '_sec', SimpleNamespace(
        SecKeychainGetUserInteractionAllowed=NativeCall(get_allowed),
        SecKeychainSetUserInteractionAllowed=NativeCall(set_allowed),
    ))
    monkeypatch.setattr(secrets.PlatformSecretStore, '_legacy_interaction_uncertain', False)
    return state


def test_macos_noninteractive_read_sets_security_ui_fail(monkeypatch):
    from app.services import secrets
    from keyring.backends.macOS import api

    captured = {}
    store = object.__new__(secrets.PlatformSecretStore)
    store.backend = SimpleNamespace(keychain=None)
    monkeypatch.setattr(secrets.sys, 'platform', 'darwin')
    state = synthetic_legacy_ui(monkeypatch, secrets, api)
    monkeypatch.setattr(api, 'k_', lambda name: name)
    monkeypatch.setattr(api, 'create_query', lambda **items: captured.update(items) or object())
    monkeypatch.setattr(api, 'SecItemCopyMatching', lambda query, output: 0)
    monkeypatch.setattr(api.Error, 'raise_for_status', lambda status: None)
    monkeypatch.setattr(api, 'cfstr_to_str', lambda data: 'synthetic-secret')

    assert store.get_no_ui('synthetic-ref') == 'synthetic-secret'
    assert captured['kSecUseAuthenticationUI'] == 'kSecUseAuthenticationUIFail'
    assert captured['kSecAttrService'] == store.SERVICE
    assert captured['kSecAttrAccount'] == 'synthetic-ref'
    assert state['calls'] == [('get', 1), ('set', 0), ('set', 1)]
    assert api._sec.SecKeychainGetUserInteractionAllowed.argtypes is not None
    assert api._sec.SecKeychainSetUserInteractionAllowed.argtypes is not None


def test_macos_noninteractive_read_latches_when_legacy_ui_restore_fails(monkeypatch):
    import pytest
    from app.services import secrets
    from app.services.secrets import SecretStoreUnavailable
    from keyring.backends.macOS import api

    store = object.__new__(secrets.PlatformSecretStore)
    store.backend = SimpleNamespace(get_password=lambda *_: (_ for _ in ()).throw(AssertionError('must stay latched')))
    monkeypatch.setattr(secrets.sys, 'platform', 'darwin')
    synthetic_legacy_ui(monkeypatch, secrets, api, restore_status=-1)
    monkeypatch.setattr(api, 'k_', lambda name: name)
    monkeypatch.setattr(api, 'create_query', lambda **items: object())
    monkeypatch.setattr(api, 'SecItemCopyMatching', lambda query, output: 0)
    monkeypatch.setattr(api.Error, 'raise_for_status', lambda status: None)
    monkeypatch.setattr(api, 'cfstr_to_str', lambda data: 'synthetic-secret')

    with pytest.raises(SecretStoreUnavailable):
        store.get_no_ui('synthetic-ref')
    with pytest.raises(SecretStoreUnavailable):
        store.get('synthetic-ref')


def test_macos_noninteractive_read_maps_legacy_access_statuses_to_explicit_unlock(monkeypatch):
    import pytest
    from app.services import secrets
    from app.services.secrets import SecretInteractionRequired
    from keyring.backends.macOS import api

    store = object.__new__(secrets.PlatformSecretStore)
    store.backend = SimpleNamespace(keychain=None)
    monkeypatch.setattr(secrets.sys, 'platform', 'darwin')
    state = synthetic_legacy_ui(monkeypatch, secrets, api)
    monkeypatch.setattr(api, 'k_', lambda name: name)
    monkeypatch.setattr(api, 'create_query', lambda **items: object())

    for status in (api.error.sec_interaction_not_allowed, api.error.keychain_denied, api.error.sec_auth_failed):
        monkeypatch.setattr(api, 'SecItemCopyMatching', lambda query, output, status=status: status)
        with pytest.raises(SecretInteractionRequired):
            store.get_no_ui('synthetic-ref')

    assert state['allowed'] == 1
    assert state['calls'] == [
        ('get', 1), ('set', 0), ('set', 1),
        ('get', 1), ('set', 0), ('set', 1),
        ('get', 1), ('set', 0), ('set', 1),
    ]


def test_background_read_does_not_wait_for_explicit_keychain_operation(monkeypatch):
    import pytest
    from app.services import secrets
    from app.services.secrets import SecretInteractionRequired

    entered, release = threading.Event(), threading.Event()

    class BlockingBackend:
        def get_password(self, *_):
            entered.set()
            release.wait(2)
            return 'synthetic-secret'

    store = object.__new__(secrets.PlatformSecretStore)
    store.backend = BlockingBackend()
    monkeypatch.setattr(secrets.sys, 'platform', 'darwin')
    monkeypatch.setattr(secrets.PlatformSecretStore, '_legacy_interaction_uncertain', False)
    worker = threading.Thread(target=lambda: store.get('synthetic-ref'))
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(SecretInteractionRequired):
            store.get_no_ui('synthetic-ref')
    finally:
        release.set()
        worker.join(2)


def test_cleared_pending_unlock_cannot_repopulate_memory_cache():
    from app.services.secrets import SessionCredentialCache

    entered = threading.Event()
    release = threading.Event()

    class BlockingStore:
        def get(self, ref):
            entered.set()
            release.wait(2)
            return 'synthetic-secret'

    cache = SessionCredentialCache()
    key = (1, 2, 'synthetic-ref')
    try:
        assert cache.start(key, BlockingStore(), 'synthetic-ref') == 'pending'
        assert entered.wait(1)
        cache.clear(key)
        assert cache.status(key) == 'pending'
        release.set()
        for _ in range(100):
            if cache.status(key) == 'locked':
                break
            threading.Event().wait(.01)
        assert cache.status(key) == 'locked'
        assert cache.get(key) is None
    finally:
        release.set()
        cache.close()


def test_invalidation_rejects_late_noninteractive_cache_fill():
    from app.services.secrets import SessionCredentialCache

    cache = SessionCredentialCache()
    key = (1, 2, 'synthetic-ref')
    try:
        reservation = cache.reserve(key)
        cache.clear(key)
        assert cache.put_if_current(key, reservation, 'synthetic-secret') is False
        assert cache.get(key) is None
    finally:
        cache.close()


def test_global_and_owner_invalidation_reject_untracked_reservations():
    from app.services.secrets import SessionCredentialCache

    cache = SessionCredentialCache()
    key = (7, 2, 'synthetic-ref')
    try:
        global_reservation = cache.reserve(key)
        cache.clear_all()
        assert cache.put_if_current(key, global_reservation, 'synthetic-secret') is False
        owner_reservation = cache.reserve(key)
        cache.clear_owner(7)
        assert cache.put_if_current(key, owner_reservation, 'synthetic-secret') is False
        assert cache.get(key) is None
    finally:
        cache.close()


def test_explicit_unlocks_never_queue_more_than_two_keychain_reads():
    from app.services.secrets import SessionCredentialCache

    entered = threading.Event()
    release = threading.Event()
    started = 0
    lock = threading.Lock()

    class BlockingStore:
        def get(self, ref):
            nonlocal started
            with lock:
                started += 1
                if started == 2:
                    entered.set()
            release.wait(2)
            return 'synthetic-secret'

    cache = SessionCredentialCache()
    try:
        assert cache.start((1, 1, 'first'), BlockingStore(), 'first') == 'pending'
        assert cache.start((1, 2, 'second'), BlockingStore(), 'second') == 'pending'
        assert entered.wait(1)
        assert cache.start((1, 3, 'third'), BlockingStore(), 'third') == 'busy'
        assert started == 2
    finally:
        release.set()
        cache.close()
