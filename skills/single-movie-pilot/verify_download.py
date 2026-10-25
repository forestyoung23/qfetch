#!/usr/bin/env python3
"""Verify a completed external video without assuming a pinned BT torrent."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

from acquire import inspect, sha256, write_json


def validate(m):
    if not isinstance(m, dict):
        raise ValueError('manifest must be an object')
    if not re.fullmatch(r'imdb:tt[0-9]+', m.get('movie_id', '')):
        raise ValueError('explicit IMDb movie_id required')
    if not isinstance(m.get('title'), str) or not m['title'].strip() or re.search(r'[\x00-\x1f<>:"/\\|?*]', m['title']):
        raise ValueError('safe movie title required')
    if type(m.get('year')) is not int or not 1000 <= m['year'] <= 9999:
        raise ValueError('four digit year required')
    if type(m.get('size_bytes')) is not int or m['size_bytes'] <= 0:
        raise ValueError('known positive file size required')
    filename = m.get('filename', '')
    if not isinstance(filename, str) or not filename or re.search(r'[\x00-\x1f\\]', filename) or Path(filename).name != filename or Path(filename).suffix.lower() not in {'.mkv', '.mp4', '.mov', '.webm', '.m4v', '.avi'}:
        raise ValueError('final video basename required')
    for key in ('min_width', 'min_height', 'min_duration_seconds', 'max_duration_seconds'):
        if type(m.get(key)) not in {int, float} or not 0 < m[key] < 10**8:
            raise ValueError('positive finite media threshold required: ' + key)
    if m['min_duration_seconds'] > m['max_duration_seconds']:
        raise ValueError('duration bounds inverted')


def verify(m, media, library, ffprobe, ffmpeg):
    validate(m)
    media = media.expanduser().absolute()
    if media.name != m['filename'] or media.name.endswith('.qkdownloading'):
        raise ValueError('only the expected final filename may be verified')
    if media.is_symlink():
        raise ValueError('source video must not be a symbolic link')
    before = media.stat()
    if not media.is_file() or before.st_size != m['size_bytes']:
        raise ValueError('file size differs from selected cloud file')
    library = library.expanduser().resolve(strict=True)
    if not library.is_dir() or library.stat().st_dev != before.st_dev:
        raise ValueError('existing library on the same filesystem required for hard links')
    details = inspect(media, m, ffprobe)
    if not details['audio']:
        raise ValueError('movie has no audio stream')
    samples = []
    for second in (0, details['duration_seconds'] / 2, max(0, details['duration_seconds'] - 20)):
        r = subprocess.run([ffmpeg, '-v', 'error', '-xerror', '-ss', str(second), '-i', str(media),
                            '-map', '0:v:0', '-an', '-t', '1', '-progress', 'pipe:1',
                            '-nostats', '-f', 'null', '-'], capture_output=True, text=True, timeout=60)
        frames = max([int(v) for v in re.findall(r'^frame=([0-9]+)$', r.stdout, re.M)] or [0])
        if r.returncode or frames < 1 or r.stderr.strip():
            raise ValueError('sample decode failed at ' + str(second))
        samples.append({'second': round(second, 3), 'decoded_frames': frames})
    digest = sha256(media)
    after = media.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('file changed during verification')
    imdb = m['movie_id'].split(':')[1]
    base = f"{m['title']} ({m['year']}) [imdbid-{imdb}]"
    folder = library / base
    if folder.is_symlink():
        raise ValueError('library movie folder must not be a symbolic link')
    folder.mkdir(exist_ok=True)
    if folder.resolve().parent != library:
        raise ValueError('library movie folder escapes configured library')
    target = folder / (base + media.suffix.lower())
    nfo = ET.Element('movie')
    for tag, value in [('title', m['title']), ('year', str(m['year'])), ('imdbid', imdb)]:
        ET.SubElement(nfo, tag).text = value
    ET.SubElement(nfo, 'uniqueid', {'type': 'imdb', 'default': 'true'}).text = imdb
    nfo_bytes = ET.tostring(nfo, encoding='utf-8', xml_declaration=True)
    nfo_path = folder / 'movie.nfo'
    if nfo_path.is_symlink() or target.is_symlink():
        raise ValueError('library entries must not be symbolic links')
    if nfo_path.exists() and nfo_path.read_bytes() != nfo_bytes:
        raise FileExistsError('existing library metadata differs; refusing overwrite')
    if target.exists():
        if not target.samefile(media):
            raise FileExistsError('existing library video differs; refusing overwrite')
    else:
        os.link(media, target)
    if not nfo_path.exists():
        with nfo_path.open('xb') as f:
            f.write(nfo_bytes)
    return {'status': 'verified_external', 'source_path': str(media), 'path': str(target),
            'nfo_path': str(nfo_path), 'sha256': digest, 'verification': details,
            'decode_samples': samples, 'hard_link': target.samefile(media),
            'extra_video_bytes_copied': 0,
            'boundary': 'Size against selected file, stream properties and three decode samples verified. '
                        'SHA256 is a local record, not publisher authentication. '
                        'No BT piece verification or Jellyfin playback/import claim.'}


def main(args):
    m = json.loads(args.manifest.read_text())
    validate(m)
    if not 0 <= args.wait_seconds <= 14400:
        raise ValueError('wait_seconds must be 0..14400')
    probe, ffmpeg = shutil.which(args.ffprobe), shutil.which(args.ffmpeg)
    if not probe or not ffmpeg:
        raise ValueError('ffprobe and ffmpeg required')
    if args.file.name != m['filename']:
        raise ValueError('only the expected final video may be awaited')
    deadline = time.monotonic() + args.wait_seconds
    while not args.file.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError('final video not available; partial files preserved')
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    # Hold only our existing pilot root lock during verification/delivery, not during transfer.
    if args.job:
        with (args.job.parent / '.pilot.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state_path = args.job / 'state.json'
            state = json.loads(state_path.read_text())
            if state.get('backend') != 'QuarkClient' or state.get('movie_id') != m['movie_id']:
                raise ValueError('job does not match this QuarkClient movie')
            if state.get('manifest_sha256'):
                digest = hashlib.sha256(json.dumps(m, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if (digest != state['manifest_sha256'] or state.get('expected_filename') != args.file.name
                        or Path(state['output_directory']).resolve() != args.file.parent.resolve()
                        or state.get('total_bytes') != m['size_bytes']
                        or Path(state['library_root']).resolve() != args.library_root.resolve()):
                    raise ValueError('verification differs from frozen job manifest or paths')
                if state.get('status') in {'cancelled', 'capacity_shortfall'}:
                    raise ValueError('cancelled job cannot be verified')
            result = verify(m, args.file, args.library_root, probe, ffmpeg)
            write_json(args.output, result)
            state.update(status='verified_external', completed_bytes=m['size_bytes'],
                         total_bytes=m['size_bytes'], external_media_path=result['path'],
                         sha256=result['sha256'], verification=result['verification'],
                         speed_bytes_per_second=0, last_observed_unix=time.time(),
                         commitment={'payload_bytes': m['size_bytes'], 'unpacked_bytes': 0})
            state.pop('reason', None)
            write_json(state_path, state)
    else:
        result = verify(m, args.file, args.library_root, probe, ffmpeg)
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--file', type=Path, required=True)
    p.add_argument('--library-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--job', type=Path)
    p.add_argument('--wait-seconds', type=int, default=0)
    p.add_argument('--ffprobe', default='ffprobe')
    p.add_argument('--ffmpeg', default='ffmpeg')
    a = p.parse_args()
    try:
        main(a)
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, subprocess.TimeoutExpired) as error:
        write_json(a.output, {'status': 'not_verified', 'reason': str(error)})
        print(json.dumps({'status': 'not_verified', 'reason': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
