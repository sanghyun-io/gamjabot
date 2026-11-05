"""Resolve an Agent's exact saved connection and dispatch a frozen room transcript."""

from sqlalchemy.orm import object_session

from app.models.collaboration import RoomEvent
from app.services.connections import ConnectionFailure, resolve_connection_async
from app.services.providers import ProviderFailure, ProviderRequest, adapter_for


class LLMFailure(Exception):
    """Safe public failure; upstream response bodies and exception text never enter events."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class LLMService:
    async def generate_response(self, agent, messages, user_message):
        # The old call shape remains until Task 8 replaces the legacy turn loop.
        # `messages` already contains the current committed user event; we instead
        # query canonical events once at a frozen watermark.
        db = object_session(agent)
        if db is None or not agent.connection_id or not agent.model_id:
            raise LLMFailure("provider_not_configured", "Select a saved connection and exact model ID for this Agent.")
        try:
            config = await resolve_connection_async(db, agent.connection_id, agent.user_id,
                                                    getattr(agent, "_provider_store", None),
                                                    cache=getattr(agent, "_provider_cache", None),
                                                    model_id_override=agent.model_id)
            from app.services.model_features import permitted
            if not permitted(config,"text_generation"):
                raise LLMFailure("model_feature_unavailable","Check this exact model’s text response in Connections before running the Agent.")
            room_id = agent._room_id
            watermark = db.query(RoomEvent.seq).filter_by(room_id=room_id).order_by(RoomEvent.seq.desc()).limit(1).scalar() or 0
            events = db.query(RoomEvent).filter(RoomEvent.room_id == room_id, RoomEvent.seq <= watermark).order_by(RoomEvent.seq).all()
            request = ProviderRequest(connection=config, model_id=agent.model_id, system=agent.system_prompt,
                                      agent_id=agent.id, room_id=room_id, events=events, watermark_seq=watermark,
                                      temperature=float(agent.temperature) if agent.temperature not in (None, "auto") else None,
                                      max_tokens=agent.max_tokens if agent.max_tokens else None,
                                      cancel=getattr(agent, "_provider_cancel", None))
            transport = getattr(agent, "_provider_transport", None)
            adapter = (adapter_for(config.protocol_id, config.provider_id, transport)
                       if config.protocol_id and config.provider_id
                       else adapter_for(config.provider_kind, transport))
            return await adapter.generate(request)
        except (ConnectionFailure, ProviderFailure) as error:
            raise LLMFailure(error.code, error.message) from None
        except (TypeError, ValueError, AttributeError):
            raise LLMFailure("invalid_configuration", "Agent provider settings are invalid.") from None


llm_service = LLMService()
