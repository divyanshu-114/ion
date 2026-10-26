import httpx
import pytest

from ion.config import load_config, resolve_profile
from ion.model_selection import select_model
from ion.models.catalog import ModelCatalog


@pytest.mark.asyncio
async def test_openrouter_catalog_identifies_free_text_tool_model(tmp_path):
    config = load_config(__import__("pathlib").Path(__file__).resolve().parents[1] / "ion.toml")
    profile = resolve_profile(config, "openrouter-qwen-free", "product")

    def handle(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [
            {"id": "qwen/example:free", "context_length": 32768, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}, "supported_parameters": ["tools"], "pricing": {"prompt": "0.000000", "completion": "0"}, "top_provider": {"max_completion_tokens": 2048}},
            {"id": "deepseek/paid", "context_length": 32768, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}, "supported_parameters": ["tools"], "pricing": {"prompt": "0.1", "completion": "0.2"}, "top_provider": {"max_completion_tokens": 2048}},
        ]})

    result = await ModelCatalog("fixture-key", httpx.MockTransport(handle)).list(profile)
    assert result.status == "available"
    assert result.entries[0].free is True
    assert result.entries[1].free is False
    selected = select_model(profile, result.entries[0], "product")
    assert selected.model_id == "qwen/example:free"
    assert selected.max_output_tokens == 2048
    with pytest.raises(ValueError):
        select_model(profile, result.entries[0], "evaluation")


@pytest.mark.asyncio
async def test_catalog_cache_does_not_mix_profiles_sharing_endpoint(tmp_path):
    config = load_config(__import__("pathlib").Path(__file__).resolve().parents[1] / "ion.toml")
    first = resolve_profile(config, "openrouter-qwen-free", "product")
    second = first.model_copy(update={"model_id": "other/configured-model"})
    calls = []

    def handle(request):
        calls.append(request)
        model_id = first.model_id if len(calls) == 1 else second.model_id
        return httpx.Response(200, json={"data": [{"id": model_id, "context_length": 32768,
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["tools"]}]})

    catalog = ModelCatalog("fixture-key", httpx.MockTransport(handle))
    assert (await catalog.list(first)).entries[0].model_id == first.model_id
    assert (await catalog.list(second)).entries[0].model_id == second.model_id
    assert len(calls) == 2
