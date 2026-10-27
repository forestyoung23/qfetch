#!/usr/bin/env python3
"""Local task/file checks for Agent-operated Quark client downloads; no network or UI control."""
import argparse
import json
import os
import tempfile
from pathlib import Path
import shutil

TASK = '.qfetch.json'
MARGIN = 64 * 1024 ** 2


def directory(path):
    folder = Path(path).expanduser().resolve(strict=True)
    if not folder.is_dir():
        raise ValueError('download directory must already exist')
    return folder


def validate(task):
    if task.get('version') != 1:
        raise ValueError('unsupported task record; preserve it and inspect the original download')
    name = task['filename']
    if (not isinstance(name, str) or not name or name in {'.', '..', TASK, '.quark-download.json'}
            or any(c in name for c in '/\\') or any(ord(c) < 32 for c in name)
            or name.endswith(('.part', '.tmp', '.qkdownloading'))):
        raise ValueError('expected final filename required')
    if task['size_bytes'] is not None and (type(task['size_bytes']) is not int or task['size_bytes'] <= 0):
        raise ValueError('file size must be positive exact bytes or omitted')
    if not isinstance(task['source'], str) or not task['source'].strip():
        raise ValueError('stable source identity required, such as account alias and cloud file path')


def read_task(folder):
    record = folder / TASK
    if record.is_symlink():
        raise ValueError('task record must not be a symbolic link')
    task = json.loads(record.read_text(encoding='utf-8'))
    validate(task)
    return task


def snapshot(folder, task):
    target = folder / task['filename']
    if target.is_symlink():
        raise ValueError('target must not be a symbolic link')
    actual = None
    if target.exists():
        if (not target.is_file() or target.stat().st_size <= 0
                or (task['size_bytes'] is not None and target.stat().st_size != task['size_bytes'])):
            raise ValueError('existing final file differs; preserve it and inspect the client task')
        actual = target.stat().st_size
    temporary = []
    for path in sorted(folder.iterdir()):
        if path.name.endswith(('.qkdownloading', '.part')):
            if path.is_symlink() or not path.is_file():
                raise ValueError('temporary download must be a regular file')
            temporary.append({'name': path.name, 'bytes': path.stat().st_size})
    return {'status': 'file_present' if actual is not None else 'pending',
            'path': str(target), 'expected_bytes': task['size_bytes'], 'actual_bytes': actual,
            'size_verified': actual is not None and task['size_bytes'] is not None,
            'temporary_files': temporary}


def prepare(path, filename, size, source):
    folder = directory(path)
    task = {'version': 1, 'filename': filename, 'size_bytes': size, 'source': source}
    validate(task)
    if (folder / '.quark-download.json').exists():
        raise ValueError('legacy task record preserved; inspect the original task first')
    if any(p.name.endswith('.part') for p in folder.iterdir()):
        raise ValueError('web downloader partial file preserved; this helper only follows client tasks')
    record = folder / TASK
    existing = record.exists() or record.is_symlink()
    updated = False
    if existing:
        previous = read_task(folder)
        if any(previous[key] != task[key] for key in ('version', 'filename', 'source')):
            raise ValueError('directory belongs to another task; preserve it and inspect')
        if previous['size_bytes'] is not None:
            if size is not None and size != previous['size_bytes']:
                raise ValueError('exact size conflicts with existing task; preserve it and inspect')
            task['size_bytes'] = size = previous['size_bytes']
        else:
            updated = size is not None
    result = snapshot(folder, task)
    free = shutil.disk_usage(folder).free
    if result['status'] == 'pending' and free < (size or 0) + MARGIN:
        raise ValueError('insufficient free space on target disk; require full file size plus 64MiB')
    if not existing:
        with record.open('x', encoding='utf-8') as handle:
            json.dump(task, handle, ensure_ascii=False)
    if updated:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder,
                                             prefix='.qfetch-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(task, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, record)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if result['status'] == 'pending' and not existing and not result['temporary_files']:
        result['status'] = 'ready'
    return dict(result, existing_task=existing, metadata_updated=updated, free_bytes=free)


def status(path):
    folder = directory(path)
    return snapshot(folder, read_task(folder))


def check(path, client_completed=False):
    if not client_completed:
        raise ValueError('first observe this exact task and destination as completed in the Quark client')
    result = status(path)
    if result['status'] != 'file_present' or result['temporary_files']:
        raise ValueError('final file is missing or temporary files remain; inspect the client task')
    return dict(result, status='complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'status', 'check'):
        command = commands.add_parser(name)
        command.add_argument('--directory', required=True)
        if name == 'prepare':
            command.add_argument('--filename', required=True)
            command.add_argument('--size', type=int, help='exact cloud file bytes; omit when unavailable')
            command.add_argument('--source', required=True, help='stable identity, without credentials or signed URLs')
        elif name == 'check':
            command.add_argument('--client-completed', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare(args.directory, args.filename, args.size, args.source)
        elif args.command == 'status':
            result = status(args.directory)
        else:
            result = check(args.directory, args.client_completed)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({'status': 'needs_attention', 'reason': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
