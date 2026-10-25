#!/usr/bin/env python3
"""Small live catalogue adapter and metadata-only magnet preflight; no payload download."""
import argparse
import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import time
from urllib.parse import parse_qs, urlsplit
import urllib.request

from acquire import torrent_metadata, validate_manifest, write_json, VIDEO, direct_environment, direct_opener

CATALOGUE = 'https://webtorrent.io/free-torrents'
LIMIT = 8 * 1024 * 1024
ALIASES = {'大雄兔': 'Big Buck Bunny', '辛特尔': 'Sintel', '钢铁之泪': 'Tears of Steel'}


def fetch(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('only public HTTPS metadata URLs without query/credentials are accepted')
    ctx = ssl.create_default_context()
    if not ssl.get_default_verify_paths().cafile and Path('/etc/ssl/cert.pem').is_file():
        ctx.load_verify_locations('/etc/ssl/cert.pem')
    with direct_opener(ctx).open(url, timeout=30) as r:
        if urlsplit(r.url).scheme != 'https':
            raise ValueError('insecure metadata redirect')
        raw = r.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('metadata response exceeds 8 MiB')
    return raw


def magnet_parts(uri):
    p = urlsplit(uri)
    if p.scheme != 'magnet' or p.netloc or p.path or p.fragment:
        raise ValueError('invalid magnet URI')
    q = parse_qs(p.query)
    xt = q.get('xt', [])
    if len(xt) != 1 or not xt[0].startswith('urn:btih:'):
        raise ValueError('exactly one v1 btih is required; v2-only magnets are unsupported')
    value = xt[0][9:]
    if re.fullmatch('[0-9a-fA-F]{40}', value):
        digest = value.lower()
    elif re.fullmatch('[A-Za-z2-7]{32}', value):
        digest = base64.b32decode(value.upper()).hex()
    else:
        raise ValueError('invalid magnet infohash')
    return digest, q


class Catalogue(HTMLParser):
    def __init__(self):
        super().__init__()
        self.row = None
        self.rows = []
        self.in_link = False

    def handle_starttag(self, tag, attrs):
        if tag == 'li':
            self.row = {'title': '', 'source_page': CATALOGUE}
        if tag == 'a' and self.row is not None:
            self.in_link = True
            href = dict(attrs).get('href', '')
            if href.startswith('https://webtorrent.io/torrents/') and href.endswith('.torrent'):
                self.row['torrent_url'] = href
            elif href.startswith('magnet:?'):
                self.row['magnet'] = href

    def handle_data(self, text):
        if self.row is not None and not self.in_link:
            self.row['title'] += text

    def handle_endtag(self, tag):
        if tag == 'a':
            self.in_link = False
        if tag == 'li' and self.row is not None:
            if 'torrent_url' in self.row and 'magnet' in self.row:
                self.row['title'] = self.row['title'].strip()
                self.rows.append(self.row)
            self.row = None


def search(title, html):
    parser = Catalogue()
    parser.feed(html)
    if not parser.rows:
        raise ValueError('catalogue layout changed or empty; refuse guessed URLs')
    query = ALIASES.get(title.strip(), title.strip()).casefold()
    if not query:
        raise ValueError('empty title')
    exact = [r for r in parser.rows if r['title'].casefold() == query]
    rows = exact or [r for r in parser.rows if query in r['title'].casefold()]
    status = 'found' if len(rows) == 1 else 'ambiguous' if rows else 'not_found'
    return {'status': status, 'query': title, 'scope': CATALOGUE, 'candidates': rows,
            'quality_comparison': 'This catalogue exposes one candidate per title; highest quality is unproven.'}


def resolve(uri, directory, seconds=90):
    digest, parts = magnet_parts(uri)
    directory = directory.absolute()
    if not directory.is_dir():
        raise ValueError('metadata directory must already exist')
    if not 1 <= seconds <= 300:
        raise ValueError('metadata timeout must be 1..300 seconds')
    if shutil.disk_usage(directory).free < 64 * 1024 * 1024:
        raise ValueError('metadata requires 64 MiB free space')
    destination = directory / (digest + '.torrent')
    method = 'cached_validated_metadata'
    if not destination.exists():
        if list(directory.iterdir()):
            raise ValueError('new metadata resolution requires an empty isolated directory')
        xs = parts.get('xs', [])
        if xs:
            if len(xs) != 1:
                raise ValueError('multiple exact sources require review')
            raw = fetch(xs[0])
            if torrent_metadata(raw)['infohash'] != digest:
                raise ValueError('exact-source torrent does not match magnet hash')
            destination.write_bytes(raw)
            method = 'https_exact_source_metadata_only'
        else:
            aria = shutil.which('aria2c')
            if not aria:
                raise ValueError('aria2c missing')
            command = [aria, '--no-conf=true', '--bt-metadata-only=true', '--bt-save-metadata=true',
                '--enable-dht=true', '--enable-dht6=false', '--bt-enable-lpd=false',
                '--enable-rpc=false', '--seed-time=0', '--file-allocation=none',
                '--bt-load-saved-metadata=false', '--console-log-level=warn',
                '--dir=' + str(directory), '--dht-file-path=' + str(directory / 'dht.dat'), uri]
            started = time.monotonic()
            with (directory / 'metadata.log').open('w') as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=direct_environment())
                try:
                    while process.poll() is None:
                        if time.monotonic() - started > seconds:
                            raise TimeoutError('magnet peer metadata timed out; no payload submitted')
                        if sum(p.stat().st_size for p in directory.rglob('*') if p.is_file()) > 32 * 1024 * 1024:
                            raise RuntimeError('metadata directory exceeded 32 MiB; stopped')
                        time.sleep(1)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            if process.returncode or not destination.exists():
                raise RuntimeError('aria2 did not produce magnet metadata; inspect metadata.log')
            method = 'peer_metadata_only'
    raw = destination.read_bytes()
    details = torrent_metadata(raw)
    if details['infohash'] != digest:
        raise ValueError('resolved metadata hash differs from magnet')
    if any(p.suffix.lower() in VIDEO for p in directory.rglob('*')):
        raise RuntimeError('unexpected video in metadata directory')
    return dict(details, status='metadata_ready', method=method, torrent_path=str(destination),
                torrent_sha256=hashlib.sha256(raw).hexdigest(), payload_submitted=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('search')
    s.add_argument('--title', required=True)
    s.add_argument('--output', type=Path)
    m = sub.add_parser('resolve')
    m.add_argument('--magnet-file', type=Path, required=True)
    m.add_argument('--directory', type=Path, required=True)
    m.add_argument('--max-seconds', type=int, default=90)
    m.add_argument('--output', type=Path)
    a = sub.add_parser('prepare')
    a.add_argument('--title', required=True)
    a.add_argument('--directory', type=Path, required=True)
    a.add_argument('--year', type=int, required=True)
    a.add_argument('--min-width', type=int, required=True)
    a.add_argument('--min-height', type=int, required=True)
    a.add_argument('--min-duration', type=float, required=True)
    a.add_argument('--max-duration', type=float, required=True)
    args = p.parse_args()
    if args.command == 'resolve':
        result = resolve(args.magnet_file.read_text().strip(), args.directory, args.max_seconds)
    else:
        html = fetch(CATALOGUE).decode('utf-8')
        result = search(args.title, html)
        if args.command == 'prepare':
            if result['status'] != 'found':
                raise ValueError('title missing or ambiguous; no manifest created')
            if not args.directory.is_dir() or list(args.directory.iterdir()):
                raise ValueError('preparation requires an existing empty directory; reuse original manifests for resume')
            row = result['candidates'][0]
            raw = fetch(row['torrent_url'])
            details = torrent_metadata(raw)
            if details['infohash'] != magnet_parts(row['magnet'])[0]:
                raise ValueError('catalogue magnet and torrent hash mismatch')
            videos = [name for name in details['files'] if Path(name).suffix.lower() in VIDEO]
            if len(videos) != 1:
                raise ValueError('expected exactly one movie; explicit file selection required')
            torrent = args.directory.absolute() / 'source.torrent'
            manifest = dict(title=row['title'], year=args.year, source_page=CATALOGUE,
                url=row['torrent_url'], transport='bittorrent', torrent_path=str(torrent),
                torrent_sha256=hashlib.sha256(raw).hexdigest(), filename=Path(videos[0]).name,
                payload_relative_path=videos[0], size_bytes=details['size_bytes'],
                min_width=args.min_width, min_height=args.min_height,
                min_duration_seconds=args.min_duration, max_duration_seconds=args.max_duration)
            validate_manifest(manifest)
            torrent.write_bytes(raw)
            write_json(args.directory / 'manifest.json', manifest)
            write_json(args.directory / 'discovery.json', dict(result, metadata=details))
            (args.directory / 'magnet.txt').write_text(row['magnet'] + '\n')
            result = {'status': 'prepared', 'manifest': str(args.directory / 'manifest.json'),
                      'size_bytes': details['size_bytes'], 'payload_submitted': False}
    if getattr(args, 'output', None):
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 3 if result['status'] in {'not_found', 'ambiguous'} else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, KeyError, IndexError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
