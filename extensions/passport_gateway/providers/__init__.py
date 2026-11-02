"""Provider selection. Fixtures are returned only in explicit test mode; otherwise missing
credentials surface as `provider_unconfigured` to the device (never a fake answer)."""
from __future__ import annotations

from ..config import Config
from .base import AsrProvider, ProviderError, ProviderUnconfigured, TtsProvider


class UnconfiguredAsr:
    def __init__(self, name: str, model: str, env: str):
        self.name, self.model, self._env = name, model, env

    async def open(self):
        raise ProviderUnconfigured(f"{self._env} is not set")

    def diagnostics(self) -> dict:
        return {"provider": self.name, "model": self.model, "error": "provider_unconfigured"}


class UnconfiguredTts:
    def __init__(self, name: str, model: str, voice: str, env: str):
        self.name, self.model, self.voice, self._env = name, model, voice, env

    def diagnostics(self) -> dict:
        return {"provider": self.name, "configured_model": self.model, "actual_model": None,
                "voice": self.voice, "error": "provider_unconfigured"}

    async def synthesize(self, text: str, mood: str):
        raise ProviderUnconfigured(f"{self._env} is not set")
        yield b""  # pragma: no cover  (makes this an async generator)


def build_providers(cfg: Config) -> tuple[AsrProvider, TtsProvider]:
    if cfg.test_mode:
        from .fake import FakeAsr, FakeTts
        return FakeAsr(), FakeTts()
    if cfg.provider == "dashscope":
        if not cfg.ds_api_key:
            return (UnconfiguredAsr("dashscope", cfg.ds_asr_model, "DASHSCOPE_API_KEY"),
                    UnconfiguredTts("dashscope", cfg.ds_tts_model, cfg.ds_tts_voice, "DASHSCOPE_API_KEY"))
        from .dashscope import DashScopeAsr, DashScopeTts
        return (DashScopeAsr(cfg.ds_asr_url, cfg.ds_asr_model, cfg.ds_api_key, cfg.asr_timeout_s),
                DashScopeTts(cfg.ds_tts_url, cfg.ds_tts_model, cfg.ds_tts_voice, cfg.ds_api_key,
                             cfg.ds_tts_instructions, cfg.ds_tts_fallback_model, cfg.ds_tts_fallback))
    if not cfg.sf_api_key:
        return (UnconfiguredAsr("siliconflow", cfg.sf_asr_model, "SILICONFLOW_API_KEY"),
                UnconfiguredTts("siliconflow", cfg.sf_tts_model, cfg.sf_tts_voice, "SILICONFLOW_API_KEY"))
    from .siliconflow import SiliconFlowAsr, SiliconFlowTts
    return (SiliconFlowAsr(cfg.sf_base_url, cfg.sf_asr_model, cfg.sf_api_key, cfg.asr_timeout_s),
            SiliconFlowTts(cfg.sf_base_url, cfg.sf_tts_model, cfg.sf_tts_voice, cfg.sf_api_key,
                           cfg.sf_tts_style, cfg.sf_tts_speed, cfg.sf_tts_gain,
                           cfg.tts_first_audio_timeout_s, style_soft=cfg.sf_tts_style_soft))


__all__ = ["build_providers", "ProviderError", "ProviderUnconfigured"]
