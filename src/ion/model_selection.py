from ion.contracts import ModelInfo, ModelProfile


def select_model(profile: ModelProfile, model: ModelInfo, mode: str) -> ModelProfile:
    if mode == "evaluation" or profile.locked:
        raise ValueError("model selection is locked in evaluation mode")
    if not model.available:
        raise ValueError("model is unavailable for this provider profile")
    if not model.context_window or not model.max_output_tokens:
        raise ValueError("model context and output limits are unknown")
    if model.text_only is not True:
        raise ValueError("model capabilities are unconfirmed")
    protocol = "native" if model.supports_tools is True else "structured_json"
    return profile.model_copy(update={
        "model_id": model.model_id,
        "context_window": model.context_window,
        "max_output_tokens": min(model.max_output_tokens, profile.max_output_tokens),
        "tool_protocol": protocol,
    })
