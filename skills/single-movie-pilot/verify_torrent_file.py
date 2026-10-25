#!/usr/bin/env python3
"""Offline v1 torrent verification for a selected file, including declared boundary limits."""
import argparse
import hashlib
import json
from pathlib import Path

from acquire import write_json, verify_torrent_file as verify




if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--torrent', type=Path, required=True)
    p.add_argument('--torrent-sha256', required=True)
    p.add_argument('--relative-path', required=True)
    p.add_argument('--file', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    try:
        raw = a.torrent.read_bytes()
        if hashlib.sha256(raw).hexdigest() != a.torrent_sha256:
            raise ValueError('torrent differs from pinned SHA256')
        result = verify(raw, a.relative_path, a.file)
        write_json(a.output, result)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status': 'not_verified', 'reason': str(exc)}))
        raise SystemExit(2)
