#!/usr/bin/env python3
"""Qualify the built bbackup wheel in a rebootable Ubuntu/systemd guest.

The harness uses only a private state directory, a signed Canonical cloud image,
and a task-owned, digest-pinned Alpine/QEMU container. It never touches host
systemd, installs host packages, or removes containers it did not create.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid


REPO = Path(__file__).resolve().parents[1]
DEFAULT_STATE = REPO / ".agents/state/repo-review/systemd"
ALPINE_IMAGE = (
    "docker.io/library/alpine@sha256:"
    "1f3591b8a02ea153f41c5bba878ad477f63ab3d19349762cb77504db02a23e15"
)
ALPINE_PACKAGES = (
    "qemu=10.1.5-r0",
    "qemu-img=10.1.5-r0",
    "qemu-system-x86_64=10.1.5-r0",
    "seabios=1.17.0-r0",
)
UBUNTU_IMAGES = {
    "noble": "ubuntu-24.04-server-cloudimg-amd64.img",
    "resolute": "ubuntu-26.04-server-cloudimg-amd64.img",
}
UBUNTU_PYTHON = {"noble": "/usr/bin/python3.12", "resolute": "/usr/bin/python3.14"}
UBUNTU_KEYRING = Path("/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg")
RESTIC_VERSION = "0.19.1"
RESTIC_FINGERPRINT = "CF8F18F2844575973F79D4E191A6868BD3F7A907"


GUEST_STAGE = r'''#!/usr/bin/python3
import hashlib, json, os, pwd, secrets, shutil, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/var/lib/bbackup-systemd-qualification')
LOG = Path('/var/log/bbackup-systemd-qualification.log')
PHASE = ROOT / 'phase'
UID = 1001
HOME = Path('/home/qualifier')
XDG = HOME / '.config/bbackup'
CFG = XDG / 'config.v3.json'
BIND = XDG / 'bindings.v3.json'
BIN = Path('/opt/bbackup/venv/bin/bbackup')
RESTIC = Path('/usr/local/libexec/bbackup/restic.real')
INPUTS = Path('/mnt/bbackup-inputs')
EXPECTED_CANDIDATE = '@CANDIDATE_SHA256@'
EXPECTED_UBUNTU_RELEASE = '@UBUNTU_RELEASE@'
EXPECTED_PYTHON = '@UBUNTU_PYTHON@'

def log(message):
    line = str(message)
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(line + '\n')
        stream.flush()
        os.fsync(stream.fileno())

def run(argv, *, check=True, timeout=120, env=None, capture=True):
    log(f'COMMAND start: {argv[0]} {" ".join(argv[1:])}')
    result = subprocess.run(argv, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None, timeout=timeout,
                            env=env, check=False)
    clean = (result.stdout or '')[-5000:].strip()
    log(f'COMMAND rc={result.returncode}: {argv[0]} {" ".join(argv[1:])}')
    if clean:
        log(clean)
    if check and result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}): {argv[0]}')
    return result

def as_qualifier(*args, check=True, timeout=120):
    env = {
        'HOME': str(HOME), 'XDG_CONFIG_HOME': str(HOME / '.config'),
        'XDG_STATE_HOME': str(HOME / '.local/state'),
        'XDG_RUNTIME_DIR': f'/run/user/{UID}',
        'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{UID}/bus',
        'PATH': '/usr/local/bin:/opt/bbackup/venv/bin:/usr/bin:/bin',
    }
    result = subprocess.run(
        ['runuser', '-u', 'qualifier', '--', 'env',
         *[f'{key}={value}' for key, value in env.items()],
         str(BIN), 'production', '--config', str(CFG), '--bindings', str(BIND), *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout, check=False)
    log(f'CLI rc={result.returncode}: {args[0]}')
    if result.stdout.strip():
        log(result.stdout[-5000:].strip())
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        envelope = json.loads(lines[-1])
    except (ValueError, TypeError, IndexError):
        raise RuntimeError('production CLI did not return its JSON envelope') from None
    if not isinstance(envelope, dict):
        raise RuntimeError('production CLI returned a non-object envelope')
    if check and (result.returncode != 0 or not envelope.get('success')):
        raise RuntimeError(f'production command failed: {args[0]}')
    return result.returncode, envelope

def userctl(*args, check=True, timeout=60):
    env = {
        'HOME': str(HOME), 'XDG_RUNTIME_DIR': f'/run/user/{UID}',
        'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{UID}/bus',
        'PATH': '/usr/local/bin:/usr/bin:/bin',
    }
    return run(['runuser', '-u', 'qualifier', '--', 'env',
                *[f'{key}={value}' for key, value in env.items()],
                '/usr/bin/systemctl', '--user', *args], check=check, timeout=timeout)

def service_ok(name):
    result = userctl('show', f'bbackup-{name}.service', '--no-pager',
                     '-p', 'Result', '-p', 'ExecMainStatus', '-p', 'ExecMainStartTimestamp',
                     '-p', 'ExecMainExitTimestamp', '-p', 'ActiveState', '-p', 'SubState')
    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return (props.get('Result') == 'success' and props.get('ExecMainStatus') == '0'
            and bool(props.get('ExecMainStartTimestamp'))
            and bool(props.get('ExecMainExitTimestamp'))
            and props.get('ActiveState') == 'inactive' and props.get('SubState') == 'dead')

def timer_triggered(name):
    result = userctl('show', f'bbackup-{name}.timer', '--no-pager', '-p', 'LastTriggerUSec')
    if '=' not in result.stdout:
        return False
    value = result.stdout.strip().split('=', 1)[-1]
    return value not in ('', '0', 'n/a')

def wait_for(predicate, label, timeout=110):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            log(f'PASS {label}')
            return
        time.sleep(2)
    raise RuntimeError(f'timed out waiting for {label}')

def systemctl_root(*args, check=True, timeout=60):
    return run(['/usr/bin/systemctl', *args], check=check, timeout=timeout)

def verify_timer_run(name):
    wait_for(lambda: timer_triggered(name) and service_ok(name),
             f'{name} user timer executed a successful service')

def snapshots(repo, password):
    result = run([str(RESTIC), '-r', str(repo), '--password-file', str(password),
                  '--no-cache', '--json', 'snapshots'], timeout=120)
    data = json.loads(result.stdout)
    return data if isinstance(data, list) else []

def restore_and_check(repo_name, snapshot_id, target, marker, expected):
    _, envelope = as_qualifier('restore', '--repository', repo_name,
                               '--snapshot', snapshot_id, '--target', str(target))
    if not envelope.get('data', {}).get('files_verified'):
        raise RuntimeError('production restore did not report file verification')
    matches = list(target.rglob(marker))
    if len(matches) != 1 or matches[0].read_bytes() != expected:
        raise RuntimeError(f'restored bytes did not match fixture for {marker}')
    return envelope['data']

def write_candidates(catchup_calendar, catchup_epoch):
    policy = {
        'schema_version': 3,
        'repositories': [{'name': 'local', 'role': 'local'},
                         {'name': 'replica', 'role': 'replica'}],
        'sources': [{'name': 'fixture', 'kind': 'files'}],
        'jobs': [
            {'name': 'smoke', 'repository': 'local', 'sources': ['fixture'],
             'replicas': ['replica'], 'schedule': '*-*-* *:*:00', 'timeout_seconds': 900},
            {'name': 'catchup', 'repository': 'local', 'sources': ['fixture'],
             'replicas': ['replica'], 'schedule': catchup_calendar, 'timeout_seconds': 900},
            {'name': 'uncertain', 'repository': 'local', 'sources': ['fixture'],
             'replicas': ['replica'], 'schedule': 'daily', 'timeout_seconds': 900},
        ],
    }
    bindings = {
        'schema_version': 3, 'state_dir': str(ROOT / 'state'),
        'repositories': {
            'local': {'repository': str(ROOT / 'repo-local'), 'password_file': str(ROOT / 'local.password')},
            'replica': {'repository': str(ROOT / 'repo-replica'), 'password_file': str(ROOT / 'replica.password')},
        },
        'source_bindings': {'fixture': {'paths': [str(ROOT / 'source')]}},
    }
    candidate = ROOT / 'policy-candidate.json'
    private_candidate = ROOT / 'bindings-candidate.json'
    candidate.write_text(json.dumps(policy, sort_keys=True), encoding='utf-8')
    private_candidate.write_text(json.dumps(bindings, sort_keys=True), encoding='utf-8')
    (ROOT / 'catchup-epoch').write_text(str(catchup_epoch), encoding='ascii')
    (ROOT / 'catchup-epoch').chmod(0o600)
    for path in (candidate, private_candidate):
        path.chmod(0o600)
        shutil.chown(path, user='qualifier', group='qualifier')

def initialize_guest():
    if not Path('/dev/disk/by-label/BBAINPUTS').exists():
        raise RuntimeError('candidate input ISO was not found')
    INPUTS.mkdir(mode=0o700, exist_ok=True)
    run(['/usr/bin/mount', '-o', 'ro', '/dev/disk/by-label/BBAINPUTS', str(INPUTS)])
    Path('/usr/local/bin').mkdir(mode=0o755, parents=True, exist_ok=True)
    Path('/usr/local/libexec/bbackup').mkdir(mode=0o755, parents=True, exist_ok=True)
    shutil.copyfile(INPUTS / 'uv', '/usr/local/bin/uv')
    shutil.copyfile(INPUTS / 'restic', RESTIC)
    Path('/usr/local/bin/uv').chmod(0o755)
    RESTIC.chmod(0o755)
    wrapper_script()
    expected = EXPECTED_CANDIDATE
    candidates = list(INPUTS.glob('bbackup-*.whl'))
    if len(candidates) != 1:
        raise RuntimeError('input ISO must contain exactly one named candidate wheel')
    candidate = candidates[0]
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError('candidate wheel digest changed between host and guest')
    if not Path(EXPECTED_PYTHON).is_file():
        raise RuntimeError(f'expected selected Ubuntu Python is not present: {EXPECTED_PYTHON}')
    run(['/usr/local/bin/uv', '--version'])
    run([str(RESTIC), 'version'])
    run(['/usr/local/bin/uv', 'venv', '/opt/bbackup/venv', '--python', EXPECTED_PYTHON])
    run(['/usr/local/bin/uv', '--verbose', 'pip', 'install', '--offline', '--no-index',
         '--find-links', str(INPUTS / 'wheelhouse'), '--require-hashes',
         '--python', '/opt/bbackup/venv/bin/python', '-r', str(INPUTS / 'requirements.txt')],
        timeout=300, capture=False)
    run(['/usr/local/bin/uv', 'pip', 'install', '--offline', '--no-index', '--no-deps',
         '--python', '/opt/bbackup/venv/bin/python', str(candidate)], timeout=120)
    run(['/opt/bbackup/venv/bin/bbackup', '--version'])
    shutil.chown('/opt/bbackup', user='root', group='root')
    shutil.chown('/opt/bbackup/venv', user='root', group='root')
    os.chmod('/opt/bbackup', 0o755)
    os.chmod('/opt/bbackup/venv', 0o755)
    user = pwd.getpwnam('qualifier')
    shutil.chown(ROOT, user='qualifier', group='qualifier')
    ROOT.chmod(0o700)
    envdir = HOME / '.config/environment.d'
    envdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.chown(envdir, user='qualifier', group='qualifier')
    (envdir / '10-bbackup-qualification.conf').write_text(
        'PATH=/usr/local/bin:/opt/bbackup/venv/bin:/usr/bin:/bin\n', encoding='ascii')
    (envdir / '10-bbackup-qualification.conf').chmod(0o600)
    shutil.chown(envdir / '10-bbackup-qualification.conf', user='qualifier', group='qualifier')
    run(['/usr/bin/loginctl', 'enable-linger', 'qualifier'])
    systemctl_root('start', f'user@{user.pw_uid}.service')
    wait_for(lambda: Path(f'/run/user/{user.pw_uid}/bus').exists(), 'qualifier user systemd bus')
    (ROOT / 'source').mkdir(mode=0o750)
    (ROOT / 'source' / 'marker-smoke.txt').write_bytes(b'systemd timer capture fixture v1\n')
    (ROOT / 'source' / 'marker-smoke.txt').chmod(0o640)
    shutil.chown(ROOT / 'source' / 'marker-smoke.txt', user='qualifier', group='qualifier')
    local_password = secrets.token_urlsafe(36).encode('ascii') + b'\n'
    replica_password = secrets.token_urlsafe(36).encode('ascii') + b'\n'
    for path, data in ((ROOT / 'local.password', local_password),
                       (ROOT / 'replica.password', replica_password)):
        path.write_bytes(data)
        path.chmod(0o600)
    for name in ('source', 'local.password', 'replica.password'):
        shutil.chown(ROOT / name, user='qualifier', group='qualifier')
    run(['runuser', '-u', 'qualifier', '--', '/usr/bin/test', '-r',
         str(ROOT / 'source' / 'marker-smoke.txt')])
    for name in ('state', 'repo-local', 'repo-replica'):
        path = ROOT / name
        path.mkdir(mode=0o700, exist_ok=True)
        shutil.chown(path, user='qualifier', group='qualifier')
    XDG.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.chown(HOME / '.config', user='qualifier', group='qualifier')
    shutil.chown(XDG, user='qualifier', group='qualifier')
    timestamp = int(time.time()) + 240
    catchup = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    write_candidates(catchup, timestamp)
    _, envelope = as_qualifier('setup', '--configuration-file', str(ROOT / 'policy-candidate.json'),
                               '--bindings-file', str(ROOT / 'bindings-candidate.json'))
    if envelope.get('data', {}).get('configured') is not True:
        raise RuntimeError('schema-three setup did not complete')
    as_qualifier('service', 'enroll')
    as_qualifier('repositories', 'init', '--name', 'local', timeout=300)
    as_qualifier('repositories', 'init', '--name', 'replica', timeout=300)
    rendered = as_qualifier('service', 'render', '--name', 'smoke', '--executable', str(BIN))[1]
    units = rendered.get('data', {})
    timer = units.get('bbackup-smoke.timer', '')
    if 'Persistent=true' not in timer or '*-*-* *:*:00' not in timer:
        raise RuntimeError('rendered smoke timer did not preserve persistent schedule')
    as_qualifier('service', 'install', '--name', 'smoke', '--executable', str(BIN))
    unitdir = HOME / '.config/systemd/user'
    run(['/usr/bin/systemd-analyze', 'verify', str(unitdir / 'bbackup-smoke.service'),
         str(unitdir / 'bbackup-smoke.timer')])
    userctl('set-environment', 'PATH=/usr/local/bin:/opt/bbackup/venv/bin:/usr/bin:/bin')
    as_qualifier('service', 'install', '--name', 'smoke', '--executable', str(BIN), '--activate')
    verify_timer_run('smoke')
    local = snapshots(ROOT / 'repo-local', ROOT / 'local.password')
    replica = snapshots(ROOT / 'repo-replica', ROOT / 'replica.password')
    if not local or not replica:
        raise RuntimeError('timer did not create both local and replica snapshots')
    smoke = [item for item in replica if 'bbackup-job=smoke' in (item.get('tags') or [])]
    if not smoke:
        raise RuntimeError('replica does not contain the timer-created smoke snapshot')
    smoke_id = smoke[-1]['id']
    restore_and_check('replica', smoke_id, ROOT / 'restore-smoke', 'marker-smoke.txt',
                      b'systemd timer capture fixture v1\n')
    log('PASS timer-triggered capture, additional replica copy and production restore byte check')
    as_qualifier('service', 'install', '--name', 'uncertain', '--executable', str(BIN))
    run(['/usr/bin/systemd-analyze', 'verify', str(unitdir / 'bbackup-uncertain.service'),
         str(unitdir / 'bbackup-uncertain.timer')])
    disabled = userctl('is-enabled', 'bbackup-uncertain.timer', check=False)
    if disabled.stdout.strip() != 'disabled':
        raise RuntimeError('service install without activation did not leave timer disabled')
    log('PASS disabled timer stays disabled')
    userctl('disable', '--now', 'bbackup-smoke.timer')
    (ROOT / 'source' / 'marker-catchup.txt').write_bytes(b'persistent timer catch-up fixture v2\n')
    (ROOT / 'source' / 'marker-catchup.txt').chmod(0o640)
    shutil.chown(ROOT / 'source' / 'marker-catchup.txt', user='qualifier', group='qualifier')
    run(['runuser', '-u', 'qualifier', '--', '/usr/bin/test', '-r',
         str(ROOT / 'source' / 'marker-catchup.txt')])
    catchup_units = as_qualifier('service', 'render', '--name', 'catchup', '--executable', str(BIN))[1]['data']
    catchup_timer = catchup_units.get('bbackup-catchup.timer', '')
    if 'Persistent=true' not in catchup_timer:
        raise RuntimeError('rendered catch-up timer omitted Persistent=true')
    as_qualifier('service', 'install', '--name', 'catchup', '--executable', str(BIN))
    run(['/usr/bin/systemd-analyze', 'verify', str(unitdir / 'bbackup-catchup.service'),
         str(unitdir / 'bbackup-catchup.timer')])
    as_qualifier('service', 'install', '--name', 'catchup', '--executable', str(BIN), '--activate')
    userctl('stop', 'bbackup-catchup.timer')
    deadline = timestamp + 25
    while time.time() < deadline:
        time.sleep(min(2, max(0.1, deadline - time.time())))
    if service_ok('catchup'):
        raise RuntimeError('catch-up service ran before guest reboot while its timer was stopped')
    PHASE.write_text('awaiting-reboot\n', encoding='ascii')
    log('REBOOT_1: persistent timer deadline passed while timer stopped; rebooting guest')
    systemctl_root('reboot', timeout=10)

def wrapper_script():
    wrapper = r"""#!/bin/sh
set -eu
action=
for arg in "$@"; do
  [ "$arg" != "--" ] || break
  case "$arg" in backup|copy|init|restore|forget|prune|check|unlock) action="$arg"; break;; esac
done
if [ "$action" = backup ]; then
  count=0
  [ ! -f /var/lib/bbackup-systemd-qualification/backup-count ] || count=$(cat /var/lib/bbackup-systemd-qualification/backup-count)
  printf '%s\n' "$((count + 1))" > /var/lib/bbackup-systemd-qualification/backup-count.tmp
  mv /var/lib/bbackup-systemd-qualification/backup-count.tmp /var/lib/bbackup-systemd-qualification/backup-count
  if [ -e /var/lib/bbackup-systemd-qualification/block-next ]; then
    rm -f /var/lib/bbackup-systemd-qualification/block-next
    : > /var/lib/bbackup-systemd-qualification/block-entered
    while [ -e /var/lib/bbackup-systemd-qualification/block-hold ]; do sleep 1; done
  fi
fi
exec /usr/local/libexec/bbackup/restic.real "$@"
"""
    path = Path('/usr/local/bin/restic')
    path.write_text(wrapper, encoding='utf-8')
    path.chmod(0o755)

def finish_after_reboot():
    user = pwd.getpwnam('qualifier')
    systemctl_root('start', f'user@{user.pw_uid}.service')
    wait_for(lambda: Path(f'/run/user/{user.pw_uid}/bus').exists(), 'qualifier systemd manager after reboot')
    wait_for(lambda: service_ok('catchup'), 'Persistent=true catch-up service after reboot', timeout=150)
    local = snapshots(ROOT / 'repo-local', ROOT / 'local.password')
    replica = snapshots(ROOT / 'repo-replica', ROOT / 'replica.password')
    catchup = [item for item in replica if 'bbackup-job=catchup' in (item.get('tags') or [])]
    if not local or not catchup:
        raise RuntimeError('persistent timer did not finish local and replica capture')
    catchup_id = catchup[-1]['id']
    restore_and_check('replica', catchup_id, ROOT / 'restore-catchup', 'marker-catchup.txt',
                      b'persistent timer catch-up fixture v2\n')
    log('PASS missed persistent timer ran after reboot and replica restore matched bytes')
    wrapper_script()
    (ROOT / 'block-hold').write_text('hold\n', encoding='ascii')
    (ROOT / 'block-next').write_text('block\n', encoding='ascii')
    userctl('start', '--no-block', 'bbackup-uncertain.service')
    wait_for(lambda: (ROOT / 'block-entered').exists(), 'in-flight backup entered controlled restic wrapper', timeout=90)
    count_before = int((ROOT / 'backup-count').read_text().strip())
    code, busy = as_qualifier('jobs', 'run', '--name', 'uncertain', check=False, timeout=60)
    error = (busy.get('errors') or [{}])[0]
    if code != 3 or error.get('code') != 'busy':
        raise RuntimeError('overlapping run was not refused as busy')
    if int((ROOT / 'backup-count').read_text().strip()) != count_before:
        raise RuntimeError('overlapping run dispatched a second restic backup')
    log('PASS overlapping job run refused as busy without second dispatch')
    PHASE.write_text('awaiting-uncertain-reboot\n', encoding='ascii')
    log('REBOOT_2: immediate guest reset interrupts the in-flight mutating service')
    os.sync()
    with Path('/proc/sysrq-trigger').open('w', encoding='ascii') as trigger:
        trigger.write('b')
    raise RuntimeError('immediate guest reset did not occur')

def complete_after_uncertain_reboot():
    user = pwd.getpwnam('qualifier')
    systemctl_root('start', f'user@{user.pw_uid}.service')
    wait_for(lambda: Path(f'/run/user/{user.pw_uid}/bus').exists(), 'qualifier systemd manager after interrupted reboot')
    userctl('disable', '--now', 'bbackup-catchup.timer', check=False)
    userctl('disable', '--now', 'bbackup-smoke.timer', check=False)
    count_before = int((ROOT / 'backup-count').read_text().strip())
    code, outcome = as_qualifier('jobs', 'run', '--name', 'uncertain', check=False, timeout=60)
    error = (outcome.get('errors') or [{}])[0]
    if code != 3 or error.get('code') != 'requires_reconciliation':
        raise RuntimeError('interrupted operation was not held for explicit reconciliation')
    count_after = int((ROOT / 'backup-count').read_text().strip())
    if count_after != count_before:
        raise RuntimeError('uncertain operation retry dispatched restic automatically')
    events = as_qualifier('runs', 'list')[1].get('data', [])
    if not any(event.get('kind') == 'uncertain' for event in events):
        raise RuntimeError('operation ledger did not retain an uncertain event after reboot')
    timer = userctl('is-enabled', 'bbackup-uncertain.timer', check=False).stdout.strip()
    if timer != 'disabled':
        raise RuntimeError('uncertain job timer was enabled after reboot')
    result = {
        'schema_version': 1,
        'ubuntu_codename': EXPECTED_UBUNTU_RELEASE,
        'os': Path('/etc/os-release').read_text(encoding='utf-8').splitlines()[0],
        'systemd': run(['/usr/bin/systemctl', '--version']).stdout.splitlines()[0],
        'candidate_sha256': EXPECTED_CANDIDATE,
        'restic': run([str(RESTIC), 'version']).stdout.strip(),
        'qualifier_uid': user.pw_uid,
        'boot_ids': (ROOT / 'boot-ids').read_text(encoding='ascii').splitlines(),
        'checks': {
            'schema3_setup_and_enrollment': True,
            'systemd_analyze_verify': True,
            'enabled_short_timer_capture_replica_restore': True,
            'restore_byte_match': True,
            'persistent_missed_timer_after_reboot': True,
            'disabled_timer_stays_disabled': True,
            'overlap_refused_busy': True,
            'inflight_reboot_left_uncertain': True,
            'uncertain_retry_not_dispatched': True,
            'actual_guest_reboots': len(set((ROOT / 'boot-ids').read_text(encoding='ascii').splitlines())) >= 3,
        },
    }
    output = ROOT / 'qualification.json'
    output.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    output.chmod(0o600)
    log('QUALIFICATION_RESULT ' + json.dumps(result, sort_keys=True))
    log('POWER_OFF: qualification finished')
    systemctl_root('poweroff', timeout=10)

def main():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    phase = PHASE.read_text(encoding='ascii').strip() if PHASE.exists() else 'initial'
    if phase != 'initial':
        if not RESTIC.is_file() or not Path('/usr/local/bin/uv').is_file():
            raise RuntimeError('task-owned input binaries are missing after guest reboot')
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    boot_ids = ROOT / 'boot-ids'
    with boot_ids.open('a', encoding='ascii') as stream:
        stream.write(boot_id + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    boot_ids.chmod(0o600)
    log(f'BOOT_PHASE {phase} boot_id={boot_id}')
    if phase == 'initial':
        initialize_guest()
    elif phase == 'awaiting-reboot':
        finish_after_reboot()
        # finish_after_reboot persists this phase before requesting reboot.
    elif phase == 'awaiting-uncertain-reboot':
        complete_after_uncertain_reboot()
    else:
        raise RuntimeError('unexpected persisted qualification phase')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        log(f'FAIL {type(exc).__name__}: {exc}')
        (ROOT / 'qualification-failed').write_text(f'{type(exc).__name__}: {exc}\n', encoding='utf-8')
        (ROOT / 'qualification-failed').chmod(0o600)
        systemctl_root('poweroff', check=False, timeout=10)
        raise
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _no_symlink_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"symlink path component refused: {current}")
    return path


def _regular_file(path: Path, label: str) -> Path:
    path = _no_symlink_path(path)
    info = path.lstat()
    if not path.is_file() or not info.st_mode & 0o170000 == 0o100000:
        raise ValueError(f"{label} must be a regular file")
    return path


def _private_directory(path: Path) -> Path:
    path = _no_symlink_path(path)
    if path.exists():
        info = path.stat()
        if not path.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("state root must be an owned private directory")
    else:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(path, 0o700)
    return path


def _run(argv: list[str], *, timeout: int = 120, capture: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            timeout=timeout,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        raise RuntimeError(f"timed out: {argv[0]} ({output[-1000:]})") from None


def _download(url: str, destination: Path) -> None:
    if destination.exists():
        _regular_file(destination, "downloaded input")
        return
    result = _run(["curl", "-fLsS", "--retry", "2", "--connect-timeout", "20",
                   "--max-time", "900", "-o", str(destination), url], timeout=920)
    if result.returncode:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"download failed: {url}")
    os.chmod(destination, 0o600)


def _ensure_verified_inputs(state: Path, codename: str) -> tuple[Path, Path]:
    downloads = _no_symlink_path(state / "downloads")
    if downloads.exists():
        info = downloads.lstat()
        if not downloads.is_dir() or info.st_uid != os.geteuid():
            raise ValueError("download directory must be an owned real directory")
        for entry in downloads.iterdir():
            entry_info = entry.lstat()
            if not entry.is_file() or entry.is_symlink() or entry_info.st_uid != os.geteuid():
                raise ValueError("download directory contains an unsafe input")
            entry.chmod(0o700 if entry.name == "restic" else 0o600)
        downloads.chmod(0o700)
    else:
        downloads.mkdir(mode=0o700)
    downloads = _private_directory(downloads)
    image_name = UBUNTU_IMAGES[codename]
    base_url = f"https://cloud-images.ubuntu.com/releases/{codename}/release"
    if not UBUNTU_KEYRING.is_file():
        raise RuntimeError(f"Canonical image verification keyring is missing: {UBUNTU_KEYRING}")
    sums = downloads / f"{codename}-SHA256SUMS"
    sig = downloads / f"{codename}-SHA256SUMS.gpg"
    image = downloads / image_name
    _download(f"{base_url}/SHA256SUMS", sums)
    _download(f"{base_url}/SHA256SUMS.gpg", sig)
    verified = _run(["gpgv", "--keyring", str(UBUNTU_KEYRING), str(sig), str(sums)])
    if verified.returncode:
        raise RuntimeError("Canonical SHA256SUMS signature verification failed")
    line = next((row for row in sums.read_text(encoding="ascii").splitlines()
                 if row.split()[-1].lstrip("*") == image_name), None)
    if line is None:
        raise RuntimeError("signed Canonical checksum manifest omits the selected cloud image")
    expected = line.split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("Canonical image checksum line is malformed")
    if not image.exists():
        _download(f"{base_url}/{image_name}", image)
    actual = _sha256(_regular_file(image, "Ubuntu cloud image"))
    if actual != expected:
        raise RuntimeError("Ubuntu cloud image checksum does not match signed manifest")
    os.chmod(image, 0o400)
    return image, downloads


def _copy_regular(source: Path, target: Path) -> None:
    source = _regular_file(source, "input")
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)


def _write_iso(stage: Path, directory: Path, filename: str, label: str) -> Path:
    target = stage / filename
    result = _run(["genisoimage", "-quiet", "-output", str(target), "-volid", label,
                   "-joliet", "-rock", str(directory)])
    if result.returncode:
        raise RuntimeError(f"could not create {label} ISO")
    os.chmod(target, 0o400)
    return target


def _create_seed(stage: Path, candidate_digest: str, codename: str) -> Path:
    seed_dir = stage / "seed"
    seed_dir.mkdir(mode=0o700)
    guest = (GUEST_STAGE.replace("@CANDIDATE_SHA256@", candidate_digest)
             .replace("@UBUNTU_RELEASE@", codename)
             .replace("@UBUNTU_PYTHON@", UBUNTU_PYTHON[codename]))
    service = """[Unit]\nDescription=Task-owned bbackup systemd qualification\nAfter=network-online.target systemd-logind.service\nWants=network-online.target systemd-logind.service\n\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/bbackup-systemd-qualification.py\nStandardOutput=append:/dev/ttyS0\nStandardError=inherit\nTimeoutStartSec=infinity\n\n[Install]\nWantedBy=multi-user.target\n"""
    user = (
        "#cloud-config\n"
        "users:\n"
        "  - default\n"
        "  - name: qualifier\n"
        "    uid: 1001\n"
        "    shell: /bin/bash\n"
        "    lock_passwd: true\n"
        "write_files:\n"
        "  - path: /usr/local/sbin/bbackup-systemd-qualification.py\n"
        "    owner: root:root\n"
        "    permissions: '0700'\n"
        "    encoding: b64\n"
        f"    content: {base64.b64encode(guest.encode()).decode()}\n"
        "  - path: /etc/systemd/system/bbackup-systemd-qualification.service\n"
        "    owner: root:root\n"
        "    permissions: '0644'\n"
        "    content: |\n"
        + "".join("      " + line + "\n" for line in service.splitlines())
        + "runcmd:\n"
        "  - [systemctl, mask, --now, serial-getty@ttyS0.service]\n"
        "  - [systemctl, enable, bbackup-systemd-qualification.service]\n"
        "  - [systemctl, start, --no-block, bbackup-systemd-qualification.service]\n"
    )
    (seed_dir / "user-data").write_text(user, encoding="utf-8")
    (seed_dir / "meta-data").write_text(
        f"instance-id: bbackup-systemd-{uuid.uuid4().hex}\nlocal-hostname: bbackup-systemd\n",
        encoding="ascii",
    )
    for child in seed_dir.iterdir():
        child.chmod(0o600)
    return _write_iso(stage, seed_dir, "seed.iso", "cidata")


def _container_image() -> None:
    inspect = ["docker", "image", "inspect", "--format",
               "{{.Os}}/{{.Architecture}} {{json .RepoDigests}}", ALPINE_IMAGE]
    inspected = _run(inspect)
    if inspected.returncode:
        pulled = _run(["docker", "pull", "--platform", "linux/amd64", ALPINE_IMAGE], timeout=600)
        if pulled.returncode:
            raise RuntimeError("failed to pull pinned official Alpine QEMU base image")
        inspected = _run(inspect)
    try:
        platform, digest_data = inspected.stdout.strip().split(" ", 1)
        repo_digests = json.loads(digest_data)
    except (ValueError, json.JSONDecodeError):
        raise RuntimeError("pinned Alpine image identity could not be inspected") from None
    expected_digest = ALPINE_IMAGE.split("@", 1)[1]
    if (inspected.returncode or platform != "linux/amd64" or
            not isinstance(repo_digests, list) or
            not any(item.split("@", 1)[-1] == expected_digest for item in repo_digests)):
        raise RuntimeError("pinned Alpine image is not the expected linux/amd64 image")


def _docker_qualification(stage: Path, image: Path, seed: Path, inputs_iso: Path,
                           timeout: int) -> str:
    name = f"bbackup-systemd-qualification-{uuid.uuid4().hex[:12]}"
    existing = _run(["docker", "container", "inspect", name])
    if existing.returncode == 0:
        raise RuntimeError("task container name collision; refuse to touch it")
    docker_command = [
        "docker", "run", "--rm", "--name", name, "--platform=linux/amd64",
        "--device=/dev/kvm:/dev/kvm:rwm",
        "--mount", f"type=bind,src={stage},dst=/work",
        "--mount", f"type=bind,src={image},dst=/ubuntu.img,readonly",
        ALPINE_IMAGE,
        "sh", "-ec",
        "apk update >/dev/null; "
        "apk add --no-cache qemu=10.1.5-r0 qemu-img=10.1.5-r0 "
        "qemu-system-x86_64=10.1.5-r0 seabios=1.17.0-r0 >/dev/null; "
        "apk list -I > /work/apk-installed.txt; "
        "for p in qemu-10.1.5-r0 qemu-img-10.1.5-r0 qemu-system-x86_64-10.1.5-r0 seabios-1.17.0-r0; do "
        "grep -F \"$p \" /work/apk-installed.txt >/dev/null || { echo \"missing pinned $p\"; exit 21; }; done; "
        "qemu-img create -q -f qcow2 -F qcow2 -b /ubuntu.img /work/guest.qcow2 16G; "
        "exec qemu-system-x86_64 -machine q35,accel=kvm -cpu host -smp 2 -m 4096 "
        "-drive file=/work/guest.qcow2,if=virtio,format=qcow2 "
        "-drive file=/work/seed.iso,media=cdrom,readonly=on "
        "-drive file=/work/inputs.iso,media=cdrom,readonly=on "
        "-netdev user,id=net0 -device virtio-net-pci,netdev=net0 "
        "-display none -serial stdio -monitor none",
    ]
    try:
        result = _run(docker_command, timeout=timeout)
        output = result.stdout or ""
        serial = stage / "serial.log"
        serial.write_text(output, encoding="utf-8", errors="replace")
        serial.chmod(0o600)
        if result.returncode:
            raise RuntimeError(f"QEMU container exited with status {result.returncode}; see {serial}")
        return output
    except RuntimeError:
        # The exact random name was verified absent and is owned by this run.
        inspected = _run(["docker", "container", "inspect", name])
        if inspected.returncode == 0:
            _run(["docker", "stop", "--time", "10", name], timeout=30)
            _run(["docker", "rm", name], timeout=30)
        raise


def _prepare_inputs(stage: Path, artifact: Path, wheelhouse: Path, requirements: Path,
                    restic: Path, uv: Path) -> tuple[Path, dict[str, str]]:
    input_dir = stage / "inputs"
    wheels = input_dir / "wheelhouse"
    wheels.mkdir(mode=0o700, parents=True)
    sources = {artifact.name: artifact, "requirements.txt": requirements,
               "restic": restic, "uv": uv}
    digests = {}
    for name, source in sources.items():
        checked = _regular_file(source, name)
        target = input_dir / name
        _copy_regular(checked, target)
        digests[name] = _sha256(target)
    wheelhouse = _no_symlink_path(wheelhouse)
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ValueError("wheelhouse must be a real directory")
    entries = sorted(wheelhouse.iterdir())
    if not entries or any(path.is_symlink() or not path.is_file() or path.suffix != ".whl" for path in entries):
        raise ValueError("wheelhouse must contain only regular wheel files")
    for source in entries:
        target = wheels / source.name
        shutil.copyfile(source, target)
        target.chmod(0o600)
    return _write_iso(stage, input_dir, "inputs.iso", "BBAINPUTS"), digests


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True, help="built candidate .whl")
    parser.add_argument("--wheel-sha256", "--artifact-sha256", dest="wheel_sha256", required=True,
                        help="expected candidate wheel SHA-256")
    parser.add_argument("--ubuntu-codename", choices=tuple(UBUNTU_IMAGES), default="noble",
                        help="official Canonical cloud image release to boot")
    parser.add_argument("--wheelhouse", type=Path, required=True, help="audited locked runtime wheels")
    parser.add_argument("--requirements", type=Path, required=True, help="hash-locked exported runtime requirements")
    parser.add_argument("--restic", type=Path, required=True, help="signed official restic binary for guest storage operations")
    parser.add_argument("--uv", type=Path, required=True, help="existing uv executable used to install locked wheels inside guest")
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE, help="private ignored task state root")
    parser.add_argument("--timeout-seconds", type=int, default=1500, help="maximum guest runtime")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.wheel_sha256):
        raise ValueError("candidate SHA-256 must be 64 lowercase hex characters")
    artifact = _regular_file(args.artifact, "candidate wheel")
    if artifact.suffix != ".whl" or _sha256(artifact) != args.wheel_sha256:
        raise ValueError("candidate wheel extension or digest does not match expected artifact")
    if not 600 <= args.timeout_seconds <= 3600:
        raise ValueError("timeout must be between 600 and 3600 seconds")
    state = _private_directory(args.state_root)
    image, downloads = _ensure_verified_inputs(state, args.ubuntu_codename)
    restic = _regular_file(args.restic, "restic binary")
    uv = _regular_file(args.uv, "uv binary")
    if _sha256(restic) != "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639":
        raise ValueError("restic binary differs from the signature-verified 0.19.1 release")
    stage = state / f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    stage.mkdir(mode=0o700)
    os.chmod(stage, 0o700)
    artifact_copy = stage / "candidate.whl"
    _copy_regular(artifact, artifact_copy)
    seed = _create_seed(stage, args.wheel_sha256, args.ubuntu_codename)
    inputs_iso, input_digests = _prepare_inputs(stage, artifact, args.wheelhouse,
                                                args.requirements, restic, uv)
    _container_image()
    start = time.monotonic()
    serial = _docker_qualification(stage, image, seed, inputs_iso, args.timeout_seconds)
    elapsed = round(time.monotonic() - start, 1)
    result_marker = re.search(r"QUALIFICATION_RESULT (\{.*\})", serial)
    if result_marker is None:
        failure_marker = re.search(r"FAIL [^\r\n]+", serial)
        reason = failure_marker.group(0) if failure_marker else "guest did not emit final qualification evidence"
        raise RuntimeError(f"systemd guest qualification incomplete: {reason}; artifacts: {stage}")
    evidence = json.loads(result_marker.group(1))
    if evidence.get("candidate_sha256") != args.wheel_sha256 or not all(evidence.get("checks", {}).values()):
        raise RuntimeError(f"guest evidence is incomplete or for a different wheel; artifacts: {stage}")
    if len(set(evidence.get("boot_ids", []))) < 3:
        raise RuntimeError(f"qualification did not record three guest boots; artifacts: {stage}")
    private_evidence = {
        "schema_version": 1,
        "candidate": {"path_label": artifact.name, "sha256": args.wheel_sha256},
        "guest_image": {"distribution": f"Ubuntu {args.ubuntu_codename}", "codename": args.ubuntu_codename,
                        "sha256": _sha256(image), "signed_manifest": "Canonical gpgv valid"},
        "qemu_toolchain": {"image": ALPINE_IMAGE, "packages": list(ALPINE_PACKAGES),
                           "verified": "image digest and installed package versions checked at runtime"},
        "input_digests": input_digests,
        "elapsed_seconds": elapsed,
        "guest": evidence,
        "evidence_files": ["serial.log", "apk-installed.txt", "guest.qcow2", "qualification.json"],
    }
    evidence_path = stage / "host-evidence.json"
    evidence_path.write_text(json.dumps(private_evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    evidence_path.chmod(0o600)
    print(json.dumps({"success": True, "candidate_sha256": args.wheel_sha256,
                      "guest": evidence.get("os"), "checks": evidence["checks"],
                      "elapsed_seconds": elapsed, "private_evidence": str(evidence_path)},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"qualification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
