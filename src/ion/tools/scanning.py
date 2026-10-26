"""Bounded, read-only workspace discovery."""
import json
import re
import tomllib

import yaml


def texts(workspace, *, count=2000, size=512 * 1024, total=16 * 1024 * 1024, select=lambda _: True):
    from ion.tools.registry import private_path
    used = 0
    for path in sorted(workspace._paths()):
        relative = path.relative_to(workspace.root).as_posix()
        if private_path(relative) or not select(relative):
            continue
        if count <= 0 or used >= total:
            yield None, None
            return
        count -= 1
        try:
            with path.open('rb') as stream:
                raw = stream.read(min(size, total - used) + 1)
            used += len(raw)
            if len(raw) > size or used > total or b'\0' in raw:
                yield relative, None
                continue
            yield relative, raw.decode('utf-8')
        except (OSError, UnicodeError):
            yield relative, None


def trace_symbol(workspace, symbol):
    if not symbol.strip() or len(symbol) > 128:
        raise ValueError('symbol must contain 1–128 characters')
    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    definition_pattern = re.compile(r'\b(?:class|def|interface|const|function|let|var|type|struct|enum)\s+(' + re.escape(symbol) + r')\b')
    definitions, references = [], []
    scanned = skipped = matched = 0
    truncated = False
    for path, text in texts(workspace):
        if path is None:
            truncated = True
            break
        if text is None:
            skipped += 1
            continue
        scanned += 1
        offset = 0
        for line_no, line in enumerate(text.splitlines(keepends=True), 1):
            definition_offsets = {match.start(1) for match in definition_pattern.finditer(line)}
            for match in pattern.finditer(line):
                definition = match.start() in definition_offsets
                bucket = definitions if definition else references
                matched += 1
                if len(bucket) < 20:
                    bucket.append(dict(path=path, line=line_no, offset=offset + match.start(), kind='definition' if definition else 'reference', snippet=line[max(0, match.start()-50):][:160].strip()))
            offset += len(line)
    return dict(matches=(definitions + references)[:20], scanned_files=scanned, skipped_files=skipped, truncated=truncated or matched > 20)


def infra_scan(workspace):
    roots = {'Dockerfile', 'docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml', 'package.json', 'requirements.txt', 'pyproject.toml'}
    def candidate(path):
        return path in roots or (path.endswith(('.yml', '.yaml')) and ('/' not in path or path.split('/')[0] in {'k8s', 'kubernetes', 'deploy'}))
    languages, frameworks, sources, warnings, ports, services = set(), set(), [], [], [], []
    truncated = False
    def bounded(items, count):
        nonlocal truncated
        for index, item in enumerate(items):
            if index == count:
                truncated = True
                break
            yield item
    def scalar(value):
        return str(value)[:100] if isinstance(value, (str, int)) else 'unknown'
    framework_names = {'next': 'Next.js', 'react': 'React', 'vue': 'Vue', 'express': 'Express', 'svelte': 'Svelte', 'django': 'Django', 'flask': 'Flask', 'fastapi': 'FastAPI'}
    def dependencies(names):
        for name in names:
            key = re.split(r'[\s\[<>=!~;]', str(name).lower())[0]
            if key in framework_names:
                frameworks.add(framework_names[key])
    def port(source, **values):
        nonlocal truncated
        if len(ports) < 60:
            bounded = {key: value[:100] if isinstance(value, str) else value for key, value in values.items() if isinstance(value, (str, int)) and not isinstance(value, bool)}
            ports.append({'source': source, **bounded})
        else:
            truncated = True
    for path, text in texts(workspace, count=100, size=256*1024, total=4*1024*1024, select=candidate):
        if text is None:
            truncated = True
            warnings.append(f'{path or "scan"}: scan limit, unreadable or non-text file')
            continue
        sources.append(path)
        try:
            if path == 'package.json':
                obj = json.loads(text)
                languages.add('JavaScript')
                deps = {**obj.get('dependencies', {}), **obj.get('devDependencies', {})}
                if 'typescript' in deps:
                    languages.add('TypeScript')
                dependencies(deps)
            elif path in {'requirements.txt', 'pyproject.toml'}:
                languages.add('Python')
                dependencies(text.splitlines() if path.endswith('.txt') else tomllib.loads(text).get('project', {}).get('dependencies', []))
            elif path == 'Dockerfile':
                base_languages = {'node': 'JavaScript', 'python': 'Python', 'golang': 'Go', 'rust': 'Rust', 'ruby': 'Ruby', 'php': 'PHP'}
                for base in re.findall(r'^\s*FROM\s+(?:--platform=\S+\s+)?([^\s:]+)', text, re.M | re.I):
                    if base.rsplit('/', 1)[-1] in base_languages:
                        languages.add(base_languages[base.rsplit('/', 1)[-1]])
                for declaration in re.findall(r'^\s*EXPOSE\s+([^\n#]+)', text, re.M | re.I):
                    for value in declaration.split():
                        if value.split('/')[0].isdigit():
                            port(path, container_port=int(value.split('/')[0]))
            else:
                for doc in bounded(yaml.safe_load_all(text), 100):
                    if not isinstance(doc, dict):
                        continue
                    for name, service in bounded((doc.get('services') or {}).items(), 40):
                        if len(services) < 40:
                            services.append({'name': scalar(name), 'source': path})
                        for value in bounded((service or {}).get('ports', []), 60):
                            if isinstance(value, dict):
                                port(path, service=scalar(name), host_port=value.get('published'), container_port=value.get('target'))
                            elif isinstance(value, (str, int)):
                                parts = str(value).split('/')[0].split(':')
                                if parts[-1].isdigit():
                                    port(path, service=scalar(name), container_port=int(parts[-1]), **({'host_port': int(parts[-2])} if len(parts)>1 and parts[-2].isdigit() else {}))
                    if doc.get('kind'):
                        if len(services) < 40:
                            services.append({'name': scalar(doc.get('metadata', {}).get('name', doc['kind'])), 'kind': scalar(doc['kind']), 'source': path})
                        seen = set()
                        def walk(node, depth=0):
                            nonlocal truncated
                            if id(node) in seen:
                                return
                            if depth > 15 or len(seen) > 4000:
                                truncated = True
                                return
                            seen.add(id(node))
                            if isinstance(node, dict):
                                for key, value in node.items():
                                    if key in {'containerPort', 'port', 'targetPort', 'nodePort'} and isinstance(value, (str, int)):
                                        port(path, **{key: value})
                                    elif isinstance(value, (dict, list)):
                                        walk(value, depth+1)
                            elif isinstance(node, list):
                                for value in bounded(node, 100):
                                    walk(value, depth+1)
                        walk(doc)
        except (ValueError, TypeError, AttributeError, RecursionError, yaml.YAMLError):
            warnings.append(f'{path}: invalid or unsupported manifest')
    result = dict(languages=sorted(languages), frameworks=sorted(frameworks), services=services, ports=ports, source_files=sources, scanned_files=len(sources), warnings=warnings[:100], truncated=truncated)
    while len(json.dumps(result)) > 12000:
        result['truncated'] = True
        bucket = max((result[key] for key in ('services', 'ports', 'source_files', 'warnings')), key=lambda items: len(json.dumps(items)))
        if not bucket:
            break
        bucket.pop()
    return result
