import copy
import tempfile
from pathlib import Path
import unittest
import zipfile
import json
import os
import fcntl
from unittest.mock import patch
import urllib.request
from acquire import capacity, GIB, root_check, validate_manifest, torrent_inventory, extracted_matches, pending_commitments, torrent_plan, completed_media_path
from acquire import direct_environment, direct_opener, cancel_pending_job


MANIFEST = dict(title='Test', year=2008, source_page='https://example.org/',
    url='https://example.org/test.mp4', filename='test.mp4', size_bytes=10 * GIB,
    min_width=1920, min_height=1080, min_duration_seconds=500, max_duration_seconds=700)


class Guards(unittest.TestCase):
    def test_cancel_cannot_release_budget_while_root_is_owned(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); job = root / 'old'; job.mkdir()
            state = dict(status='needs_attention', backend='Transmission', completed_bytes=0,
                         commitment=dict(payload_bytes=100,unpacked_bytes=0))
            path=job/'state.json';path.write_text(json.dumps(state))
            with (root/'.pilot.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):cancel_pending_job(job,'must wait for actual exit')
            self.assertEqual(json.loads(path.read_text()),state)

    def test_cancelled_stopped_candidate_releases_budget_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); job = root / 'old'; job.mkdir()
            payload = job / 'partial.mkv'; payload.write_bytes(b'partial')
            state = dict(status='needs_attention', backend='Transmission',completed_bytes=7,
                         commitment=dict(payload_bytes=100,unpacked_bytes=0))
            (job / 'state.json').write_text(json.dumps(state))
            self.assertGreater(pending_commitments(root,root/'new'),0)
            cancel_pending_job(job,'switch source after stopped trial')
            self.assertEqual(pending_commitments(root,root/'new'),0)
            self.assertEqual(payload.read_bytes(),b'partial')
            self.assertEqual(cancel_pending_job(job,'repeat')['status'],'cancelled')
            for status in ('downloading','verified_external'):
                state['status']=status;(job/'state.json').write_text(json.dumps(state))
                with self.assertRaises(ValueError):cancel_pending_job(job,'cannot cancel active/completed')

    def test_direct_mode_does_not_inherit_proxies_or_change_parent_environment(self):
        values = dict(PATH='/test/bin', http_proxy='http://127.0.0.1:7892',
                      HTTPS_PROXY='http://127.0.0.1:7892', all_proxy='socks5://127.0.0.1:7892')
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(direct_environment(), {'PATH': '/test/bin'})
            opener = direct_opener()
            self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in opener.handlers))
            self.assertEqual(os.environ['HTTPS_PROXY'], values['HTTPS_PROXY'])

    def test_verified_external_job_releases_pending_budget_and_locates_completed_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); job = root / 'old'; job.mkdir()
            final = root / 'library/movie.mkv'
            state = dict(status='verified_external', external_media_path=str(final))
            (job / 'state.json').write_text(json.dumps(state))
            self.assertEqual(pending_commitments(root, root / 'new'), 0)
            self.assertEqual(completed_media_path(job, state), final)

    def test_completed_file_pointer_rejects_ambiguous_relative_external_path(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                completed_media_path(Path(d), dict(status='verified_external', external_media_path='../movie.mkv'))
            with self.assertRaises(ValueError):
                completed_media_path(Path(d), dict(status='verified', media_filename='../movie.mkv'))

    def test_payload_selection_skips_document_and_uses_metadata_order(self):
        raw = b'd4:infod5:filesld6:lengthi10e4:pathl6:ad.doceed6:lengthi100e4:pathl8:film.mp4eee4:name4:filmee'
        m = dict(size_bytes=110, payload_relative_path='film/film.mp4')
        with self.assertRaises(ValueError):
            torrent_plan(m, raw)
        m['select_payload_only'] = True
        files, index = torrent_plan(m, raw)
        self.assertEqual(index, 2)
        self.assertEqual(sum(files.values()), 110)  # budget still covers the entire torrent
        m['payload_relative_path'] = 'film/ad.doc'
        with self.assertRaises(ValueError):
            torrent_plan(m, raw)

    def test_paused_jobs_reserve_remaining_bytes_and_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for key, state in {
                'paused': dict(status='needs_attention', completed_bytes=60,
                    commitment=dict(payload_bytes=100, unpacked_bytes=200)),
                'done': dict(status='verified'),
                'current': dict(status='downloading'),
            }.items():
                (root / key).mkdir()
                (root / key / 'state.json').write_text(json.dumps(state))
            self.assertEqual(pending_commitments(root, root / 'current'), 244)

    def test_unknown_pending_budget_blocks_new_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'old').mkdir()
            (root / 'old/state.json').write_text(json.dumps(dict(status='needs_attention')))
            with self.assertRaises(RuntimeError):
                pending_commitments(root, root / 'new')

    def test_extraction_recovery_requires_size_and_crc_not_just_existence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            with zipfile.ZipFile(p / 'source.zip', 'w') as z:
                z.writestr('film.mp4', b'correct')
            with zipfile.ZipFile(p / 'source.zip') as z:
                member = z.getinfo('film.mp4')
                target = p / member.filename
                target.write_bytes(b'correct')
                self.assertTrue(extracted_matches(target, member))
                target.write_bytes(b'incorre')  # identical byte length, different contents
                self.assertFalse(extracted_matches(target, member))
                target.write_bytes(b'short')
                self.assertFalse(extracted_matches(target, member))

    def test_unknown_size_cannot_be_submitted(self):
        for value in [0, -1, None, float('nan'), True]:
            m = dict(MANIFEST, size_bytes=value)
            with self.assertRaises(ValueError):
                validate_manifest(m)

    def test_archive_requires_explicit_extra_storage(self):
        m = dict(MANIFEST, filename='film.zip')
        with self.assertRaises(ValueError):
            validate_manifest(m)
        m['unpacked_limit_bytes'] = 15 * GIB
        validate_manifest(m)
        b = capacity(70 * GIB, 100 * GIB, 10 * GIB, 15 * GIB, 50, .1, 0)
        self.assertFalse(b['ok'])  # archive + extracted video + reserve exceeds 70 GiB

    def test_pending_tasks_can_block_an_otherwise_affordable_job(self):
        args = (80 * GIB, 100 * GIB, 10 * GIB, 0, 50, .1)
        self.assertTrue(capacity(*args, 0)['ok'])
        self.assertFalse(capacity(*args, 25 * GIB)['ok'])

    def test_missing_volume_never_falls_back_to_parent(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                root_check(Path(d) / 'missing-drive' / 'Movies')

    def test_changed_filesystem_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                root_check(Path(d), Path(d).stat().st_dev + 1)

    def test_no_path_traversal_or_signed_url_persistence(self):
        for change in [{'filename': '../film.mp4'}, {'url': 'file:///tmp/file'},
                       {'url': 'https://example.org/movie?token=secret'}]:
            m = copy.deepcopy(MANIFEST)
            m.update(change)
            with self.assertRaises(ValueError):
                validate_manifest(m)

    def test_torrent_inventory_rejects_escape_paths(self):
        # info: name=film, files=[{length:10,path:[..,film.mp4]}]
        raw = b'd4:infod5:filesld6:lengthi10e4:pathl2:..8:film.mp4eee4:name4:filmee'
        with self.assertRaises(ValueError):
            torrent_inventory(raw)

    def test_torrent_size_is_from_metadata_not_filename(self):
        raw = b'd4:infod6:lengthi12345e4:name8:film.mp4ee'
        self.assertEqual(torrent_inventory(raw), {'film.mp4': 12345})


if __name__ == '__main__':
    unittest.main()
