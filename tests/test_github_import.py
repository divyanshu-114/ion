import asyncio

import httpx
import pytest

from ion.tools.github import import_issue, issue_identity


@pytest.mark.parametrize('url', ['http://github.com/a/b/issues/1', 'https://evil.test/a/b/issues/1', 'https://github.com/a/b/pull/1', 'https://github.com@evil.test/a/b/issues/1', 'https://github.com/a/b/issues/1?x=y'])
def test_invalid_issue_url(url):
    with pytest.raises(ValueError):
        issue_identity(url)


@pytest.mark.asyncio
async def test_imports_public_issue_comments_and_local_paths(tmp_path):
    (tmp_path / 'app.py').write_text('')
    seen = []
    def handle(request):
        assert request.url.host == 'api.github.com'
        assert 'authorization' not in request.headers
        seen.append(request.url.path)
        if request.url.path.endswith('/comments'):
            assert request.url.params['per_page'] == '100'
            return httpx.Response(200, json=[{'user': {'login': 'helper'}, 'body': 'Inspect `app.py` and [missing](src/missing.py)'}])
        return httpx.Response(200, json={'title': 'Crash on startup', 'body': 'Repro steps', 'comments': 102})
    markdown = await import_issue('https://github.com/acme/repo/issues/12', tmp_path, transport=httpx.MockTransport(handle))
    assert len(seen) == 2
    assert 'Crash on startup' in markdown and 'Comment by helper' in markdown
    assert '`app.py` — exists locally' in markdown
    assert '`src/missing.py` — not found locally' in markdown
    assert '101 additional comments omitted' in markdown
    assert 'untrusted data' in markdown


@pytest.mark.asyncio
async def test_pull_request_payload_rejected(tmp_path):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={'title': 'PR', 'pull_request': {}}))
    with pytest.raises(ValueError, match='pull request'):
        await import_issue('https://github.com/acme/repo/issues/12', tmp_path, transport=transport)


@pytest.mark.asyncio
async def test_known_remote_mismatch_fails_before_network(tmp_path):
    for args in [('init', '-q'), ('remote', 'add', 'origin', 'git@github.com:acme/other.git')]:
        proc = await asyncio.create_subprocess_exec('git', *args, cwd=tmp_path)
        assert await proc.wait() == 0
    def handle(_):
        pytest.fail('must reject before fetching')
    with pytest.raises(ValueError, match='different repository'):
        await import_issue('https://github.com/acme/repo/issues/12', tmp_path, transport=httpx.MockTransport(handle))


@pytest.mark.asyncio
async def test_http_cancellation_propagates(tmp_path):
    entered = asyncio.Event()
    async def handle(_):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(import_issue('https://github.com/acme/repo/issues/12', tmp_path, transport=httpx.MockTransport(handle)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_issue_checkout_clones_real_repository_and_keeps_launch_tree(tmp_path, monkeypatch):
    from ion.tools.github import prepare_issue_workspace

    source = tmp_path / 'source'
    source.mkdir()
    (source / 'app.py').write_text('answer = 42\n')
    for args in [('init', '-q'), ('add', 'app.py'), ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')]:
        process = await asyncio.create_subprocess_exec('git', *args, cwd=source)
        assert await process.wait() == 0
    original_exec = asyncio.create_subprocess_exec
    calls = []
    async def local_clone(*args, **kwargs):
        calls.append((args, kwargs))
        args = tuple(str(source) if arg == 'https://github.com/acme/repo.git' else arg for arg in args)
        return await original_exec(*args, **kwargs)
    monkeypatch.setenv('AI_API_KEY', 'fixture-secret')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', local_clone)
    first = await prepare_issue_workspace('https://github.com/acme/repo/issues/12', tmp_path / 'checkouts')
    second = await prepare_issue_workspace('https://github.com/acme/repo/issues/12', tmp_path / 'checkouts')
    assert first != second
    assert (first / 'app.py').read_text() == 'answer = 42\n'
    assert (first / '.git').is_dir()
    assert 'AI_API_KEY' not in calls[0][1]['env']
    assert calls[0][1]['env']['GIT_TERMINAL_PROMPT'] == '0'
    assert (source / 'app.py').read_text() == 'answer = 42\n'


@pytest.mark.asyncio
async def test_failed_clone_cleans_partial_checkout(tmp_path, monkeypatch):
    from ion.tools.github import prepare_issue_workspace

    original_exec = asyncio.create_subprocess_exec
    async def fail_clone(*args, **kwargs):
        return await original_exec('git', 'clone', str(tmp_path / 'missing'), str(args[-1]), **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fail_clone)
    with pytest.raises(ValueError, match='clone'):
        await prepare_issue_workspace('https://github.com/acme/repo/issues/12', tmp_path / 'checkouts')
    assert list((tmp_path / 'checkouts').iterdir()) == []


@pytest.mark.parametrize('url', ['https://github.com/../repo/issues/1', 'https://github.com/acme/../issues/1'])
def test_issue_identity_rejects_dot_path_segments(url):
    with pytest.raises(ValueError):
        issue_identity(url)


@pytest.mark.asyncio
async def test_cancelled_clone_stops_process_and_removes_partial_checkout(tmp_path, monkeypatch):
    import sys
    from ion.tools.github import prepare_issue_workspace

    entered = asyncio.Event()
    processes = []
    original_exec = asyncio.create_subprocess_exec
    async def slow_clone(*args, **kwargs):
        process = await original_exec(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        processes.append(process)
        entered.set()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', slow_clone)
    task = asyncio.create_task(prepare_issue_workspace('https://github.com/acme/repo/issues/1', tmp_path / 'checkouts'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
    assert list((tmp_path / 'checkouts').iterdir()) == []


@pytest.mark.asyncio
async def test_clone_reports_progress_while_process_is_running(tmp_path, monkeypatch):
    import sys
    from ion.tools.github import prepare_issue_workspace

    original_exec = asyncio.create_subprocess_exec
    reported = []
    async def progress_clone(*args, **kwargs):
        assert '--progress' in args and '--quiet' not in args
        return await original_exec(sys.executable, '-c',
            'import sys,time;sys.stderr.write("Receiving objects: 50%\\r");sys.stderr.flush();time.sleep(.1);sys.stderr.write("Receiving objects: 100%\\n")', **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', progress_clone)
    await prepare_issue_workspace('https://github.com/acme/repo/issues/1', tmp_path / 'checkouts', on_progress=reported.append)
    assert any('50%' in line for line in reported)
    assert any('100%' in line for line in reported)
