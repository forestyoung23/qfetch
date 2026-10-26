from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
import install


class Installation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.src=self.root/'source'; self.src.mkdir()
        (self.src/'SKILL.md').write_text('version one'); (self.src/'download.py').write_text('# entry')
        self.target=self.root/'agent/skills/qfetch'

    def test_fresh_and_repeat_install(self):
        self.assertEqual(install.install(self.src,self.target)['status'],'installed')
        self.assertEqual(install.install(self.src,self.target)['status'],'already_installed')

    def test_conflict_preserves_original(self):
        install.install(self.src,self.target); (self.src/'SKILL.md').write_text('version two')
        with self.assertRaises(FileExistsError): install.install(self.src,self.target)
        self.assertEqual((self.target/'SKILL.md').read_text(),'version one')

    def test_upgrade_backs_up_code_and_leaves_external_data(self):
        install.install(self.src,self.target)
        data=self.root/'movie-data'; data.mkdir(); (data/'movie.mkv').write_bytes(b'keep media')
        (self.src/'SKILL.md').write_text('version two')
        r=install.install(self.src,self.target,True)
        self.assertEqual(r['status'],'upgraded')
        self.assertEqual((Path(r['backup'])/'SKILL.md').read_text(),'version one')
        self.assertEqual((self.target/'SKILL.md').read_text(),'version two')
        self.assertEqual((data/'movie.mkv').read_bytes(),b'keep media')

    def test_legacy_data_prevents_upgrade(self):
        install.install(self.src,self.target); (self.target/'downloads').mkdir()
        (self.src/'SKILL.md').write_text('version two')
        with self.assertRaisesRegex(ValueError,'runtime data'): install.install(self.src,self.target,True)
        self.assertTrue((self.target/'downloads').is_dir())

    def test_task_record_prevents_upgrade(self):
        install.install(self.src,self.target)
        (self.target/'.quark-download.json').write_text('{}')
        (self.src/'SKILL.md').write_text('version two')
        with self.assertRaisesRegex(ValueError,'runtime data'):
            install.install(self.src,self.target,True)
        self.assertEqual((self.target/'.quark-download.json').read_text(),'{}')

    def test_current_download_data_prevents_upgrade(self):
        install.install(self.src,self.target)
        (self.target/'.qfetch.json').write_text('{"fid":"test"}')
        (self.target/'example.bin.part').write_bytes(b'keep')
        (self.src/'SKILL.md').write_text('version two')
        with self.assertRaisesRegex(ValueError,'runtime data'):
            install.install(self.src,self.target,True)
        self.assertEqual((self.target/'example.bin.part').read_bytes(),b'keep')

    def test_custom_codex_home(self):
        with patch.dict(os.environ,{'CODEX_HOME':str(self.root/'custom')}):
            self.assertEqual(install.default_target(),self.root/'custom/skills/qfetch')

    def test_symlink_installation_is_preserved(self):
        self.target.parent.mkdir(parents=True); self.target.symlink_to(self.src,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symbolic'): install.install(self.src,self.target,True)
        self.assertTrue(self.target.is_symlink())

    def test_bytecode_is_not_installed(self):
        (self.src/'__pycache__').mkdir(); (self.src/'__pycache__/entry.pyc').write_bytes(b'compiled')
        install.install(self.src,self.target)
        self.assertFalse((self.target/'__pycache__').exists())

    def test_failed_activation_restores_previous_install(self):
        install.install(self.src,self.target); (self.src/'SKILL.md').write_text('version two')
        rename=Path.rename
        def fail_activation(path,destination):
            if path.name=='skill' and path.parent.name.startswith('.quark-skill-'):
                raise OSError('simulated activation failure')
            return rename(path,destination)
        with patch.object(Path,'rename',fail_activation):
            with self.assertRaisesRegex(OSError,'activation'): install.install(self.src,self.target,True)
        self.assertEqual((self.target/'SKILL.md').read_text(),'version one')
