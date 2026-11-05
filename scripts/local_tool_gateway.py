#!/usr/bin/env python3
"""Loopback-only, exact-model tool allowlist for an existing local OpenAI server.

This does not execute tools or grant consent. Gamja's native broker remains the
executor. Policy entries are operator-recorded *actual* tool evidence, not model
advertisements. No chat, arguments, credentials, or model outputs are logged.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

LIMIT = 1_000_000
KNOWN = frozenset({'read_file', 'write_file', 'move_file', 'delete_file'})
PROBE = 'gamja_' + 'probe_echo'.encode().hex()
READ = 'gamja_' + 'read_file'.encode().hex()
WRITE = 'gamja_' + 'write_file'.encode().hex()
EDIT = 'gamja_' + 'edit_file'.encode().hex()
WORKFLOW = 'read_then_replace_literal'
MULTILINE_WORKFLOW = 'read_then_replace_multiline_literal'
EDIT_GUIDANCE = {
    'invalid_literal_edit': '기존 블록과 새 블록을 각각 2,000자 이하로 정확히 알려주세요. 기존 블록은 비워둘 수 없으며, 같거나 지원하지 않는 줄바꿈이면 바꾸지 않습니다.',
    'literal_edit_file_not_found': '파일을 찾지 못했습니다. 현재 파일의 정확한 절대 경로를 확인해 다시 요청해주세요.',
    'literal_edit_regular_file_required': '이 기능은 일반 텍스트 파일의 정확한 절대 경로만 지원합니다. 폴더나 심볼릭 링크 대신 원래 파일을 지정해주세요.',
    'literal_edit_file_too_large': '파일이 부분 수정 한도인 12,000바이트를 넘습니다. 필요한 내용을 별도의 작은 작업 파일로 나눈 뒤 요청해주세요. 원본은 자동으로 나누지 않습니다.',
    'literal_edit_read_binding_changed': '읽기 결과와 현재 파일 상태가 일치하는지 확인하지 못했습니다. 최신 내용을 기준으로 다시 요청해주세요. 이전 승인을 재사용하거나 자동으로 재시도하지 않습니다.',
    'literal_edit_incomplete_read': '읽기 결과에서 전체 원문을 확인하지 못했습니다. 작은 UTF-8 텍스트 파일을 지정해주세요. 생략되거나 가려진 부분을 추측해 쓰지 않습니다.',
    'literal_edit_text_not_found': '지정한 기존 블록이 파일에 없습니다. 현재 파일에서 바꿀 구간을 줄바꿈·들여쓰기까지 그대로 복사해 다시 요청해주세요.',
    'literal_edit_text_ambiguous': '지정한 기존 블록이 두 곳 이상에 나타납니다. 바꿀 위치의 앞뒤 줄을 포함해 한 곳만 일치하도록 기존 블록과 새 블록을 다시 알려주세요. 임의로 첫 번째 위치를 선택하지 않습니다.',
    'literal_edit_no_change': '기존 블록과 새 블록이 같습니다. 실제로 바꿀 내용을 포함해 다시 요청해주세요.',
    'multiline_source_newlines_not_verified': '여러 줄 수정은 LF 또는 CRLF로 통일된 파일만 지원합니다. 줄바꿈이 섞여 있어 자동 변환하지 않았습니다. 별도 작업 사본에서 줄바꿈을 정리한 뒤 요청해주세요.',
}


def stamp(path):
    s = Path(path).stat()
    return {k: getattr(s, k) for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}


def process_stamp(pid):
    return subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart=,comm='], text=True).strip()


class Refused(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status


def edit_refusal_response(code, model_id):
    """A truthful final chat message, with no tool calls or inferred file result.

    Only finite edit-planning failures qualify. Authentication, policy identity,
    unknown tools and infrastructure faults still use the HTTP error path.
    Raw paths, file contents and provider errors never enter these messages.
    """
    hint = EDIT_GUIDANCE.get(code)
    if hint is None:
        return None
    return {'id': 'gamja-edit-blocked-' + uuid.uuid4().hex, 'object': 'chat.completion',
            'created': int(time.time()), 'model': model_id,
            'gamja_local_result': {'state': 'blocked', 'code': code, 'write_proposed': False},
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': '이번 부분 수정은 실행하지 않았습니다.\n\n' + hint}}]}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def load_policy(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) & 0o077:
        raise Refused('invalid_private_policy', 503)
    p = json.loads(path.read_text())
    u = urlsplit(p['upstream'])
    if (p.get('version') != 1 or u.scheme != 'http' or u.hostname != '127.0.0.1'
            or u.username or u.password or u.path or u.query or u.fragment):
        raise Refused('invalid_upstream', 503)
    if (stamp(p['model_file']) != p['model_stamp']
            or process_stamp(p['server_pid']) != p['process_stamp']):
        raise Refused('model_identity_changed_recheck_required', 503)
    allowed = p.get('tools', {})
    if not isinstance(allowed, dict) or not set(allowed) <= KNOWN:
        raise Refused('invalid_tool_policy', 503)
    for family, proof in allowed.items():
        if (not isinstance(proof, dict) or proof.get('tested') is not True
                or not isinstance(proof.get('evidence'), str) or not Path(proof['evidence']).is_file()):
            raise Refused('actual_tool_evidence_required', 503)
        if family == 'write_file' and not {'create', 'replace_existing'} <= set(proof.get('operations', [])):
            raise Refused('write_create_and_edit_evidence_required', 503)
    proof = p.get('partial_edit')
    if proof:
        evidence = json.loads(Path(proof['evidence']).read_text())
        if (proof.get('tested') is not True or evidence.get('passed') is not True
                or evidence.get('workflow') != WORKFLOW
                or evidence.get('model_stamp') != p['model_stamp']
                or not {'read_file', 'write_file'} <= set(allowed)):
            raise Refused('actual_partial_edit_evidence_required', 503)
    proof = p.get('multiline_edit')
    if proof:
        evidence = json.loads(Path(proof['evidence']).read_text())
        if (not p.get('partial_edit') or proof.get('tested') is not True
                or evidence.get('passed') is not True
                or evidence.get('workflow') != MULTILINE_WORKFLOW
                or evidence.get('model_stamp') != p['model_stamp']
                or not {'LF', 'CRLF'} <= set(evidence.get('native_passed_endings', []))):
            raise Refused('actual_multiline_edit_evidence_required', 503)
    return p


def message_key(messages):
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PartialEdits:
    """Compile a literal edit through two separately approved native actions.

    The relay never opens file contents or executes an OS tool. Pending edits
    are memory-only, expire, and bind to the exact initial conversation and
    native read call. The original text comes only from its completed result.
    """
    def __init__(self, experimental_root=None):
        self.root = experimental_root
        self.pending = {}
        self.lock = threading.Lock()

    def enabled(self, policy):
        return bool(policy.get('partial_edit') or self.root)

    def multiline(self, policy):
        return bool(policy.get('multiline_edit') or self.root)

    def offer(self, body, policy, offered):
        if not self.enabled(policy) or any(m.get('role') == 'tool' for m in body['messages']):
            return body, offered
        if not {READ, WRITE} <= offered:
            return body, offered
        tool = {'type': 'function', 'function': {
            'name': EDIT,
            'description': ('Safely edit an existing UTF-8 text file: replace exactly one literal '
                            + ('contiguous block, including multiple lines. ' if self.multiline(policy)
                               else 'single-line occurrence. ')
                            + 'Use this instead of read_file plus write_file '
                            'for partial changes. Preserves all other bytes. Native read and write '
                            'each require approval. Never include full file contents.'),
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string'}, 'old_text': {'type': 'string'},
                'new_text': {'type': 'string'}},
                'required': ['path', 'old_text', 'new_text'], 'additionalProperties': False}}}
        return {**body, 'tools': [*body.get('tools', []), tool],
                'parallel_tool_calls': False}, offered | {EDIT}

    def translate(self, data, body, policy):
        if not self.enabled(policy):
            return data
        for choice in data['choices']:
            for call in choice['message'].get('tool_calls') or []:
                if call['function']['name'] != EDIT:
                    continue
                args = json.loads(call['function']['arguments'])
                if (set(args) != {'path', 'old_text', 'new_text'}
                        or not all(isinstance(v, str) for v in args.values())
                        or not args['old_text'] or args['old_text'] == args['new_text']
                        or any('\x00' in args[k] for k in ('old_text', 'new_text'))
                        or any('\r' in args[k].replace('\r\n', '') for k in ('old_text', 'new_text'))
                        or (not self.multiline(policy) and any('\n' in args[k] for k in ('old_text', 'new_text')))
                        or max(len(args['old_text']), len(args['new_text'])) > 2000):
                    raise Refused('invalid_literal_edit', 502)
                path = Path(args['path'])
                try:
                    regular = stat.S_ISREG(path.lstat().st_mode)
                except (FileNotFoundError, NotADirectoryError):
                    raise Refused('literal_edit_file_not_found', 502) from None
                if (not path.is_absolute() or path.is_symlink() or not regular
                        or (self.root and not path.resolve().is_relative_to(self.root))):
                    raise Refused('literal_edit_regular_file_required', 502)
                identity = stamp(path)
                if identity['st_size'] > 12000:
                    raise Refused('literal_edit_file_too_large', 502)
                call_id = call.get('id')
                if not isinstance(call_id, str) or not call_id:
                    raise Refused('invalid_edit_call_id', 502)
                entry = {'args': args, 'stamp': identity, 'context': message_key(body['messages']),
                         'expires': time.monotonic() + 600}
                with self.lock:
                    self.pending = {k: v for k, v in self.pending.items() if v['expires'] > time.monotonic()}
                    if len(self.pending) >= 32 or call_id in self.pending:
                        raise Refused('pending_edit_limit', 503)
                    self.pending[call_id] = entry
                call['function'] = {'name': READ, 'arguments': json.dumps({'path': str(path)})}
        return data

    def continuation(self, body, policy):
        messages = body['messages']
        if not self.enabled(policy) or len(messages) < 2:
            return None
        assistant, observation = messages[-2:]
        if assistant.get('role') != 'assistant' or observation.get('role') != 'tool':
            return None
        call_id = observation.get('tool_call_id')
        with self.lock:
            pending = self.pending.pop(call_id, None)
        if pending is None:
            return None
        calls = assistant.get('tool_calls') or []
        args = pending['args']
        text = observation.get('content')
        if (pending['expires'] <= time.monotonic()
                or message_key(messages[:-2]) != pending['context']
                or len(calls) != 1 or calls[0].get('id') != call_id
                or calls[0].get('function', {}).get('name') != READ
                or json.loads(calls[0]['function']['arguments']) != {'path': args['path']}
                or not isinstance(text, str) or not text.startswith('completed: ')
                or stamp(args['path']) != pending['stamp']):
            raise Refused('literal_edit_read_binding_changed', 409)
        original = text[len('completed: '):]
        if len(original.encode('utf-8')) != pending['stamp']['st_size'] or '\x00' in original:
            raise Refused('literal_edit_incomplete_read', 409)
        old, new = args['old_text'], args['new_text']
        pure_crlf = '\r\n' in original and not any(c in original.replace('\r\n', '') for c in '\r\n')
        if ('\n' in old or '\n' in new) and '\r' in original and not pure_crlf:
            raise Refused('multiline_source_newlines_not_verified', 409)
        # Chat/tool arguments commonly use LF. For a uniformly CRLF source,
        # adapt the two literals only; never normalize the untouched file.
        if self.multiline(policy) and pure_crlf:
            old = old.replace('\r\n', '\n').replace('\n', '\r\n')
            new = new.replace('\r\n', '\n').replace('\n', '\r\n')
        if not old or old == new:
            raise Refused('literal_edit_no_change', 409)
        if original.find(old) < 0:
            raise Refused('literal_edit_text_not_found', 409)
        if original.find(old) != original.rfind(old):
            raise Refused('literal_edit_text_ambiguous', 409)
        content = original.replace(old, new, 1)
        return {'id': 'gamja-edit-' + uuid.uuid4().hex, 'object': 'chat.completion',
                'created': int(time.time()), 'model': policy['model_id'],
                'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                    'role': 'assistant', 'content': None, 'tool_calls': [{
                        'id': 'edit_' + uuid.uuid4().hex, 'type': 'function',
                        'function': {'name': WRITE, 'arguments': json.dumps({
                            'path': args['path'], 'content': content}, ensure_ascii=False)}}]}}]}


def wire_names(policy):
    return {'gamja_' + name.encode().hex() for name in policy['tools']}


def filter_request(body, policy):
    if not isinstance(body, dict) or body.get('model') != policy['model_id']:
        raise Refused('exact_model_required')
    if body.get('stream') is True:
        raise Refused('stream_not_supported')
    if any(k in body for k in ('functions', 'function_call', 'modalities', 'audio', 'response_format')):
        raise Refused('unverified_protocol_feature')
    allowed = wire_names(policy)
    messages = body.get('messages')
    if not isinstance(messages, list):
        raise Refused('invalid_messages')
    for message in messages:
        if not isinstance(message, dict):
            raise Refused('invalid_messages')
        content = message.get('content')
        if isinstance(content, list) and any(not isinstance(x, dict) or x.get('type') != 'text' for x in content):
            raise Refused('image_audio_input_not_verified')
        for call in message.get('tool_calls') or []:
            if (not isinstance(call, dict) or not isinstance(call.get('function'), dict)
                    or call['function'].get('name') not in allowed | {PROBE}):
                raise Refused('unverified_tool_continuation')
    # Generic tool chains remain blocked. PartialEdits handles the separately
    # proven read -> literal replacement -> write workflow before model dispatch.
    continuation = any(m.get('role') == 'tool' for m in messages)
    selectable = set() if continuation else allowed | {PROBE}
    tools = body.get('tools')
    offered = set()
    if tools is not None:
        if not isinstance(tools, list):
            raise Refused('invalid_tools')
        selected = []
        for t in tools:
            if not isinstance(t, dict) or t.get('type') != 'function':
                raise Refused('invalid_tools')
            if not isinstance(t.get('function'), dict):
                raise Refused('invalid_tools')
            name = t['function'].get('name')
            # probe_echo is a virtual protocol self-test, never an OS tool.
            if name in selectable:
                selected.append(t)
                offered.add(name)
        if tools and not selected and not continuation:
            raise Refused('no_verified_requested_tools')
        body = {**body, 'tools': selected}
        if not selected:
            body.pop('tools', None)
    choice = body.get('tool_choice')
    if isinstance(choice, dict) and (not isinstance(choice.get('function'), dict)
                                    or choice['function'].get('name') not in offered):
        raise Refused('unverified_tool_choice')
    return body, offered


def validate_response(data, offered):
    choices = data.get('choices')
    if not isinstance(choices, list) or not choices:
        raise Refused('invalid_upstream_response', 502)
    total_calls = 0
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
            raise Refused('invalid_upstream_response', 502)
        message = choice['message']
        if message.get('function_call'):
            raise Refused('unverified_legacy_tool_response', 502)
        for call in message.get('tool_calls') or []:
            total_calls += 1
            if (not isinstance(call, dict) or call.get('type') != 'function'
                    or not isinstance(call.get('function'), dict)
                    or call['function'].get('name') not in offered):
                raise Refused('unverified_tool_response_blocked', 502)
    if total_calls > 1:
        raise Refused('multiple_tool_actions_not_verified', 502)


class Gateway:
    def __init__(self, policy, state):
        self.policy, self.state = policy, state
        self.lock = threading.Lock()
        self.counters = {'forwarded_chat_requests': 0, 'blocked_requests': 0, 'blocked_responses': 0}

    def count(self, name):
        with self.lock:
            self.counters[name] += 1
            temp = self.state.with_suffix('.pending')
            with temp.open('w') as f:
                json.dump({'pid': os.getpid(), **self.counters}, f)
            temp.chmod(0o600)
            os.replace(temp, self.state)

    def upstream(self, policy, path, body=None):
        request = Request(policy['upstream'] + path,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=125) as response:
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise Refused('upstream_response_too_large', 502)
        return json.loads(raw)


def serve(policy_path, port, state_path, experimental_root=None):
    gateway = Gateway(policy_path, state_path)
    edits = PartialEdits(experimental_root)
    load_policy(policy_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            policy = None
            try:
                origin = self.headers.get('Origin')
                if origin and origin != f'http://127.0.0.1:{port}':
                    raise Refused('cross_origin_refused', 403)
                policy = load_policy(policy_path)
                if self.command == 'GET' and self.path == '/gamja/tool-capabilities':
                    return self.reply(200, {'model_id': policy['model_id'], 'tools': policy['tools'],
                                           'other_tools': 'blocked', 'max_tools_per_response': 1,
                                           'tools_after_tool_result': 'verified_literal_edit_only' if edits.enabled(policy) else 'blocked',
                                           'partial_edit': WORKFLOW if edits.enabled(policy) else 'blocked',
                                           'multiline_edit': MULTILINE_WORKFLOW if edits.multiline(policy) else 'blocked',
                                           'approval': 'Gamja broker unchanged'})
                if self.command == 'GET' and self.path in ('/v1/models', '/props'):
                    data = gateway.upstream(policy, self.path)
                    if self.path == '/v1/models':
                        data['data'] = [m for m in data.get('data', []) if m.get('id') == policy['model_id']]
                        if not data['data']:
                            raise Refused('upstream_model_changed', 503)
                    elif data.get('model_path') and data['model_path'] != policy['model_file']:
                        raise Refused('upstream_model_changed', 503)
                    return self.reply(200, data)
                if self.command != 'POST' or self.path != '/v1/chat/completions':
                    raise Refused('endpoint_not_verified', 404)
                if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    raise Refused('json_required', 415)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= LIMIT:
                    raise Refused('request_too_large', 413)
                if self.headers.get('Transfer-Encoding'):
                    raise Refused('transfer_encoding_not_supported')
                body, offered = filter_request(json.loads(self.rfile.read(length)), policy)
                data = edits.continuation(body, policy)
                if data is None:
                    body, offered = edits.offer(body, policy, offered)
                    gateway.count('forwarded_chat_requests')
                    data = gateway.upstream(policy, '/v1/chat/completions', body)
                    validate_response(data, offered)
                    data = edits.translate(copy.deepcopy(data), body, policy)
                else:
                    offered = {WRITE}
                # Recheck the exact file/process after the generation await, too.
                current = load_policy(policy_path)
                if current != policy:
                    raise Refused('policy_changed_during_request', 409)
                try:
                    validate_response(data, (offered - {EDIT}) | ({READ} if EDIT in offered else set()))
                except Refused:
                    gateway.count('blocked_responses')
                    raise
                self.reply(200, data)
            except Refused as e:
                gateway.count('blocked_requests')
                guidance = edit_refusal_response(e.code, policy['model_id']) if policy else None
                if guidance is not None:
                    return self.reply(200, guidance)
                self.reply(e.status, {'error': {'code': e.code, 'message': 'Local capability policy refused this request.'}})
            except (ValueError, TypeError, KeyError, OSError, HTTPError, URLError, subprocess.SubprocessError):
                gateway.count('blocked_requests')
                self.reply(503, {'error': {'code': 'local_gateway_unavailable', 'message': 'No verified response was delivered.'}})

    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18093)
    parser.add_argument('--experimental-edit-root', type=Path,
                        help='Test only: restrict unproven literal edits to this private owned fixture directory')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.experimental_edit_root
    if root:
        root = root.resolve(strict=True)
        s = root.stat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) & 0o077:
            parser.error('experimental fixture directory must be private and owned')
    serve(args.policy, args.port, args.state, root)
