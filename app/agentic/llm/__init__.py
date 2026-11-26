# app/agentic/llm/__init__.py
"""The agents' LLM layer: profiles, the metered HTTP transport, the chat-client factory.

Separate from app/llm (the worker's single-shot JSON calls, unchanged): it reuses the
worker's provider presets (base URLs, default models, model extras) only.
"""
