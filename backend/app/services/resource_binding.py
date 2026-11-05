"""Immutable, provider-free copies of DB-derived resource-scope proofs."""
from dataclasses import dataclass
from datetime import datetime
import json
from uuid import UUID


def _uuid(value):
    return isinstance(value,str) and str(UUID(value))==value


@dataclass(frozen=True)
class ResourceBinding:
    serialized: str
    lineage: tuple[tuple[str,int], ...]
    execution_room_id: int
    agent_id: int
    run_generation: int
    deadline_ms: int

    def as_dict(self):
        return json.loads(self.serialized)

    def affected(self, revocations):
        return any(scope in revocations and epoch <= revocations[scope]
                   for scope,epoch in self.lineage)


def freeze_binding(value):
    if value is None:return None
    if isinstance(value,ResourceBinding):return value
    if hasattr(value,'model_dump'):value=value.model_dump(mode='json')
    if not isinstance(value,dict):raise ValueError('Invalid resource binding')
    required={'root_scope_id','scope_id','owner_id','root_room_id','root_request_event_id',
        'execution_room_id','agent_id','run_generation','job_id','phase','context_mode','deadline_at','lineage'}
    if set(value)-{'version'} != required or type(value.get('version',1)) is not int or value.get('version',1)!=1:
        raise ValueError('Invalid resource binding fields')
    for name in ('root_scope_id','scope_id','root_request_event_id'):
        if not _uuid(value[name]):raise ValueError('Invalid resource identity')
    for name in ('owner_id','root_room_id','execution_room_id','agent_id'):
        if type(value[name]) is not int or value[name]<1:raise ValueError('Invalid resource recipient')
    if type(value['run_generation']) is not int or value['run_generation']<0:raise ValueError('Invalid resource generation')
    if value['context_mode'] not in ('user_room','delegated'):raise ValueError('Invalid resource purpose')
    if value['job_id'] is not None and not _uuid(value['job_id']):raise ValueError('Invalid resource job')
    if value['phase'] not in (None,'response','return') or (value['job_id'] is None)!=(value['phase'] is None):
        raise ValueError('Invalid resource phase')
    rows=value['lineage']
    if not isinstance(rows,(list,tuple)) or not 1<=len(rows)<=8:raise ValueError('Invalid resource lineage')
    lineage=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'scope_id','epoch'} or not _uuid(row['scope_id']) or type(row['epoch']) is not int or row['epoch']<0:
            raise ValueError('Invalid resource epoch')
        lineage.append((row['scope_id'],row['epoch']))
    if len(set(scope for scope,_ in lineage))!=len(lineage) or lineage[0][0]!=value['root_scope_id'] or lineage[-1][0]!=value['scope_id']:
        raise ValueError('Invalid resource ancestry')
    deadline=datetime.fromisoformat(value['deadline_at'].replace('Z','+00:00'))
    if deadline.tzinfo is None or deadline.utcoffset().total_seconds()!=0:raise ValueError('Invalid resource deadline')
    return ResourceBinding(json.dumps(value,sort_keys=True,separators=(',',':')),tuple(lineage),
        value['execution_room_id'],value['agent_id'],value['run_generation'],int(deadline.timestamp()*1000))


def revocation_map(bindings):
    result={}
    if not isinstance(bindings,(list,tuple)) or len(bindings)>1024:raise ValueError('Invalid revocation set')
    for item in bindings:
        if not isinstance(item,dict) or not _uuid(item.get('scope_id')) or type(item.get('epoch')) is not int or item['epoch']<0:
            raise ValueError('Invalid revocation identity')
        result[item['scope_id']]=max(result.get(item['scope_id'],-1),item['epoch'])
    return result
