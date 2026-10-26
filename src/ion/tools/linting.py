"""Select project-native lint commands without installing dependencies."""
import json
import re
import shlex
import shutil


def select_linter(root, configured, target):
    if configured:
        if not all(isinstance(arg, str) and arg and '\0' not in arg for arg in configured):
            raise ValueError('lint_command must be a nonempty argv list')
        return list(configured), 'project'
    def read(name):
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256*1024:
            return ''
        return path.read_text(errors='replace')
    def executable(name):
        for folder in ('.venv/bin', 'node_modules/.bin', 'vendor/bin'):
            path = root / folder / name
            if path.is_file():
                return str(path)
        return shutil.which(name)
    makefile = next((read(name) for name in ('GNUmakefile', 'makefile', 'Makefile') if (root / name).is_file()), '')
    if re.search(r'^lint\s*:(?!=)', makefile, re.M):
        command = executable('make')
        if not command:
            raise ValueError('Project declares make lint, but make is not installed')
        return [command, 'lint'], 'project'
    package = json.loads(read('package.json') or '{}')
    if not isinstance(package, dict) or not isinstance(package.get('scripts', {}), dict):
        raise ValueError('package.json must contain an object with a scripts object')
    if package.get('scripts', {}).get('lint'):
        manager = str(package.get('packageManager', '')).split('@')[0]
        if manager not in {'npm', 'pnpm', 'yarn', 'bun'}:
            manager = next((name for lock, name in [('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lock', 'bun'), ('bun.lockb', 'bun')] if (root/lock).exists()), 'npm')
        command = executable(manager)
        if not command:
            raise ValueError(f'Project lint script requires {manager}; install it before linting')
        return [command, 'run', 'lint'], 'project'
    pyproject = read('pyproject.toml')
    adapters = [
        ('ruff', ['check', '--no-fix', '--', target], bool(pyproject or read('requirements.txt') or read('ruff.toml') or read('.ruff.toml')), 'target'),
        ('mypy', ['--', target], '[tool.mypy]' in pyproject or bool(read('mypy.ini')), 'target'),
        ('eslint', ['--no-fix', '--', target], bool(package), 'target'),
        ('go', ['vet', './...'], bool(read('go.mod')), 'project'),
        ('cargo', ['clippy', '--all-targets'], bool(read('Cargo.toml')), 'project'),
        ('rubocop', ['--', target], bool(read('Gemfile')), 'target'),
        ('phpstan', ['analyse', '--', target], bool(read('composer.json')), 'target'),
    ]
    for name, args, detected, scope in adapters:
        command = executable(name) if detected else None
        if command:
            return [command, *args], scope
    raise ValueError('No installed project linter found. Configure [tools].lint_command as an argv list or add a lint task; Ion does not install linters.')


async def run_linter(workspace, supervisor, configured=(), relative_path=None):
    target = relative_path or '.'
    if target != '.':
        path = workspace.resolve(target, allow_new=True)
        if not path.exists():
            raise ValueError('lint target does not exist')
    argv, scope = select_linter(workspace.root, configured, target)
    data = await supervisor.run(shlex.join(argv))
    status = ('timed_out' if data.get('timed_out') else
              'passed' if data.get('exit_code') == 0 else
              'diagnostics' if data.get('exit_code') == 1 else 'execution_error')
    return {**data, 'lint_status': status, 'passed': status == 'passed',
            'scope': scope, 'requested_path': target, 'command': shlex.join(argv)}
