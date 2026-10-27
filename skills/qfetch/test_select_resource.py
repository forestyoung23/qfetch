import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import select_resource as selector


def candidate(identifier, resolution=2160, size=9_000_000_000, **changes):
    value = dict(id=identifier, filename=f'{identifier}.mkv',
                 source_page='https://example.org/movie', requirements_met=True,
                 resolution=resolution, size_bytes=size,
                 size_kind='unknown' if size is None else 'exact',
                 source_confidence=2, compatibility_confidence=2)
    value.update(changes)
    return value


class ResourceSelection(unittest.TestCase):
    def test_documented_selection_examples(self):
        examples = [
            ([(2160, 18)], 0),
            ([(2160, 9), (1440, 7), (1080, 4)], 0),
            ([(2160, 18), (1440, 8), (1080, 5)], 1),
            ([(2160, 18), (1440, 12), (1080, 11)], 2),
            ([(2160, 18), (1440, 12)], 1),
            ([(1080, 14)], 0),
        ]
        for specs, expected in examples:
            with self.subTest(specs=specs):
                values = [candidate(str(i), res, gb * 10**9) for i, (res, gb) in enumerate(specs)]
                self.assertEqual(selector.select_resource(values, True)['selected_id'], str(expected))

    def test_decimal_10gb_boundary_and_gib_conversion(self):
        # 10 GiB must not enter the <=10 GB pool.
        for size, expected in [(10_000_000_000, '4k'), (10_000_000_001, 'hd'),
                               (10 * 1024**3, 'hd')]:
            with self.subTest(size=size):
                values = [candidate('4k', size=size), candidate('hd', 1080, 5 * 10**9)]
                self.assertEqual(selector.select_resource(values, True)['selected_id'], expected)

    def test_first_large_or_lower_resolution_result_does_not_end_search(self):
        for value in [candidate('large', size=18 * 10**9), candidate('hd', 1080)]:
            self.assertEqual(selector.select_resource([value])['status'], 'continue_search')
        self.assertEqual(selector.select_resource([candidate('4k')])['status'], 'selected')

    def test_unknown_size_is_not_within_limit(self):
        values = [candidate('unknown', size=None), candidate('known', 1080, 5 * 10**9)]
        result = selector.select_resource(values, True)
        self.assertEqual(result['selected_id'], 'known')
        self.assertEqual(result['unknown_size_ids'], ['unknown'])
        values[1]['size_bytes'] = 11 * 10**9
        result = selector.select_resource(values, True)
        self.assertEqual(result['selected_id'], 'known')
        self.assertEqual(result['reason'], 'lowest_resolution_with_unknown_sizes')
        result = selector.select_resource([candidate('only', size=None)], True)
        self.assertEqual(result['selected_id'], 'only')
        self.assertFalse(result['size_exact'])

    def test_conservative_upper_bound_can_qualify_but_is_not_exact(self):
        result = selector.select_resource([candidate('bound', size_kind='upper_bound')])
        self.assertEqual(result['status'], 'selected')
        self.assertFalse(result['size_exact'])

    def test_large_upper_bound_does_not_prove_actual_size_exceeds_limit(self):
        values = [candidate('bound', size=18 * 10**9, size_kind='upper_bound'),
                  candidate('hd', 1080, 11 * 10**9)]
        result = selector.select_resource(values, True)
        self.assertEqual(result['selected_id'], 'hd')
        self.assertEqual(result['reason'], 'lowest_resolution_with_unknown_sizes')

    def test_confirmed_duplicate_is_not_another_version(self):
        values = [candidate('large', size=18 * 10**9),
                  candidate('copy', size=18 * 10**9, duplicate_of='large')]
        result = selector.select_resource(values, True)
        self.assertEqual(result['eligible_count'], 1)
        self.assertEqual(result['reason'], 'only_eligible_candidate')
        self.assertEqual(result['excluded'], [{'id': 'copy', 'reason': 'confirmed_duplicate'}])
        # Same name and size alone do not establish content identity.
        del values[1]['duplicate_of']
        self.assertEqual(selector.select_resource(values, True)['eligible_count'], 2)

    def test_hard_requirements_and_resolution_precede_ranking(self):
        values = [candidate('cam', requirements_met=False), candidate('low', 720),
                  candidate('unknown', resolution=None), candidate('hd', 1080)]
        result = selector.select_resource(values, True)
        self.assertEqual(result['selected_id'], 'hd')
        self.assertEqual(len(result['excluded']), 3)

    def test_ties_use_evidence_then_compatibility_then_size_then_input_order(self):
        pairs = [
            (candidate('a', source_confidence=1), candidate('b', size=10**10), 'b'),
            (candidate('a', compatibility_confidence=1), candidate('b', size=10**10), 'b'),
            (candidate('a'), candidate('b', size=8 * 10**9), 'b'),
            (candidate('a'), candidate('b'), 'a'),
            (candidate('a', size=None), candidate('b', size=18 * 10**9), 'b'),
        ]
        for first, second, expected in pairs:
            with self.subTest(expected=expected, first=first):
                self.assertEqual(selector.select_resource([first, second], True)['selected_id'], expected)

    def test_mkv_tiebreak_is_case_insensitive_and_after_size_and_quality(self):
        mp4 = candidate('mp4', filename='movie.mp4')
        mkv = candidate('mkv', filename='movie.MKV')
        for values in ([mp4, mkv], [mkv, mp4]):
            self.assertEqual(selector.select_resource(values, True)['selected_id'], 'mkv')
        for change in (dict(size_bytes=9_000_000_001), dict(source_confidence=1),
                       dict(compatibility_confidence=1), dict(resolution=1080)):
            self.assertEqual(selector.select_resource([mp4, dict(mkv, **change)], True)['selected_id'], 'mp4')

    def test_empty_results_distinguish_pending_from_exhausted(self):
        self.assertEqual(selector.select_resource([])['status'], 'continue_search')
        self.assertEqual(selector.select_resource([], True)['status'], 'no_match')

    def test_invalid_data_is_rejected_without_mutation(self):
        changes = [dict(size_bytes=True), dict(size_bytes=-1), dict(size_kind='estimated'),
                   dict(size_bytes=None), dict(size_kind='unknown'), dict(resolution=True),
                   dict(requirements_met='yes'), dict(source_confidence=True),
                   dict(compatibility_confidence=3), dict(id=''), dict(duplicate_of='missing')]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                selector.select_resource([candidate('a', **change)], True)
        with self.assertRaises(ValueError):
            selector.select_resource([candidate('a'), candidate('a')], True)
        values = [candidate('a')]
        before = json.dumps(values)
        selector.select_resource(values, True)
        self.assertEqual(json.dumps(values), before)

    def test_cli_returns_json_for_success_and_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / 'candidates.json'
            command = [sys.executable, selector.__file__, str(manifest)]
            for data, code, status in [
                (dict(candidates=[candidate('a')], search_complete=True), 0, 'selected'),
                (dict(candidates=[], search_complete=False), 0, 'continue_search'),
                (dict(candidates=[], search_complete=True), 0, 'no_match'),
                ({}, 2, 'needs_attention'),
                ([], 2, 'needs_attention'),
            ]:
                manifest.write_text(json.dumps(data))
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], status)
            manifest.write_text('invalid JSON')
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['status'], 'needs_attention')


if __name__ == '__main__':
    unittest.main()
