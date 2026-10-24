"""Incremental, guarded installation on the inspected Lighthouse only.

Run tests before invoking this as root. No new cloud resources or dependency
installation. Keeps intake disabled and the existing Bilibili process running.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import time

PLUGINS=Path('/srv/maibot/plugins')
PAID=Path('/srv/maibot/paid')
CORE=Path('/srv/maibot/unified')
PYTHON='/opt/maibot/tools/maibot/bin/python'
UNIT=Path('/etc/systemd/system/paidtasks.service')
CURRENT=Path('/opt/maibot/paidtasks-current')

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def command(*args):
    result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode:raise RuntimeError('command_failed:'+Path(args[0]).name)
    return result.stdout.decode().strip()

def atomic(path, data, mode=0o644):
    temp=path.with_name(path.name+'.paid-tmp')
    with temp.open('wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    temp.chmod(mode);os.replace(temp,path)

def apply(bundle, credentials=None):
    if os.geteuid()!=0:raise RuntimeError('root_required')
    manifest=json.loads((bundle/'build/integration/manifest.json').read_text())
    user=pwd.getpwnam('maibot')
    if command('systemctl','is-active','maibot.service')!='active':raise RuntimeError('baseline_not_running')
    if UNIT.exists() or CURRENT.exists():raise RuntimeError('existing_paid_installation_needs_reviewed_upgrade')
    if not PAID.exists() and not credentials:raise RuntimeError('credentials_file_required')
    for name,record in manifest.items():
        relative=name.replace('maibot_unified_core/','unified_core/',1)
        target=PLUGINS/relative
        original=record['before_sha256']
        if original is None:
            if target.exists():raise RuntimeError('new_file_already_exists:'+relative)
        elif not target.exists() or sha(target)!=original:raise RuntimeError('baseline_changed:'+relative)
        if sha(bundle/'build/integration'/name)!=record['after_sha256']:raise RuntimeError('bundle_integrity')
    # Confirm runtime imports and CJK renderer before touching the running bot.
    env={**os.environ,'PYTHONPATH':str(bundle),'PYTHONDONTWRITEBYTECODE':'1'}
    test=subprocess.run([PYTHON,'-m','unittest','discover','-s',str(bundle/'tests')],env=env,cwd=bundle,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if test.returncode:raise RuntimeError('server_tests_failed_no_changes_applied')
    stamp=time.strftime('%Y%m%d-%H%M%S',time.gmtime())
    backup=Path('/srv/maibot/paid-backups')/stamp;backup.mkdir(parents=True,mode=0o700)
    backup.parent.chmod(0o700)
    originals={}
    for name,record in manifest.items():
        relative=name.replace('maibot_unified_core/','unified_core/',1)
        target=PLUGINS/relative
        if target.exists():
            saved=backup/'plugins'/relative;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
        originals[str(target)]={'existed':target.exists(),'after_sha256':record['after_sha256'],
            'backup':str(backup/'plugins'/relative)}
    shutil.copy2(CORE/'policy.json',backup/'unified-policy.json')
    for source,name in ((CORE/'state.sqlite','unified-state.sqlite'),(PAID/'state.sqlite','paid-state.sqlite')):
        if source.exists():
            with sqlite3.connect(str(source)) as src,sqlite3.connect(str(backup/name)) as dst:src.backup(dst)
    record={'created_at':time.time(),'originals':originals,'service_was_active':True,
            'prior_paid_policy':(PAID/'policy.json').exists(),'prior_bridge_flag':json.loads((CORE/'policy.json').read_text()).get('paid_tasks_bridge_enabled')}
    (backup/'rollback.json').write_text(json.dumps(record,indent=2))
    release=Path('/opt/maibot/paidtasks-releases')/stamp;release.mkdir(parents=True)
    shutil.copytree(bundle/'paidtasks',release/'paidtasks',ignore=shutil.ignore_patterns('__pycache__'))
    if (bundle/'assets').exists():shutil.copytree(bundle/'assets',release/'assets')
    PAID.mkdir(mode=0o700,exist_ok=True);PAID.chmod(0o700);os.chown(PAID,user.pw_uid,user.pw_gid)
    if not (PAID/'afdian.env').exists():
        if not credentials:raise RuntimeError('credentials_file_required')
        atomic(PAID/'afdian.env',credentials.read_bytes(),0o600)
    if not (PAID/'policy.json').exists():
        policy=json.loads((bundle/'config.example.json').read_text())
    else:policy=json.loads((PAID/'policy.json').read_text())
    policy.update(intake_enabled=False,model_tasks_enabled=False,promotion_enabled=False,owner_notification_verified=False)
    atomic(PAID/'policy.json',json.dumps(policy,ensure_ascii=False,indent=2).encode(),0o600)
    os.chown(PAID/'policy.json',user.pw_uid,user.pw_gid)
    # Files have been validated and backup is complete. This is the only bot restart.
    try:
        command('systemctl','stop','maibot.service')
        for name in manifest:
            target=PLUGINS/name.replace('maibot_unified_core/','unified_core/',1)
            atomic(target,(bundle/'build/integration'/name).read_bytes())
            os.chown(target,user.pw_uid,user.pw_gid)
        policy_core=json.loads((CORE/'policy.json').read_text());policy_core['paid_tasks_bridge_enabled']=True
        atomic(CORE/'policy.json',json.dumps(policy_core,ensure_ascii=False,indent=2).encode(),0o600)
        os.chown(CORE/'policy.json',user.pw_uid,user.pw_gid)
        CURRENT.symlink_to(release,target_is_directory=True)
        atomic(UNIT,(bundle/'deploy/paidtasks.service').read_bytes())
        command('systemctl','daemon-reload');command('systemctl','start','maibot.service')
        command('systemctl','enable','--now','paidtasks.service')
        sys.path.insert(0,str(release))
        from paidtasks.runtime import rpc
        for _ in range(20):
            try:
                rpc({'op':'status'},path=PAID/'paid.sock',timeout=2)
                rpc({'op':'paid_info'},path=CORE/'core.sock',timeout=2)
                break
            except (OSError,ValueError):time.sleep(1)
        else:raise RuntimeError('service_health_unconfirmed')
        command('systemctl','is-active','maibot.service','paidtasks.service','bilibot.service')
    except Exception:
        # Preserve the ledger; roll back only this installation's code and flags.
        rollback(backup)
        raise RuntimeError('installation_failed_original_code_restored') from None
    return {'installed':True,'intake_enabled':False,'backup':str(backup),'real_payment_verified':False}

def rollback(backup):
    if os.geteuid()!=0:raise RuntimeError('root_required')
    record=json.loads((backup/'rollback.json').read_text())
    for target,info in record['originals'].items():
        path=Path(target)
        if path.exists() and sha(path)!=info['after_sha256']:
            if not info['existed'] or sha(path)!=sha(Path(info['backup'])):raise RuntimeError('rollback_conflicts_with_later_change')
    subprocess.run(['systemctl','disable','--now','paidtasks.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    command('systemctl','stop','maibot.service')
    for target,info in record['originals'].items():
        path=Path(target)
        if info['existed']:shutil.copy2(info['backup'],path)
        else:path.unlink(missing_ok=True)
    policy=json.loads((CORE/'policy.json').read_text())
    if record['prior_bridge_flag'] is None:policy.pop('paid_tasks_bridge_enabled',None)
    else:policy['paid_tasks_bridge_enabled']=record['prior_bridge_flag']
    atomic(CORE/'policy.json',json.dumps(policy,ensure_ascii=False,indent=2).encode(),0o600)
    user=pwd.getpwnam('maibot');os.chown(CORE/'policy.json',user.pw_uid,user.pw_gid)
    UNIT.unlink(missing_ok=True)
    if CURRENT.is_symlink():CURRENT.unlink()
    command('systemctl','daemon-reload');command('systemctl','start','maibot.service')
    return {'rolled_back':True,'payment_database_preserved':True}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path);p.add_argument('--credentials',type=Path);p.add_argument('--rollback',type=Path)
    a=p.parse_args()
    try:print(json.dumps(rollback(a.rollback) if a.rollback else apply(a.bundle.resolve(),a.credentials)))
    except Exception as exc:
        # Exception text is code-controlled and contains no credential material.
        print(json.dumps({'ok':False,'reason':str(exc)}));raise SystemExit(1)
