"""Gemini GenerateContent protocol; full model Content retains thought signatures."""

from urllib.parse import quote

from .base import (ProviderAdapter, ProviderFailure, ProviderRequest, ProviderResult,
                   attributed_messages, check_continuation, continuation_scope, image_part,
                   normalized_call, observation_text, wire_tool_name)


class GeminiAdapter(ProviderAdapter):
    kind = "gemini"
    origin = "https://generativelanguage.googleapis.com/v1beta"

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        contents = [{"role": "model" if item["role"] == "assistant" else "user",
                     "parts": [{"text": item["content"]}]} for item in attributed_messages(request)]
        if request.vision is not None:
            contents.append({"role": "user", "parts": [{"text": request.vision.prompt},
                             image_part(request.vision, "gemini")]})
        if request.continuation is not None:
            state = request.continuation
            check_continuation(request, state, self.kind)
            if not isinstance(state.get("model_content"), dict):
                raise ProviderFailure("invalid_continuation", "Provider continuation does not match the connection.")
            expected = set(state.get("call_ids", []))
            if expected != {item.call_id for item in request.observations} or len(expected) != len(request.observations):
                raise ProviderFailure("invalid_continuation", "Tool observations do not match pending calls.")
            contents.append(state["model_content"])
            parts = []
            for item in request.observations:
                parts.append({"functionResponse": {"id": item.call_id,
                              "name": state["call_names"][item.call_id],
                              "response": {"state": item.state, "content": observation_text(item)}}})
                if item.image_bytes is not None:
                    parts.append(image_part(item, "gemini"))
            contents.append({"role": "user", "parts": parts})
        elif request.observations:
            raise ProviderFailure("invalid_continuation", "Tool observations require a pending provider call.")
        body = {"contents": contents, "systemInstruction": {"parts": [{"text": request.system}]}}
        generation = {}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation["maxOutputTokens"] = request.max_tokens
        if generation:
            body["generationConfig"] = generation
        if request.tools:
            body["tools"] = [{"functionDeclarations": [{"name": wire_tool_name(tool["name"]),
                              "description": tool.get("description", ""),
                              "parameters": tool["parameters"]} for tool in request.tools]}]
        data = await self._post(request, f"/models/{quote(request.model_id, safe='')}:generateContent",
                                {"x-goog-api-key": request.connection.api_key or ""}, body)
        try:
            content = data["candidates"][0]["content"]
            parts = content["parts"]
            text = "\n".join(item["text"] for item in parts if "text" in item)
            raw_calls = [item["functionCall"] for item in parts if "functionCall" in item]
            calls = [normalized_call(item["id"], item["name"], item["args"], request.tools) for item in raw_calls]
            if (not text.strip() and not calls) or len({call.call_id for call in calls}) != len(calls):
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderFailure("provider_invalid_response", "Provider returned an invalid response.") from None
        continuation = ({"provider_kind": self.kind, **continuation_scope(request), "model_content": content,
                         "call_ids": [call.call_id for call in calls],
                         "call_names": {call.call_id: wire_tool_name(call.family) for call in calls}} if calls else None)
        return ProviderResult(text, calls, continuation,
                              {"connection_id": request.connection_id, "model_id": request.model_id,
                               "provider_kind": self.kind, "watermark_seq": request.watermark_seq})
