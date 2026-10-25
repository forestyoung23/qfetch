import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from acquire import GIB, pending_commitments, write_json
import movie_manager as mm


class Registry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg=mm.executable('ffmpeg'); cls.ffprobe=mm.executable('ffprobe')
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest('real ffmpeg/ffprobe required')
        cls.fixture=tempfile.TemporaryDirectory(); cls.clip=Path(cls.fixture.name)/'sample.mkv'
        subprocess.run([cls.ffmpeg,'-v','error','-f','lavfi','-i','color=c=blue:s=64x36:r=10:d=2',
                        '-f','lavfi','-i','anullsrc=r=8000:cl=mono','-t','2','-c:v','mpeg4','-c:a','aac',
                        '-shortest',str(cls.clip)],check=True,timeout=20)

    @classmethod
    def tearDownClass(cls): cls.fixture.cleanup()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)/'home'; mm.setup(self.home); self.c=mm.config(self.home)
        self.root=Path(self.c['state_root'])
        self.m=dict(title='示例',year=2020,movie_id='imdb:tt0000001',filename='sample.mkv',
                    size_bytes=self.clip.stat().st_size,min_width=64,min_height=36,
                    min_duration_seconds=1,max_duration_seconds=3)
        self.disk=patch.object(mm.shutil,'disk_usage',return_value=SimpleNamespace(total=1000*GIB,free=500*GIB))
        self.disk.start(); self.addCleanup(self.disk.stop)

    def prepare(self,m=None): return mm.prepare(self.c,m or self.m,0)

    def complete(self):
        p=self.prepare(); shutil.copyfile(self.clip,p['expected_file'])
        return mm.finish(self.c,Path(p['job']))

    def test_same_id_different_source_returns_original_pending(self):
        first=self.prepare(); m=dict(self.m,title='different name',filename='different.mkv',share_url='https://pan.quark.cn/s/example')
        second=self.prepare(m)
        self.assertEqual(second['status'],'existing_pending'); self.assertEqual(first['job'],second['job'])
        self.assertEqual(len(list(self.root.glob('*/state.json'))),1)

    def test_verified_movie_reused_across_candidates(self):
        done=self.complete(); m=dict(self.m,filename='alternate.mkv',min_width=1920)
        again=self.prepare(m)
        self.assertEqual(again['status'],'already_verified'); self.assertEqual(again['path'],done['path'])
        self.assertEqual(pending_commitments(self.root,None),0)
        self.assertEqual(len(list(Path(self.c['download_root']).glob('*'))),1)

    def test_corrupt_existing_movie_does_not_trigger_new_download(self):
        done=self.complete(); p=Path(done['path']); b=p.read_bytes(); p.write_bytes(bytes([b[0]^1])+b[1:])
        again=self.prepare(); self.assertEqual(again['status'],'existing_needs_attention')
        self.assertEqual(len(list(self.root.glob('*/state.json'))),1)

    def test_missing_verified_file_blocks_new_submission(self):
        done=self.complete(); Path(done['path']).unlink()
        self.assertEqual(self.prepare()['status'],'existing_needs_attention')

    def test_capacity_shortfall_creates_no_task(self):
        with patch.object(mm.shutil,'disk_usage',return_value=SimpleNamespace(total=1000*GIB,free=101*GIB)):
            r=mm.prepare(self.c,self.m,2*GIB)
        self.assertEqual(r['status'],'capacity_shortfall'); self.assertEqual(list(self.root.glob('*/state.json')),[])

    def test_paused_unknown_budget_fails_closed(self):
        p=self.root/'old'; p.mkdir(); write_json(p/'state.json',dict(status='paused'))
        with self.assertRaisesRegex(RuntimeError,'commitment'): self.prepare()

    def test_second_movie_waits_for_existing_client(self):
        first=self.prepare(); m=dict(self.m,movie_id='imdb:tt0000002')
        r=self.prepare(m); self.assertEqual(r['status'],'busy_existing'); self.assertEqual(r['job'],first['job'])

    def test_frozen_manifest_edit_cannot_weaken_verification(self):
        p=self.prepare(); job=Path(p['job']); m=dict(self.m,min_width=1)
        write_json(job/'manifest.json',m)
        with self.assertRaisesRegex(ValueError,'frozen'): mm.finish(self.c,job)
        self.assertGreater(pending_commitments(self.root,None),0)

    def test_new_process_config_recovers_same_pending_job(self):
        p=self.prepare(); c=mm.config(self.home)
        self.assertEqual(mm.prepare(c,dict(self.m,filename='new.mkv'),0)['job'],p['job'])
        self.assertEqual(mm.finish(c,Path(p['job']))['status'],'waiting_for_download')

    def test_download_folder_escape_is_rejected(self):
        p=self.prepare(); job=Path(p['job']); s=json.loads((job/'state.json').read_text())
        s['output_directory']=str(self.home); write_json(job/'state.json',s)
        with self.assertRaisesRegex(ValueError,'directory'): mm.finish(self.c,job)

    def test_observe_pause_and_release_keep_files_and_budget_until_stopped(self):
        p=self.prepare(); job=Path(p['job']); partial=Path(p['output_directory'])/'partial.qkdownloading'; partial.write_bytes(b'keep')
        mm.observe(self.c,job,'paused'); self.assertGreater(pending_commitments(self.root,None),0)
        with self.assertRaisesRegex(ValueError,'stopped'): mm.release(self.c,job,'test')
        mm.release(self.c,job,'client task stopped in UI',True)
        self.assertEqual(pending_commitments(self.root,None),0); self.assertEqual(partial.read_bytes(),b'keep')

    def test_private_metadata_and_signed_links_are_not_saved(self):
        for extra in ({'fid':'private'},{'share_url':'https://example.test/file?token=secret'}):
            with self.assertRaises(ValueError): self.prepare(dict(self.m,**extra))
        self.assertEqual(list(self.root.glob('*/state.json')),[])

    def test_filesystem_change_detected_on_reload(self):
        p=self.home/'config.json'; c=json.loads(p.read_text()); c['download_root_device']=-1; write_json(p,c)
        with self.assertRaisesRegex(ValueError,'filesystem'): mm.config(self.home)

    def test_two_writers_cannot_register_simultaneously(self):
        with mm.locked(self.root):
            with self.assertRaises(BlockingIOError): self.prepare()
        self.assertEqual(list(self.root.glob('*/state.json')),[])

    def test_failed_media_validation_retains_reservation(self):
        p=self.prepare(); target=Path(p['expected_file']); target.write_bytes(b'X'*self.m['size_bytes'])
        with self.assertRaisesRegex(RuntimeError,'ffprobe'): mm.finish(self.c,Path(p['job']))
        s=json.loads((Path(p['job'])/'state.json').read_text()); self.assertEqual(s['status'],'awaiting_client')
        self.assertGreater(pending_commitments(self.root,None),0)

    def test_cancelled_candidate_can_switch_without_overwriting_evidence(self):
        old=self.prepare(); job=Path(old['job'])
        evidence=job/'evidence.txt'; evidence.write_text('keep')
        mm.release(self.c,job,'original client stopped',True)
        new=self.prepare(dict(self.m,filename='alternative.mkv'))
        self.assertEqual(new['status'],'awaiting_client')
        self.assertNotEqual(old['job'],new['job']); self.assertEqual(evidence.read_text(),'keep')

    def test_completed_finish_is_idempotent(self):
        done=self.complete(); again=mm.finish(self.c,Path(done['job']))
        self.assertEqual(again['status'],'already_verified')
        self.assertEqual(again['path'],done['path'])
        with self.assertRaisesRegex(ValueError,'completed'): mm.observe(self.c,Path(done['job']),'downloading')
        with self.assertRaisesRegex(ValueError,'completed'): mm.release(self.c,Path(done['job']),'stop',True)

    def test_changed_record_size_blocks_finish(self):
        p=self.prepare(); job=Path(p['job']); s=json.loads((job/'state.json').read_text())
        s['total_bytes']+=1; write_json(job/'state.json',s)
        with self.assertRaisesRegex(ValueError,'frozen'): mm.finish(self.c,job)

    def test_external_disk_unknown_temp_preserves_system_headroom(self):
        m=dict(self.m,size_bytes=10*GIB)
        with patch.object(mm,'system_device',return_value=-1), patch.object(mm.shutil,'disk_usage',side_effect=[
                SimpleNamespace(total=1000*GIB,free=500*GIB),SimpleNamespace(total=500*GIB,free=31*GIB)]):
            r=mm.prepare(self.c,m,0)
        self.assertEqual(r['status'],'capacity_shortfall')
        self.assertGreater(r['system_required_bytes'],31*GIB)
        self.assertEqual(list(self.root.glob('*/state.json')),[])
