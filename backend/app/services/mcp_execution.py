"""MCP permission follows an existing Owner task, never peer instruction."""
from app.services.action_review import ReviewChanged


def require_origin(db, *, owner_id, room_id, agent_id, root_id, generation,
                   execution_binding=None, teammate_binding=None):
    from app.services import execution_scopes, skill_persistence, teammates
    if execution_binding is None:
        if teammate_binding is not None:raise ReviewChanged('MCP Owner origin changed')
        from app.models.collaboration import RoomEvent
        root=db.get(RoomEvent,root_id,populate_existing=True)
        if root and root.actor_type=='system':
            from app.models.owner_routines import OwnerRoutineRun
            from app.services.owner_routines import current
            tag=root.payload.get('routine') if isinstance(root.payload,dict) else None
            run=db.get(OwnerRoutineRun,tag.get('run_id'),populate_existing=True) if isinstance(tag,dict) and isinstance(tag.get('run_id'),str) else None
            if (not run or run.owner_id!=owner_id or run.room_id!=room_id or run.agent_id!=agent_id
                    or run.root_event_id!=root_id or run.generation!=generation or current(db,run) is None):
                raise ReviewChanged('MCP routine origin changed')
            return
        if not skill_persistence.eligible(db,room_id,root_id,agent_id,generation):
            raise ReviewChanged('MCP Owner origin changed')
        return
    proof=execution_binding
    current_scope=execution_scopes.require_binding(db,proof)
    if (proof.get('owner_id')!=owner_id or proof.get('execution_room_id')!=room_id
            or proof.get('agent_id')!=agent_id or proof.get('root_request_event_id')!=root_id
            or proof.get('run_generation')!=generation):
        raise ReviewChanged('MCP scope identity changed')
    if current_scope.job_kind == 'teammate' and teammate_binding is None:
        raise ReviewChanged('MCP teammate proof missing')
    if teammate_binding is not None:
        from app.models.teammate import TeammateHandoff
        if (not isinstance(teammate_binding,dict) or set(teammate_binding)!={'handoff_id','phase'}
                or teammate_binding['phase'] not in {'response','return'}):
            raise ReviewChanged('MCP teammate proof invalid')
        row=db.get(TeammateHandoff,teammate_binding['handoff_id'],populate_existing=True)
        phase=teammate_binding['phase']
        if (not row or row.owner_id!=owner_id or row.room_id!=room_id or row.root_request_event_id!=root_id
                or (row.recipient_agent_id if phase=='response' else row.sender_agent_id)!=agent_id
                or proof.get('job_id') is not None):
            raise ReviewChanged('MCP teammate identity changed')
        try:teammates.require_job_current(db,teammate_binding)
        except Exception:raise ReviewChanged('MCP teammate origin changed') from None
        scope=execution_scopes.job_scope(db,row,'teammate')
        if phase=='return':scope=db.get(type(scope),scope.parent_scope_id)
        if not scope or scope.scope_id!=proof.get('scope_id') or current_scope.scope_id!=scope.scope_id:
            raise ReviewChanged('MCP teammate scope changed')
    elif proof.get('job_id') is None:
        from app.models.teammate import TeammateHandoff
        # A returning same-room job shares its parent's branch scope; never
        # reclassify that live handoff as a direct Owner action if proof is lost.
        if db.query(TeammateHandoff).filter_by(owner_id=owner_id,room_id=room_id,
                root_request_event_id=root_id,sender_agent_id=agent_id,reply_state='running').first():
            raise ReviewChanged('MCP teammate proof missing')
        if not skill_persistence.eligible(db,room_id,root_id,agent_id,generation):
            raise ReviewChanged('MCP Owner origin changed')
    else:
        from app.services.execution_origin import load_execution_origin
        try:
            origin=load_execution_origin(db,job_id=proof['job_id'],phase=proof['phase'],
                execution_room_id=room_id,agent_id=agent_id,generation=generation)
        except Exception:raise ReviewChanged('MCP delegated origin changed') from None
        if origin.owner_id!=owner_id or origin.root_request_event_id!=root_id:
            raise ReviewChanged('MCP delegated origin changed')
