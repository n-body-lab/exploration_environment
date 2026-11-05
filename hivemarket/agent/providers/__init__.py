from __future__ import annotations

from hivemarket.common.config import AgentRuntimeConfig


def make_provider(cfg: AgentRuntimeConfig):
    route_url = f"{cfg.gateway_url.rstrip('/')}/{cfg.spec.provider}"
    if cfg.provider_kind == "scripted":
        from hivemarket.agent.providers.scripted import ScriptedProvider
        return ScriptedProvider(cfg)
    if cfg.provider_kind == "anthropic":
        from hivemarket.agent.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(cfg.spec, route_url, cfg.token)
    from hivemarket.agent.providers.openai_provider import OpenAIProvider
    return OpenAIProvider(cfg.spec, route_url, cfg.token)
