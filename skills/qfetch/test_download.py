import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import download


class DownloadChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.file = self.folder / 'example.mp4'
        self.space = patch('download.shutil.disk_usage', return_value=SimpleNamespace(free=100*download.GIB))
        self.space.start()
        self.addCleanup(self.space.stop)

    def prepare(self):
        return download.prepare(self.folder, self.file.name, 10)

    def test_prepare_only_records_selection_and_repeat_reuses_it(self):
        first = self.prepare()
        record = (self.folder/download.TASK).read_bytes()
        again = self.prepare()
        self.assertEqual(first['status'], 'ready')
        self.assertFalse(first['existing_task'])
        self.assertEqual(again['status'], 'pending')
        self.assertTrue(again['existing_task'])
        self.assertEqual(record, (self.folder/download.TASK).read_bytes())
        self.assertFalse(self.file.exists())

    def test_other_file_cannot_replace_existing_task(self):
        self.prepare()
        record = (self.folder/download.TASK).read_bytes()
        with self.assertRaisesRegex(ValueError, 'another task'):
            download.prepare(self.folder, 'other.mp4', 11)
        self.assertEqual(record, (self.folder/download.TASK).read_bytes())

    def test_low_space_creates_no_record_or_download(self):
        with patch('download.shutil.disk_usage', return_value=SimpleNamespace(free=download.GIB)):
            with self.assertRaisesRegex(ValueError, 'insufficient space'):
                self.prepare()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_external_disk_also_checks_system_disk(self):
        with patch('download.Path.home') as home, patch('download.shutil.disk_usage', side_effect=[
                SimpleNamespace(free=100*download.GIB), SimpleNamespace(free=download.GIB)]) as usage:
            home.return_value.stat.return_value = SimpleNamespace(st_dev=-1)
            with self.assertRaisesRegex(ValueError, 'insufficient space'):
                self.prepare()
        self.assertEqual(usage.call_count, 2)
        self.assertFalse((self.folder/download.TASK).exists())

    def test_repeat_checks_current_space_without_losing_task(self):
        self.prepare()
        with patch('download.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, 'insufficient space'):
                self.prepare()
        self.assertEqual(download.status(self.folder)['status'], 'pending')

    def test_missing_directory_is_not_created(self):
        missing = self.folder/'not-mounted'/'downloads'
        with self.assertRaises(FileNotFoundError):
            download.prepare(missing, 'example.mp4', 10)
        self.assertFalse(missing.parent.exists())

    def test_existing_matching_file_is_preserved_without_needing_more_space(self):
        self.file.write_bytes(b'x'*10)
        with patch('download.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            self.assertEqual(self.prepare()['status'], 'file_present')
        self.assertEqual(self.file.read_bytes(), b'x'*10)

    def test_wrong_size_is_preserved_and_cannot_be_marked_complete(self):
        self.prepare()
        self.file.write_bytes(b'partial')
        with self.assertRaisesRegex(ValueError, 'file size differs'):
            download.check(self.folder, True)
        self.assertEqual(self.file.read_bytes(), b'partial')
        with self.assertRaises(ValueError):
            self.prepare()

    def test_preallocated_size_alone_is_not_completion(self):
        self.prepare()
        with self.file.open('wb') as handle:
            handle.truncate(10)
        self.assertEqual(download.status(self.folder)['status'], 'file_present')
        with self.assertRaisesRegex(ValueError, 'confirm this task'):
            download.check(self.folder)
        self.assertEqual(download.check(self.folder, True)['status'], 'complete')

    def test_missing_final_file_remains_pending_even_if_client_flag_is_set(self):
        self.prepare()
        (self.folder/'example.mp4.qkdownloading').write_bytes(b'x'*10)
        self.assertEqual(download.status(self.folder)['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'final file is missing'):
            download.check(self.folder, True)

    def test_invalid_names_and_sizes_create_no_task(self):
        for name in ('../movie.mp4', '/movie.mp4', 'a\\b.mp4', '..', '', 'a\n.mp4',
                     'movie.qkdownloading', download.TASK):
            with self.subTest(name=name), self.assertRaises(ValueError):
                download.prepare(self.folder, name, 10)
        for size in (0, -1, True, 1.5, '10'):
            with self.subTest(size=size), self.assertRaises(ValueError):
                download.prepare(self.folder, 'example.mp4', size)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_symlinked_target_and_task_record_are_not_followed(self):
        outside = self.folder/'outside'
        outside.write_bytes(b'keep')
        self.file.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            self.prepare()
        self.file.unlink()
        (self.folder/download.TASK).symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            self.prepare()
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_cli_requires_client_confirmation_and_reports_json(self):
        self.prepare()
        self.file.write_bytes(b'x'*10)
        command = [sys.executable, download.__file__, 'check', '--directory', str(self.folder)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'needs_attention')
        result = subprocess.run(command+['--client-completed'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
