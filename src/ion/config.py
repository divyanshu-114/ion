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
    enabled: bool = True
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


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    default_profile: str
    evaluation_profile: str | None = None
    profiles: dict[str, dict]
    model_catalog: dict = Field(default_factory=dict)
    economy: EconomyConfig = Field(default_factory=EconomyConfig)


def load_config(path: Path) -> AppConfig:
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
    return config


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
