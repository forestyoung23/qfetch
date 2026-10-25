import argparse
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error

from transmission_pilot import check_selection, guard_space, Client, local_proxy, run as transmission_run
from acquire import GIB, run


class TransmissionGuards(unittest.TestCase):
    def test_local_proxy_is_explicit_and_excludes_credentials(self):
        self.assertEqual(local_proxy(''), '')
        for value in ('http://127.0.0.1:7892', 'socks5h://localhost:7892'):
            self.assertEqual(local_proxy(value), value)
        for value in ('socks5://localhost:7892', 'http://remote.example:7892',
                      'http://user:secret@localhost:7892', 'http://localhost:7892/?token=secret',
                      'http://localhost:7892/path', 'http://localhost'):
            with self.assertRaises(ValueError): local_proxy(value)

    def test_verified_repeat_does_not_start_daemon(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            raw = b'd4:infod6:lengthi100e4:name9:movie.mkvee'
            torrent = root / 'movie.torrent'; torrent.write_bytes(raw)
            media = root / 'movie.mkv'; media.write_bytes(b'x' * 100)
            m = dict(title='Test', year=1994, source_page='https://example.org/',
                url='https://example.org/movie.torrent', filename='movie.mkv', size_bytes=100,
                min_width=1, min_height=1, min_duration_seconds=1, max_duration_seconds=2,
                transport='bittorrent', torrent_path=str(torrent), torrent_sha256=hashlib.sha256(raw).hexdigest(),
                payload_relative_path='movie.mkv')
            manifest = root / 'manifest.json'; manifest.write_text(json.dumps(m))
            key = hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()[:20]
            job = root / key; job.mkdir()
            (job / 'state.json').write_text(json.dumps(dict(status='verified_external',
                external_media_path=str(media), sha256=hashlib.sha256(media.read_bytes()).hexdigest())))
            args = argparse.Namespace(manifest=manifest,root=root,max_seconds=1000,other_reserved_gib=0)
            with patch('transmission_pilot.inspect') as inspect_media, patch('transmission_pilot.subprocess.Popen') as spawn:
                self.assertEqual(transmission_run(args), 0)
                spawn.assert_not_called(); inspect_media.assert_called_once()
                media.write_bytes(b'y' * 100)
                with self.assertRaisesRegex(ValueError, 'changed'): transmission_run(args)
                spawn.assert_not_called()

    def trial_with_error(self, temporary, clock_values, task_error):
        import hashlib
        root = Path(temporary)
        raw = b'd4:infod6:lengthi100e4:name9:movie.mkvee'
        torrent = root / 'movie.torrent'; torrent.write_bytes(raw)
        m = dict(title='Test', year=1994, source_page='https://example.org/',
            url='https://example.org/movie.torrent', filename='movie.mkv', size_bytes=100,
            min_width=1, min_height=1, min_duration_seconds=1, max_duration_seconds=2,
            transport='bittorrent', torrent_path=str(torrent), torrent_sha256=hashlib.sha256(raw).hexdigest(),
            payload_relative_path='movie.mkv')
        manifest = root / 'manifest.json'; manifest.write_text(json.dumps(m))
        client = Mock(); process = Mock(); process.poll.return_value = None
        port_socket = Mock(); port_socket.getsockname.return_value = ('127.0.0.1', 9091)
        def call(method, **params):
            if method == 'torrent_get':
                if params['fields'] == ['hash_string']: return {'torrents': []}
                return {'torrents': [dict(files=[dict(name='movie.mkv', length=100, bytes_completed=0)],
                    file_stats=[dict(wanted=True)], download_dir=str(next(root.glob('*/transmission-payload'))),
                    status=4, error=task_error, error_string='simulated backend error',
                    rate_download=0, peers_connected=0, left_until_done=100, tracker_stats=[])]}
            return {}
        client.call.side_effect = call
        with patch('transmission_pilot.Client', return_value=client), \
             patch('transmission_pilot.socket.socket') as socket_factory, \
             patch('transmission_pilot.subprocess.Popen', return_value=process), \
             patch('transmission_pilot.shutil.which', return_value='/test/transmission-daemon'), \
             patch('transmission_pilot.shutil.disk_usage', return_value=Mock(free=100*GIB,total=200*GIB)), \
             patch('transmission_pilot.time.monotonic', side_effect=clock_values):
            socket_factory.return_value.__enter__.return_value = port_socket
            code = transmission_run(argparse.Namespace(manifest=manifest,root=root,max_seconds=1000,other_reserved_gib=0))
        return code, client, process, json.loads(next(root.glob('*/state.json')).read_text())

    def test_backend_error_stops_only_owned_torrent_and_daemon(self):
        with tempfile.TemporaryDirectory() as d:
            code, client, process, state = self.trial_with_error(d, [0, 0, 1], 2)
            self.assertEqual(code, 4)
            self.assertIn('simulated backend error', state['reason'])
            add = next(c for c in client.call.call_args_list if c.args[0] == 'torrent_add')
            self.assertTrue(add.kwargs['paused'])
            self.assertEqual(add.kwargs['files_wanted'], [0])
            stopped = [c for c in client.call.call_args_list if c.args[0] == 'torrent_stop']
            self.assertEqual(len(stopped), 1)
            self.assertEqual(len(stopped[0].kwargs['ids']), 1)
            process.send_signal.assert_called_once()
            process.wait.assert_called_once()

    def test_no_progress_exits_and_preserves_pending_budget(self):
        with tempfile.TemporaryDirectory() as d:
            code, client, process, state = self.trial_with_error(d, [0, 0, 301], 0)
            self.assertEqual(code, 4)
            self.assertIn('no progress', state['reason'])
            self.assertEqual(state['status'], 'needs_attention')
            self.assertEqual(state['commitment']['payload_bytes'], 100)
            self.assertEqual(state['completed_bytes'], 0)
            process.send_signal.assert_called_once()

    def test_wrong_selection_or_path_cannot_start(self):
        files = {'film/ad.doc': 10, 'film/movie.mkv': 100}
        task = dict(files=[dict(name=k, length=v) for k, v in files.items()],
                    file_stats=[dict(wanted=False), dict(wanted=True)], download_dir='/tmp/movie')
        check_selection(task, files, 'film/movie.mkv', Path('/tmp/movie'))
        task['file_stats'][0]['wanted'] = True
        with self.assertRaises(ValueError): check_selection(task, files, 'film/movie.mkv', Path('/tmp/movie'))
        task['file_stats'][0]['wanted'] = False
        with self.assertRaises(ValueError): check_selection(task, files, 'film/movie.mkv', Path('/tmp/other'))
        task['files'][1]['length'] = 99
        with self.assertRaises(ValueError): check_selection(task, files, 'film/movie.mkv', Path('/tmp/movie'))

    def test_capacity_crossing_and_missing_mount_stop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); device = root.stat().st_dev
            with patch('transmission_pilot.shutil.disk_usage', return_value=Mock(free=49 * GIB)):
                with self.assertRaises(RuntimeError): guard_space(root, device, 50 * GIB, 0, root)
            with patch('transmission_pilot.shutil.disk_usage', return_value=Mock(free=80 * GIB)):
                guard_space(root, device, 50 * GIB, GIB, root)
                with self.assertRaises(RuntimeError): guard_space(root / 'missing', device, 50 * GIB, 0, root)

    def test_rpc_409_handshake_retries_same_request_with_session(self):
        client = Client(9091, 'test-only-password')
        reply = io.BytesIO(json.dumps(dict(result={'version': '4.1.3'})).encode())
        opener = Mock()
        opener.open.side_effect = [urllib.error.HTTPError(client.url, 409, 'session',
            {'X-Transmission-Session-Id': 'session-test'}, None), reply]
        client.opener = opener
        self.assertEqual(client.call('session_get', fields=['version']), {'version': '4.1.3'})
        first = opener.open.call_args_list[0].args[0]
        second = opener.open.call_args_list[1].args[0]
        self.assertEqual(first.data, second.data)
        self.assertIn('session-test', second.headers.values())

    def test_original_backend_refuses_existing_transmission_job(self):
        import hashlib
        m = dict(title='Test', year=1994, source_page='https://example.org/',
                 url='https://example.org/movie.mkv', filename='movie.mkv', size_bytes=100,
                 min_width=1, min_height=1, min_duration_seconds=1, max_duration_seconds=2)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); manifest = root / 'manifest.json'; manifest.write_text(json.dumps(m))
            key = hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()[:20]
            job = root / key; job.mkdir()
            (job / 'state.json').write_text(json.dumps(dict(backend='Transmission', status='needs_attention')))
            args = argparse.Namespace(manifest=manifest, root=root, reserve_gib=50,
                reserve_fraction=.1, other_reserved_gib=0, max_seconds=60, rate_kib=0)
            with self.assertRaisesRegex(RuntimeError, 'second backend'): run(args)


if __name__ == '__main__': unittest.main()
