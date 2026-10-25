#!/usr/bin/env python3
"""Inspect a public 1LOU film post: bind the first post's IMDb ID to actual torrent attachments."""
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from acquire import torrent_metadata, write_json
from discover import fetch

HOSTS = {'www.1lou.me', '1lou.me', 'www.1lou.pro', '1lou.pro'}


class Post(HTMLParser):
    def __init__(self, url):
        super().__init__()
        self.url = url
        self.depth = 0
        self.ids = set()
        self.attachments = []
        self.title = ''
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'h4':
            self.in_title = True
        if tag == 'div':
            if self.depth:
                self.depth += 1
            elif 'message' in attrs.get('class', '').split() and attrs.get('isfirst') == '1':
                self.depth = 1
        if self.depth and tag == 'a':
            link = urljoin(self.url, attrs.get('href', ''))
            parsed = urlsplit(link)
            if parsed.hostname in {'imdb.com', 'www.imdb.com'}:
                match = re.fullmatch(r'/title/(tt[0-9]+)/?', parsed.path)
                if match:
                    self.ids.add(match[1])
            if (parsed.scheme == 'https' and parsed.hostname == urlsplit(self.url).hostname
                    and re.fullmatch(r'/attach-download-[0-9]+\.htm', parsed.path)
                    and not parsed.query and not parsed.fragment and link not in self.attachments):
                self.attachments.append(link)

    def handle_endtag(self, tag):
        if tag == 'div' and self.depth:
            self.depth -= 1
        if tag == 'h4':
            self.in_title = False

    def handle_data(self, value):
        if self.in_title:
            self.title += value
        if self.depth:
            # The live forum renders metadata URLs as plain text, not always anchors.
            self.ids.update(re.findall(r'https?://(?:www\.)?imdb\.com/title/(tt[0-9]+)(?:/|\b)', value))


def parse_page(url, html, imdb_id):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in HOSTS
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r'/thread-[0-9]+\.htm', parsed.path)):
        raise ValueError('adapter supports only public 1LOU HTTPS thread URLs')
    if not re.fullmatch(r'tt[0-9]+', imdb_id):
        raise ValueError('invalid IMDb ID')
    page = Post(url)
    page.feed(html)
    if page.ids != {imdb_id}:
        raise ValueError('first-post IMDb identity is missing, ambiguous or different')
    if not page.attachments or len(page.attachments) > 5:
        raise ValueError('no bounded attachment set; page structure may have changed')
    return {'source_page': url, 'imdb_id': imdb_id, 'page_title': page.title.strip(),
            'attachment_urls': page.attachments}


def inspect_page(url, imdb_id, directory):
    if not directory.is_dir():
        raise ValueError('inspection directory must already exist')
    # Validate the URL before any request.
    parsed = urlsplit(url)
    if parsed.hostname not in HOSTS or not re.fullmatch(r'/thread-[0-9]+\.htm', parsed.path):
        raise ValueError('unsupported source URL')
    html = fetch(url).decode('utf-8')
    result = parse_page(url, html, imdb_id)
    candidates = []
    for attachment in result.pop('attachment_urls'):
        raw = fetch(attachment)
        meta = torrent_metadata(raw)
        digest = hashlib.sha256(raw).hexdigest()
        target = directory.absolute() / (digest + '.torrent')
        if target.exists() and target.read_bytes() != raw:
            raise ValueError('existing pinned metadata differs; refusing overwrite')
        if not target.exists():
            target.write_bytes(raw)
        candidates.append(dict(meta, torrent_path=str(target), torrent_sha256=digest,
                               attachment_url=attachment))
    (directory / 'page.html').write_text(html)
    result.update(status='metadata_ready', candidates=candidates, payload_submitted=False,
                  boundary='IMDb matched on the first post; media identity and claimed quality still require verification.')
    write_json(directory / 'inspection.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', required=True)
    p.add_argument('--imdb-id', required=True)
    p.add_argument('--directory', type=Path, required=True)
    a = p.parse_args()
    try:
        print(json.dumps(inspect_page(a.url, a.imdb_id, a.directory), ensure_ascii=False))
    except (ValueError, OSError, KeyError, IndexError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
