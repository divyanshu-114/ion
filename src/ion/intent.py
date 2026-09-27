"""Conservative hints for completion handling, not a parser for user tasks."""
from __future__ import annotations

import re
from typing import Literal


def task_intent(text: str) -> Literal['answer', 'edit', 'task']:
    text = text.strip()
    text = re.sub(r'^(?:(?:(?:can|could|would|will)\s+you|please)\s+)+', '', text, flags=re.I)
    # Questions about editing are requests for explanations, not edits.
    question = re.match(r'^(?:how|what|why|where|when|which|who|explain|describe|summarize|inspect|review|show me|tell me|help me understand)\b', text, re.I)
    # Permit a question followed by an explicit action: "How does it work?
    # Improve the README." The model still interprets the full original text.
    action_text = re.split(r'[?\n]', text, maxsplit=1)[-1] if question and re.search(r'[?\n]', text) else text
    if question and action_text == text:
        return 'answer'
    if re.search(r"^(?:stop editing|just explain|only explain)\b", text, re.I) or re.search(r"\b(?:do not|don't)\s+(?:edit|change|modify|write)\w*\s+(?:anything|any files|any code|files|the repository|the repo)\s*[.!]?$", text, re.I):
        return 'answer'
    # A constraint such as "do not change the test file" does not cancel
    # permission to edit other files in the original task.
    action_text = re.sub(r"\b(?:do not|don't|without)\s+(?:edit|change|modify|write)\w*\b", '', action_text, flags=re.I)
    edit = re.search(r'\b(?:add|change|create|edit|fix|generate|implement|make|write|remove|rename|replace|rewrite|update|improv|enhanc|refactor|polish|clarify|simplify|optimi[sz]|document|expand|correct)\w*\b', action_text, re.I)
    make_change = re.search(r'\bmake\b.+\b(?:clearer|better|easier|shorter|longer|faster|readable|concise)\b', action_text, re.I)
    if edit or make_change:
        return 'edit'
    return 'answer' if question else 'task'
