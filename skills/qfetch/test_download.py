import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import download


class LocalChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.file = self.folder/'example.mkv'
        self.source = 'account:/movies/example.mkv'
        space = patch('download.shutil.disk_usage', return_value=SimpleNamespace(free=10**10))
        space.start()
        self.addCleanup(space.stop)

    def prepare(self):
        return download.prepare(self.folder, self.file.name, 10, self.source)

    def test_prepare_does_not_download_and_repeated_request_reuses_record(self):
        first = self.prepare()
        saved = (self.folder/download.TASK).read_bytes()
        again = self.prepare()
        self.assertEqual(first['status'], 'ready')
        self.assertFalse(first['existing_task'])
        self.assertTrue(again['existing_task'])
        self.assertEqual(again['status'], 'pending')
        self.assertEqual((self.folder/download.TASK).read_bytes(), saved)
        self.assertFalse(self.file.exists())

    def test_different_source_cannot_replace_same_name_and_size(self):
        self.prepare()
        self.source = 'another-account:/movies/example.mkv'
        with self.assertRaisesRegex(ValueError, 'another task'):
            self.prepare()

    def test_low_space_does_not_create_record(self):
        with patch('download.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, 'insufficient'):
                self.prepare()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_repeat_checks_space_without_changing_task(self):
        self.prepare()
        saved = (self.folder/download.TASK).read_bytes()
        with patch('download.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, 'insufficient'):
                self.prepare()
        self.assertEqual((self.folder/download.TASK).read_bytes(), saved)

    def test_missing_mount_directory_is_not_created(self):
        with self.assertRaises(FileNotFoundError):
            download.prepare(self.folder/'missing'/'downloads', 'movie.mkv', 10, self.source)
        self.assertFalse((self.folder/'missing').exists())

    def test_existing_file_requires_client_completion_even_when_preallocated(self):
        self.prepare()
        with self.file.open('wb') as handle:
            handle.truncate(10)
        self.assertEqual(download.status(self.folder)['status'], 'file_present')
        with self.assertRaisesRegex(ValueError, 'first observe'):
            download.check(self.folder)
        self.assertEqual(download.check(self.folder, True)['status'], 'complete')

    def test_existing_matching_file_is_preserved(self):
        self.file.write_bytes(b'0123456789')
        with patch('download.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            self.assertEqual(self.prepare()['status'], 'file_present')
        self.assertEqual(self.file.read_bytes(), b'0123456789')

    def test_wrong_size_is_preserved(self):
        self.file.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.prepare()
        self.assertEqual(self.file.read_bytes(), b'keep')
        self.assertFalse((self.folder/download.TASK).exists())

    def test_client_partial_is_observed_without_being_treated_as_complete(self):
        partial = self.folder/'example-99c462c6c4b2.mkv.qkdownloading'
        partial.write_bytes(b'0123456789')
        result = self.prepare()
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['temporary_files'], [{'name': partial.name, 'bytes': 10}])
        with self.assertRaisesRegex(ValueError, 'final file'):
            download.check(self.folder, True)
        self.file.write_bytes(b'0123456789')
        with self.assertRaisesRegex(ValueError, 'temporary files'):
            download.check(self.folder, True)

    def test_legacy_api_records_and_partials_are_preserved(self):
        record = self.folder/download.TASK
        record.write_text('{"fid":"old-id","file_name":"example.mkv","size":10}')
        with self.assertRaisesRegex(ValueError, 'unsupported task'):
            self.prepare()
        self.assertIn('old-id', record.read_text())
        record.unlink()
        partial = self.folder/'example.mkv.part'
        partial.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'web downloader'):
            self.prepare()
        self.assertFalse(record.exists())
        self.assertEqual(partial.read_bytes(), b'keep')

    def test_legacy_client_task_is_not_silently_migrated(self):
        (self.folder/'.quark-download.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'legacy task'):
            self.prepare()
        self.assertFalse((self.folder/download.TASK).exists())

    def test_invalid_names_sizes_and_source_are_rejected(self):
        for name in ('../bad', 'a/b', 'a\\b', '', '.', '..', download.TASK, 'a.part', 'a\n'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                download.prepare(self.folder, name, 10, self.source)
        for size in (0, -1, True, '10'):
            with self.assertRaises(ValueError):
                download.prepare(self.folder, self.file.name, size, self.source)
        with self.assertRaises(ValueError):
            download.prepare(self.folder, self.file.name, 10, ' ')
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_symlinked_record_target_or_temporary_file_are_not_followed(self):
        outside = self.folder/'outside'
        outside.write_bytes(b'keep')
        for path in (self.file, self.folder/download.TASK, self.folder/'a.qkdownloading'):
            path.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'symbolic|regular'):
                self.prepare()
            path.unlink()
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_unknown_size_can_complete_without_claiming_size_verification(self):
        result = download.prepare(self.folder, self.file.name, None, self.source)
        self.assertEqual(result['status'], 'ready')
        self.assertIsNone(result['expected_bytes'])
        self.assertIn('free_bytes', result)
        self.file.write_bytes(b'0123456789')
        with self.assertRaisesRegex(ValueError, 'first observe'):
            download.check(self.folder)
        completed = download.check(self.folder, True)
        self.assertEqual(completed['status'], 'complete')
        self.assertFalse(completed['size_verified'])
        self.assertEqual(completed['actual_bytes'], 10)

    def test_size_can_be_enriched_and_omission_preserves_known_size(self):
        download.prepare(self.folder, self.file.name, None, self.source)
        self.file.write_bytes(b'0123456789')
        self.assertTrue(self.prepare()['metadata_updated'])
        self.assertTrue(download.check(self.folder, True)['size_verified'])
        result = download.prepare(self.folder, self.file.name, None, self.source)
        self.assertEqual(result['expected_bytes'], 10)
        self.assertFalse(result['metadata_updated'])
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            download.prepare(self.folder, self.file.name, 11, self.source)

    def test_failed_size_enrichment_preserves_record(self):
        download.prepare(self.folder, self.file.name, None, self.source)
        record = self.folder/download.TASK
        saved = record.read_bytes()
        self.file.write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.prepare()
        self.assertEqual(record.read_bytes(), saved)
        self.file.unlink()
        with patch('download.os.replace', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(record.read_bytes(), saved)
        self.assertEqual(list(self.folder.iterdir()), [record])

    def test_known_size_completion_reports_verification(self):
        self.prepare()
        self.file.write_bytes(b'0123456789')
        self.assertTrue(download.check(self.folder, True)['size_verified'])

    def test_cli_can_prepare_without_exact_size(self):
        result = subprocess.run([sys.executable, download.__file__, 'prepare',
            '--directory', str(self.folder), '--filename', self.file.name,
            '--source', self.source], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout)['expected_bytes'])

    def test_cli_requires_explicit_client_observation_and_reports_json(self):
        self.prepare()
        self.file.write_bytes(b'0123456789')
        command = [sys.executable, download.__file__, 'check', '--directory', str(self.folder)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'needs_attention')
        result = subprocess.run(command + ['--client-completed'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
