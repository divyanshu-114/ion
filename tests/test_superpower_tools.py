import json

import httpx
import pytest

from ion.artifacts import ArtifactStore
from ion.contracts import Phase, ToolCall
from ion.processes import CommandSupervisor
from ion.tools.bundles import select_tool_bundle
from ion.tools.registry import ToolDispatcher, tool_schemas
from ion.workspace import Workspace


def dispatcher(tmp_path, **kwargs):
    repo = tmp_path / 'repo'
    repo.mkdir()
    workspace = Workspace.capture(repo)
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    return repo, ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts), **kwargs)


async def call(d, name, **arguments):
    return await d.execute(ToolCall(task_id='fixture', tool=name, arguments=arguments))


@pytest.mark.asyncio
async def test_trace_definitions_precede_references_and_skip_private_files(tmp_path):
    repo, d = dispatcher(tmp_path)
    (repo / 'a.py').write_text('Widget()\nNotWidget()\n')
    (repo / 'z.py').write_text('class Widget:\n    pass\n')
    (repo / '.env').write_text('Widget=secret')
    (repo / 'binary').write_bytes(b'Widget\x00\xff')
    (repo / 'outside').symlink_to(tmp_path / 'private')
    result = await call(d, 'trace_symbol', symbol='Widget')
    assert result.status == 'succeeded'
    assert [(m['kind'], m['path'], m['line']) for m in result.data['matches']] == [('definition', 'z.py', 1), ('reference', 'a.py', 1)]
    assert not d.reads
    assert (await call(d, 'trace_symbol', symbol='')).status == 'failed'


@pytest.mark.asyncio
async def test_infra_reports_observed_frameworks_ports_and_parse_warnings(tmp_path):
    repo, d = dispatcher(tmp_path)
    (repo / 'package.json').write_text(json.dumps({'dependencies': {'next': '15'}}))
    (repo / 'Dockerfile').write_text('FROM node:22\nEXPOSE 3000/tcp\n')
    (repo / 'docker-compose.yml').write_text('services:\n  web:\n    ports:\n      - "8080:3000"\n')
    (repo / 'requirements.txt').write_text('fastapi>=0.1\n')
    (repo / 'deploy').mkdir()
    (repo / 'deploy/app.yaml').write_text('apiVersion: v1\nkind: Service\nmetadata:\n  name: api\nspec:\n  ports:\n    - port: 80\n      targetPort: 3000\n')
    (repo / 'compose.yaml').write_text('services: [unterminated')
    result = await call(d, 'infra_scan')
    assert result.status == 'succeeded'
    assert {'Next.js', 'FastAPI'} <= set(result.data['frameworks'])
    assert {'Python', 'JavaScript'} <= set(result.data['languages'])
    assert any(p.get('container_port') == 3000 and p.get('host_port') == 8080 for p in result.data['ports'])
    assert result.data['warnings']


@pytest.mark.asyncio
async def test_web_search_returns_three_bounded_snippets_without_auth(tmp_path):
    def handle(request):
        assert request.url.host == 'html.duckduckgo.com'
        assert 'authorization' not in request.headers
        return httpx.Response(200, text=''.join(f'<div class="result"><a class="result__a" href="https://example.org/{i}">Title {i}</a><a class="result__snippet">' + ('snippet ' * 200) + '</a></div>' for i in range(5)))
    _, d = dispatcher(tmp_path, http_transport=httpx.MockTransport(handle))
    result = await call(d, 'web_search', query='Python docs')
    assert result.status == 'succeeded'
    assert result.data['result_count'] == 3
    assert len(result.data['text']) <= 2000
    assert all(f'https://example.org/{i}' in result.data['text'] for i in range(3))


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(429, ''), (200, '<form id="challenge-form">captcha</form>'), (200, '<html>unexpected</html>')])
async def test_search_failures_are_not_results(tmp_path, status, body):
    _, d = dispatcher(tmp_path, http_transport=httpx.MockTransport(lambda _: httpx.Response(status, text=body)))
    result = await call(d, 'web_search', query='example')
    assert result.status == 'failed'


@pytest.mark.asyncio
async def test_linter_uses_configured_project_command_and_preserves_diagnostics(tmp_path):
    import sys
    repo, d = dispatcher(tmp_path, allow_commands=True, lint_command=(sys.executable, 'lint.py'))
    (repo / 'lint.py').write_text('import sys\nprint("src/app.xyz:4:2: unused name")\nsys.exit(1)\n')
    result = await call(d, 'run_linter')
    assert result.status == 'succeeded'
    assert result.data['passed'] is False
    assert result.data['exit_code'] == 1
    assert 'src/app.xyz:4:2: unused name' in result.data['output']
    assert result.artifact_ids
    assert (await call(d, 'run_linter', relative_path='../outside')).status == 'failed'
    d.allow_commands = False
    assert (await call(d, 'run_linter')).status == 'failed'


def test_superpower_bundles_and_optional_linter_path():
    navigate = select_tool_bundle(Phase.inspect)
    edit = select_tool_bundle(Phase.act, edit_intent=True, allow_commands=True)
    verify = select_tool_bundle(Phase.verify, allow_commands=True)
    assert {'trace_symbol', 'web_search', 'infra_scan'} <= set(navigate)
    assert {'trace_symbol', 'run_linter'} <= set(edit)
    assert {'web_search', 'run_linter'} <= set(verify)
    assert 'run_linter' not in select_tool_bundle(Phase.verify)
    schema = tool_schemas(('run_linter',))[0]['function']['parameters']
    assert not schema['required']
    assert 'relative_path' in schema['properties']


@pytest.mark.asyncio
async def test_trace_keeps_late_definitions_after_many_references(tmp_path):
    repo, d = dispatcher(tmp_path)
    (repo / 'a.py').write_text('Thing()\n' * 40)
    (repo / 'z.py').write_text('def Thing(): pass\n')
    (repo / 'large.py').write_text('Thing' + ' ' * (512*1024))
    result = await call(d, 'trace_symbol', symbol='Thing')
    assert result.data['matches'][0]['kind'] == 'definition'
    assert len(result.data['matches']) == 20
    assert result.data['truncated']
    assert result.data['skipped_files'] == 1


@pytest.mark.asyncio
async def test_infra_handles_alias_cycles_and_caps_output(tmp_path):
    repo, d = dispatcher(tmp_path)
    (repo / 'app.yaml').write_text('kind: Service\nspec: &loop\n  recurse: *loop\n  port: 80\n')
    result = await call(d, 'infra_scan')
    assert result.status == 'succeeded'
    assert result.data['ports'][0]['port'] == 80
    (repo / 'docker-compose.yml').write_text('services:\n' + ''.join(f'  service{i}:\n    ports: ["{8000+i}:80"]\n' for i in range(100)))
    result = await call(d, 'infra_scan')
    assert result.data['truncated']
    assert len(json.dumps(result.data)) <= 12000


@pytest.mark.asyncio
async def test_search_distinguishes_empty_and_oversized_responses(tmp_path):
    _, d = dispatcher(tmp_path, http_transport=httpx.MockTransport(lambda _: httpx.Response(200, text='<div class="no-results">No results found</div>')))
    result = await call(d, 'web_search', query='no result')
    assert result.status == 'succeeded' and result.data['result_count'] == 0
    d.http_transport = httpx.MockTransport(lambda _: httpx.Response(200, text='a' * (1024*1024+1)))
    assert (await call(d, 'web_search', query='large')).status == 'failed'


def test_linter_selection_project_tasks_and_local_adapters(tmp_path, monkeypatch):
    from ion.tools.linting import select_linter
    monkeypatch.setattr('ion.tools.linting.shutil.which', lambda name: f'/bin/{name}')
    (tmp_path / 'Makefile').write_text('lint:\n\tcheck-style\n')
    (tmp_path / 'package.json').write_text(json.dumps({'scripts': {'lint': 'eslint .'}, 'packageManager': 'pnpm@9'}))
    assert select_linter(tmp_path, ('custom', 'lint'), '.')[0] == ['custom', 'lint']
    assert select_linter(tmp_path, (), '.')[0] == ['/bin/make', 'lint']
    (tmp_path / 'Makefile').unlink()
    assert select_linter(tmp_path, (), '.')[0] == ['/bin/pnpm', 'run', 'lint']
    (tmp_path / 'package.json').unlink()
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="demo"\n')
    (tmp_path / '.venv/bin').mkdir(parents=True)
    (tmp_path / '.venv/bin/ruff').write_text('')
    argv, scope = select_linter(tmp_path, (), 'src/test.py')
    assert argv == [str(tmp_path / '.venv/bin/ruff'), 'check', '--no-fix', '--', 'src/test.py']
    assert scope == 'target'
    monkeypatch.setattr('ion.tools.linting.shutil.which', lambda _: None)
    (tmp_path / '.venv/bin/ruff').unlink()
    with pytest.raises(ValueError, match='No installed project linter'):
        select_linter(tmp_path, (), '.')
