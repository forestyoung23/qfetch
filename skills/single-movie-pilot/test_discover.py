import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acquire import torrent_metadata
from discover import search, magnet_parts, resolve

RAW = b'd4:infod6:lengthi12345e4:name8:film.mp4ee'
HASH = hashlib.sha1(b'd6:lengthi12345e4:name8:film.mp4e').hexdigest()
HTML = '''<ul><li><p>Sintel <a href="https://webtorrent.io/torrents/sintel.torrent">torrent</a>
<a href="magnet:?xt=urn:btih:''' + HASH + '''">magnet</a></p></li>
<li>Big Buck Bunny <a href="https://webtorrent.io/torrents/big-buck-bunny.torrent">torrent</a>
<a href="magnet:?xt=urn:btih:''' + HASH + '''">magnet</a></li></ul>'''


class Discovery(unittest.TestCase):
    def test_infohash_uses_exact_original_info_bytes(self):
        self.assertEqual(torrent_metadata(RAW)['infohash'], HASH)
        # Announce changes must not alter the content identifier.
        self.assertEqual(torrent_metadata(b'd8:announce8:https://4:infod6:lengthi12345e4:name8:film.mp4ee')['infohash'], HASH)

    def test_duplicate_info_key_is_rejected(self):
        with self.assertRaises(ValueError):
            torrent_metadata(b'd4:infod6:lengthi1e4:name5:a.mp4e4:infod6:lengthi2e4:name5:b.mp4ee')

    def test_exact_title_and_chinese_alias(self):
        self.assertEqual(search('辛特尔', HTML)['candidates'][0]['title'], 'Sintel')
        self.assertEqual(search('大雄兔', HTML)['status'], 'found')

    def test_missing_ambiguous_and_changed_site_never_guess_links(self):
        self.assertEqual(search('not a film', HTML)['status'], 'not_found')
        duplicate = HTML.replace('Sintel ', 'Big Buck Bunny Alternate ')
        self.assertEqual(search('Buck', duplicate)['status'], 'ambiguous')
        with self.assertRaises(ValueError):
            search('Sintel', '<html>captcha</html>')

    def test_bad_or_multiple_magnet_hashes_rejected(self):
        for uri in ['magnet:?xt=urn:btih:nope',
                    'magnet:?xt=urn:btih:' + HASH + '&xt=urn:btih:' + HASH]:
            with self.assertRaises(ValueError):
                magnet_parts(uri)

    def test_xs_hash_mismatch_cannot_save_or_start_payload(self):
        with tempfile.TemporaryDirectory() as d, patch('discover.fetch', return_value=RAW), \
                patch('discover.subprocess.Popen') as spawn:
            with self.assertRaises(ValueError):
                resolve('magnet:?xt=urn:btih:' + '0' * 40 + '&xs=https://example.org/a.torrent', Path(d))
            self.assertEqual(list(Path(d).iterdir()), [])
            spawn.assert_not_called()

    def test_xs_metadata_only_and_cached_repeat(self):
        with tempfile.TemporaryDirectory() as d, patch('discover.fetch', return_value=RAW) as fetch, \
                patch('discover.subprocess.Popen') as spawn:
            uri = 'magnet:?xt=urn:btih:' + HASH + '&xs=https://example.org/a.torrent'
            first = resolve(uri, Path(d))
            second = resolve(uri, Path(d))
            self.assertFalse(first['payload_submitted'])
            self.assertEqual(second['method'], 'cached_validated_metadata')
            self.assertEqual(len(list(Path(d).iterdir())), 1)
            self.assertEqual(fetch.call_count, 1)
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
