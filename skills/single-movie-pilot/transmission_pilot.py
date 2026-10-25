#!/usr/bin/env python3
"""Isolated, bounded Transmission 4.1+ trial using pinned metadata and existing budgets."""
import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from acquire import (GIB, capacity, pending_commitments, root_check, sha256,
                     torrent_metadata, torrent_plan, validate_manifest, write_json,
                     inspect, verify_torrent_file, completed_media_path)


class Client:
    def __init__(self, port, password):
        self.url = f'http://127.0.0.1:{port}/transmission/rpc'
        self.session = ''
        self.authorization = 'Basic ' + base64.b64encode(('pilot:' + password).encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, **params):
        payload = json.dumps(dict(jsonrpc='2.0', method=method, params=params, id=1)).encode()
        for attempt in range(2):
            request = urllib.request.Request(self.url, data=payload, headers={
                'Content-Type': 'application/json', 'Authorization': self.authorization,
                'X-Transmission-Session-Id': self.session})
            try:
                with self.opener.open(request, timeout=5) as response:
                    result = json.load(response)
                if 'error' in result:
                    raise RuntimeError('RPC failure: ' + json.dumps(result['error']))
                return result['result']
            except urllib.error.HTTPError as error:
                if error.code != 409 or attempt:
                    raise
                self.session = error.headers['X-Transmission-Session-Id']
        raise RuntimeError('RPC session handshake failed')


def local_proxy(value):
    if not value:
        return ''
    url = urlsplit(value)
    if (url.scheme not in {'http', 'socks5h'} or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or not url.port or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}):
        raise ValueError('trial proxy must be a local HTTP/SOCKS5h endpoint without credentials')
    return value


def check_selection(task, files, target, directory):
    expected = list(files.items())
    actual = task['files']
    if len(actual) != len(expected) or len(task['file_stats']) != len(expected):
        raise ValueError('Transmission inventory length differs')
    for record, (name, size) in zip(actual, expected):
        if (record['name'], record['length']) != (name, size):
            raise ValueError('Transmission inventory differs from pinned torrent')
    chosen = [expected[i][0] for i, stat in enumerate(task['file_stats']) if stat['wanted']]
    if chosen != [target] or Path(task['download_dir']).resolve() != directory.resolve():
        raise ValueError('Transmission selection/directory differs from approved movie')


def guard_space(root, device, reserve, other, system_root):
    root_check(root, device)
    if shutil.disk_usage(root).free < reserve + other or shutil.disk_usage(system_root).free < 30 * GIB:
        raise RuntimeError('free space crossed reserve; transfer stopped')


def run(args):
    proxy = local_proxy(getattr(args, 'proxy_url', ''))
    m = json.loads(args.manifest.read_text())
    validate_manifest(m)
    if m.get('transport') != 'bittorrent':
        raise ValueError('Transmission trial requires pinned torrent')
    if not math.isfinite(args.max_seconds) or args.max_seconds <= 0 or not math.isfinite(args.other_reserved_gib) or args.other_reserved_gib < 0:
        raise ValueError('invalid duration or external storage budget')
    raw = Path(m['torrent_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != m['torrent_sha256']:
        raise ValueError('pinned torrent changed')
    files, _ = torrent_plan(m, raw)
    target = m['payload_relative_path']
    selected = list(files).index(target)
    infohash = torrent_metadata(raw)['infohash']
    root = args.root.resolve(strict=True)
    device = root_check(root)
    job = root / hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()[:20]
    job.mkdir(exist_ok=True)
    state_file = job / 'state.json'
    with (root / '.pilot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        if state.get('status') in {'verified', 'verified_external'}:
            media = completed_media_path(job, state)
            if not media.is_file() or sha256(media) != state['sha256']:
                raise ValueError('previously verified media missing or changed')
            inspect(media, m, shutil.which('ffprobe') or 'ffprobe')
            print(json.dumps(dict(status='already_verified', path=str(media)), ensure_ascii=False))
            return 0
        pending = pending_commitments(root, job) + math.ceil(args.other_reserved_gib * GIB)
        usage = shutil.disk_usage(root)
        # Do not count old aria2 fragments as progress in the new backend.
        previous_complete = state.get('completed_bytes', 0) if state.get('backend') == 'Transmission' else 0
        budget = capacity(usage.free, usage.total, max(0, m['size_bytes'] - previous_complete), 0, 50, .1, pending)
        if not budget['ok'] or shutil.disk_usage(Path.home()).free < 30 * GIB:
            raise ValueError('capacity preflight rejected Transmission trial')
        directory = job / 'transmission-payload'
        directory.mkdir(exist_ok=True)
        config = job / 'transmission-config'
        config.mkdir(mode=0o700, exist_ok=True)
        config.chmod(0o700)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        password = secrets.token_urlsafe(32)
        settings = dict(rpc_enabled=True, rpc_bind_address='127.0.0.1', rpc_port=port,
            rpc_authentication_required=True, rpc_username='pilot', rpc_password=password,
            rpc_whitelist_enabled=True, rpc_whitelist='127.0.0.1',
            rpc_host_whitelist_enabled=True, rpc_host_whitelist='localhost',
            download_dir=str(directory), start_added_torrents=False, start_paused=True,
            port_forwarding_enabled=False, preallocation=0, incomplete_dir_enabled=False,
            trash_original_torrent_files=False, dht_enabled=True, pex_enabled=True,
            lpd_enabled=False, speed_limit_up_enabled=True, speed_limit_up=256,
            scrape_paused_torrents_enabled=False, proxy_url=proxy)
        settings_file = config / 'settings.json'
        settings_file.write_text(json.dumps(settings)); settings_file.chmod(0o600)
        binary = shutil.which('transmission-daemon')
        if not binary:
            raise ValueError('transmission-daemon must be installed')
        environment = {k: v for k, v in os.environ.items() if k.lower() not in
                       {'http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy'} }
        client = Client(port, password)
        process = None
        active = False
        started = time.monotonic()
        if state.get('backend') != 'Transmission':
            state['previous_transport_attempt'] = {k: state.get(k) for k in
                ('status', 'reason', 'completed_bytes', 'total_bytes', 'attempt')}
        state.update(status='starting', backend='Transmission', title=m['title'], year=m['year'],
            capacity=budget, commitment=dict(payload_bytes=m['size_bytes'], unpacked_bytes=0),
            completed_bytes=previous_complete, total_bytes=files[target], speed_bytes_per_second=0,
            runtime_pid=os.getpid(), rpc_port=port)
        state.update(http_proxy_url=proxy, proxy_scope='HTTP(S) requests only; peers/DHT/UDP are not proxied by this setting')
        state.pop('seeders', None)  # Do not retain a different backend's stale peer statistics.
        write_json(state_file, state)
        try:
            with (job / 'transmission.log').open('a') as log:
                process = subprocess.Popen([binary, '--foreground', '--paused', '--config-dir', str(config),
                    '--no-portmap', '--log-level=info'], stdout=log, stderr=subprocess.STDOUT, env=environment)
            for _ in range(30):
                try:
                    client.call('session_get', fields=['version']); break
                except (OSError, urllib.error.URLError):
                    if process.poll() is not None: raise RuntimeError('daemon exited during startup')
                    time.sleep(.5)
            else: raise RuntimeError('daemon RPC startup timeout')
            existing = client.call('torrent_get', fields=['hash_string'])['torrents']
            if any(t['hash_string'].lower() != infohash for t in existing):
                raise ValueError('isolated daemon contains an unexpected torrent')
            if not existing:
                options = dict(metainfo=base64.b64encode(raw).decode(), download_dir=str(directory),
                               paused=True, files_wanted=[selected])
                unwanted = [i for i in range(len(files)) if i != selected]
                if unwanted: options['files_unwanted'] = unwanted
                client.call('torrent_add', **options)
            def get():
                rows = client.call('torrent_get', ids=[infohash], fields=['hash_string', 'files', 'file_stats',
                    'download_dir', 'status', 'error', 'error_string', 'rate_download', 'peers_connected',
                    'left_until_done', 'tracker_stats'])['torrents']
                if len(rows) != 1: raise RuntimeError('expected exactly one torrent')
                check_selection(rows[0], files, target, directory)
                return rows[0]
            task = get()
            guard_space(root, device, budget['reserve_bytes'], pending, Path.home())
            client.call('torrent_start', ids=[infohash]); active = True
            last_bytes = task['files'][selected]['bytes_completed']
            last_progress = time.monotonic()
            last_print = 0
            while True:
                guard_space(root, device, budget['reserve_bytes'], pending, Path.home())
                task = get()
                complete = task['files'][selected]['bytes_completed']
                now = time.monotonic()
                state.update(status='downloading', completed_bytes=complete, total_bytes=files[target],
                    speed_bytes_per_second=task['rate_download'], connections=task['peers_connected'],
                    backend_status=task['status'], last_observed_unix=time.time(),
                    tracker_errors=[t.get('last_announce_result') for t in task['tracker_stats']
                        if t.get('last_announce_result') and not t.get('last_announce_succeeded')])
                state.pop('reason', None)
                write_json(state_file, state)
                if now - last_print >= 15:
                    print(json.dumps({k: state[k] for k in ('status', 'completed_bytes', 'total_bytes', 'speed_bytes_per_second', 'connections')}), flush=True)
                    last_print = now
                if complete > last_bytes: last_progress = now; last_bytes = complete
                if task['error']: raise RuntimeError('Transmission: ' + task['error_string'])
                if task['left_until_done'] == 0:
                    client.call('torrent_stop', ids=[infohash]); active = False
                    media = directory / target
                    state.update(status='verifying'); write_json(state_file, state)
                    proof = verify_torrent_file(raw, target, media)
                    validation = inspect(media, m, shutil.which('ffprobe') or 'ffprobe')
                    state.update(status='verified_external', external_media_path=str(media),
                        sha256=sha256(media), verification=validation, piece_verification=proof,
                        speed_bytes_per_second=0)
                    write_json(state_file, state)
                    return 0
                if now - started >= args.max_seconds: raise TimeoutError('execution limit; rerun same manifest resumes')
                if now - last_progress >= 300: raise TimeoutError('no progress for 300 seconds; review candidate/network')
                time.sleep(2)
        except (Exception, KeyboardInterrupt) as error:
            state.update(status='needs_attention', reason=str(error) or 'interrupted', speed_bytes_per_second=0)
            write_json(state_file, state)
            print(json.dumps(dict(status=state['status'], reason=state['reason'])), flush=True)
            return 4
        finally:
            if active:
                try: client.call('torrent_stop', ids=[infohash])
                except Exception: pass
            if process and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired: process.kill(); process.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--max-seconds', type=float, default=14400)
    parser.add_argument('--other-reserved-gib', type=float, default=0)
    parser.add_argument('--proxy-url', default='', help='local HTTP/SOCKS5h proxy for HTTP(S) requests only')
    try: raise SystemExit(run(parser.parse_args()))
    except (ValueError, OSError, RuntimeError) as error:
        print(json.dumps(dict(status='blocked', reason=str(error)))); raise SystemExit(3)
