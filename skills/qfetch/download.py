#!/usr/bin/env python3
"""Local checks for an Agent-operated Quark download; never controls the client."""
import argparse
import json
from pathlib import Path
import shutil

TASK = '.quark-download.json'
GIB = 1024 ** 3


def directory(path):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError('download directory must already exist')
    return path


def validate(filename, size):
    if (not isinstance(filename, str) or not filename or filename in {'.', '..', TASK}
            or any(c in filename for c in '/\\') or any(ord(c) < 32 for c in filename)
            or filename.endswith(('.qkdownloading', '.part', '.tmp'))):
        raise ValueError('expected final filename required')
    if type(size) is not int or size <= 0:
        raise ValueError('known positive file size in bytes required')


def read_task(folder):
    record = folder / TASK
    if record.is_symlink():
        raise ValueError('task record must not be a symbolic link')
    task = json.loads(record.read_text(encoding='utf-8'))
    validate(task['filename'], task['size_bytes'])
    return task


def snapshot(folder, task):
    target = folder / task['filename']
    if target.is_symlink():
        raise ValueError('target must not be a symbolic link')
    actual = None
    if target.exists():
        if not target.is_file():
            raise ValueError('target exists and is not a regular file')
        actual = target.stat().st_size
        if actual != task['size_bytes']:
            raise ValueError('existing file size differs; preserve it and inspect the client task')
    return {'status': 'file_present' if actual is not None else 'pending',
            'path': str(target), 'size_bytes': task['size_bytes'], 'actual_bytes': actual}


def check_space(folder, size):
    # One conservative rule; the client's temporary-file location is not assumed.
    required = 2 * size + 5 * GIB
    disks = [folder]
    if folder.stat().st_dev != Path.home().stat().st_dev:
        disks.append(Path.home())
    for disk in disks:
        if shutil.disk_usage(disk).free < required:
            raise ValueError(f'insufficient space on {disk}: require {required} free bytes')


def prepare(path, filename, size):
    folder = directory(path)
    validate(filename, size)
    task = {'filename': filename, 'size_bytes': size}
    record = folder / TASK
    if record.exists() or record.is_symlink():
        if read_task(folder) != task:
            raise ValueError('directory belongs to another task; use a separate download directory')
        result = snapshot(folder, task)
        if result['status'] == 'pending':
            check_space(folder, size)
        return dict(result, existing_task=True)
    result = snapshot(folder, task)
    if result['status'] == 'pending':
        check_space(folder, size)
    # Exclusive creation prevents two callers from replacing each other's selection.
    with record.open('x', encoding='utf-8') as handle:
        json.dump(task, handle, ensure_ascii=False)
    return dict(result, status='ready' if result['status'] == 'pending' else 'file_present',
                existing_task=False)


def status(path):
    folder = directory(path)
    return snapshot(folder, read_task(folder))


def check(path, client_completed=False):
    if not client_completed:
        raise ValueError('first confirm this task is completed in the Quark client')
    result = status(path)
    if result['status'] != 'file_present':
        raise ValueError('final file is missing; download is not complete')
    return dict(result, status='complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'status', 'check'):
        command = commands.add_parser(name)
        command.add_argument('--directory', type=Path, required=True)
        if name == 'prepare':
            command.add_argument('--filename', required=True)
            command.add_argument('--size', type=int, required=True, help='exact cloud file size in bytes')
        elif name == 'check':
            command.add_argument('--client-completed', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare(args.directory, args.filename, args.size)
        elif args.command == 'status':
            result = status(args.directory)
        else:
            result = check(args.directory, args.client_completed)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'needs_attention', 'reason': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
