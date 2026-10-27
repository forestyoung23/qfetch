#!/usr/bin/env python3
"""Select one verified video candidate locally; never search, transfer or download."""
import argparse
import json
from pathlib import Path

LIMIT = 10_000_000_000
RESOLUTIONS = (1080, 1440, 2160)


def tie_key(candidate):
    # Unknown size is not evidence that a file is smaller.
    size = candidate['size_bytes']
    return (-candidate['source_confidence'], -candidate['compatibility_confidence'],
            size if size is not None else float('inf'))


def select_resource(candidates, search_complete=False):
    """Apply the default video policy to Agent-verified, explicitly deduplicated data."""
    if not isinstance(candidates, list) or type(search_complete) is not bool:
        raise ValueError('candidates must be a list and search_complete must be boolean')
    by_id = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError('each candidate must be an object')
        for field in ('id', 'filename', 'source_page'):
            if not isinstance(candidate.get(field), str) or not candidate[field].strip():
                raise ValueError(f'{field} must be a nonempty string')
        if candidate['id'] in by_id:
            raise ValueError('candidate IDs must be unique')
        by_id[candidate['id']] = candidate
        if type(candidate.get('requirements_met')) is not bool:
            raise ValueError('requirements_met must be boolean')
        resolution = candidate.get('resolution')
        if resolution is not None and (type(resolution) is not int or resolution <= 0):
            raise ValueError('resolution must be positive integer pixels or null')
        kind, size = candidate.get('size_kind'), candidate.get('size_bytes')
        if kind not in ('exact', 'upper_bound', 'unknown'):
            raise ValueError('size_kind must be exact, upper_bound or unknown')
        if kind == 'unknown':
            if size is not None:
                raise ValueError('unknown size must use null size_bytes')
        elif type(size) is not int or size <= 0:
            raise ValueError('known size must be positive integer bytes')
        for field in ('source_confidence', 'compatibility_confidence'):
            if type(candidate.get(field)) is not int or candidate[field] not in (0, 1, 2):
                raise ValueError(f'{field} must be 0, 1 or 2')
        if 'duplicate_of' in candidate and not isinstance(candidate['duplicate_of'], str):
            raise ValueError('duplicate_of must reference a canonical candidate ID')

    excluded = []
    eligible = []
    for candidate in candidates:
        duplicate = candidate.get('duplicate_of')
        if duplicate is not None:
            root = by_id.get(duplicate)
            if root is None or root is candidate or 'duplicate_of' in root:
                raise ValueError('duplicate_of must reference another nonduplicate candidate')
            reason = 'confirmed_duplicate'
        elif not candidate['requirements_met']:
            reason = 'requirements_not_verified'
        elif candidate.get('resolution') not in RESOLUTIONS:
            reason = 'resolution_not_eligible'
        else:
            eligible.append(candidate)
            continue
        excluded.append({'id': candidate['id'], 'reason': reason})

    small = [c for c in eligible if c['size_bytes'] is not None and c['size_bytes'] <= LIMIT]
    preferred = [c for c in small if c['resolution'] == 2160]
    base = {'eligible_count': len(eligible), 'excluded': excluded}
    if not search_complete and not preferred:
        return dict(base, status='continue_search', selected_id=None, reason='search_not_complete')
    if not eligible:
        return dict(base, status='no_match', selected_id=None, reason='no_eligible_candidate')
    if len(eligible) == 1:
        pool, reason = eligible, 'only_eligible_candidate'
    elif small:
        resolution = max(c['resolution'] for c in small)
        pool = [c for c in small if c['resolution'] == resolution]
        reason = 'highest_resolution_within_10gb'
    else:
        resolution = min(c['resolution'] for c in eligible)
        pool = [c for c in eligible if c['resolution'] == resolution]
        reason = ('lowest_resolution_with_unknown_sizes' if any(c['size_kind'] != 'exact' for c in eligible)
                  else 'lowest_resolution_above_10gb')
    selected = min(pool, key=tie_key)
    return dict(base, status='selected', selected_id=selected['id'], reason=reason,
                size_exact=selected['size_kind'] == 'exact',
                unknown_size_ids=[c['id'] for c in eligible if c['size_kind'] == 'unknown'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='JSON with candidates and search_complete')
    args = parser.parse_args()
    try:
        data = json.loads(args.manifest.read_text(encoding='utf-8'))
        result = select_resource(data['candidates'], data['search_complete'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'needs_attention', 'reason': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
