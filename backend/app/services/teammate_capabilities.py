"""Bounded, advisory recipient capability snapshots for collaboration tools."""
import json
from copy import deepcopy
from dataclasses import replace

from app.models.models import Agent
from app.models.collaboration import ProviderConnection
from app.services.connections import ConnectionConfig
from app.services.model_features import record, permitted, bind_saved_text_test
from app.services import provider_catalog
from app.services.providers.base import passthrough_tool_schemas
from app.services.providers.chatgpt import chatgpt_filtered_tools

# Representative concrete tool names, not an authority or resource grant.
TOOLS = ('start_process', 'read_file', 'write_file', 'browser_navigate',
         'browser_inspect', 'browser_interact', 'screenshot')


def snapshot(agent, connection, owner_id, db=None):
    result = {
        'version': 1,
        'model_id': agent.model_id,
        'evidence': 'unknown',
        'adapter_filter': {name: 'unknown' for name in TOOLS},
        'execution': 'unknown',
    }
    if (not connection or connection.user_id != owner_id or not connection.enabled
            or not agent.model_id):
        return result
    config = ConnectionConfig(connection.id, connection.kind, agent.model_id,
                              connection.base_url, None, connection.capabilities or {},
                              provider_id=connection.provider_id,
                              protocol_id=connection.protocol_id,
                              config_revision=connection.readiness_revision,
                              auth_grant_revision=connection.auth_grant_revision)
    saved = record(config)
    result['evidence'] = 'current_model_revision' if saved else 'unknown_or_stale'
    # Require an actual matching saved test fingerprint; never promote a cached
    # model_features record into test evidence by itself.
    evidence_caps = deepcopy(config.capabilities)
    evidence_caps.pop('model_features', None)
    evidence_config = replace(config, capabilities=evidence_caps)
    if db is not None:
        bind_saved_text_test(db, connection, evidence_config)
    test_record = record(evidence_config)
    result['model_tool_test'] = ('passed' if test_record.get('source') == 'saved_connection_tests'
                                 and permitted(evidence_config, 'tool_calling') else 'unknown')
    if test_record.get('source') == 'saved_connection_tests':
        result['evidence'] = 'matching_saved_test'
    if connection.provider_id == 'opencode-go':
        if agent.model_id in provider_catalog.OPENCODE_GO_RESPONSES_MODELS or connection.protocol_id == 'openai_responses':
            result['adapter_support'] = 'unsupported_protocol'
        elif ((connection.protocol_id == 'openai_chat_completions' and agent.model_id in provider_catalog.OPENCODE_GO_CHAT_MODELS)
              or (connection.protocol_id == 'anthropic_messages' and agent.model_id in provider_catalog.OPENCODE_GO_MESSAGES_MODELS)):
            passed = {t['name'] for t in passthrough_tool_schemas([{'name': n} for n in TOOLS])}
            result['adapter_filter'] = {name: ('no_name_filter' if name in passed else 'unknown') for name in TOOLS}
            result['adapter_support'] = 'known_model_protocol'
        else:
            result['adapter_support'] = 'unknown_or_mismatched_model_protocol'
    if connection.provider_id == 'chatgpt' and connection.protocol_id == 'openai_responses':
        offered = {t['name'] for t in chatgpt_filtered_tools(config, [{'name': n} for n in TOOLS])}
        result['adapter_filter'] = {name: ('passes_filter' if name in offered else 'blocked_by_filter')
                                    for name in TOOLS}
    return result


def annotate_roster(db, owner_id, roster):
    for entry in roster:
        agent = db.get(Agent, entry['agent_id'])
        if not agent or agent.user_id != owner_id or not agent.is_active:
            continue
        connection = db.get(ProviderConnection, agent.connection_id) if agent.connection_id else None
        entry['capabilities'] = snapshot(agent, connection, owner_id, db)
    return roster


ROSTER_NOTE = ("Advisory snapshot only: passing an adapter filter does not establish "
               "that a tool is offered, authorized, approved, or will succeed. "
               "Delegate the actual task when appropriate; do not ask colleagues to "
               "probe capabilities. Report unknown or unavailable honestly.")
ROSTER_LIMIT = 15000  # Below the shared 16000-character observation limit.


def roster_page(db, owner_id, rows, key, offset):
    """Return complete JSON within the observation budget; offsets follow current roster order."""
    total = len(rows)
    start = min(offset, total)
    result = {key: [], 'capability_note': ROSTER_NOTE,
              'page': {'offset': start, 'total': total, 'next_offset': None,
                       'omitted': total - start, 'fields_truncated': False,
                       'order_note': 'Roster may change between pages.'}}
    for index in range(start, total):
        row = dict(rows[index])
        annotate_roster(db, owner_id, [row])
        # Preserve identity even when arbitrary role/avatar content exceeds the budget.
        if len(json.dumps(row, ensure_ascii=False, default=str)) > 8000:
            row = {'agent_id': row['agent_id'], 'name': str(row.get('name') or '')[:512],
                   'fields_truncated': True, 'capabilities': {'execution': 'unknown',
                   'evidence': 'omitted_oversized'}}
            result['page']['fields_truncated'] = True
        result[key].append(row)
        page = result['page']
        page['omitted'] = total - index - 1
        page['next_offset'] = index + 1 if page['omitted'] else None
        if len(json.dumps(result, ensure_ascii=False, default=str)) > ROSTER_LIMIT:
            result[key].pop()
            page['omitted'] = total - index
            page['next_offset'] = index
            break
    return result
