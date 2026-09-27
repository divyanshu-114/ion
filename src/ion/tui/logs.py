"""Readable activity messages; structured diagnostics stay on disk."""
from __future__ import annotations

import os
from datetime import datetime


TOOL_LABELS = {
    'file_read': 'Reading file', 'repo_list': 'Listing files',
    'repo_search': 'Searching repository', 'edit_file': 'Editing file',
    'write_file': 'Writing file', 'patch_apply': 'Applying changes',
    'command_start': 'Running command', 'command_poll': 'Checking command',
    'finish_request': 'Reviewing completion',
}
PHASE_LABELS = {
    'intake': 'Starting', 'inspect': 'Inspecting', 'plan': 'Planning',
    'act': 'Working', 'verify': 'Verifying', 'finalize': 'Finished',
}


def mask_keys(text: str) -> str:
    secrets = {value for name, value in os.environ.items() if name.endswith('_API_KEY') and value}
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, '***')
    return text


def activity_message(message: str) -> str:
    for tool, label in TOOL_LABELS.items():
        if message.startswith(f'{tool} requested'):
            return label + message[len(tool) + len(' requested'):]
        if message.startswith(f'{tool} failed:'):
            return f'{label} failed:' + message[len(tool) + len(' failed:'):]
    return message


def diagnostic_message(record: dict) -> tuple[str, str]:
    event = record.get('event')
    if event == 'run.start':
        return 'START', f"Task started · {record.get('provider', '')} / {record.get('model', '')}"
    if event == 'request.prepare':
        return 'MODEL', f"Request {record.get('request', '?')} · about {record.get('estimated_input_tokens', '?')} input tokens · output limit {record.get('output_cap', '?')}"
    if event == 'model.response':
        if record.get('error'):
            return 'ERROR', f"Request {record.get('request', '?')} failed · {record['error']}"
        tools = record.get('tools') or []
        tokens = record.get('completion_tokens')
        usage = f'{tokens} reported output tokens' if tokens is not None else 'token usage not reported'
        return 'MODEL', f"Response received · {len(tools)} tool action(s) · {usage}"
    if event == 'tool.request':
        label = TOOL_LABELS.get(record.get('tool'), 'Running tool')
        detail = f" · {record['path']}" if record.get('path') else ''
        if record.get('offset'):
            detail += f" · offset {record['offset']}"
        return 'ACTION', label + detail
    if event == 'tool.result':
        status = record.get('status', 'unknown')
        label = TOOL_LABELS.get(record.get('tool'), 'Tool action')
        files = ', '.join(record.get('changed_files') or [])
        return ('ERROR' if status == 'failed' else 'RESULT'), f"{label}: {status}" + (f' · {files}' if files else '')
    if event == 'intake.prefetch':
        return 'READ', f"Initial file read · {record.get('path', '')} · {record.get('status', '')}"
    if event == 'phase.transition':
        return 'PROGRESS', 'Inspection complete; preparing changes'
    if event == 'request.retry':
        return 'RETRY', 'Response was cut short; retrying with a larger output limit'
    if event == 'run.stop':
        return 'STOPPED', str(record.get('reason', 'Task stopped'))
    if event == 'run.finish':
        files = record.get('changed_files') or []
        return 'FINISHED', f"{str(record.get('outcome', 'finished')).replace('_', ' ')} · {len(files)} file(s) changed · {record.get('requests', 0)} requests"
    return 'INFO', 'Additional diagnostic event recorded'


def format_diagnostic(record: dict) -> str:
    try:
        time = datetime.fromisoformat(str(record.get('time', ''))).astimezone().strftime('%H:%M:%S')
    except ValueError:
        time = '--:--:--'
    label, message = diagnostic_message(record)
    task = str(record.get('task_id', ''))[:8]
    return mask_keys(f'{time}  {label}  {message}' + (f'  · task {task}' if task else ''))
