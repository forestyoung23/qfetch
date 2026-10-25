#!/usr/bin/env python3
"""Read an existing job state without contacting or modifying its downloader."""
import argparse
import datetime
import json
from pathlib import Path
import shutil
import time


def report(job):
    job = job.expanduser().resolve(strict=True)
    state_file = job / 'state.json'
    state = json.loads(state_file.read_text())
    completed = state.get('completed_bytes', 0)
    total = state.get('total_bytes', 0)
    speed = state.get('speed_bytes_per_second', 0)
    client_pending = state.get('backend') == 'QuarkClient' and state.get('status') not in {'verified', 'verified_external'}
    allocated = state.get('allocated_bytes')
    if client_pending and state.get('partial_path'):
        try:
            allocated = Path(state['partial_path']).stat().st_blocks * 512
        except FileNotFoundError:
            allocated = None
    budget = state.get('capacity', {})
    usage = shutil.disk_usage(job)
    reserved = budget.get('reserve_bytes', 0) + budget.get('other_reserved_bytes', 0)
    return {
        'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'job': str(job),
        'title': state.get('title'),
        'backend': state.get('backend', 'aria2'),
        'http_proxy_url': state.get('http_proxy_url'),
        'proxy_scope': state.get('proxy_scope'),
        'recorded_status': state.get('status'),
        'state_age_seconds': round(max(0, time.time() - state_file.stat().st_mtime), 1),
        'completed_bytes': None if client_pending else completed,
        'total_bytes': total,
        'percent': state.get('ui_percent') if client_pending else round(completed * 100 / total, 2) if total > 0 else None,
        'speed_bytes_per_second': None if client_pending else speed,
        'client_recorded_speed': state.get('ui_speed') if client_pending else None,
        'allocated_bytes_now': allocated if client_pending else None,
        'progress_source': 'Last observed client UI; allocated blocks are not completed bytes. '
                           'Ledger completed_bytes=0 conservatively reserves future space.'
                           if client_pending else 'Stored downloader byte counters',
        'eta_seconds_at_recorded_speed': round(max(0, total - completed) / speed)
            if not client_pending and total > completed and speed > 0 else None,
        'connections': state.get('connections'),
        'seeders': state.get('seeders') if state.get('backend') != 'Transmission' else None,
        'tracker_errors': state.get('tracker_errors', []),
        'free_bytes_now': usage.free,
        'headroom_above_recorded_reserves_bytes': usage.free - reserved if budget else None,
        'reason': state.get('reason'),
        'boundary': 'Read-only snapshot. Stored status/speed do not prove process liveness. '
                    'Reservations are from this attempt; external tasks may have changed. '
                    'Completion requires separate verification.',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(report(args.job), ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({'status': 'observation_failed', 'reason': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
