"""User-configured OpenAI-compatible Chat Completions endpoint."""

from .openai import OpenAIAdapter


class CompatibleAdapter(OpenAIAdapter):
    kind = "compatible"
