"""Anthropic Messages wire format and complete tool-use continuation."""

from .base import (ProviderAdapter, ProviderFailure, ProviderRequest, ProviderResult,
                   attributed_messages, check_continuation, continuation_scope, image_part,
                   normalized_call, observation_text, passthrough_tool_schemas, wire_tool_name, verified, UnsupportedOption)


class AnthropicAdapter(ProviderAdapter):
    kind = "anthropic"
    origin = "https://api.anthropic.com/v1"

    def _headers(self, request: ProviderRequest) -> dict[str, str]:
        return {"x-api-key": request.connection.api_key or "",
                "anthropic-version": "2023-06-01"}

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        messages = attributed_messages(request)
        if request.vision is not None:
            messages.append({"role": "user", "content": [{"type": "text", "text": request.vision.prompt},
                             image_part(request.vision, "anthropic")]})
        if request.continuation is not None:
            state = request.continuation
            check_continuation(request, state, self.kind)
            if self.kind=='opencode-go':
                from .go_budget import validate_go_history
                validate_go_history(request,state)
            if not isinstance(state.get("assistant_content"), list):
                raise ProviderFailure("invalid_continuation", "Provider continuation does not match the connection.")
            expected = set(state.get("call_ids", []))
            if expected != {item.call_id for item in request.observations} or len(expected) != len(request.observations):
                raise ProviderFailure("invalid_continuation", "Tool observations do not match pending calls.")
            messages.append({"role": "assistant", "content": state["assistant_content"]})
            results = []
            for item in request.observations:
                content = [{"type": "text", "text": observation_text(item)}]
                if item.image_bytes is not None:
                    content.append(image_part(item, "anthropic"))
                results.append({"type": "tool_result", "tool_use_id": item.call_id,
                                "content": content, "is_error": item.state != "completed"})
            messages.append({"role": "user", "content": results})
        elif request.observations:
            raise ProviderFailure("invalid_continuation", "Tool observations require a pending provider call.")
        body = {"model": request.model_id, "system": request.system, "messages": messages,
                "max_tokens": request.max_tokens or (4096 if self.kind == 'opencode-go' else 1024)}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [{"name": wire_tool_name(tool["name"]), "description": tool.get("description", ""),
                              "input_schema": tool["parameters"]} for tool in passthrough_tool_schemas(request.tools)]
        if self.kind == 'opencode-go' and request.continuation is not None and request.vision is None:
            from .go_budget import go_continuation_body,go_history_has_images
            if (go_history_has_images(request.continuation) or any(item.image_bytes is not None for item in request.observations)) and not verified(request,'vision'):
                raise UnsupportedOption('vision')
            body=go_continuation_body(protocol='anthropic_messages',model=request.model_id,system=request.system,
                tools=request.tools,messages=attributed_messages(request),state=request.continuation,
                observations=request.observations,temperature=request.temperature,max_tokens=request.max_tokens)
        data = await self._post(request, "/messages", self._headers(request), body)
        if isinstance(data,dict) and data.get('stop_reason')=='max_tokens':
            failure=ProviderFailure('provider_output_truncated','The model reached its output limit. No tool from this truncated response was executed. Reduce the Page or request scope and send a new request.')
            usage=data.get('usage') if isinstance(data.get('usage'),dict) else {}
            output=usage.get('output_tokens')
            failure.truncation_diagnostic={'stop_reason':'max_tokens','output_limit':body['max_tokens'],
                'reported_output_tokens':output if type(output) is int and 0<=output<=1_000_000_000 else None}
            raise failure
        try:
            blocks = data["content"]
            if not isinstance(blocks, list):
                raise ValueError()
            text = "\n".join(item["text"] for item in blocks if item.get("type") == "text")
            tool_blocks = [item for item in blocks if item.get("type") == "tool_use"]
            calls = [normalized_call(item.get("id"), item.get("name"), item.get("input"), request.tools,
                                     single_candidate=len(tool_blocks)==1) for item in tool_blocks]
            if (not text.strip() and not calls) or len({call.call_id for call in calls}) != len(calls):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderFailure("provider_invalid_response", "Provider returned an invalid response.") from None
        continuation = ({"provider_kind": self.kind, **continuation_scope(request), "assistant_content": blocks,
                         "call_ids": [call.call_id for call in calls]} if calls else None)
        if self.kind=='opencode-go':
            from .go_budget import advance_go_history
            continuation=advance_go_history(request,continuation)
        return ProviderResult(text, calls, continuation,
                              {"connection_id": request.connection_id, "model_id": request.model_id,
                               "provider_kind": self.kind, "watermark_seq": request.watermark_seq})
