#!/usr/bin/env python3
"""Cancel a stopped pilot candidate's future storage commitment without deleting files."""
import argparse
import json
from pathlib import Path
from acquire import cancel_pending_job

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    try:
        state = cancel_pending_job(args.job, args.reason)
        print(json.dumps(dict(status=state['status'], files_preserved=True), ensure_ascii=False))
    except (OSError, ValueError) as error:
        print(json.dumps(dict(status='blocked', reason=str(error)))); raise SystemExit(3)
