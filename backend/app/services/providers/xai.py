"""xAI Chat Completions origin with independent connection identity."""

from .openai import OpenAIAdapter


class XAIAdapter(OpenAIAdapter):
    kind = "xai"
    origin = "https://api.x.ai/v1"
