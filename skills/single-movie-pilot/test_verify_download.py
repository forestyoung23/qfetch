import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from verify_download import verify


class ExternalVideoVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which('ffmpeg') or str(Path.home() / 'Tools/ffmpeg')
        cls.ffprobe = shutil.which('ffprobe') or str(Path.home() / 'Tools/ffprobe')
        if not Path(cls.ffmpeg).is_file() or not Path(cls.ffprobe).is_file():
            raise unittest.SkipTest('real ffmpeg/ffprobe required')
        cls.fixture = tempfile.TemporaryDirectory()
        cls.clip = Path(cls.fixture.name) / 'sample.mkv'
        subprocess.run([cls.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=blue:s=64x36:r=10:d=2', '-f', 'lavfi', '-i',
                        'anullsrc=r=8000:cl=mono', '-t', '2', '-c:v', 'mpeg4',
                        '-c:a', 'aac', '-shortest', str(cls.clip)], check=True, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.media = self.root / 'sample.mkv'
        shutil.copyfile(self.clip, self.media)
        self.m = dict(title='示例', year=2020, movie_id='imdb:tt0000001',
                      filename='sample.mkv', size_bytes=self.media.stat().st_size,
                      min_width=64, min_height=36,
                      min_duration_seconds=1, max_duration_seconds=3)

    def call(self):
        return verify(self.m, self.media, self.library, self.ffprobe, self.ffmpeg)

    def test_real_decode_hardlink_nfo_and_repeat(self):
        r = self.call()
        again = self.call()
        self.assertTrue(Path(r['path']).samefile(self.media))
        self.assertEqual(r['sha256'], again['sha256'])
        self.assertEqual(len(r['decode_samples']), 3)
        self.assertTrue(all(x['decoded_frames'] > 0 for x in r['decode_samples']))
        nfo = ET.parse(r['nfo_path']).getroot()
        self.assertEqual(nfo.findtext('imdbid'), 'tt0000001')

    def test_wrong_cloud_size_does_not_deliver(self):
        self.m['size_bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'file size'):
            self.call()
        self.assertEqual(list(self.library.iterdir()), [])

    def test_resolution_threshold_is_not_lowered(self):
        self.m['min_width'] = 1920
        with self.assertRaisesRegex(RuntimeError, 'resolution'):
            self.call()
        self.assertEqual(list(self.library.iterdir()), [])

    def test_corrupt_matching_size_is_not_verified(self):
        self.media.write_bytes(b'not a media container')
        self.m['size_bytes'] = self.media.stat().st_size
        with self.assertRaisesRegex(RuntimeError, 'ffprobe'):
            self.call()

    def test_partial_filename_is_not_verified(self):
        self.media = self.media.rename(self.media.with_suffix('.mkv.qkdownloading'))
        with self.assertRaisesRegex(ValueError, 'final filename'):
            self.call()

    def test_existing_library_video_is_not_overwritten(self):
        folder = self.library / '示例 (2020) [imdbid-tt0000001]'
        folder.mkdir()
        target = folder / '示例 (2020) [imdbid-tt0000001].mkv'
        target.write_bytes(b'existing unrelated video')
        with self.assertRaises(FileExistsError):
            self.call()
        self.assertEqual(target.read_bytes(), b'existing unrelated video')

    def test_symlinked_library_folder_cannot_escape(self):
        outside=self.root/'outside'; outside.mkdir()
        (self.library/'示例 (2020) [imdbid-tt0000001]').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symbolic link'): self.call()
        self.assertEqual(list(outside.iterdir()),[])

    def test_symlinked_metadata_is_not_followed(self):
        outside=self.root/'outside.nfo'; outside.write_text('private')
        folder=self.library/'示例 (2020) [imdbid-tt0000001]'; folder.mkdir()
        (folder/'movie.nfo').symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'symbolic link'): self.call()
        self.assertEqual(outside.read_text(),'private')

    def test_direct_verifier_cannot_bypass_registered_manifest(self):
        from types import SimpleNamespace
        import movie_manager as mm
        from verify_download import main
        from acquire import write_json
        job=self.root/'jobs'/'one'; job.mkdir(parents=True)
        original=dict(self.m,min_width=128)
        state=dict(status='awaiting_client',backend='QuarkClient',movie_id=self.m['movie_id'],
                   manifest_sha256=mm.fingerprint(original),expected_filename=self.m['filename'],
                   output_directory=str(self.root),total_bytes=self.m['size_bytes'],library_root=str(self.library))
        write_json(job/'state.json',state); manifest=self.root/'manifest.json'; write_json(manifest,self.m)
        args=SimpleNamespace(manifest=manifest,file=self.media,library_root=self.library,
                             output=self.root/'result.json',job=job,wait_seconds=0,
                             ffprobe=self.ffprobe,ffmpeg=self.ffmpeg)
        with self.assertRaisesRegex(ValueError,'frozen'): main(args)
        self.assertEqual(json.loads((job/'state.json').read_text())['status'],'awaiting_client')
