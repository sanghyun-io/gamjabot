"""OpenAI Chat Completions protocol (also reused by explicit compatible kinds)."""

import json

from .base import (ProviderAdapter, ProviderFailure, ProviderRequest, ProviderResult, UnsupportedOption,
                   attributed_messages, check_continuation, continuation_scope, image_part,
                   normalized_call, observation_text, passthrough_tool_schemas, verified, wire_tool_name)


class OpenAIAdapter(ProviderAdapter):
    kind = "openai"
    origin = "https://api.openai.com/v1"

    def _headers(self, request: ProviderRequest) -> dict[str, str]:
        return ({"Authorization": f"Bearer {request.connection.api_key}"}
                if request.connection.api_key else {})

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        from .base import check_options
        check_options(request)
        from app.services.model_features import enforce
        await enforce(request)
        from app.services.model_features import local,permitted
        if local(request.connection):
            from dataclasses import replace
            available=[name for name in ("text_generation","tool_calling","image_input") if permitted(request.connection,name)]
            request=replace(request,system=request.system+"\nVerified model capabilities: "+", ".join(available)+". This app cannot generate image/audio files directly. Never claim an image/audio file was generated unless an actual offered tool returned that artifact. Unsupported features must be explained, not simulated.")
        messages = [{"role": "system", "content": request.system}] + attributed_messages(request)
        if request.vision is not None:
            messages.append({"role": "user", "content": [{"type": "text", "text": request.vision.prompt},
                             image_part(request.vision, "openai")]})
        if request.continuation is not None:
            state = request.continuation
            check_continuation(request, state, self.kind)
            if self.kind=='opencode-go':
                from .go_budget import validate_go_history
                validate_go_history(request,state)
            if not isinstance(state.get("assistant"), dict):
                raise ProviderFailure("invalid_continuation", "Provider continuation does not match the connection.")
            expected = set(state.get("call_ids", []))
            if expected != {item.call_id for item in request.observations} or len(expected) != len(request.observations):
                raise ProviderFailure("invalid_continuation", "Tool observations do not match pending calls.")
            messages.append(state["assistant"])
            for observation in request.observations:
                messages.append({"role": "tool", "tool_call_id": observation.call_id,
                                 "content": observation_text(observation)})
                if observation.image_bytes is not None:
                    if not verified(request, "vision"):
                        raise UnsupportedOption("vision")
                    messages.append({"role": "user", "content": [{"type": "text", "text": "Image from preceding tool result."}, image_part(observation, "openai")]})
        elif request.observations:
            raise ProviderFailure("invalid_continuation", "Tool observations require a pending provider call.")
        body = {"model": request.model_id, "messages": messages}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": wire_tool_name(tool["name"]),
                              "description": tool.get("description", ""),
                              "parameters": tool["parameters"]}} for tool in passthrough_tool_schemas(request.tools)]
        if self.kind == 'opencode-go' and request.continuation is not None and request.vision is None:
            from .go_budget import go_continuation_body,go_history_has_images
            if go_history_has_images(request.continuation) and not verified(request,'vision'):
                raise UnsupportedOption('vision')
            body=go_continuation_body(protocol='openai_chat_completions',model=request.model_id,system=request.system,
                tools=request.tools,messages=attributed_messages(request),state=request.continuation,
                observations=request.observations,temperature=request.temperature,max_tokens=request.max_tokens)
        data = await self._post(request, "/chat/completions", self._headers(request), body)
        try:
            message = data["choices"][0]["message"]
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise ValueError()
            raw_calls = message.get("tool_calls") or []
            calls = [normalized_call(call.get("id"), call["function"].get("name"),
                                     json.loads(call["function"]["arguments"]), request.tools,
                                     single_candidate=isinstance(raw_calls,list) and len(raw_calls)==1) for call in raw_calls]
            if not content.strip() and not calls:
                raise ValueError()
            if len({call.call_id for call in calls}) != len(calls):
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderFailure("provider_invalid_response", "Provider returned an invalid response.") from None
        continuation = ({"provider_kind": self.kind, **continuation_scope(request),
                         "assistant": {"role": "assistant", "content": content,
                         "tool_calls": raw_calls}, "call_ids": [call.call_id for call in calls]} if calls else None)
        if self.kind=='opencode-go':
            from .go_budget import advance_go_history
            continuation=advance_go_history(request,continuation)
        return ProviderResult(content, calls, continuation,
                              {"connection_id": request.connection_id, "model_id": request.model_id,
                               "provider_kind": self.kind, "watermark_seq": request.watermark_seq})
