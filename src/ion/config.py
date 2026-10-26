from __future__ import annotations

import tomllib
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ion.contracts import ModelProfile, TaskSpec


class EconomyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Product runs use the output-focused workflow by default.  The bounded
    # economy workflow remains available for explicit compatibility and tests.
    enabled: bool = False
    max_requests: int = Field(default=12, ge=2, le=100)
    max_total_tokens: int = Field(default=24000, ge=2048)
    inspect_output_tokens: int = Field(default=512, ge=256)
    edit_output_tokens: int = Field(default=1024, ge=384)
    rewrite_output_tokens: int = Field(default=4096, ge=768)
    verify_output_tokens: int = Field(default=512, ge=256)
    finalize_output_tokens: int = Field(default=512, ge=256)
    verification_reserve_tokens: int = Field(default=256, ge=0)
    finalization_reserve_tokens: int = Field(default=256, ge=0)
    max_tool_preview_chars: int = Field(default=4000, ge=256, le=16000)
    output_focused_read_page_chars: int = Field(default=12000, ge=4000, le=16000)
    compaction_recoveries: Literal[1] = 1

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_preview_name(cls, value):
        if isinstance(value, dict) and "tool_preview_max_chars" in value:
            value = dict(value)
            if "max_tool_preview_chars" in value:
                raise ValueError("set only one tool preview maximum")
            value["max_tool_preview_chars"] = value.pop("tool_preview_max_chars")
        return value

    @property
    def tool_preview_max_chars(self) -> int:
        return self.max_tool_preview_chars


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    lint_command: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_lint_command(self):
        if any(not arg.strip() or "\0" in arg for arg in self.lint_command):
            raise ValueError("lint_command must contain nonempty argv strings without null bytes")
        return self


class AppConfig(BaseModel):
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    default_profile: str
    evaluation_profile: str | None = None
    evaluation_profiles: tuple[str, ...] = ()
    profiles: dict[str, dict]
    model_catalog: dict = Field(default_factory=dict)
    economy: EconomyConfig = Field(default_factory=EconomyConfig)

    @model_validator(mode="after")
    def validate_evaluation_choices(self):
        for name in self.evaluation_profiles:
            if name not in self.profiles:
                raise ValueError(f"evaluation profile is missing: {name}")
        if self.evaluation_profiles and (self.evaluation_profile or self.default_profile) not in self.evaluation_profiles:
            raise ValueError("evaluation_profiles must include the evaluation default")
        return self


def load_config(path: Path, *, use_environment: bool = True) -> AppConfig:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    config = AppConfig.model_validate(data)
    if config.default_profile not in config.profiles:
        raise ValueError("default_profile is missing")
    if config.evaluation_profile:
        if config.evaluation_profile not in config.profiles:
            raise ValueError("evaluation_profile is missing")
        resolve_profile(config, config.evaluation_profile, "evaluation")
    for name in config.profiles:
        resolve_profile(config, name, "product")
    return apply_environment(config) if use_environment else config


def evaluation_requested() -> bool:
    value = os.environ.get("AI_EVALUATION", "").strip().lower()
    if value not in {"", "0", "1", "false", "true"}:
        raise ValueError("AI_EVALUATION must be 1/true or 0/false")
    return value in {"1", "true"}


def apply_environment(config: AppConfig, *, force_evaluation: bool = False) -> AppConfig:
    """Snapshot explicit routing settings once, before starting a session.

    Never infer a host from a secret or probe multiple providers with a key.
    The generated profile deliberately uses only the universal credential.
    """
    provider = os.environ.get("AI_PROVIDER", "").strip().lower()
    endpoint = os.environ.get("AI_BASE_URL", "").strip()
    model = os.environ.get("AI_MODEL", "").strip()
    requested = force_evaluation or evaluation_requested()
    evaluation = requested or bool(config.evaluation_profile)
    if evaluation:
        # Only committed choices may be selected. Developer routing settings
        # never alter their endpoints, models, limits, or evaluation credential.
        selected = config.evaluation_profile or config.default_profile
        profiles = dict(config.profiles)
        for name in config.evaluation_profiles or (selected,):
            profile = resolve_profile(config, name, "product")
            profiles[name] = profile.model_copy(update={"api_key_env": "AI_API_KEY", "locked": True}).model_dump()
        return config.model_copy(update={"profiles": profiles, "default_profile": selected,
                                         "evaluation_profile": selected})
    if not (provider or endpoint or model or evaluation):
        return config

    presets = {
        "deepseek": ("https://api.deepseek.com", "deepseek-flash"),
        "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
        "groq": ("https://api.groq.com/openai/v1", ""),
        "openrouter": ("https://openrouter.ai/api/v1", ""),
        "openai": ("https://api.openai.com/v1", ""),
    }
    if provider or endpoint:
        if provider and provider not in presets and not (endpoint and model):
            raise ValueError("Unknown AI_PROVIDER requires AI_BASE_URL and AI_MODEL")
        default_endpoint, default_model = presets.get(provider, ("", ""))
        endpoint = endpoint or default_endpoint
        model = model or default_model
        if not model:
            raise ValueError("AI_MODEL is required for this provider or custom endpoint")
        raw = dict(provider=provider or "openai-compatible", endpoint=endpoint,
                   model_id=model, context_window=8192, max_output_tokens=4096)
    else:
        name = config.evaluation_profile or config.default_profile
        raw = resolve_profile(config, name, "product").model_dump()
        if model:
            raw["model_id"] = model
    raw.update(api_key_env="AI_API_KEY", locked=evaluation)
    name = "environment"
    while name in config.profiles:
        name = "_" + name
    result = config.model_copy(update={
        "profiles": {**config.profiles, name: raw},
        "default_profile": name,
        "evaluation_profile": name if evaluation else None,
    })
    resolve_profile(result, name, "evaluation" if evaluation else "product")
    return result


def resolve_profile(config: AppConfig, name: str, mode: Literal["product", "evaluation"]) -> ModelProfile:
    raw = dict(config.profiles[name])
    if "model" in raw:
        raw["model_id"] = raw.pop("model")
    if "base_url" in raw:
        raw["endpoint"] = raw.pop("base_url")
    profile = ModelProfile.model_validate(raw)
    url = urlsplit(profile.endpoint)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("provider endpoint must be an HTTPS URL without embedded credentials")
    if mode == "evaluation" and not profile.locked:
        raise ValueError("evaluation profile must be locked")
    return profile


def validate_task(input: dict) -> TaskSpec:
    return TaskSpec.model_validate(input)


def resolve_credential(profile: ModelProfile, mode: Literal["product", "evaluation"] = "product") -> tuple[str, str]:
    if mode == "evaluation":
        return os.environ.get("AI_API_KEY", ""), "AI_API_KEY"
    for name in (profile.api_key_env, "AI_API_KEY"):
        value = os.environ.get(name, "")
        if value:
            return value, name
    return "", profile.api_key_env
