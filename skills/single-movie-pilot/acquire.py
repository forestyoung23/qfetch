#!/usr/bin/env python3
"""Single HTTPS or pinned local-torrent movie pilot; no cloud-drive or batch claims."""
import argparse
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
import ssl
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
import zipfile
import zlib

GIB = 1024 ** 3
VIDEO = {'.mp4', '.mkv', '.mov', '.m4v', '.webm', '.avi'}


def direct_environment():
    """Disable inherited process proxies without changing the user's shell/system."""
    return {k: v for k, v in os.environ.items() if k.lower() not in
            {'http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy', 'no_proxy'}}


def direct_opener(context=None):
    handlers = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def torrent_metadata(raw, include_pieces=False):
    """Bounded bencode parser, used only to inspect metadata before starting aria2."""
    pos = 0
    info_raw = None
    def decode(depth=0):
        nonlocal pos, info_raw
        if depth > 40 or pos >= len(raw):
            raise ValueError('malformed torrent metadata')
        c = raw[pos:pos + 1]
        pos += 1
        if c == b'i':
            end = raw.index(b'e', pos)
            value = int(raw[pos:end]); pos = end + 1
            return value
        if c == b'd':
            result = {}
            while raw[pos:pos + 1] != b'e':
                key = decode(depth + 1)
                if not isinstance(key, bytes) or key in result:
                    raise ValueError('invalid or duplicate torrent dictionary key')
                start = pos
                result[key] = decode(depth + 1)
                if depth == 0 and key == b'info':
                    info_raw = raw[start:pos]
            pos += 1
            return result
        if c == b'l':
            result = []
            while raw[pos:pos + 1] != b'e':
                result.append(decode(depth + 1))
            pos += 1
            return result
        pos -= 1
        end = raw.index(b':', pos)
        length = int(raw[pos:end]); pos = end + 1
        if length < 0 or pos + length > len(raw):
            raise ValueError('invalid torrent string length')
        value = raw[pos:pos + length]; pos += length
        return value
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('torrent metadata too large')
    data = decode()
    if pos != len(raw):
        raise ValueError('trailing torrent data')
    info = data[b'info']
    def component(value):
        name = value.decode('utf-8')
        if not name or name in {'.', '..'} or '/' in name or '\\' in name or '\x00' in name:
            raise ValueError('unsafe torrent path')
        return name
    name = component(info[b'name'])
    files = {}
    for row in info.get(b'files', [{b'length': info.get(b'length'), b'path': []}]):
        path = '/'.join([name] + [component(c) for c in row[b'path']])
        size = row[b'length']
        if not isinstance(size, int) or size < 0 or path in files:
            raise ValueError('invalid torrent file size or duplicate path')
        files[path] = size
    if not info_raw or not files:
        raise ValueError('torrent lacks info or files')
    result = {'infohash': hashlib.sha1(info_raw).hexdigest(), 'files': files,
              'size_bytes': sum(files.values())}
    if include_pieces:
        length, pieces = info.get(b'piece length'), info.get(b'pieces')
        if type(length) is not int or length <= 0 or not isinstance(pieces, bytes):
            raise ValueError('v1 piece length and hashes are required')
        if len(pieces) != 20 * math.ceil(result['size_bytes'] / length):
            raise ValueError('piece hash count differs from torrent size')
        result.update(piece_length=length, pieces=pieces)
    return result


def torrent_inventory(raw):
    return torrent_metadata(raw)['files']


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def validate_manifest(m):
    if not isinstance(m, dict):
        raise ValueError('manifest must be an object')
    required = ['title', 'year', 'source_page', 'url', 'filename', 'size_bytes',
                'min_width', 'min_height', 'min_duration_seconds', 'max_duration_seconds']
    if any(k not in m for k in required):
        raise ValueError('manifest lacks required identity, source, size or validation fields')
    parsed = urlsplit(m['url'])
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('only HTTPS downloads without URL credentials are supported')
    if parsed.query or parsed.fragment:
        raise ValueError('pilot does not persist signed/query URLs; supply a public direct URL')
    name = m['filename']
    if not isinstance(name, str) or Path(name).name != name or name in {'.', '..'}:
        raise ValueError('filename must be a plain filename')
    if Path(name).suffix.lower() not in VIDEO | {'.zip'}:
        raise ValueError('unsupported payload type')
    for key in ['size_bytes', 'min_width', 'min_height', 'min_duration_seconds', 'max_duration_seconds']:
        value = m[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('invalid positive number: ' + key)
    if int(m['size_bytes']) != m['size_bytes'] or m['min_duration_seconds'] > m['max_duration_seconds']:
        raise ValueError('invalid size or duration interval')
    if Path(name).suffix.lower() == '.zip' and (
            not isinstance(m.get('unpacked_limit_bytes'), int) or m['unpacked_limit_bytes'] <= 0):
        raise ValueError('ZIP requires an explicit uncompressed size budget')
    if m.get('transport', 'https') not in {'https', 'bittorrent'}:
        raise ValueError('unsupported transport')
    if m.get('transport') == 'bittorrent':
        if not all(isinstance(m.get(k), str) and m[k] for k in
                   ('torrent_path', 'torrent_sha256', 'payload_relative_path')):
            raise ValueError('local torrent metadata, SHA256 and media path are required')
        for key in ('select_payload_only', 'enable_dht'):
            if key in m and type(m[key]) is not bool:
                raise ValueError(key + ' must be a boolean')


def torrent_plan(m, raw):
    files = torrent_inventory(raw)
    target = m['payload_relative_path']
    if sum(files.values()) != m['size_bytes'] or target not in files:
        raise ValueError('torrent inventory differs from manifest')
    if Path(target).suffix.lower() not in VIDEO:
        raise ValueError('torrent target is not a supported video')
    selected = list(files).index(target) + 1 if m.get('select_payload_only') else None
    if not selected and any(Path(f).suffix.lower() not in VIDEO | {'.srt', '.jpg', '.png', '.nfo', '.txt'} for f in files):
        raise ValueError('torrent contains unsupported extra files; explicit payload-only selection required')
    return files, selected


def capacity(free, total, payload, unpacked, reserve_gib, reserve_fraction, other_reserved):
    reserve = max(math.ceil(reserve_gib * GIB), math.ceil(total * reserve_fraction))
    required = reserve + other_reserved + math.ceil(payload * 1.1) + unpacked
    return {'ok': free >= required, 'free_bytes': free, 'reserve_bytes': reserve,
            'other_reserved_bytes': other_reserved, 'required_bytes': required}


def pending_commitments(root, current_job):
    """Reserve remaining work from this pilot's paused jobs, excluding the current retry."""
    reserved = 0
    for path in root.glob('*/state.json'):
        if path.parent == current_job:
            continue
        state = json.loads(path.read_text())
        if state.get('status') in {'verified', 'verified_external', 'capacity_shortfall', 'cancelled'}:
            continue
        commitment = state.get('commitment')
        if not isinstance(commitment, dict):
            raise RuntimeError('pending job lacks a storage commitment; reconcile: ' + str(path))
        payload = commitment.get('payload_bytes')
        unpacked = commitment.get('unpacked_bytes')
        complete = state.get('completed_bytes', 0)
        if any(type(v) is not int or v < 0 for v in (payload, unpacked, complete)) or complete > payload:
            raise RuntimeError('invalid pending storage commitment: ' + str(path))
        # Free space already reflects bytes on disk. Keep archive extraction conservative.
        reserved += math.ceil((payload - complete) * 1.1) + unpacked
    return reserved


def cancel_pending_job(job, reason):
    """Release future reservation for a stopped, abandoned candidate; retain all files."""
    job = Path(job).resolve(strict=True)
    if not reason.strip():
        raise ValueError('cancellation reason required')
    with (job.parent / '.pilot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = job / 'state.json'
        state = json.loads(path.read_text())
        if state.get('status') == 'cancelled':
            return state
        if state.get('status') != 'needs_attention' or state.get('backend') not in {None, 'aria2', 'Transmission'}:
            raise ValueError('only stopped pilot-owned pending jobs can be cancelled')
        state['cancelled_attempt'] = {k: state.get(k) for k in ('status', 'reason', 'completed_bytes')}
        state.update(status='cancelled', reason=reason, cancelled_unix=time.time(), speed_bytes_per_second=0)
        write_json(path, state)
        return state


def root_check(root, device=None):
    # Never walk to an existing ancestor: a missing external volume must fail.
    if not root.is_dir() or (device is not None and root.stat().st_dev != device):
        raise RuntimeError('destination missing or filesystem changed')
    return root.stat().st_dev


def completed_media_path(job, state):
    if state.get('status') == 'verified_external':
        path = Path(state['external_media_path'])
        if not path.is_absolute():
            raise ValueError('verified external media path must be absolute')
        return path
    path = job / state['media_filename']
    if not path.resolve().is_relative_to(job.resolve()):
        raise ValueError('verified media path escapes job directory')
    return path


def rpc(port, token, method, params=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}/jsonrpc',
        data=json.dumps({'jsonrpc': '2.0', 'id': 'pilot', 'method': 'aria2.' + method,
                         'params': ['token:' + token] + (params or [])}).encode(),
        headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=3) as response:
        result = json.load(response)
    if 'error' in result:
        raise RuntimeError('aria2 RPC error: ' + str(result['error']['code']))
    return result['result']


def inspect(path, m, ffprobe):
    result = subprocess.run([ffprobe, '-v', 'error', '-show_format', '-show_streams',
                             '-of', 'json', str(path)], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('ffprobe could not read the payload')
    data = json.loads(result.stdout)
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    duration = float(data.get('format', {}).get('duration', 0))
    if not videos or not (m['min_duration_seconds'] <= duration <= m['max_duration_seconds']):
        raise RuntimeError('video missing or duration outside expected bounds')
    v = videos[0]
    if v['width'] < m['min_width'] or v['height'] < m['min_height']:
        raise RuntimeError('resolution is below selected candidate')
    return {'duration_seconds': duration, 'video_codec': v['codec_name'],
            'width': v['width'], 'height': v['height'],
            'audio': [{'codec': s.get('codec_name'), 'language': s.get('tags', {}).get('language', 'und')}
                      for s in data['streams'] if s.get('codec_type') == 'audio'],
            'subtitles': [s.get('tags', {}).get('language', 'und')
                          for s in data['streams'] if s.get('codec_type') == 'subtitle'],
            'boundary': 'container/stream validation, not frame-by-frame or identity proof'}


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def extracted_matches(path, member):
    """Recover a completed extraction after a later failure without overwriting it."""
    if not path.is_file() or path.stat().st_size != member.file_size:
        return False
    crc = 0
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            crc = zlib.crc32(chunk, crc)
    return crc == member.CRC


def verify_torrent_file(raw, relative_path, local_file):
    meta = torrent_metadata(raw, include_pieces=True)
    if relative_path not in meta['files']:
        raise ValueError('selected path is absent from pinned torrent')
    offset = 0
    for name, size in meta['files'].items():
        if name == relative_path:
            break
        offset += size
    size = meta['files'][relative_path]
    if not local_file.is_file() or local_file.stat().st_size != size:
        raise ValueError('local file length differs from torrent')
    length = meta['piece_length']
    end = offset + size
    verified = verified_bytes = 0
    boundary = []
    count = len(meta['pieces']) // 20
    with local_file.open('rb') as f:
        for index in range(offset // length, min(count, (end + length - 1) // length)):
            start, stop = index * length, min((index + 1) * length, meta['size_bytes'])
            if start < offset or stop > end:
                boundary.append(index)
                continue
            f.seek(start - offset)
            remaining = stop - start
            digest = hashlib.sha1()
            while remaining:
                chunk = f.read(min(4 * 1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('short read in selected torrent file')
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.digest() != meta['pieces'][index * 20:(index + 1) * 20]:
                raise ValueError(f'torrent piece mismatch at index {index}; file not verified')
            verified += 1
            verified_bytes += stop - start
    if not verified:
        raise ValueError('no complete contained pieces; selected-file check is insufficient')
    return {'status': 'contained_pieces_verified', 'infohash': meta['infohash'],
            'path': str(local_file), 'file_bytes': size, 'verified_pieces': verified,
            'verified_bytes': verified_bytes, 'unverified_boundary_piece_indices': boundary,
            'unverified_boundary_bytes': size - verified_bytes,
            'boundary': 'Boundary pieces spanning omitted files cannot be verified by this file alone; no frame-by-frame claim.'}



def run(args):
    m = json.loads(args.manifest.read_text())
    validate_manifest(m)
    for v in (args.reserve_gib, args.reserve_fraction, args.other_reserved_gib, args.max_seconds, args.rate_kib):
        if not math.isfinite(v) or v < 0:
            raise ValueError('budgets and time/rate limits must be finite nonnegative numbers')
    if args.reserve_fraction > 1 or args.max_seconds <= 0:
        raise ValueError('invalid reserve fraction or execution timeout')
    root = args.root.expanduser().absolute()
    device = root_check(root)
    # This lock serializes all pilot jobs in this root, including retries.
    with (root / '.pilot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = json.dumps(m, sort_keys=True).encode()
        job = root / hashlib.sha256(identity).hexdigest()[:20]
        job.mkdir(exist_ok=True)
        state_path = job / 'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get('backend') == 'Transmission' and state.get('status') not in {'verified', 'verified_external'}:
            raise RuntimeError('existing Transmission task must resume via transmission_pilot.py; refusing a second backend')
        torrent = m.get('transport') == 'bittorrent'
        source = m['url']
        media_size = m['size_bytes']
        selected = None
        if torrent:
            meta = Path(m['torrent_path']).expanduser().resolve(strict=True)
            if sha256(meta) != m['torrent_sha256']:
                raise ValueError('torrent metadata hash mismatch')
            files, selected = torrent_plan(m, meta.read_bytes())
            media_size = files[m['payload_relative_path']]
            payload = job / m['payload_relative_path']
            source = str(meta)
        else:
            payload = job / m['filename']
        pending = pending_commitments(root, job)
        other = math.ceil(args.other_reserved_gib * GIB) + pending
        usage = shutil.disk_usage(root)
        budget = capacity(usage.free, usage.total, m['size_bytes'], m.get('unpacked_limit_bytes', 0),
                          args.reserve_gib, args.reserve_fraction, other)
        budget['pending_pilot_bytes'] = pending
        root_usage = shutil.disk_usage(Path.home())
        system_ok = root_usage.free >= 30 * GIB
        if args.check_only:
            print(json.dumps({'capacity': budget, 'system_disk_ok': system_ok, 'job': str(job)}, indent=2))
            return 0 if budget['ok'] and system_ok else 3
        aria = shutil.which(args.aria2)
        probe = shutil.which('ffprobe')
        if not aria or not probe:
            raise RuntimeError('aria2c and ffprobe must be installed')
        # Completed calls do not create another network transfer.
        if state.get('status') in {'verified', 'verified_external'}:
            final = completed_media_path(job, state)
            root_check(root, device)
            if not final.is_file() or sha256(final) != state['sha256']:
                raise RuntimeError('previously verified file is missing or changed')
            if torrent and not state.get('piece_verification'):
                state['piece_verification'] = verify_torrent_file(meta.read_bytes(), m['payload_relative_path'], final)
                write_json(state_path, state)
            state['verification'] = inspect(final, m, probe)
            print(json.dumps({'status': 'already_verified', 'path': str(final)}, ensure_ascii=False))
            return 0
        if not budget['ok'] or not system_ok:
            state.update(status='capacity_shortfall', capacity=budget, system_disk_ok=system_ok)
            write_json(state_path, state)
            print(json.dumps(state))
            return 3
        state.update(status='preflight_passed', title=m['title'], year=m['year'], capacity=budget,
                     attempt=int(state.get('attempt', 0)) + 1,
                     commitment={'payload_bytes': int(m['size_bytes']),
                                 'unpacked_bytes': m.get('unpacked_limit_bytes', 0)})
        state.pop('reason', None)
        write_json(state_path, state)
        process = None
        started = time.monotonic()
        try:
            if payload.exists() and not list(job.rglob('*.aria2')):
                if payload.stat().st_size != media_size:
                    raise RuntimeError('existing file has wrong size without resume metadata; refusing overwrite')
            else:
                # Check identity without accepting arbitrary changed remote objects.
                if not torrent:
                    request = urllib.request.Request(m['url'], method='HEAD')
                    context = ssl.create_default_context()
                    if not ssl.get_default_verify_paths().cafile and Path('/etc/ssl/cert.pem').is_file():
                        context.load_verify_locations('/etc/ssl/cert.pem')
                    with direct_opener(context).open(request, timeout=30) as response:
                        remote_size = int(response.headers.get('Content-Length', '0'))
                        etag = response.headers.get('ETag')
                    if remote_size != m['size_bytes']:
                        raise RuntimeError('remote size differs from manifest')
                    if state.get('etag') and etag != state['etag']:
                        raise RuntimeError('remote ETag changed; refusing mixed-version resume')
                    state['etag'] = etag
                    if payload.exists() and (not etag or etag.startswith('W/')):
                        raise RuntimeError('resume requires a strong ETag in this pilot')
                token = secrets.token_urlsafe(32)
                with socket.socket() as s:
                    s.bind(('127.0.0.1', 0))
                    port = s.getsockname()[1]
                config = job / 'aria2.conf'
                config.write_text(f'enable-rpc=true\nrpc-listen-all=false\nrpc-listen-port={port}\nrpc-secret={token}\n')
                config.chmod(0o600)
                command = [aria, '--conf-path=' + str(config), '--continue=true',
                    '--auto-file-renaming=false', '--allow-overwrite=false', '--file-allocation=none',
                    '--max-connection-per-server=4', '--split=4', '--max-tries=3', '--retry-wait=3',
                    '--connect-timeout=15', '--timeout=30', '--console-log-level=warn',
                    '--dir=' + str(job), '--seed-time=0', source]
                if torrent:
                    command.insert(-1, '--check-integrity=true')
                    command.insert(-1, '--enable-dht=' + str(m.get('enable_dht', False)).lower())
                    command.insert(-1, '--enable-dht6=false')
                    command.insert(-1, '--bt-enable-lpd=false')
                    command.insert(-1, '--dht-file-path=' + str(job / 'dht.dat'))
                    if selected:
                        command.insert(-1, '--select-file=' + str(selected))
                        command.insert(-1, '--bt-remove-unselected-file=false')
                else:
                    command.insert(-1, '--out=' + m['filename'])
                if args.rate_kib:
                    command.insert(-1, f'--max-download-limit={int(args.rate_kib)}K')
                with (job / 'transport.log').open('a') as log:
                    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=direct_environment())
                    last_progress = time.monotonic()
                    last_bytes = 0
                    last_print = 0
                    failures = 0
                    while True:
                        root_check(root, device)
                        free = shutil.disk_usage(root).free
                        if free < budget['reserve_bytes'] + other or shutil.disk_usage(Path.home()).free < 30 * GIB:
                            raise RuntimeError('free space crossed reserve; transfer stopped')
                        if time.monotonic() - started > args.max_seconds:
                            raise TimeoutError('execution time limit reached; rerun resumes same job')
                        try:
                            tasks = rpc(port, token, 'tellActive') + rpc(port, token, 'tellStopped', [0, 10])
                            failures = 0
                        except Exception:
                            tasks = []
                            failures += 1
                            if failures >= 5 or process.poll() is not None:
                                raise RuntimeError('cannot reliably observe downloader state')
                        if tasks:
                            task = tasks[0]
                            total = int(task['totalLength'])
                            complete = int(task['completedLength'])
                            accepted_totals = {m['size_bytes'], media_size} if selected else {m['size_bytes']}
                            if total and total not in accepted_totals:
                                raise RuntimeError('downloader size differs from approved manifest')
                            if selected and task.get('files'):
                                chosen = [f for f in task['files'] if f.get('selected') == 'true']
                                if len(chosen) != 1 or chosen[0]['index'] != str(selected) or Path(chosen[0]['path']) != payload:
                                    raise RuntimeError('downloader selected files differ from pinned movie')
                            state.update(status='downloading', completed_bytes=complete, total_bytes=total,
                                         speed_bytes_per_second=int(task['downloadSpeed']),
                                         connections=int(task.get('connections', 0)),
                                         seeders=int(task.get('numSeeders', 0)))
                            write_json(state_path, state)
                            if complete > last_bytes:
                                last_progress = time.monotonic()
                                last_bytes = complete
                            if time.monotonic() - last_print > 15:
                                print(json.dumps({'status': state['status'], 'completed_bytes': complete,
                                                  'total_bytes': total, 'speed': state['speed_bytes_per_second']}), flush=True)
                                last_print = time.monotonic()
                            if task['status'] == 'complete':
                                break
                            if task['status'] in {'error', 'removed'}:
                                raise RuntimeError('downloader terminal status: ' + task['status'])
                        if time.monotonic() - last_progress > 180:
                            raise RuntimeError('no progress for 180 seconds; candidate needs review')
                        time.sleep(2)
            if payload.stat().st_size != media_size or list(job.rglob('*.aria2')):
                raise RuntimeError('payload size/control state is incomplete')
            if torrent:
                state['piece_verification'] = verify_torrent_file(meta.read_bytes(), m['payload_relative_path'], payload)
            media = payload
            if payload.suffix.lower() == '.zip':
                with zipfile.ZipFile(payload) as z:
                    items = [i for i in z.infolist() if not i.is_dir()]
                    if len(items) != 1 or Path(items[0].filename).name != items[0].filename:
                        raise RuntimeError('ZIP must contain exactly one plain media filename')
                    member = items[0]
                    if Path(member.filename).suffix.lower() not in VIDEO or member.file_size > m['unpacked_limit_bytes']:
                        raise RuntimeError('ZIP payload does not match extraction budget/type')
                    media = job / member.filename
                    if media.exists():
                        if not extracted_matches(media, member):
                            raise RuntimeError('existing extraction differs from archive; refusing overwrite')
                    else:
                        usage = shutil.disk_usage(root)
                        if usage.free < member.file_size + budget['reserve_bytes'] + other:
                            raise RuntimeError('insufficient space to extract')
                        partial = media.with_suffix(media.suffix + '.extracting')
                        # ZipExtFile verifies CRC at EOF; source archive is retained.
                        with z.open(member) as src, partial.open('xb') as dst:
                            while chunk := src.read(4 * 1024 * 1024):
                                root_check(root, device)
                                if shutil.disk_usage(root).free < budget['reserve_bytes'] + other:
                                    raise RuntimeError('space reserve crossed while extracting')
                                dst.write(chunk)
                        partial.replace(media)
            result = inspect(media, m, probe)
            state.update(status='verified', media_filename=str(media.relative_to(job)), sha256=sha256(media),
                         verification=result, elapsed_seconds=round(time.monotonic() - started, 2))
            write_json(state_path, state)
            print(json.dumps({'status': 'verified', 'path': str(media), 'verification': result}, ensure_ascii=False))
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            state.update(status='needs_attention', reason=str(exc) or 'interrupted')
            write_json(state_path, state)
            print(json.dumps({'status': state['status'], 'reason': state['reason']}, ensure_ascii=False))
            return 4
        finally:
            if process and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            config = job / 'aria2.conf'
            if config.exists():
                config.unlink()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True, help='must already exist')
    p.add_argument('--aria2', default='aria2c')
    p.add_argument('--reserve-gib', type=float, default=50)
    p.add_argument('--reserve-fraction', type=float, default=0.10)
    p.add_argument('--other-reserved-gib', type=float, default=0,
                   help='external download commitments; same-root pilot jobs are counted automatically')
    p.add_argument('--max-seconds', type=float, default=1800)
    p.add_argument('--rate-kib', type=float, default=0, help='optional download cap for reproducible resume tests')
    p.add_argument('--check-only', action='store_true')
    args = p.parse_args()
    try:
        raise SystemExit(run(args))
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
