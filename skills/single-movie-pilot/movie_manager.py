#!/usr/bin/env python3
"""Movie identity registry and guarded handoff to a user-visible downloader."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

from acquire import GIB, inspect, pending_commitments, sha256, write_json
from verify_download import validate, verify

FINAL = {'verified', 'verified_external'}
IGNORED = {'cancelled', 'capacity_shortfall'}


def identity(value):
    if not re.fullmatch(r'imdb:tt[0-9]+', value):
        raise ValueError('movie_id must be imdb:tt followed by digits')
    return value


def fingerprint(m):
    return hashlib.sha256(json.dumps(m, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def executable(name):
    return shutil.which(name) or (str(Path.home() / 'Tools' / name) if (Path.home() / 'Tools' / name).is_file() else None)


def system_device():
    return Path.home().stat().st_dev


def check_devices(c):
    for key in ('state_root','download_root','library_root'):
        if Path(c[key]).stat().st_dev != c[key + '_device']:
            raise ValueError('configured filesystem changed: ' + key)


def config(home):
    home = home.expanduser().resolve(strict=True)
    c = json.loads((home / 'config.json').read_text())
    for key in ('state_root', 'download_root', 'library_root'):
        p = Path(c[key]).resolve(strict=True)
        if not p.is_dir():
            raise ValueError('configured directory missing: ' + key)
        if c.get(key + '_device') != p.stat().st_dev:
            raise ValueError('configured filesystem changed: ' + key)
        c[key] = str(p)
    return c


@contextmanager
def locked(root):
    with (root / '.pilot.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def states(root):
    for p in sorted(root.glob('*/state.json')):
        d = json.loads(p.read_text())
        if not isinstance(d, dict) or not isinstance(d.get('status'), str):
            raise ValueError('invalid job record: ' + str(p))
        yield p.parent, d


def media_path(s):
    p = s.get('external_media_path') or s.get('media_path') or s.get('path')
    return Path(p) if p else None


def existing(root, movie_id, check=False):
    matches = [(p,s) for p,s in states(root) if s.get('movie_id') == identity(movie_id) and s['status'] not in IGNORED]
    # Never create a second download when an existing movie record needs repair.
    for job,s in sorted(matches, key=lambda x: x[1]['status'] not in FINAL):
        result = {'movie_id': movie_id, 'title': s.get('title'), 'job': str(job), 'recorded_status': s['status']}
        if s['status'] not in FINAL:
            return dict(result, status='existing_pending', action='Inspect or resume original client task; never submit a second download.')
        p = media_path(s)
        if not p or not p.is_file():
            return dict(result, status='existing_needs_attention', reason='registered verified media is missing; restore path before proceeding')
        expected = s.get('total_bytes') or s.get('file_bytes') or s.get('completed_bytes')
        if not expected or p.stat().st_size != expected:
            return dict(result, status='existing_needs_attention', reason='registered file size changed')
        if check:
            probe = executable('ffprobe')
            if not probe:
                raise ValueError('ffprobe required to recheck existing movie')
            before = p.stat()
            if not s.get('sha256') or sha256(p) != s['sha256']:
                return dict(result, status='existing_needs_attention', reason='registered SHA256 differs or is absent')
            v = s.get('verification', {})
            d = v.get('duration_seconds', 0)
            if not d or not v.get('width') or not v.get('height'):
                return dict(result, status='existing_needs_attention', reason='verified stream record absent')
            inspect(p, dict(min_width=v['width'], min_height=v['height'], min_duration_seconds=max(.01,d-1), max_duration_seconds=d+1), probe)
            after = p.stat()
            if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
                raise ValueError('existing movie changed during recheck')
        return dict(result, status='already_verified' if check else 'existing_verified_record', path=str(p), verification=s.get('verification'), rechecked=check)
    return {'status': 'not_found', 'movie_id': movie_id}


def setup(home, state_root=None):
    home = home.expanduser().absolute()
    home.mkdir(parents=True, exist_ok=True)
    path = home / 'config.json'
    if path.exists():
        c = config(home)
        if state_root and Path(c['state_root']) != state_root.resolve():
            raise ValueError('existing config differs; use another home rather than overwriting')
        return dict(status='configured', home=str(home), **c)
    for name in ('downloads', 'transfers', 'library'):
        (home / name).mkdir(exist_ok=True)
    root = state_root.expanduser().resolve(strict=True) if state_root else (home / 'downloads').resolve()
    if not root.is_dir():
        raise ValueError('state root must be an existing directory')
    c = {'version': 1, 'state_root': str(root), 'download_root': str((home/'transfers').resolve()),
         'library_root': str((home/'library').resolve())}
    for key in ('state_root','download_root','library_root'):
        c[key + '_device'] = Path(c[key]).stat().st_dev
    # Exclusive creation: a second configure process cannot replace the first config.
    with path.open('x') as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    return dict(status='configured', home=str(home.resolve()), **c)


def prepare(c, manifest, other_bytes=0, temporary_copies=2):
    validate(manifest)
    allowed = {'title','year','movie_id','filename','size_bytes','min_width','min_height',
               'min_duration_seconds','max_duration_seconds','source_page','share_url','version'}
    if set(manifest) - allowed:
        raise ValueError('manifest contains unsupported/private fields; use only public source metadata')
    if re.search(r'[\x00-\x1f\\]', manifest['filename']):
        raise ValueError('portable video filename required')
    for key in ('source_page','share_url'):
        if key in manifest:
            u = urlsplit(manifest[key])
            if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError('public HTTPS source URL without credentials/query required')
    if type(other_bytes) is not int or other_bytes < 0 or temporary_copies not in (1,2):
        raise ValueError('nonnegative known external budget and 1 or 2 temporary copies required')
    root = Path(c['state_root'])
    with locked(root):
        check_devices(c)
        found = existing(root, manifest['movie_id'], check=True)
        if found['status'] != 'not_found':
            return found
        active = [(p,s) for p,s in states(root) if s.get('backend') == 'QuarkClient' and s['status'] not in FINAL | IGNORED]
        if active:
            return {'status': 'busy_existing', 'job': str(active[0][0]), 'reason': 'finish or explicitly stop the existing client task before submitting another movie'}
        output = Path(c['download_root']); library = Path(c['library_root'])
        if output.stat().st_dev != library.stat().st_dev:
            raise ValueError('download and library must share a filesystem for hard links')
        pending = pending_commitments(root, None)
        u = shutil.disk_usage(output)
        reserve = max(50*GIB, math.ceil(u.total*.1))
        required = reserve + pending + other_bytes + math.ceil(manifest['size_bytes']*1.1)*temporary_copies
        system = shutil.disk_usage(Path.home())
        system_required = 30*GIB
        if output.stat().st_dev != system_device() and temporary_copies == 2:
            # Unknown native-client temp location may be the internal/system disk.
            system_required += math.ceil(manifest['size_bytes']*1.1) + pending + other_bytes
        if system.free < system_required or u.free < required:
            return {'status':'capacity_shortfall','free_bytes':u.free,'required_bytes':required,'reserve_bytes':reserve,
                    'other_reserved_bytes':pending+other_bytes,'system_free_bytes':system.free,'system_required_bytes':system_required,'task_created':False}
        job = root / ('movie-' + manifest['movie_id'].split(':')[1] + '-' + fingerprint(manifest)[:12])
        if job.exists():
            raise ValueError('movie job directory already exists; inspect it rather than overwriting')
        # Bind every verifier input to a fixed manifest; do not store cloud credentials.
        check_devices(c)
        job.mkdir()
        folder = output / job.name
        folder.mkdir(exist_ok=False)
        write_json(job/'manifest.json',manifest)
        state = {'status':'awaiting_client','backend':'QuarkClient','movie_id':manifest['movie_id'],
                 'title':manifest['title'],'year':manifest['year'],'manifest_sha256':fingerprint(manifest),
                 'expected_filename':manifest['filename'],'output_directory':str(folder),
                 'library_root':str(library),'completed_bytes':0,'total_bytes':manifest['size_bytes'],
                 'commitment':{'payload_bytes':manifest['size_bytes'],'unpacked_bytes':math.ceil(manifest['size_bytes']*1.1)*(temporary_copies-1)},
                 'temporary_copies':temporary_copies,'capacity':{'ok':True,'free_bytes':u.free,'reserve_bytes':reserve,
                 'required_bytes':required,'other_reserved_bytes':pending+other_bytes},'last_observed_unix':time.time()}
        write_json(job/'state.json',state)
        return {'status':'awaiting_client','job':str(job),'manifest':str(job/'manifest.json'),
                'output_directory':str(folder),'expected_file':str(folder/manifest['filename']),
                'action':'Download exactly this file once using the official client. Continue with finish; a reservation is not a submitted download.'}


def bound_job(c, job):
    check_devices(c)
    job = job.expanduser().resolve(strict=True)
    if job.parent != Path(c['state_root']):
        raise ValueError('job must belong to configured state root')
    s = json.loads((job/'state.json').read_text())
    m = json.loads((job/'manifest.json').read_text())
    validate(m)
    expected = (s.get('backend')=='QuarkClient' and s.get('manifest_sha256')==fingerprint(m)
                and s.get('movie_id')==m['movie_id'] and s.get('total_bytes')==m['size_bytes']
                and s.get('expected_filename')==m['filename'] and s.get('library_root')==c['library_root'])
    if not expected:
        raise ValueError('job no longer matches its frozen manifest/configuration')
    folder = Path(s['output_directory']).resolve(strict=True)
    if folder.parent != Path(c['download_root']) or folder.name != job.name:
        raise ValueError('job download directory differs from configured location')
    return job,s,m,folder/m['filename']


def finish(c, job, wait_seconds=0):
    if not 0 <= wait_seconds <= 14400:
        raise ValueError('wait_seconds must be 0..14400')
    job,s,m,media = bound_job(c,job)
    deadline=time.monotonic()+wait_seconds
    while not media.is_file():
        if time.monotonic()>=deadline:
            return {'status':'waiting_for_download','job':str(job),'expected_file':str(media)}
        time.sleep(min(5,max(0,deadline-time.monotonic())))
    with locked(Path(c['state_root'])):
        job,s,m,media=bound_job(c,job)
        if s['status'] in IGNORED:
            raise ValueError('cancelled task must not be finished')
        if s['status'] in FINAL:
            return existing(Path(c['state_root']),m['movie_id'],check=True)
        probe,ffmpeg=executable('ffprobe'),executable('ffmpeg')
        if not probe or not ffmpeg:
            raise ValueError('ffprobe and ffmpeg required')
        result=verify(m,media,Path(c['library_root']),probe,ffmpeg)
        # Verification failures leave the reservation in place and never mark completion.
        check_devices(c)
        write_json(job/'verification.json',result)
        s.update(status='verified_external',external_media_path=result['path'],sha256=result['sha256'],
                 verification=result['verification'],completed_bytes=m['size_bytes'],speed_bytes_per_second=0,
                 commitment={'payload_bytes':m['size_bytes'],'unpacked_bytes':0},last_observed_unix=time.time())
        write_json(job/'state.json',s)
        return dict(result,job=str(job))


def observe(c, job, phase):
    with locked(Path(c['state_root'])):
        job,s,m,media=bound_job(c,job)
        if s['status'] in FINAL | IGNORED:
            raise ValueError('completed/cancelled job cannot be marked in progress')
        s.update(status=phase,last_observed_unix=time.time())
        write_json(job/'state.json',s)
        return {'status':phase,'job':str(job),'boundary':'Agent-observed client state; not proof of downloader liveness or completed bytes.'}


def release(c, job, reason, client_stopped=False):
    if not client_stopped or not reason.strip():
        raise ValueError('verify original client task stopped, then provide client-stopped and reason')
    with locked(Path(c['state_root'])):
        job,s,m,media=bound_job(c,job)
        if s['status'] in FINAL:
            raise ValueError('completed job cannot be cancelled')
        s.update(status='cancelled',reason=reason,last_observed_unix=time.time())
        write_json(job/'state.json',s)
        return {'status':'cancelled','job':str(job),'files_preserved':True,
                'boundary':'Reservation released only; this command does not stop the client.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home',type=Path,default=Path.home()/'Documents/Codex/movie-library')
    subs=p.add_subparsers(dest='command',required=True)
    a=subs.add_parser('init'); a.add_argument('--state-root',type=Path)
    subs.add_parser('doctor'); subs.add_parser('status')
    a=subs.add_parser('lookup'); a.add_argument('--movie-id',required=True); a.add_argument('--verify',action='store_true')
    a=subs.add_parser('prepare'); a.add_argument('--manifest',type=Path,required=True)
    a.add_argument('--other-reserved-bytes',type=int,required=True)
    a.add_argument('--temporary-copies',type=int,choices=(1,2),default=2)
    a=subs.add_parser('finish'); a.add_argument('--job',type=Path,required=True); a.add_argument('--wait-seconds',type=int,default=0)
    a=subs.add_parser('observe'); a.add_argument('--job',type=Path,required=True); a.add_argument('--phase',choices=('downloading','paused'),required=True)
    a=subs.add_parser('release'); a.add_argument('--job',type=Path,required=True); a.add_argument('--reason',required=True); a.add_argument('--client-stopped',action='store_true')
    args=p.parse_args()
    if args.command=='init': result=setup(args.home,args.state_root)
    else:
        c=config(args.home); root=Path(c['state_root'])
        if args.command=='doctor':
            result={'status':'ready' if executable('ffprobe') and executable('ffmpeg') else 'missing_dependencies',
                    'ffprobe':executable('ffprobe'),'ffmpeg':executable('ffmpeg'),
                    'quark_app_present':Path('/Applications/Quark.app').is_dir(),
                    'config':c,'free_bytes':shutil.disk_usage(c['download_root']).free,
                    'boundary':'Client login, network reachability and UI control require live Agent checks.'}
        elif args.command=='lookup': result=existing(root,args.movie_id,args.verify)
        elif args.command=='prepare': result=prepare(c,json.loads(args.manifest.read_text()),args.other_reserved_bytes,args.temporary_copies)
        elif args.command=='observe': result=observe(c,args.job,args.phase)
        elif args.command=='release': result=release(c,args.job,args.reason,args.client_stopped)
        elif args.command=='finish': result=finish(c,args.job,args.wait_seconds)
        else:
            result={'status':'snapshot','pending_reserved_bytes':pending_commitments(root,None),
                    'jobs':[dict(job=str(j),movie_id=s.get('movie_id'),title=s.get('title'),status=s['status'],path=str(media_path(s)) if media_path(s) else None) for j,s in states(root)],
                    'free_bytes':shutil.disk_usage(c['download_root']).free}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 2 if result['status'] in {'capacity_shortfall','existing_needs_attention','busy_existing','missing_dependencies'} else 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError,RuntimeError,KeyError,TypeError,subprocess.TimeoutExpired) as e:
        print(json.dumps({'status':'needs_attention','reason':str(e)},ensure_ascii=False))
        raise SystemExit(2)
