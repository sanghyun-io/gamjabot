"""Stable teammate-task identity for custody; never an effect grant or a lease.

This module is intentionally not connected to execution until Core can enforce
its separately issued, expiring lease and durable cancellation protocol.
"""
from dataclasses import dataclass
import hashlib
import json

from app.models.collaboration import RoomAgentRun
from app.models.bot_message import BotMessage
from app.models.bot_batch import BotBatch
from app.models.execution_scope import ExecutionScope
from app.models.teammate import TeammateHandoff
from app.services import bot_messages, execution_scopes, teammates
from app.services.action_review import ReviewChanged


@dataclass(frozen=True)
class TaskIdentity:
    canonical: str

    def as_dict(self):
        return json.loads(self.canonical)

    @property
    def digest(self):
        return hashlib.sha256(self.canonical.encode('utf-8')).hexdigest()

    def affected_by_stop(self, room_id, agent_id=None):
        """Match an already-validated same-room task or any ancestor participant."""
        try:
            value=self.as_dict()
            return bool(value.get('task_kind')!='cross' and value['room_id']==room_id
                and (agent_id is None or any(agent_id in (node['sender_agent_id'],node['recipient_agent_id'])
                                             for node in value['lineage'])))
        except (KeyError,TypeError,ValueError):
            return False


def _identity(db, *, owner_id, room_id, agent_id, root_id, generation, job, execution_binding):
    if (type(owner_id) is not int or owner_id < 1 or type(room_id) is not int or room_id < 1
            or type(agent_id) is not int or agent_id < 1 or type(generation) is not int or generation < 1
            or not isinstance(root_id,str) or not isinstance(job,dict)
            or job.get('phase') not in ('response','return') or not isinstance(job.get('handoff_id'),str)):
        raise ReviewChanged('Teammate task identity is unavailable')
    try:
        root_scope=execution_scopes.root_scope(db,root_id)
        scoped=None
        if root_scope is None:
            if (execution_binding is not None
                    or db.query(BotMessage).filter_by(root_request_event_id=root_id).first() is not None
                    or db.query(BotBatch).filter_by(root_request_event_id=root_id).first() is not None):
                raise ReviewChanged('Typed task ancestry is unavailable')
        else:
            if not isinstance(execution_binding,dict):raise ReviewChanged('Scoped task proof is unavailable')
            scope=execution_scopes.require_binding(db,execution_binding)
            delivery=db.get(ExecutionScope,execution_scopes.identity(root_id,'teammate:'+job['handoff_id']))
            expected=delivery if job['phase']=='response' else db.get(ExecutionScope,delivery.parent_scope_id) if delivery else None
            if (not expected or scope.scope_id!=expected.scope_id or execution_binding.get('owner_id')!=owner_id
                    or execution_binding.get('execution_room_id')!=room_id or execution_binding.get('agent_id')!=agent_id
                    or execution_binding.get('root_request_event_id')!=root_id or execution_binding.get('run_generation')!=generation):
                raise ReviewChanged('Scoped teammate task changed')
            scoped=[{'scope_id':entry.scope_id,'parent_scope_id':entry.parent_scope_id,'kind':entry.kind,
                     'job_id':entry.job_id,'job_kind':entry.job_kind,'epoch':entry.epoch}
                    for entry in execution_scopes.lineage(db,scope)]
        teammates.require_job_current(db,job)
        row=db.get(TeammateHandoff,job['handoff_id'],populate_existing=True)
        run=db.get(RoomAgentRun,(room_id,agent_id),populate_existing=True)
        if (not row or (row.owner_id,row.room_id,row.root_request_event_id)!=(owner_id,room_id,root_id)
                or (row.recipient_agent_id if job['phase']=='response' else row.sender_agent_id)!=agent_id
                or not run or run.state!='running' or run.paused or run.generation!=generation
                or run.trigger_event_id!=root_id):
            raise ReviewChanged('Teammate task identity changed')
        chain=[];seen=set();current=row
        while current:
            if (current.handoff_id in seen or len(chain)>=teammates.MAX_DEPTH
                    or (current.owner_id,current.room_id,current.root_request_event_id)!=(owner_id,room_id,root_id)
                    or current.state in ('cancelled','interrupted') or current.reply_state=='cancelled'):
                raise ReviewChanged('Teammate ancestry changed')
            seen.add(current.handoff_id)
            chain.append({'handoff_id':current.handoff_id,'sender_agent_id':current.sender_agent_id,
                          'recipient_agent_id':current.recipient_agent_id})
            if not current.parent_handoff_id:break
            parent=db.get(TeammateHandoff,current.parent_handoff_id,populate_existing=True)
            if (not parent or current.sender_agent_id not in (parent.sender_agent_id,parent.recipient_agent_id)
                    or current.depth!=parent.depth+1):
                raise ReviewChanged('Teammate ancestry is unavailable')
            current=parent
        if current.depth!=1:
            raise ReviewChanged('Teammate ancestry is incomplete')
        value={'version':1,'owner_id':owner_id,'room_id':room_id,'agent_id':agent_id,'root_request_event_id':root_id,
               'run_generation':generation,'handoff_id':row.handoff_id,'phase':job['phase'],
               'lineage':list(reversed(chain)),'scope_lineage':scoped}
        return TaskIdentity(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False))
    except ReviewChanged:
        raise
    except Exception as error:
        raise ReviewChanged('Teammate task identity is unavailable') from error


def bind(db, *, owner_id, room_id, agent_id, root_id, generation, job, execution_binding=None):
    """Freeze actual DB identity while task is active, without creating a scope."""
    return _identity(db,owner_id=owner_id,room_id=room_id,agent_id=agent_id,root_id=root_id,generation=generation,job=job,execution_binding=execution_binding)


def bind_cross(db, *, owner_id, room_id, agent_id, root_id, generation, job, execution_binding):
    """Freeze cross-room identity from the existing trusted resolver and typed scope chain."""
    if not isinstance(job,dict) or job.get('phase') not in ('response','return') or not isinstance(job.get('message_id'),str):
        raise ReviewChanged('Cross-room task identity is unavailable')
    try:
        job_id=job['message_id'];phase=job['phase']
        origin=bot_messages.resolve_execution_origin(db,job_id,phase,room_id,agent_id,generation)
        scope=execution_scopes.require_binding(db,execution_binding)
        message=db.get(BotMessage,job_id,populate_existing=True)
        batch=db.get(BotBatch,job_id,populate_existing=True)
        if (bool(message)==bool(batch) or origin['owner_id']!=owner_id or origin['root_request_event_id']!=root_id
                or execution_binding.get('job_id')!=job_id or execution_binding.get('phase')!=phase
                or execution_binding.get('execution_room_id')!=room_id or execution_binding.get('agent_id')!=agent_id
                or execution_binding.get('run_generation')!=generation or execution_binding.get('owner_id')!=owner_id):
            raise ReviewChanged('Cross-room task identity changed')
        task=message or batch
        nodes=[];seen=set();current=task;kind='bot_message' if message else 'bot_batch'
        while current is not None:
            ident=current.message_id if kind=='bot_message' else current.batch_id if kind=='bot_batch' else current.handoff_id
            key=(kind,ident)
            if (key in seen or len(nodes)>=teammates.MAX_DEPTH+1 or current.owner_id!=owner_id
                    or current.root_request_event_id!=root_id or current.state in ('cancelled','interrupted')
                    or current.reply_state=='cancelled'):
                raise ReviewChanged('Cross-room ancestry changed')
            seen.add(key)
            sender_room=getattr(current,'sender_room_id',getattr(current,'room_id',None))
            node={'kind':kind,'id':ident,'sender_room_id':sender_room,'sender_agent_id':current.sender_agent_id}
            if kind=='bot_message':
                node.update(recipient_room_id=current.recipient_room_id,recipient_agent_id=current.recipient_agent_id)
                if current.batch_id:
                    owner_batch=db.get(BotBatch,current.batch_id,populate_existing=True)
                    delivery=db.get(ExecutionScope,execution_scopes.identity(root_id,'bot_message:'+current.message_id))
                    if (not owner_batch or (owner_batch.owner_id,owner_batch.root_request_event_id,
                            owner_batch.sender_room_id,owner_batch.sender_agent_id,owner_batch.parent_kind,owner_batch.parent_job_id)
                            !=(owner_id,root_id,current.sender_room_id,current.sender_agent_id,current.parent_kind,current.parent_job_id)
                            or owner_batch.state in ('cancelled','interrupted') or owner_batch.reply_state=='cancelled'
                            or type(current.batch_index) is not int or current.batch_index<0 or not delivery
                            or delivery.parent_scope_id!=owner_batch.source_scope_id):
                        raise ReviewChanged('Batch membership changed')
                    node.update(batch_id=owner_batch.batch_id,batch_index=current.batch_index,
                                batch_source_scope_id=owner_batch.source_scope_id)
            nodes.append(node)
            parent_id=getattr(current,'parent_job_id',None) if kind!='teammate' else current.parent_handoff_id
            parent_kind=getattr(current,'parent_kind',None) if kind!='teammate' else 'teammate'
            if not parent_id:break
            cls=BotMessage if parent_kind=='bot_message' else TeammateHandoff if parent_kind=='teammate' else None
            parent=db.get(cls,parent_id,populate_existing=True) if cls else None
            if not parent:raise ReviewChanged('Cross-room parent is unavailable')
            parent_room=getattr(parent,'recipient_room_id',getattr(parent,'room_id',None))
            sender_room_of_parent=getattr(parent,'sender_room_id',getattr(parent,'room_id',None))
            if (sender_room,current.sender_agent_id) not in ((parent_room,parent.recipient_agent_id),
                    (sender_room_of_parent,parent.sender_agent_id)):
                raise ReviewChanged('Cross-room parent recipient changed')
            current=parent;kind=parent_kind
        scoped=[{'scope_id':entry.scope_id,'parent_scope_id':entry.parent_scope_id,'kind':entry.kind,
                 'job_id':entry.job_id,'job_kind':entry.job_kind,'epoch':entry.epoch}
                for entry in execution_scopes.lineage(db,scope)]
        value={'version':1,'task_kind':'cross','owner_id':owner_id,'room_id':room_id,'agent_id':agent_id,
               'root_request_event_id':root_id,'run_generation':generation,'job_id':job_id,'phase':phase,
               'lineage':list(reversed(nodes)),'scope_lineage':scoped}
        return TaskIdentity(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False))
    except ReviewChanged:raise
    except Exception as error:raise ReviewChanged('Cross-room task identity is unavailable') from error


def require_current(db, identity, *, execution_binding=None):
    """Revalidate an immutable identity; approval and execution lease are separate."""
    if not isinstance(identity,TaskIdentity):
        raise ReviewChanged('Teammate task identity is unavailable')
    try:
        value=identity.as_dict()
        if value.get('task_kind')=='cross':
            current=bind_cross(db,owner_id=value['owner_id'],room_id=value['room_id'],agent_id=value['agent_id'],
                root_id=value['root_request_event_id'],generation=value['run_generation'],
                job={'message_id':value['job_id'],'phase':value['phase']},execution_binding=execution_binding)
            if current!=identity:raise ReviewChanged('Cross-room task identity changed')
            return current
        current=_identity(db,owner_id=value['owner_id'],room_id=value['room_id'],agent_id=value['agent_id'],
                          root_id=value['root_request_event_id'],generation=value['run_generation'],
                          job={'handoff_id':value['handoff_id'],'phase':value['phase']},execution_binding=execution_binding)
    except (KeyError,TypeError,ValueError) as error:
        raise ReviewChanged('Teammate task identity is unavailable') from error
    if current!=identity:
        raise ReviewChanged('Teammate task identity changed')
    return current
