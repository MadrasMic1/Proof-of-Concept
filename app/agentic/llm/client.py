# app/agentic/llm/client.py
"""Model profiles and the Agent Framework chat client.

Profiles: orchestrator, specialist, summariser (AGENT_MODEL_* / AGENT_TEMPERATURE_*).
Provider presets (base URL, default model, output-token field, model extras such as
"thinking off" for Nemotron) come from the worker's app/llm/providers.py, unchanged.
AGENT_LLM_API_KEY empty -> LLM_API_KEY when the provider is the same (one NVIDIA key).
Every provider is called through Chat Completions (OpenAIChatCompletionClient).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import openai
from agent_framework.openai import OpenAIChatCompletionClient

from app.agentic.config import AgenticSettings
from app.agentic.llm.transport import MeteredTransport, Recorder, hx
from app.core.config import Settings
from app.llm.providers import PRESETS, model_extras
from app.llm.types import LlmConfigError

ProfileName = Literal["orchestrator", "specialist", "summariser"]


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float
    fallback: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def _key(value: Any) -> str | None:
    secret = value.get_secret_value() if value is not None else None
    return secret.strip() if secret and secret.strip() else None


def _target(
    provider: str,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    timeout: float,
    *,
    fallback: bool,
) -> ModelTarget:
    if provider == "fake":
        name = model or "fake-model"
        return ModelTarget("fake", name, "http://fake.local/v1", "fake", timeout, fallback)
    preset = PRESETS[provider]
    resolved_model = model or preset.default_model
    resolved_url = base_url or preset.base_url
    if not resolved_model:
        raise LlmConfigError(f"set the agent model (deployment name) for {provider}")
    if not resolved_url:
        raise LlmConfigError(f"set AGENT_LLM_BASE_URL for {provider}")
    if not api_key:
        raise LlmConfigError(f"set AGENT_LLM_API_KEY for {provider}")
    return ModelTarget(provider, resolved_model, resolved_url, api_key, timeout, fallback)


def profile_model(settings: AgenticSettings, profile: ProfileName) -> str | None:
    return {
        "orchestrator": settings.agent_model_orchestrator,
        "specialist": settings.agent_model_specialist,
        "summariser": settings.agent_model_summariser,
    }[profile]


def profile_temperature(settings: AgenticSettings, profile: ProfileName) -> float:
    if profile == "orchestrator":
        return settings.agent_temperature_orchestrator
    return settings.agent_temperature_specialist


def primary_target(settings: AgenticSettings, core: Settings, profile: ProfileName) -> ModelTarget:
    provider = settings.agent_llm_provider
    key = _key(settings.agent_llm_api_key)
    if key is None and core.llm_provider == provider:
        key = _key(core.llm_api_key)
    return _target(
        provider,
        profile_model(settings, profile),
        key,
        settings.agent_llm_base_url,
        settings.agent_llm_timeout_seconds,
        fallback=False,
    )


def fallback_target(settings: AgenticSettings, core: Settings) -> ModelTarget | None:
    provider = settings.agent_fallback_provider
    if provider == "none":
        return None
    key = _key(settings.agent_fallback_api_key)
    if key is None and provider == settings.agent_llm_provider:
        key = _key(settings.agent_llm_api_key)
    if key is None and core.llm_provider == provider:
        key = _key(core.llm_api_key)
    return _target(
        provider,
        settings.agent_fallback_model,
        key,
        settings.agent_fallback_base_url,
        settings.agent_fallback_timeout_seconds,
        fallback=True,
    )


def build_chat_client(
    target: ModelTarget,
    settings: AgenticSettings,
    profile: ProfileName,
    *,
    recorder: Recorder | None = None,
    inner: Any | None = None,
    enabled: Callable[[], bool] | None = None,
) -> OpenAIChatCompletionClient:
    """inner: an httpx transport for tests (a scripted model); None = the network."""
    if target.provider == "fake" and inner is None:
        raise LlmConfigError("AGENT_LLM_PROVIDER=fake needs a scripted model (tests only)")
    preset = PRESETS.get(target.provider)
    transport = MeteredTransport(
        provider=target.provider,
        model=target.model,
        extras=model_extras(target.provider, target.model),
        temperature=profile_temperature(settings, profile),
        max_tokens=settings.agent_llm_max_output_tokens,
        token_param=preset.token_param if preset else "max_tokens",
        enabled=enabled or (lambda: settings.agent_enabled),
        recorder=recorder,
        inner=inner,
    )
    headers = {"api-key": target.api_key} if target.provider == "foundry" else None
    async_client = openai.AsyncOpenAI(
        api_key=target.api_key,
        base_url=target.base_url,
        timeout=target.timeout_seconds,
        max_retries=settings.agent_llm_max_retries,
        default_headers=headers,
        http_client=hx.AsyncClient(transport=transport, timeout=target.timeout_seconds),
    )
    return OpenAIChatCompletionClient(model=target.model, async_client=async_client)
