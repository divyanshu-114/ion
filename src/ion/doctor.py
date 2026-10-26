from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ion.config import AppConfig, resolve_credential, resolve_profile
from ion.contracts import ModelProfile
from ion.models.catalog import ModelCatalog


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: str
    detail: str


class Doctor:
    async def run(
        self,
        config: AppConfig,
        profile_name: str,
        mode: Literal["product", "evaluation"] = "product",
        profile_override: ModelProfile | None = None,
        catalog: ModelCatalog | None = None,
    ) -> list[DiagnosticCheck]:
        profile = profile_override or resolve_profile(config, profile_name, mode)
        credential, key_name = resolve_credential(profile, mode)
        checks = [
            DiagnosticCheck("configuration", "passed", "TOML profile validated"),
            DiagnosticCheck("credential", "passed" if credential else "failed", f"{key_name} is set" if credential else f"Set {key_name} in your terminal"),
            DiagnosticCheck("limits", "passed", f"{profile.context_window} context; {profile.max_output_tokens} output"),
        ]
        if not credential:
            checks.extend([
                DiagnosticCheck("network", "skipped", "Credential required for a live catalog check"),
                DiagnosticCheck("model", "skipped", f"Not checked: {profile.model_id}"),
            ])
            return checks

        ttl = int(config.model_catalog.get("cache_ttl_seconds", 300))
        live_catalog = catalog or ModelCatalog(credential, cache_ttl_seconds=ttl)
        result = await live_catalog.list(profile, refresh=True)
        if result.status != "available":
            status = "failed" if result.status in {"authentication_failed", "access_denied", "provider_error"} else "warning"
            checks.extend([
                DiagnosticCheck("network", status, result.detail or result.status.replace("_", " ")),
                DiagnosticCheck("model", "skipped", f"Could not check {profile.model_id}"),
            ])
            return checks

        checks.append(DiagnosticCheck("network", "passed", f"Connected to {profile.endpoint}"))
        model = next((item for item in result.entries if item.model_id == profile.model_id), None)
        if model is None or not model.available:
            checks.append(DiagnosticCheck("model", "failed", f"{profile.model_id} is absent from the live catalog"))
        else:
            checks.append(DiagnosticCheck("model", "passed", f"{profile.provider}: {profile.model_id}"))
            if profile.tool_protocol == "native" and model.supports_tools is not True:
                checks.append(DiagnosticCheck("tools", "failed", "Native tool calling is not advertised by this model"))
            else:
                protocol = "native tools" if profile.tool_protocol == "native" else "structured JSON fallback"
                checks.append(DiagnosticCheck("tools", "passed", protocol))
            if model.context_window and profile.context_window > model.context_window:
                checks.append(DiagnosticCheck("live limits", "warning", f"Configured context exceeds live limit {model.context_window}"))

        if profile.provider == "openrouter":
            quota = await live_catalog.quota(profile, refresh=True)
            if quota.status == "available":
                checks.append(DiagnosticCheck("free quota", "passed" if quota.remaining else "failed", f"{quota.remaining}/{quota.limit} requests remaining today"))
            else:
                checks.append(DiagnosticCheck("free quota", "warning", quota.detail or "Quota endpoint unavailable"))
        return checks
