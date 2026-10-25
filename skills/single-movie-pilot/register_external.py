#!/usr/bin/env python3
"""Verify an externally completed movie, create a same-volume library link, and register the result."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

from acquire import inspect, root_check, sha256, torrent_plan, validate_manifest, verify_torrent_file, write_json


def register(args):
    m = json.loads(args.manifest.read_text())
    validate_manifest(m)
    if m.get('transport') != 'bittorrent':
        raise ValueError('external registration currently requires a pinned torrent')
    match = re.fullmatch(r'imdb:(tt[0-9]+)', m.get('movie_id', ''))
    if not match:
        raise ValueError('an explicit IMDb movie_id is required')
    if not isinstance(m['title'], str) or not m['title'].strip() or re.search(r'[<>:"/\\|?*]', m['title']):
        raise ValueError('movie title cannot be used as a safe library folder name')
    if type(m['year']) is not int or not 1000 <= m['year'] <= 9999:
        raise ValueError('movie year must be a four-digit integer')
    root = args.root.absolute()
    device = root_check(root)
    library = args.library_root.absolute()
    root_check(library)
    media = args.file.absolute()
    before = media.stat()
    if media.stat().st_dev != library.stat().st_dev:
        raise ValueError('hard-link delivery requires source and library on the same filesystem')
    with (root / '.pilot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        key = hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()[:20]
        job = root / key
        if not job.is_dir():
            raise ValueError('expected original pilot job directory is absent')
        state_file = job / 'state.json'
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        raw = Path(m['torrent_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != m['torrent_sha256']:
            raise ValueError('torrent differs from pinned SHA256')
        torrent_plan(m, raw)
        piece_result = verify_torrent_file(raw, m['payload_relative_path'], media)
        probe, ffmpeg = shutil.which('ffprobe'), shutil.which('ffmpeg')
        if not probe or not ffmpeg:
            raise ValueError('ffprobe and ffmpeg are required')
        result = inspect(media, m, probe)
        samples = []
        for second in (0, result['duration_seconds'] / 2, max(0, result['duration_seconds'] - 20)):
            command = [ffmpeg, '-v', 'error', '-xerror', '-ss', str(second), '-i', str(media),
                       '-map', '0:v:0', '-an', '-t', '1', '-progress', 'pipe:1', '-nostats', '-f', 'null', '-']
            run = subprocess.run(command, capture_output=True, text=True, timeout=60)
            frames = max([int(v) for v in re.findall(r'^frame=([0-9]+)$', run.stdout, re.M)] or [0])
            if run.returncode or frames < 1 or run.stderr.strip():
                raise ValueError('decode sample failed or yielded no frame at ' + str(second))
            samples.append({'second': round(second, 3), 'decoded_frames': frames, 'exit_code': run.returncode})
        digest = sha256(media)
        after = media.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('source changed during verification; wait for completion')
        root_check(root, device)
        base = f"{m['title']} ({m['year']}) [imdbid-{match[1]}]"
        folder = library / base
        folder.mkdir(exist_ok=True)
        target = folder / (base + media.suffix.lower())
        if target.exists():
            if not target.samefile(media):
                raise FileExistsError('library movie already exists; refusing overwrite')
        else:
            os.link(media, target)
        nfo = ET.Element('movie')
        for tag, value in [('title', m['title']), ('year', str(m['year'])), ('imdbid', match[1])]:
            ET.SubElement(nfo, tag).text = value
        ET.SubElement(nfo, 'uniqueid', {'type': 'imdb', 'default': 'true'}).text = match[1]
        nfo_bytes = ET.tostring(nfo, encoding='utf-8', xml_declaration=True)
        nfo_path = folder / 'movie.nfo'
        if nfo_path.exists() and nfo_path.read_bytes() != nfo_bytes:
            raise FileExistsError('library NFO already differs; refusing overwrite')
        if not nfo_path.exists():
            nfo_path.write_bytes(nfo_bytes)
        if 'previous_transport_attempt' not in state:
            state['previous_transport_attempt'] = {k: state[k] for k in
                ('status', 'reason', 'attempt', 'completed_bytes', 'total_bytes') if k in state}
        state.update(status='verified_external', title=m['title'], year=m['year'], movie_id=m['movie_id'],
                     backend=args.backend, external_media_path=str(target), sha256=digest,
                     verification=result, piece_verification=piece_result, decode_samples=samples,
                     completed_bytes=after.st_size, total_bytes=after.st_size,
                     speed_bytes_per_second=0)
        state.pop('connections', None)
        state.pop('seeders', None)
        state.pop('reason', None)
        write_json(state_file, state)
        report = {'status': 'verified_external', 'backend': args.backend, 'path': str(target),
                  'source_path': str(media), 'sha256': digest, 'verification': result,
                  'piece_verification': piece_result, 'decode_samples': samples,
                  'hard_link': target.samefile(media), 'extra_video_bytes_copied': 0,
                  'nfo_path': str(nfo_path), 'pilot_job': str(job)}
        write_json(args.output, report)
        return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--file', type=Path, required=True)
    p.add_argument('--library-root', type=Path, required=True)
    p.add_argument('--backend', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    try:
        print(json.dumps(register(a), ensure_ascii=False))
    except (ValueError, OSError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'status': 'not_registered', 'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
