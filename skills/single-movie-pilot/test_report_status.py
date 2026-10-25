import json
from pathlib import Path
import tempfile
import unittest

from report_status import report


class ClientProgressBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = Path(self.tmp.name)

    def run_state(self, state):
        (self.job / 'state.json').write_text(json.dumps(state))
        return report(self.job)

    def test_sparse_partial_and_reservation_do_not_claim_completion(self):
        partial = self.job / 'movie.qkdownloading'
        with partial.open('wb') as f:
            f.truncate(10_000_000)
        r = self.run_state(dict(backend='QuarkClient', status='downloading',
                                completed_bytes=0, total_bytes=10_000_000,
                                ui_percent=5.2, partial_path=str(partial)))
        self.assertIsNone(r['completed_bytes'])
        self.assertEqual(r['percent'], 5.2)
        self.assertLess(r['allocated_bytes_now'], 10_000_000)

    def test_verified_byte_result_overrides_stale_ui_percent(self):
        r = self.run_state(dict(backend='QuarkClient', status='verified_external',
                                completed_bytes=1000, total_bytes=1000, ui_percent=5.2))
        self.assertEqual(r['completed_bytes'], 1000)
        self.assertEqual(r['percent'], 100)

    def test_existing_byte_based_backend_keeps_its_progress(self):
        r = self.run_state(dict(backend='Transmission', status='needs_attention',
                                completed_bytes=250, total_bytes=1000))
        self.assertEqual(r['completed_bytes'], 250)
        self.assertEqual(r['percent'], 25)
