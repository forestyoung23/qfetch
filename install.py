#!/usr/bin/env python3
"""Install or explicitly upgrade the bundled skill; runtime data stays separate."""
import argparse
import datetime
import os
from pathlib import Path
import shutil
import tempfile
import uuid


def default_target():
    return Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'skills/qfetch'


def inventory(root):
    return {p.relative_to(root):p for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def install(source,target,upgrade=False):
    source=source.resolve(strict=True); target=target.expanduser().absolute()
    if not (source/'SKILL.md').is_file() or not (source/'download.py').is_file():
        raise ValueError('bundled skill is incomplete')
    if target.is_symlink():
        raise ValueError('refusing to replace a symbolic-link installation')
    if source==target.resolve():
        return {'status':'already_installed','target':str(target)}
    if target.exists():
        expected,actual=inventory(source),inventory(target)
        if set(expected)==set(actual) and all(p.read_bytes()==actual[k].read_bytes() for k,p in expected.items()):
            return {'status':'already_installed','target':str(target)}
        if not upgrade:
            raise FileExistsError('different installation preserved; use --upgrade to back it up and replace the skill')
        # Data in a legacy skill directory must never be moved out from under a running client.
        if any((target/name).exists() for name in ('downloads','transfers','library','config.json','.quark-download.json','.qfetch.json')):
            raise ValueError('installation contains runtime data; move/stop it explicitly before upgrading')
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.quark-skill-',dir=target.parent)); backup=None
    try:
        staged=stage/'skill'
        shutil.copytree(source,staged,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        if target.exists():
            backups=target.parent.parent/'skill-backups'; backups.mkdir(exist_ok=True)
            stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup=backups/(target.name+'-'+stamp+'-'+uuid.uuid4().hex[:8])
            target.rename(backup)
        try:
            staged.rename(target)
        except OSError:
            if backup and not target.exists(): backup.rename(target)
            raise
    finally:
        shutil.rmtree(stage)
    return {'status':'upgraded' if backup else 'installed','target':str(target),
            'backup':str(backup) if backup else None}


if __name__=='__main__':
    import json
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',type=Path,default=default_target())
    p.add_argument('--upgrade',action='store_true')
    a=p.parse_args()
    try:
        print(json.dumps(install(Path(__file__).resolve().parent/'skills/qfetch',a.target,a.upgrade),ensure_ascii=False,indent=2))
    except (OSError,ValueError) as e:
        p.exit(2,str(e)+'\n')
