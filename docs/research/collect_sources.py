"""Read-only public-source collector; save auditable research excerpts locally."""
import concurrent.futures
import hashlib
import html.parser
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent


class TextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.skip += 1
        elif tag in ('p', 'div', 'h1', 'h2', 'h3', 'li', 'tr', 'br', 'section'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def collect(item):
    result = dict(item, fetched_at=datetime.now(timezone.utc).isoformat())
    try:
        request = urllib.request.Request(item['url'], headers={'User-Agent': 'Mozilla/5.0 (public-source research)'})
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = response.read(8_000_000)
            result.update(status=response.status, final_url=response.url,
                          sha256=hashlib.sha256(raw).hexdigest())
            charset = response.headers.get_content_charset() or 'utf-8'
        content = raw.decode(charset, errors='replace')
        if '<html' in content[:3000].lower() or '<!doctype html' in content[:1000].lower():
            parser = TextExtractor()
            parser.feed(content)
            content = '\n'.join(re.sub(r'\s+', ' ', line).strip() for line in ''.join(parser.parts).splitlines())
            content = re.sub(r'\n{3,}', '\n\n', content)
        path = ROOT / 'sources' / (item['id'] + '.txt')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        result.update(ok=True, text_path=str(path.relative_to(ROOT)), characters=len(content))
    except Exception as error:
        result.update(ok=False, error=str(error))
    return result


if __name__ == '__main__':
    manifest = ROOT / (sys.argv[1] if len(sys.argv) > 1 else 'source-plan.json')
    items = json.loads(manifest.read_text(encoding='utf-8'))
    output = ROOT / (manifest.stem + '-results.json')
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(collect, items))
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    for item in results:
        print(item['id'], 'OK' if item['ok'] else 'FAILED', item.get('characters', item.get('error')), flush=True)
