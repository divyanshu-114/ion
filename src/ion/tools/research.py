"""Keyless, bounded HTTP research. Never inherits model credentials."""
import asyncio
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx


async def fetch(url, *, params=None, transport=None):
    try:
        async with asyncio.timeout(15):
            async with httpx.AsyncClient(transport=transport, timeout=15, follow_redirects=False, trust_env=False) as client:
                async with client.stream('GET', url, params=params, headers={'Accept': 'application/json, text/html', 'User-Agent': 'ion-research/1.0'}) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1024*1024:
                            raise ValueError('Research response exceeds 1 MiB')
                    return raw.decode('utf-8', 'replace')
    except (httpx.HTTPError, TimeoutError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 'connection/timeout'
        raise ValueError(f'Research request failed ({status}); retry later') from None


class Results(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.current = None
        self.field = None
        self.tag = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if 'result__a' in classes:
            url = attrs.get('href', '')
            parsed = urlsplit(url)
            url = parse_qs(parsed.query).get('uddg', [url])[0]
            if urlsplit(url).scheme not in {'http', 'https'}:
                self.current = None
                return
            self.current = {'url': url, 'title': '', 'snippet': ''}
            self.items.append(self.current)
            self.field, self.tag = 'title', tag
        elif 'result__snippet' in classes and self.current:
            self.field, self.tag = 'snippet', tag

    def handle_data(self, data):
        if self.current is not None and self.field:
            self.current[self.field] += data

    def handle_endtag(self, tag):
        if tag == self.tag:
            self.field = self.tag = None


async def web_search(query, transport=None):
    if not query.strip() or len(query) > 500:
        raise ValueError('query must contain 1–500 characters')
    html = await fetch('https://html.duckduckgo.com/html/', params={'q': query}, transport=transport)
    if any(marker in html.lower() for marker in ('challenge-form', 'anomaly.js', 'anomaly-modal')):
        raise ValueError('Search service presented a challenge; retry later')
    parser = Results()
    parser.feed(html)
    unique = {}
    for item in parser.items:
        unique.setdefault(item['url'], item)
    items = list(unique.values())[:3]
    if not items and 'no-results' not in html and 'No results found' not in html:
        raise ValueError('Search service returned an unrecognized response')
    blocks = [f"{i+1}. {item['title'].strip()[:100]}\n{item['url'][:300]}\n{item['snippet'].strip()[:240]}" for i, item in enumerate(items)]
    return {'text': '\n\n'.join(blocks)[:2000] or 'No results found.', 'result_count': len(items), 'source': 'DuckDuckGo'}
