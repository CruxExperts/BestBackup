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


# This second guest program deliberately runs through the normal production CLI
# and guest inventory.  It is opt-in so the established reboot/timer drill
# above retains its exact default behavior.
COVERED_ROOT_GUEST_STAGE = r"""#!/usr/bin/python3
import base64, ctypes, ctypes.util, grp, hashlib, json, os, pwd, shutil, signal
import sqlite3, stat, subprocess, sys, time, uuid
from pathlib import Path

LOG = Path('/var/log/bbackup-covered-root-qualification.log')
INPUTS = Path('/mnt/bbackup-inputs')
RECOVERY = Path('/mnt/bbackup-recovery')
MATERIAL = RECOVERY / 'material'
REPOSITORY = RECOVERY / 'repository'
PASSWORD = MATERIAL / 'repository.password'
CONFIG_DIR = Path('/etc/bbackup/covered-root')
CONFIG = CONFIG_DIR / 'config.v3.json'
BINDINGS = CONFIG_DIR / 'bindings.v3.json'
DECLARATION = MATERIAL / 'coverage-declaration.json'
STATE = Path('/var/lib/bbackup-covered-root-state')
DATA_ROOT = Path('/var/lib/bbackup-covered-root')
FILES = DATA_ROOT / 'files'
DATABASE = DATA_ROOT / 'app.sqlite'
APP_NAME = 'coverageapp'
APP_UNIT = 'bbackup-covered-fixture.service'
JOB_NAME = 'covered'
BIN = Path('/opt/bbackup/venv/bin/bbackup')
RESTIC = Path('/usr/local/libexec/bbackup/restic.real')
EXPECTED_CANDIDATE = '@CANDIDATE_SHA256@'
EXPECTED_RELEASE = '@UBUNTU_RELEASE@'
EXPECTED_PYTHON = '@UBUNTU_PYTHON@'
RESTORE_TARGET = Path('/var/tmp/bbackup-covered-root-restore')
REHEARSAL_ROOT = Path('/var/tmp/bbackup-covered-root-rehearsal')
RESTORE_DIAGNOSTICS = Path('/var/log/bbackup-covered-root-restore-diagnostics')
RESTORE_DIAGNOSTIC_SITE = r'''import os
import sys
import traceback
from bbackup import production_cli

original_emit = production_cli._emit

def record_restore_exception(command, data=None, error=None):
    if command == 'restore' and isinstance(error, dict) and error.get('code') == 'operation_failed':
        details = sys.exc_info()
        if details[1] is not None:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
            try:
                descriptor = os.open(
                    '/var/log/bbackup-covered-root-restore-diagnostics/exception.txt',
                    flags, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    traceback.print_exception(*details, file=stream)
            except OSError:
                pass
    return original_emit(command, data=data, error=error)

production_cli._emit = record_restore_exception
'''


def log(message):
    line = str(message)
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(line + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    print(line, flush=True)


def run(argv, *, check=True, timeout=120, capture=True, env=None):
    log(f'COMMAND start: {argv[0]} {" ".join(argv[1:])}')
    result = subprocess.run(argv, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None, timeout=timeout,
        env=env, check=False)
    output = (result.stdout or '').strip()
    log(f'COMMAND rc={result.returncode}: {argv[0]} {" ".join(argv[1:])}')
    if output:
        log(output[-3000:])
    if check and result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}): {argv[0]}')
    return result


def rootctl(*args, check=True, timeout=90):
    return run(['/usr/bin/systemctl', *args], check=check, timeout=timeout)


def production(*args, check=True, timeout=180):
    env = None
    if args and args[0] == 'restore':
        RESTORE_DIAGNOSTICS.mkdir(mode=0o700, exist_ok=True)
        RESTORE_DIAGNOSTICS.chmod(0o700)
        site = RESTORE_DIAGNOSTICS / 'sitecustomize.py'
        site.write_text(RESTORE_DIAGNOSTIC_SITE, encoding='utf-8')
        site.chmod(0o600)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(RESTORE_DIAGNOSTICS) + (
            os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    result = subprocess.run([str(BIN), 'production', '--config', str(CONFIG),
        '--bindings', str(BINDINGS), *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout, check=False, env=env)
    log(f'PRODUCTION rc={result.returncode}: {args[0]}')
    if result.stdout.strip():
        log(result.stdout[-4000:].strip())
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


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n',
                    encoding='utf-8')
    path.chmod(0o600)
    return path


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def machine_digest():
    machine = Path('/etc/machine-id').read_text(encoding='ascii').strip().lower()
    if len(machine) != 32 or not machine or set(machine) == {'0'}:
        raise RuntimeError('guest machine identity is unavailable')
    return sha256_bytes(machine.encode('ascii'))


def mount_inputs():
    INPUTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not INPUTS.is_mount():
        run(['/usr/bin/mount', '-o', 'ro', '/dev/disk/by-label/BBAINPUTS', str(INPUTS)])
    candidates = list(INPUTS.glob('bbackup-*.whl'))
    if len(candidates) != 1 or sha256_file(candidates[0]) != EXPECTED_CANDIDATE:
        raise RuntimeError('candidate input wheel or digest is invalid')
    return candidates[0]


def mount_recovery(role):
    RECOVERY.mkdir(mode=0o700, parents=True, exist_ok=True)
    device = Path('/dev/vdb')
    if not device.exists():
        raise RuntimeError('dedicated repository/recovery disk is missing')
    label = run(['/usr/sbin/blkid', '-s', 'LABEL', '-o', 'value', str(device)],
                check=False).stdout.strip()
    if role == 'source' and not label:
        run(['/usr/sbin/mkfs.ext4', '-F', '-L', 'BBARECOVERY', str(device)], timeout=120)
    if role == 'replacement' and label != 'BBARECOVERY':
        raise RuntimeError('replacement did not receive the source recovery disk')
    if not RECOVERY.is_mount():
        run(['/usr/bin/mount', '-o', 'noatime', '/dev/disk/by-label/BBARECOVERY', str(RECOVERY)])
    (RECOVERY / 'material').mkdir(mode=0o700, exist_ok=True)
    (RECOVERY / 'repository').mkdir(mode=0o700, exist_ok=True)
    os.chmod(RECOVERY / 'material', 0o700)
    os.chmod(RECOVERY / 'repository', 0o700)


def install_candidate(candidate):
    Path('/usr/local/bin').mkdir(mode=0o755, exist_ok=True)
    Path('/usr/local/libexec/bbackup').mkdir(mode=0o755, parents=True, exist_ok=True)
    shutil.copyfile(INPUTS / 'uv', '/usr/local/bin/uv')
    shutil.copyfile(INPUTS / 'restic', RESTIC)
    Path('/usr/local/bin/uv').chmod(0o755)
    RESTIC.chmod(0o755)
    wrapper_script()
    if not Path(EXPECTED_PYTHON).is_file():
        raise RuntimeError('selected Ubuntu Python interpreter is unavailable')
    run(['/usr/local/bin/uv', 'venv', '/opt/bbackup/venv', '--python', EXPECTED_PYTHON])
    run(['/usr/local/bin/uv', 'pip', 'install', '--offline', '--no-index',
         '--find-links', str(INPUTS / 'wheelhouse'), '--require-hashes',
         '--python', '/opt/bbackup/venv/bin/python', '-r', str(INPUTS / 'requirements.txt')],
        timeout=300)
    run(['/usr/local/bin/uv', 'pip', 'install', '--offline', '--no-index', '--no-deps',
         '--python', '/opt/bbackup/venv/bin/python', str(candidate)], timeout=120)
    run([str(BIN), '--version'])
    for path in (Path('/opt/bbackup'), Path('/opt/bbackup/venv')):
        shutil.chown(path, user='root', group='root')
        path.chmod(0o755)
    user_data = Path('/usr/local/bin')
    user_data.chmod(0o755)


def wrapper_script():
    wrapper = r'''#!/bin/sh
set -u
for argument do
    if [ "$argument" = restore ]; then
        umask 077
        diagnostic=/var/log/bbackup-covered-root-restore-diagnostics/restic.stderr
        status=/var/log/bbackup-covered-root-restore-diagnostics/restic.status
        printf 'started\n' > "$status"
        /usr/local/libexec/bbackup/restic.real "$@" 2> "$diagnostic"
        code=$?
        cat "$diagnostic" >&2
        printf 'exit_code=%s\n' "$code" > "$status"
        exit "$code"
    fi
done
exec /usr/local/libexec/bbackup/restic.real "$@"
'''
    path = Path('/usr/local/bin/restic')
    path.write_text(wrapper, encoding='utf-8')
    path.chmod(0o755)


def app_uid_gid():
    used_uids = {item.pw_uid for item in pwd.getpwall()}
    used_gids = {item.gr_gid for item in grp.getgrall()}
    for value in range(52000, 53000):
        if value not in used_uids and value not in used_gids:
            return value, value
    raise RuntimeError('no free numeric identity is available for the fixture app')


def ensure_app_identity(uid, gid):
    try:
        group = grp.getgrnam(APP_NAME)
    except KeyError:
        run(['/usr/sbin/groupadd', '--gid', str(gid), APP_NAME])
        group = grp.getgrnam(APP_NAME)
    if group.gr_gid != gid:
        raise RuntimeError('fixture application group differs from reviewed numeric identity')
    try:
        user = pwd.getpwnam(APP_NAME)
    except KeyError:
        run(['/usr/sbin/useradd', '--uid', str(uid), '--gid', str(gid),
             '--home-dir', str(FILES / 'home'), '--shell', '/usr/sbin/nologin',
             '--no-create-home', APP_NAME])
        user = pwd.getpwnam(APP_NAME)
    if (user.pw_uid, user.pw_gid, user.pw_dir, user.pw_shell) != (
            uid, gid, str(FILES / 'home'), '/usr/sbin/nologin'):
        raise RuntimeError('fixture application account differs from its reconstruction recipe')


def set_acl_if_available(path):
    try:
        acl_user = next(user for user in pwd.getpwall()
                        if user.pw_uid not in (0, os.stat(path).st_uid)
                        and user.pw_uid >= 1000)
    except StopIteration:
        return None
    command = shutil.which('setfacl')
    if command:
        result = subprocess.run([command, '-m', f'u:{acl_user.pw_uid}:r--', str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            return acl_user.pw_uid
    library_name = ctypes.util.find_library('acl')
    if not library_name:
        return None
    library = ctypes.CDLL(library_name)
    library.acl_from_text.argtypes = [ctypes.c_char_p]
    library.acl_from_text.restype = ctypes.c_void_p
    library.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    library.acl_set_file.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    text = f'u::rw-,u:{acl_user.pw_uid}:r--,g::r--,m::r--,o::---'.encode('ascii')
    acl = library.acl_from_text(text)
    if not acl:
        return None
    try:
        return acl_user.pw_uid if library.acl_set_file(os.fsencode(path), 0x8000, acl) == 0 else None
    finally:
        library.acl_free(acl)


def has_fixture_acl(path, uid):
    try:
        raw = os.getxattr(path, 'system.posix_acl_access')
    except OSError:
        return False
    if len(raw) < 12 or (len(raw) - 4) % 8 or int.from_bytes(raw[:4], 'little') != 2:
        return False
    return any(int.from_bytes(raw[offset:offset + 2], 'little') == 2
               and int.from_bytes(raw[offset + 2:offset + 4], 'little') == 4
               and int.from_bytes(raw[offset + 4:offset + 8], 'little') == uid
               for offset in range(4, len(raw), 8))


def metadata(path):
    info = path.lstat()
    attrs = []
    for name in sorted(os.listxattr(path, follow_symlinks=False)):
        value = os.getxattr(path, name, follow_symlinks=False)
        attrs.append({'name': name, 'value_base64': base64.b64encode(value).decode('ascii')})
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': info.st_mode & 0o7777,
            'links': info.st_nlink, 'type': ('symlink' if stat.S_ISLNK(info.st_mode)
                else 'directory' if stat.S_ISDIR(info.st_mode) else 'file'),
            'target': os.readlink(path) if stat.S_ISLNK(info.st_mode) else None,
            'xattrs': attrs}


def create_app_files(uid, gid):
    DATA_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    if FILES.exists():
        shutil.rmtree(FILES)
    FILES.mkdir(mode=0o750)
    home = FILES / 'home'
    home.mkdir(mode=0o750)
    app_code = r'''#!/usr/bin/python3
import hashlib, json, sqlite3, sys
from pathlib import Path
root = Path(__file__).resolve().parent
config_bytes = (root / 'config.json').read_bytes()
config = json.loads(config_bytes)
secret = (root / config['secret']).read_bytes()
database_path = (root / config['database']).resolve()
with sqlite3.connect(database_path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
    rows = [row[0] for row in db.execute('SELECT value FROM committed ORDER BY rowid')]
    integrity = db.execute('PRAGMA quick_check').fetchone()[0]
if integrity != 'ok':
    raise SystemExit('fixture SQLite integrity failed')
output = {'schema_version': 1, 'rows': rows,
          'secret_sha256': hashlib.sha256(secret).hexdigest()}
encoded = (json.dumps(output, sort_keys=True, separators=(',', ':')) + '\n').encode()
(root / config['output']).write_bytes(encoded)
'''
    (FILES / 'app.py').write_text(app_code, encoding='utf-8')
    (FILES / 'app.py').chmod(0o750)
    (FILES / 'config.json').write_text(json.dumps({
        'database': '../app.sqlite', 'secret': 'synthetic.secret',
        'output': 'app-output.json'}, sort_keys=True) + '\n', encoding='utf-8')
    (FILES / 'config.json').chmod(0o640)
    (FILES / 'synthetic.secret').write_bytes(b'synthetic coverage fixture secret v1\n')
    (FILES / 'synthetic.secret').chmod(0o640)
    (FILES / 'hardlink-original.txt').write_bytes(b'linked fixture content\n')
    (FILES / 'hardlink-original.txt').chmod(0o640)
    os.link(FILES / 'hardlink-original.txt', FILES / 'hardlink-copy.txt')
    (FILES / 'config-link').symlink_to('config.json')
    (FILES / 'xattr-probe.txt').write_bytes(b'xattr fixture\n')
    (FILES / 'xattr-probe.txt').chmod(0o640)
    (FILES / 'app-output.json').write_bytes(b'')
    (FILES / 'app-output.json').chmod(0o600)
    for path in FILES.rglob('*'):
        if path.is_symlink():
            continue
        shutil.chown(path, user=uid, group=gid)
    shutil.chown(FILES, user=uid, group=gid)
    shutil.chown(home, user=uid, group=gid)
    try:
        os.setxattr(FILES / 'xattr-probe.txt', 'user.coverage', b'covered-root-fixture-v1')
    except (AttributeError, OSError) as exc:
        raise RuntimeError('guest filesystem cannot store fixture xattrs') from exc
    acl_uid = set_acl_if_available(FILES / 'xattr-probe.txt')
    if acl_uid is None:
        raise RuntimeError('guest filesystem cannot create the required fixture ACL')
    os.chown(FILES / 'xattr-probe.txt', uid, gid)
    if not has_fixture_acl(FILES / 'xattr-probe.txt', acl_uid):
        raise RuntimeError('required fixture ACL did not survive readback')
    acl_applied = True
    writer = sqlite3.connect(DATABASE)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('CREATE TABLE committed(value TEXT NOT NULL)')
    writer.execute('INSERT INTO committed VALUES (?)', ('committed fixture row',))
    writer.commit()
    writer.execute('INSERT INTO committed VALUES (?)', ('uncommitted fixture row',))
    try:
        os.setxattr(DATABASE, 'user.coverage', b'covered-root-sqlite-v1')
    except (AttributeError, OSError) as exc:
        writer.close()
        raise RuntimeError('guest filesystem cannot store SQLite xattrs') from exc
    shutil.chown(DATABASE, user=uid, group=gid)
    DATABASE.chmod(0o640)
    metadata_path = FILES / 'numeric-metadata.json'
    metadata_path.write_text('{}\n', encoding='utf-8')
    metadata_path.chmod(0o600)
    entries = {str(path.relative_to(FILES)): metadata(path)
               for path in sorted(FILES.rglob('*'))}
    entries['.'] = metadata(FILES)
    metadata_doc = {'schema_version': 1, 'entries': entries,
                    'acl_applied': acl_applied,
                    'sqlite_identity': metadata(DATABASE)}
    metadata_path.write_text(json.dumps(metadata_doc, sort_keys=True) + '\n', encoding='utf-8')
    fixture_bytes = sum(path.lstat().st_size for path in FILES.rglob('*'))
    fixture_bytes += DATABASE.stat().st_size
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(DATABASE) + suffix)
        if sidecar.exists():
            fixture_bytes += sidecar.stat().st_size
    if fixture_bytes >= 1024 * 1024:
        writer.close()
        raise RuntimeError('covered-root logical fixture exceeds 1 MiB')
    return writer, acl_applied, fixture_bytes


def app_unit_text():
    return ("[Unit]\nDescription=Covered root fixture application\nAfter=local-fs.target\n\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            "User=coverageapp\nGroup=coverageapp\n"
            "ExecStart=/var/lib/bbackup-covered-root/files/app.py\n"
            "UMask=0077\nKillMode=control-group\nTimeoutStartSec=45\n"
            "NoNewPrivileges=yes\n")


def verify_root_enrollment():
    for path in (CONFIG, BINDINGS, DECLARATION):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise RuntimeError('coverage policy, bindings, and declaration must be root-owned and private')
    if STATE.exists() and (STATE.stat().st_uid != 0 or STATE.stat().st_mode & 0o077):
        raise RuntimeError('production coverage state is not root-owned and private')


def write_unit(path, content):
    path = Path(path)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    path.chmod(0o644)


def verify_installed_units(*, include_fixture):
    unit_dir = Path('/etc/systemd/system')
    verify_paths = [unit_dir / f'bbackup-{JOB_NAME}.service',
                    unit_dir / f'bbackup-{JOB_NAME}.timer']
    if include_fixture:
        verify_paths.append(unit_dir / APP_UNIT)
    run(['/usr/bin/systemd-analyze', 'verify', *map(str, verify_paths)])


def install_production_units(*, include_fixture=True):
    _, rendered = production('service', 'render-system', '--name', JOB_NAME,
                             '--executable', str(BIN))
    units = rendered.get('data') or {}
    expected = {f'bbackup-{JOB_NAME}.service', f'bbackup-{JOB_NAME}.timer'}
    if set(units) != expected:
        raise RuntimeError('production system unit renderer returned an unexpected unit set')
    service = units[f'bbackup-{JOB_NAME}.service']
    timer = units[f'bbackup-{JOB_NAME}.timer']
    if ('User=root' not in service or 'Group=root' not in service
            or 'OnCalendar=*-*-* *:*:00' not in timer or 'Persistent=true' not in timer):
        raise RuntimeError('production system unit renderer omitted root or timer requirements')
    unit_dir = Path('/etc/systemd/system')
    write_unit(unit_dir / f'bbackup-{JOB_NAME}.service', service)
    write_unit(unit_dir / f'bbackup-{JOB_NAME}.timer', timer)
    app_unit = app_unit_text()
    write_unit(unit_dir / APP_UNIT, app_unit)
    private_json(MATERIAL / 'fixture-app-unit.json', {'text': app_unit})
    rootctl('daemon-reload')
    verify_installed_units(include_fixture=include_fixture)
    return app_unit


def wait_for(predicate, label, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            log(f'PASS {label}')
            return
        time.sleep(2)
    raise RuntimeError(f'timed out waiting for {label}')


def unit_success(name, *, retained=False):
    result = rootctl('show', name, '--no-pager', '-p', 'Result', '-p', 'ExecMainStatus',
        '-p', 'ExecMainStartTimestamp', '-p', 'ExecMainExitTimestamp',
        '-p', 'ActiveState', '-p', 'SubState')
    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    active, sub = ('active', 'exited') if retained else ('inactive', 'dead')
    return (props.get('Result') == 'success' and props.get('ExecMainStatus') == '0'
        and bool(props.get('ExecMainStartTimestamp'))
        and bool(props.get('ExecMainExitTimestamp'))
        and props.get('ActiveState') == active and props.get('SubState') == sub)


def timer_triggered(name):
    result = rootctl('show', name, '--no-pager', '-p', 'LastTriggerUSec')
    value = result.stdout.strip().split('=', 1)[-1] if '=' in result.stdout else ''
    return value not in ('', '0', 'n/a')


def fixture_identity(uid, gid):
    account = pwd.getpwnam(APP_NAME)
    group = grp.getgrnam(APP_NAME)
    return {'name': APP_NAME, 'uid': account.pw_uid, 'gid': group.gr_gid,
            'home': account.pw_dir, 'shell': account.pw_shell}


def base_inventory():
    users = [{'name': item.pw_name, 'uid': item.pw_uid, 'gid': item.pw_gid,
              'home': item.pw_dir, 'shell': item.pw_shell}
             for item in pwd.getpwall() if item.pw_name != APP_NAME]
    groups = [{'name': item.gr_name, 'gid': item.gr_gid,
               'members': sorted(item.gr_mem)}
              for item in grp.getgrall() if item.gr_name != APP_NAME]
    units = []
    root = Path('/etc/systemd/system')
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    stack.append(path)
                elif path.name.endswith('.service'):
                    if stat.S_ISLNK(info.st_mode):
                        kind = 'link:' + sha256_bytes(os.readlink(path).encode(
                            'utf-8', 'surrogateescape'))
                    elif stat.S_ISREG(info.st_mode):
                        kind = 'regular'
                    else:
                        raise RuntimeError('unsupported systemd service inventory type')
                    units.append({'name': path.name,
                        'relative_path': str(path.relative_to(root)), 'kind': kind})
    units.sort(key=lambda item: item['relative_path'])
    package_bytes = Path('/var/lib/dpkg/status').read_bytes()
    mount_rows = []
    for line in Path('/proc/self/mountinfo').read_text(encoding='utf-8').splitlines():
        before, after = line.split(' - ', 1)
        fields, tail = before.split(), after.split()
        mount_rows.append({'mount_id': int(fields[0]), 'mountpoint': fields[4],
                           'filesystem': tail[0], 'source_sha256': sha256_bytes(tail[1].encode())})
    return {'users': users, 'groups': groups, 'units': units,
        'packages_sha256': sha256_bytes(package_bytes),
        'mounts': mount_rows, 'package_bytes': len(package_bytes)}


BASE_AUDIT_SCRIPT = r'''#!/usr/bin/python3
import grp, hashlib, json, os, pwd, stat, subprocess
from pathlib import Path
material = Path('/mnt/bbackup-recovery/material')
expected = json.loads((material / 'base-inventory.json').read_text())
users = [{'name': p.pw_name, 'uid': p.pw_uid, 'gid': p.pw_gid,
          'home': p.pw_dir, 'shell': p.pw_shell}
         for p in pwd.getpwall() if p.pw_name != 'coverageapp']
groups = [{'name': g.gr_name, 'gid': g.gr_gid, 'members': sorted(g.gr_mem)}
          for g in grp.getgrall() if g.gr_name != 'coverageapp']
if sorted(users, key=lambda x: x['name']) != sorted(expected['users'], key=lambda x: x['name']):
    raise SystemExit('base user reconstruction check failed')
if sorted(groups, key=lambda x: x['name']) != sorted(expected['groups'], key=lambda x: x['name']):
    raise SystemExit('base group reconstruction check failed')
package_bytes = Path('/var/lib/dpkg/status').read_bytes()
if hashlib.sha256(package_bytes).hexdigest() != expected['packages_sha256']:
    raise SystemExit('base package inventory differs from reviewed image')
root = Path('/etc/systemd/system')
actual = []
stack = [root]
while stack:
    directory = stack.pop()
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif path.name.endswith('.service'):
                if stat.S_ISLNK(info.st_mode):
                    kind = 'link:' + hashlib.sha256(os.readlink(path).encode(
                        'utf-8', 'surrogateescape')).hexdigest()
                elif stat.S_ISREG(info.st_mode):
                    kind = 'regular'
                else:
                    raise SystemExit('unsupported systemd service inventory type')
                actual.append({'name': path.name, 'relative_path': str(path.relative_to(root)),
                               'kind': kind})
actual.sort(key=lambda item: item['relative_path'])
if actual != expected['units']:
    raise SystemExit('systemd service reconstruction inventory differs')
for name in sorted({item['name'] for item in actual}):
    load = subprocess.run(['/usr/bin/systemctl', 'show', name, '-p', 'LoadState', '--value'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    if load.returncode or load.stdout.strip() not in ('loaded', 'masked'):
        raise SystemExit('systemd service reconstruction check failed')
'''


FIXTURE_REBUILD_SCRIPT = r'''#!/usr/bin/python3
import base64, hashlib, json, os, pwd, grp, shutil, sqlite3, stat, subprocess, sys
from pathlib import Path
material = Path('/mnt/bbackup-recovery/material')
proof = json.loads((material / 'source-proof.json').read_text())
if len(sys.argv) != 3 or sys.argv[1] not in ('rehearsal', 'replacement'):
    raise SystemExit('fixture reconstruction mode is invalid')
mode, restore_root = sys.argv[1], Path(sys.argv[2])
prefix = Path('/var/tmp/bbackup-covered-root-rehearsal') if mode == 'rehearsal' else Path('/')
files_source = restore_root / 'var/lib/bbackup-covered-root/files'
manifest_paths = list(restore_root.rglob('bbackup-capture.json'))
if not files_source.is_dir() or len(manifest_paths) != 1:
    raise SystemExit('restored fixture files or manifest are missing')
manifest = json.loads(manifest_paths[0].read_text())
entries = [item for item in manifest['exports'] if item.get('source') == 'database']
if len(entries) != 1:
    raise SystemExit('covered SQLite export is missing from manifest')
entry = entries[0]
if (entry.get('format') != 'sqlite' or entry.get('original_path') != proof['database_path']
        or entry.get('coverage_root_id') != 'database'
        or entry.get('baseline_digest') != proof['baseline_digest']):
    raise SystemExit('covered SQLite manifest differs from reviewed source')
export_path = restore_root / str(entry['path']).lstrip('/')
if hashlib.sha256(export_path.read_bytes()).hexdigest() != entry['export_sha256']:
    raise SystemExit('covered SQLite export hash differs from manifest')
raw_restore = restore_root / str(proof['database_path']).lstrip('/')
if raw_restore.exists() or any(Path(str(raw_restore) + s).exists()
        for s in ('-wal', '-shm', '-journal')):
    raise SystemExit('raw SQLite database artifact was restored')
files_target = prefix / 'var/lib/bbackup-covered-root/files'
database_target = prefix / 'var/lib/bbackup-covered-root/app.sqlite'
files_target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
if files_target.exists():
    shutil.rmtree(files_target)
subprocess.run(['/bin/cp', '-a', str(files_source), str(files_target.parent)], check=True)
database_target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
if database_target.exists():
    database_target.unlink()
shutil.copyfile(export_path, database_target)
identity = entry['reviewed_identity']
for key in ('uid', 'gid', 'mode', 'links', 'xattrs'):
    if identity.get(key) != proof['sqlite_identity'].get(key):
        raise SystemExit('reviewed SQLite metadata differs from source evidence')
os.chown(database_target, identity['uid'], identity['gid'])
os.chmod(database_target, identity['mode'])
for attr in identity['xattrs']:
    os.setxattr(database_target, attr['name'], base64.b64decode(attr['value_base64']))
database_info = database_target.lstat()
database_attrs = [{'name': name, 'value_base64': base64.b64encode(os.getxattr(
    database_target, name, follow_symlinks=False)).decode('ascii')}
    for name in sorted(os.listxattr(database_target, follow_symlinks=False))]
if (database_info.st_uid != identity['uid'] or database_info.st_gid != identity['gid']
        or database_info.st_mode & 0o7777 != identity['mode']
        or database_info.st_nlink != identity['links'] or database_attrs != identity['xattrs']):
    raise SystemExit('restored SQLite numeric owner, mode, link or xattr metadata differs')
with sqlite3.connect(database_target.as_uri() + '?mode=ro', uri=True) as database:
    if database.execute('PRAGMA quick_check').fetchone() != ('ok',):
        raise SystemExit('restored SQLite integrity check failed')
    rows = [row[0] for row in database.execute('SELECT value FROM committed ORDER BY rowid')]
if rows != ['committed fixture row']:
    raise SystemExit('restored SQLite contains missing or uncommitted rows')
identity_info = proof['app_identity']
try:
    group = grp.getgrnam(identity_info['name'])
except KeyError:
    subprocess.run(['/usr/sbin/groupadd', '--gid', str(identity_info['gid']),
                    identity_info['name']], check=True)
    group = grp.getgrnam(identity_info['name'])
if group.gr_gid != identity_info['gid']:
    raise SystemExit('fixture application group could not be reconstructed')
try:
    user = pwd.getpwnam(identity_info['name'])
except KeyError:
    subprocess.run(['/usr/sbin/useradd', '--uid', str(identity_info['uid']),
        '--gid', str(identity_info['gid']), '--home-dir', identity_info['home'],
        '--shell', identity_info['shell'], '--no-create-home', identity_info['name']], check=True)
    user = pwd.getpwnam(identity_info['name'])
if (user.pw_uid, user.pw_gid, user.pw_dir, user.pw_shell) != (
        identity_info['uid'], identity_info['gid'], identity_info['home'], identity_info['shell']):
    raise SystemExit('fixture application account differs from reviewed identity')
if mode == 'replacement':
    unit = json.loads((material / 'fixture-app-unit.json').read_text())['text']
    unit_path = Path('/etc/systemd/system/bbackup-covered-fixture.service')
    unit_path.write_text(unit)
    unit_path.chmod(0o644)
    subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True)
    subprocess.run(['/usr/bin/systemd-analyze', 'verify', str(unit_path)], check=True)
    subprocess.run(['/usr/bin/systemctl', 'start', 'bbackup-covered-fixture.service'], check=True)
    status = subprocess.run(['/usr/bin/systemctl', 'show', 'bbackup-covered-fixture.service',
        '-p', 'Result', '-p', 'ExecMainStatus', '-p', 'ExecMainStartTimestamp',
        '-p', 'ExecMainExitTimestamp', '-p', 'ActiveState', '-p', 'SubState'], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, check=True).stdout
    props = dict(line.split('=', 1) for line in status.splitlines() if '=' in line)
    if (props.get('Result') != 'success' or props.get('ExecMainStatus') != '0'
            or not props.get('ExecMainStartTimestamp') or not props.get('ExecMainExitTimestamp')
            or props.get('ActiveState') != 'active' or props.get('SubState') != 'exited'):
        raise SystemExit('fixture application system service did not complete successfully')
else:
    output = files_target / 'app-output.json'
    subprocess.run(['/usr/sbin/runuser', '-u', identity_info['name'], '--',
                    str(files_target / 'app.py')], check=True)
if hashlib.sha256((files_target / 'app-output.json').read_bytes()).hexdigest() != proof['app_output_sha256']:
    raise SystemExit('reconstructed fixture application output differs')
metadata = json.loads((files_target / 'numeric-metadata.json').read_text())
for relative, expected in metadata['entries'].items():
    actual_path = files_target if relative == '.' else files_target / relative
    actual = actual_path.lstat()
    if (actual.st_uid != expected['uid'] or actual.st_gid != expected['gid']
            or actual.st_mode & 0o7777 != expected['mode']
            or actual.st_nlink != expected['links']):
        raise SystemExit('restored numeric owner, mode, or link metadata differs')
    if stat.S_ISLNK(actual.st_mode) and os.readlink(actual_path) != expected['target']:
        raise SystemExit('restored symlink target differs')
    attrs = [{'name': name, 'value_base64': base64.b64encode(os.getxattr(
        actual_path, name, follow_symlinks=False)).decode('ascii')}
        for name in sorted(os.listxattr(actual_path, follow_symlinks=False))]
    if attrs != expected['xattrs']:
        raise SystemExit('restored xattr or ACL metadata differs')
hard_a, hard_b = files_target / 'hardlink-original.txt', files_target / 'hardlink-copy.txt'
if hard_a.stat().st_ino != hard_b.stat().st_ino:
    raise SystemExit('restored hardlink relationship differs')
if os.readlink(files_target / 'config-link') != 'config.json':
    raise SystemExit('restored symlink relationship differs')
'''


def inventory_unit_records():
    root = Path('/etc/systemd/system')
    result = []
    stack = [root]
    visited = 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            children = sorted((Path(entry.path) for entry in entries), reverse=True)
        for path in children:
            visited += 1
            if visited > 8192:
                raise RuntimeError('systemd service inventory exceeds supported bound')
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif path.name.endswith('.service'):
                if stat.S_ISLNK(info.st_mode):
                    kind = 'link:' + sha256_bytes(os.readlink(path).encode('utf-8', 'surrogateescape'))
                elif stat.S_ISREG(info.st_mode):
                    kind = 'regular'
                else:
                    raise RuntimeError('unsupported systemd service inventory type')
                result.append({'name': path.name, 'relative_path': str(path.relative_to(root)),
                               'kind': kind})
    result.sort(key=lambda item: item['relative_path'])
    return result


def recipe_evidence(recipe_id, procedure, environment='isolated-local-fixture'):
    procedure = Path(procedure)
    procedure.chmod(0o700)
    digest = sha256_file(procedure)
    evidence_path = MATERIAL / 'recipes' / f'{recipe_id}.evidence.json'
    result = run(['/usr/bin/python3', str(procedure)], timeout=120)
    if result.returncode:
        raise RuntimeError(f'reconstruction procedure failed: {recipe_id}')
    evidence = {'schema_version': 1, 'recipe_id': recipe_id, 'recipe_sha256': digest,
        'environment': environment,
        'checks': {'procedure_executed': True, 'result_verified': True}}
    private_json(evidence_path, evidence)
    return ({'reason': 'reconstruct from the verified Ubuntu base or covered fixture',
             'consequence': 'reviewed local emulation depends on this exact procedure',
             'recipe_id': recipe_id, 'recipe_sha256': digest},
            {'recipe_id': recipe_id, 'recipe_sha256': digest,
             'procedure_path': str(procedure), 'evidence_path': str(evidence_path),
             'evidence_sha256': sha256_file(evidence_path)})


def make_config_and_bindings():
    scope = {'schema_version': 1, 'approved_roots': ['files', 'database'],
        'require_privileged': True, 'filesystem_boundaries': 'explicit',
        'metadata_expectations': {key: True for key in
            ('numeric_owner', 'mode', 'acl', 'xattr', 'links')}}
    policy = {'schema_version': 3,
        'repositories': [{'name': 'local', 'role': 'local'}],
        'sources': [{'name': 'files', 'kind': 'files'},
                    {'name': 'database', 'kind': 'sqlite'}],
        'jobs': [{'name': JOB_NAME, 'repository': 'local',
            'sources': ['files', 'database'], 'schedule': '*-*-* *:*:00',
            'timeout_seconds': 900, 'coverage_policy': scope}]}
    bindings = {'schema_version': 3, 'state_dir': str(STATE),
        'repositories': {'local': {'repository': str(REPOSITORY),
                                   'password_file': str(PASSWORD)}},
        'source_bindings': {
            'files': {'paths': [str(FILES)]},
            'database': {'paths': [str(DATABASE)]}},
        'coverage_declarations': {JOB_NAME: str(DECLARATION)}}
    policy_path = MATERIAL / 'policy-candidate.json'
    bindings_path = MATERIAL / 'bindings-candidate.json'
    private_json(policy_path, policy)
    private_json(bindings_path, bindings)
    return scope, policy, bindings, policy_path, bindings_path


def file_tree_size():
    total = sum(path.lstat().st_size for path in FILES.rglob('*'))
    total += DATABASE.stat().st_size
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(DATABASE) + suffix)
        if sidecar.exists():
            total += sidecar.stat().st_size
    return total


def source_role(candidate):
    mount_inputs()
    mount_recovery('source')
    os.chown(RECOVERY, 0, 0)
    install_candidate(candidate)
    uid, gid = app_uid_gid()
    run(['/usr/sbin/groupadd', '--gid', str(gid), APP_NAME])
    run(['/usr/sbin/useradd', '--uid', str(uid), '--gid', str(gid),
         '--home-dir', str(FILES / 'home'), '--shell', '/usr/sbin/nologin',
         '--no-create-home', APP_NAME])
    writer, acl_applied, logical_size = create_app_files(uid, gid)
    password = (uuid.uuid4().hex + uuid.uuid4().hex).encode('ascii') + b'\n'
    PASSWORD.write_bytes(password)
    PASSWORD.chmod(0o600)
    _, _, _, policy_path, bindings_path = make_config_and_bindings()
    _, setup = production('setup', '--configuration-file', str(policy_path),
                          '--bindings-file', str(bindings_path))
    if setup.get('data', {}).get('configured') is not True:
        writer.close()
        raise RuntimeError('root-owned schema-three setup did not complete')
    install_production_units()
    rootctl('start', APP_UNIT)
    wait_for(lambda: unit_success(APP_UNIT, retained=True),
             'fixture application systemd invocation')
    app_output = FILES / 'app-output.json'
    app_identity = fixture_identity(uid, gid)
    app_output_hash = sha256_file(app_output)
    logical_size = file_tree_size()
    if logical_size >= 1024 * 1024:
        writer.close()
        raise RuntimeError('covered-root logical fixture exceeds 1 MiB')
    source_machine = machine_digest()
    fixture_meta = read_json(FILES / 'numeric-metadata.json')
    sqlite_info = metadata(DATABASE)
    source_proof = {'schema_version': 1, 'source_machine_digest': source_machine,
        'app_identity': app_identity, 'database_path': str(DATABASE),
        'sqlite_identity': sqlite_info, 'app_output_sha256': app_output_hash,
        'secret_sha256': sha256_file(FILES / 'synthetic.secret'),
        'fixture_logical_bytes': logical_size, 'acl_applied': acl_applied,
        'fixture_metadata': fixture_meta, 'baseline_digest': '0' * 64,
        'snapshot_id': None, 'capture_manifest_sha256': None,
        'sqlite_export_sha256': None}
    private_json(MATERIAL / 'source-proof.json', source_proof)
    # The actual guest inventory is read after all application and production
    # units are rendered and installed, exactly as production discovery reads it.
    inventory = base_inventory()
    private_json(MATERIAL / 'base-inventory.json', inventory)
    (MATERIAL / 'recipes').mkdir(mode=0o700, parents=True, exist_ok=True)
    base_proc = MATERIAL / 'recipes' / 'base-reconstruction.py'
    base_proc.write_text(BASE_AUDIT_SCRIPT, encoding='utf-8')
    base_proc.chmod(0o700)
    # The base recipe is exercised now, and again on the clean replacement.
    base_recipe, base_evidence = recipe_evidence('base_reconstruction', base_proc)
    fixture_proc = MATERIAL / 'recipes' / 'fixture-reconstruction.py'
    fixture_proc.write_text(FIXTURE_REBUILD_SCRIPT, encoding='utf-8')
    fixture_proc.chmod(0o700)
    fixture_evidence = _exercise_fixture_recipe(fixture_proc, source_proof)
    # Build the reviewed declaration after observing real passwd, group, mount,
    # package and systemd inventories; these paths stay private to the guest.
    users = inventory['users']
    identity_decisions = [{'name': item['name'], 'home': item['home'],
        'recipe': base_recipe} for item in users
        if Path(item['home']).is_absolute() and not (
            Path(item['home']) == FILES or FILES in Path(item['home']).parents)]
    mounts = []
    for item in inventory['mounts']:
        mountpoint = Path(item['mountpoint'])
        if mountpoint == FILES or FILES in mountpoint.parents or mountpoint == DATABASE:
            mounts.append({'mount_id': item['mount_id'], 'mountpoint': str(mountpoint),
                           'decision': 'include'})
    applications = []
    seen_units = set()
    for index, item in enumerate(inventory_unit_records()):
        stem = item['name'][:-len('.service')]
        if stem in seen_units:
            continue
        seen_units.add(stem)
        if stem == APP_UNIT[:-len('.service')]:
            continue
        apps = [{'id': f'guest_unit_{index:03d}', 'unit': stem,
                 'source_id': 'files', 'state_roots': [],
                 'startup_recipe': base_recipe}]
        applications.extend(apps)
    fixture_recipe_id = 'fixture_reconstruction'
    fixture_digest = sha256_file(fixture_proc)
    fixture_recipe = {'reason': 'recreate the tiny covered app and its SQLite state',
        'consequence': 'replacement must reapply numeric metadata and verify startup output',
        'recipe_id': fixture_recipe_id, 'recipe_sha256': fixture_digest}
    applications.extend([
        {'id': 'fixture_files', 'unit': APP_UNIT[:-len('.service')],
         'source_id': 'files', 'state_roots': [str(FILES)],
         'startup_recipe': fixture_recipe},
        {'id': 'fixture_database', 'unit': APP_UNIT[:-len('.service')],
         'source_id': 'database', 'state_roots': [str(DATABASE)],
         'startup_recipe': fixture_recipe},
    ])
    declaration = {'schema_version': 1,
        'roots': [{'id': 'files', 'path': str(FILES), 'source_id': 'files', 'adapter': 'files'},
                  {'id': 'database', 'path': str(DATABASE),
                   'source_id': 'database', 'adapter': 'sqlite'}],
        'exclusions': [], 'mount_decisions': mounts,
        'identity_decisions': identity_decisions, 'applications': applications,
        'package_reconstruction': base_recipe,
        'reconstruction_evidence': [base_evidence, fixture_evidence]}
    private_json(DECLARATION, declaration)
    production('service', 'enroll')
    verify_root_enrollment()
    production('repositories', 'init', '--name', 'local', timeout=300)
    discovered = production('coverage', 'discover', '--name', JOB_NAME)[1].get('data', {})
    if discovered.get('coverage_status') != 'complete':
        raise RuntimeError('actual production coverage discovery has unresolved inventory gaps')
    reviewed = production('coverage', 'review', '--name', JOB_NAME,
                          '--candidate-digest', discovered['candidate_digest'])[1].get('data', {})
    if reviewed.get('coverage_status') != 'complete':
        raise RuntimeError('actual production coverage review did not complete')
    check = production('coverage', 'check', '--name', JOB_NAME)[1].get('data', {})
    if check.get('coverage_status') != 'complete' or not check.get('capture_ready'):
        raise RuntimeError('actual production coverage check is not capture-ready')
    source_proof['baseline_digest'] = reviewed['baseline_digest']
    source_proof['candidate_digest'] = reviewed['candidate_digest']
    private_json(MATERIAL / 'source-proof.json', source_proof)
    # Evidence binds the exact final recipe digest and files, not a descriptive
    # label. The recipe was run before both reviews above.
    rootctl('enable', '--now', f'bbackup-{JOB_NAME}.timer')
    timer = f'bbackup-{JOB_NAME}.timer'
    service = f'bbackup-{JOB_NAME}.service'
    wait_for(lambda: timer_triggered(timer) and unit_success(service),
             'production root systemd timer completed a covered capture', timeout=170)
    check = production('coverage', 'check', '--name', JOB_NAME)[1].get('data', {})
    if check.get('coverage_status') != 'complete':
        raise RuntimeError('coverage drifted during the production root timer capture')
    with sqlite3.connect(STATE / 'snapshots.sqlite3') as receipts:
        rows = receipts.execute('SELECT snapshot,operation FROM snapshots '
            'WHERE repository=? AND job=? ORDER BY rowid DESC', ('local', JOB_NAME)).fetchall()
    snapshots = run([str(RESTIC), '-r', str(REPOSITORY), '--password-file', str(PASSWORD),
                     '--no-cache', '--json', 'snapshots'], timeout=120)
    listed = json.loads(snapshots.stdout)
    matching = [item for item in listed if f'bbackup-job={JOB_NAME}' in (item.get('tags') or [])]
    if not rows or not matching:
        raise RuntimeError('timer capture lacks an encrypted repository snapshot or receipt')
    snapshot_id = matching[-1]['id']
    receipt = next((row for row in rows if row[0] == snapshot_id), None)
    if receipt is None:
        raise RuntimeError('timer snapshot has no matching production receipt')
    restore = production('restore', '--repository', 'local', '--snapshot', snapshot_id,
                         '--target', str(RESTORE_TARGET), timeout=300)[1].get('data', {})
    if restore.get('files_verified') is not True:
        raise RuntimeError('production restore did not verify the timer snapshot')
    manifest_paths = list(RESTORE_TARGET.rglob('bbackup-capture.json'))
    if len(manifest_paths) != 1:
        raise RuntimeError('production restore did not yield one encrypted capture manifest')
    manifest_path = manifest_paths[0]
    manifest = read_json(manifest_path)
    exports = [item for item in manifest['exports'] if item.get('source') == 'database']
    if len(exports) != 1:
        raise RuntimeError('capture manifest has no unique covered SQLite export')
    export = exports[0]
    if (export.get('format') != 'sqlite' or export.get('original_path') != str(DATABASE)
            or export.get('coverage_root_id') != 'database'
            or export.get('baseline_digest') != reviewed['baseline_digest']):
        raise RuntimeError('SQLite export does not match the reviewed source and baseline')
    export_file = RESTORE_TARGET / str(export['path']).lstrip('/')
    export_digest = sha256_file(export_file)
    if export_digest != export.get('export_sha256'):
        raise RuntimeError('SQLite export SHA-256 differs from encrypted capture manifest')
    with sqlite3.connect(export_file) as database:
        if database.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise RuntimeError('exported SQLite database failed integrity check')
        rows_data = [row[0] for row in database.execute(
            'SELECT value FROM committed ORDER BY rowid')]
    if rows_data != ['committed fixture row']:
        raise RuntimeError('transactional SQLite export included an uncommitted row')
    proof_restore = RESTORE_TARGET / str(DATABASE).lstrip('/')
    sidecars = [Path(str(proof_restore) + suffix) for suffix in ('-wal', '-shm', '-journal')]
    if proof_restore.exists() or any(path.exists() for path in sidecars):
        raise RuntimeError('production restore included a raw SQLite file or sidecar')
    manifest_digest = sha256_file(manifest_path)
    # Restic dump uses the manifest's archived path. Resolve it from metadata
    # and compare a direct encrypted-repository read with production restore.
    snapshots_meta = run([str(RESTIC), '-r', str(REPOSITORY), '--password-file', str(PASSWORD),
        '--no-cache', 'cat', 'snapshot', snapshot_id], timeout=120)
    snapshot_meta = json.loads(snapshots_meta.stdout)
    manifest_archive = [item for item in snapshot_meta.get('paths', [])
                        if Path(item).name == 'bbackup-capture.json']
    if len(manifest_archive) != 1:
        raise RuntimeError('encrypted repository snapshot has no unique capture manifest')
    direct_manifest = run([str(RESTIC), '-r', str(REPOSITORY), '--password-file', str(PASSWORD),
        '--no-cache', 'dump', snapshot_id, manifest_archive[0]], timeout=120)
    if sha256_bytes(direct_manifest.stdout.encode()) != manifest_digest:
        raise RuntimeError('production restore manifest differs from encrypted repository content')
    source_proof.update({'snapshot_id': snapshot_id,
        'capture_manifest_sha256': manifest_digest, 'sqlite_export_sha256': export_digest,
        'receipt_operation': receipt[1], 'source_coverage_status': check['coverage_status']})
    private_json(MATERIAL / 'source-proof.json', source_proof)
    logical_size = file_tree_size()
    if logical_size != source_proof['fixture_logical_bytes']:
        raise RuntimeError('covered-root logical fixture changed during capture')
    writer.rollback()
    writer.close()
    rootctl('disable', '--now', timer)
    proof_public = {'source_machine_digest': source_machine, 'baseline_digest': reviewed['baseline_digest'],
        'candidate_digest': reviewed['candidate_digest'], 'snapshot_id': snapshot_id,
        'candidate_wheel_sha256': EXPECTED_CANDIDATE,
        'capture_manifest_sha256': manifest_digest, 'sqlite_export_sha256': export_digest,
        'fixture_logical_bytes': logical_size, 'acl_applied': acl_applied}
    proof_public['root_timer_invocation'] = {
        'unit': service, 'result': 'success', 'exit_status': 0,
        'start_observed': True, 'exit_observed': True}
    log('SOURCE_COMPLETE ' + json.dumps(proof_public, sort_keys=True))
    os.sync()
    rootctl('poweroff', timeout=10)


def _exercise_fixture_recipe(procedure, proof):
    # Build the same path shape that a Restic restore returns, using SQLite's
    # transactional backup API and the real fixture files. The reconstruction
    # procedure must execute and validate metadata, committed rows and app output.
    if REHEARSAL_ROOT.exists():
        shutil.rmtree(REHEARSAL_ROOT)
    if REHEARSAL_ROOT.parent.exists():
        shutil.rmtree(REHEARSAL_ROOT.parent / 'bbackup-covered-root-rehearsal-restore',
                      ignore_errors=True)
    restore_tree = REHEARSAL_ROOT.parent / 'bbackup-covered-root-rehearsal-restore'
    restore_tree.mkdir(mode=0o700)
    target_files = restore_tree / str(FILES).lstrip('/')
    target_files.parent.mkdir(mode=0o755, parents=True)
    run(['/bin/cp', '-a', str(FILES), str(target_files.parent)], timeout=60)
    archive_path = Path('/var/lib/bbackup-covered-root-state/rehearsal/database.sqlite')
    archive_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive_path.unlink(missing_ok=True)
    with sqlite3.connect(DATABASE.as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(archive_path) as target:
            source.backup(target)
    archive_digest = sha256_file(archive_path)
    manifest = {'schema_version': 2, 'exports': [{
        'source': 'database', 'kind': 'sqlite', 'format': 'sqlite',
        'path': str(archive_path), 'coverage_root_id': 'database',
        'original_path': str(DATABASE), 'reviewed_identity': proof['sqlite_identity'],
        'recipe_digest': '0' * 64, 'baseline_digest': proof['baseline_digest'] or '0' * 64,
        'export_sha256': archive_digest}]}
    manifest_path = archive_path.parent / 'bbackup-capture.json'
    private_json(manifest_path, manifest)
    restore_manifest = restore_tree / str(manifest_path).lstrip('/')
    restore_manifest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(manifest_path, restore_manifest)
    restore_archive = restore_tree / str(archive_path).lstrip('/')
    restore_archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(archive_path, restore_archive)
    result = run(['/usr/bin/python3', str(procedure), 'rehearsal', str(restore_tree)],
                 timeout=120)
    if result.returncode:
        raise RuntimeError('fixture reconstruction recipe rehearsal failed')
    output = REHEARSAL_ROOT / 'var/lib/bbackup-covered-root/files/app-output.json'
    if not output.is_file() or sha256_file(output) != proof['app_output_sha256']:
        raise RuntimeError('fixture reconstruction recipe output was not verified')
    evidence_path = MATERIAL / 'recipes' / 'fixture_reconstruction.evidence.json'
    evidence_doc = {'schema_version': 1, 'recipe_id': 'fixture_reconstruction',
        'recipe_sha256': sha256_file(procedure), 'environment': 'isolated-local-fixture',
        'checks': {'procedure_executed': True, 'result_verified': True}}
    private_json(evidence_path, evidence_doc)
    return {'recipe_id': 'fixture_reconstruction',
        'recipe_sha256': sha256_file(procedure),
        'procedure_path': str(procedure),
        'evidence_path': str(evidence_path),
        'evidence_sha256': sha256_file(evidence_path)}


def replacement_role(candidate):
    mount_inputs()
    mount_recovery('replacement')
    install_candidate(candidate)
    proof = read_json(MATERIAL / 'source-proof.json')
    replacement_machine = machine_digest()
    if replacement_machine == proof['source_machine_digest']:
        raise RuntimeError('replacement machine identity is not distinct from source')
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(MATERIAL / 'policy-candidate.json', CONFIG)
    shutil.copyfile(MATERIAL / 'bindings-candidate.json', BINDINGS)
    CONFIG.chmod(0o600)
    BINDINGS.chmod(0o600)
    # Deliberate replacement enrollment creates machine-local state in the new
    # overlay. No source OS overlay or source enrollment files are attached.
    production('service', 'enroll')
    verify_root_enrollment()
    install_production_units(include_fixture=False)
    base_proc = MATERIAL / 'recipes' / 'base-reconstruction.py'
    run(['/usr/bin/python3', str(base_proc)], timeout=120)
    if RESTORE_TARGET.exists():
        shutil.rmtree(RESTORE_TARGET)
    restored = production('restore', '--repository', 'local', '--snapshot',
        proof['snapshot_id'], '--target', str(RESTORE_TARGET), timeout=300)[1].get('data', {})
    if restored.get('files_verified') is not True:
        raise RuntimeError('production replacement restore did not verify files')
    fixture_proc = MATERIAL / 'recipes' / 'fixture-reconstruction.py'
    # Refresh the baseline value in the persistent source proof before the
    # exact reviewed recipe validates the encrypted capture manifest.
    result = run(['/usr/bin/python3', str(fixture_proc), 'replacement', str(RESTORE_TARGET)],
                 timeout=180)
    if result.returncode:
        raise RuntimeError('fixture reconstruction and startup recipe failed on replacement')
    verify_installed_units(include_fixture=True)
    manifest_paths = list(RESTORE_TARGET.rglob('bbackup-capture.json'))
    manifest_digest = sha256_file(manifest_paths[0])
    proof_public = {'replacement_machine_digest': replacement_machine,
        'source_machine_digest': proof['source_machine_digest'],
        'candidate_wheel_sha256': EXPECTED_CANDIDATE,
        'baseline_digest': proof['baseline_digest'], 'candidate_digest': proof['candidate_digest'],
        'snapshot_id': proof['snapshot_id'], 'capture_manifest_sha256': manifest_digest,
        'sqlite_export_sha256': proof['sqlite_export_sha256'],
        'fixture_logical_bytes': proof['fixture_logical_bytes'], 'acl_applied': proof['acl_applied'],
        'checks': {'distinct_machine_identities': True,
            'root_owned_schema3_enrollment': True,
            'actual_guest_inventory_and_base_reconstruction': True,
            'production_restore': True,
            'encrypted_capture_manifest_verified': True,
            'sqlite_transactional_export_hash_and_integrity': True,
            'committed_rows_only': True, 'raw_database_and_sidecars_excluded': True,
            'numeric_owner_mode_link_xattr_acl_restore': True,
            'fixture_account_reconstructed': True,
            'synthetic_secret_and_application_output': True,
            'fixture_logical_size_under_1_mib': proof['fixture_logical_bytes'] < 1024 * 1024}}
    if not all(proof_public['checks'].values()):
        raise RuntimeError('replacement proof contains a failed check')
    output = MATERIAL / 'replacement-proof.json'
    private_json(output, proof_public)
    log('COVERED_ROOT_RESULT ' + json.dumps(proof_public, sort_keys=True))
    os.sync()
    rootctl('poweroff', timeout=10)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('covered-root qualification guest must run as root')
    if len(sys.argv) != 2 or sys.argv[1] not in ('source', 'replacement'):
        raise RuntimeError('covered-root guest role is invalid')
    role = sys.argv[1]
    candidate = mount_inputs()
    # mount_recovery is called by each role after the candidate input identity
    # has been checked, keeping the replacement's only data handoff explicit.
    log(f'BOOT_ROLE {role} machine_digest={machine_digest()}')
    if role == 'source':
        source_role(candidate)
    else:
        replacement_role(candidate)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        log(f'FAIL {type(exc).__name__}: {exc}')
        try:
            os.sync()
            rootctl('poweroff', check=False, timeout=10)
        except Exception:
            pass
        raise
"""


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


def _create_covered_root_seed(stage: Path, candidate_digest: str, codename: str,
                              role: str) -> Path:
    if role not in {"source", "replacement"}:
        raise ValueError("covered-root seed role must be source or replacement")
    seed_dir = stage / f"covered-root-seed-{role}"
    seed_dir.mkdir(mode=0o700)
    guest = (COVERED_ROOT_GUEST_STAGE.replace("@CANDIDATE_SHA256@", candidate_digest)
             .replace("@UBUNTU_RELEASE@", codename)
             .replace("@UBUNTU_PYTHON@", UBUNTU_PYTHON[codename]))
    service = (
        "[Unit]\nDescription=Task-owned covered-root qualification\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=oneshot\n"
        f"ExecStart=/usr/bin/python3 /usr/local/sbin/bbackup-covered-root-qualification.py {role}\n"
        "StandardOutput=journal+console\nStandardError=inherit\n"
        "TimeoutStartSec=infinity\n\n[Install]\nWantedBy=multi-user.target\n"
    )
    failure_command = (
        "systemctl start bbackup-covered-root-qualification.service || { "
        "systemctl status --no-pager --full bbackup-covered-root-qualification.service; "
        "journalctl --no-pager -n 40 -u bbackup-covered-root-qualification.service; "
        "systemctl poweroff; exit 1; }"
    )
    user = (
        "#cloud-config\n"
        "write_files:\n"
        "  - path: /usr/local/sbin/bbackup-covered-root-qualification.py\n"
        "    owner: root:root\n"
        "    permissions: '0700'\n"
        "    encoding: b64\n"
        f"    content: {base64.b64encode(guest.encode()).decode()}\n"
        "  - path: /etc/systemd/system/bbackup-covered-root-qualification.service\n"
        "    owner: root:root\n"
        "    permissions: '0644'\n"
        "    content: |\n"
        + "".join("      " + line + "\n" for line in service.splitlines())
        + "runcmd:\n"
        "  - [systemctl, daemon-reload]\n"
        "  - [systemctl, enable, bbackup-covered-root-qualification.service]\n"
        f"  - [sh, -c, {json.dumps(failure_command)}]\n"
    )
    (seed_dir / "user-data").write_text(user, encoding="utf-8")
    (seed_dir / "meta-data").write_text(
        f"instance-id: bbackup-covered-root-{role}-{uuid.uuid4().hex}\n"
        f"local-hostname: bbackup-covered-root-{role}\n", encoding="ascii")
    for child in seed_dir.iterdir():
        child.chmod(0o600)
    return _write_iso(stage, seed_dir, f"covered-root-seed-{role}.iso", "cidata")


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
                           timeout: int, *, accelerator: str = "kvm") -> str:
    if accelerator not in {"kvm", "tcg"}:
        raise ValueError("unsupported guest accelerator")
    name = f"bbackup-systemd-qualification-{uuid.uuid4().hex[:12]}"
    existing = _run(["docker", "container", "inspect", name])
    if existing.returncode == 0:
        raise RuntimeError("task container name collision; refuse to touch it")
    docker_command = [
        "docker", "run", "--rm", "--name", name, "--platform=linux/amd64",
        *(["--device=/dev/kvm:/dev/kvm:rwm"] if accelerator == "kvm" else []),
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
        f"exec qemu-system-x86_64 -machine q35,accel={accelerator} "
        f"-cpu {'host' if accelerator == 'kvm' else 'max'} -smp 2 -m 4096 "
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


def _docker_covered_root_qualification(stage: Path, image: Path, source_seed: Path,
                                       replacement_seed: Path, inputs_iso: Path,
                                       timeout: int, *, accelerator: str = "kvm") -> str:
    if accelerator not in {"kvm", "tcg"}:
        raise ValueError("unsupported guest accelerator")
    if source_seed.name == replacement_seed.name:
        raise ValueError("source and replacement require distinct cloud-init seed images")
    name = f"bbackup-covered-root-qualification-{uuid.uuid4().hex[:12]}"
    existing = _run(["docker", "container", "inspect", name])
    if existing.returncode == 0:
        raise RuntimeError("task container name collision; refuse to touch it")
    qemu_flags = (
        f"-machine q35,accel={accelerator} "
        f"-cpu {'host' if accelerator == 'kvm' else 'max'} -smp 2 -m 4096 "
    )
    common = (
        "-drive file=/work/{overlay},if=virtio,format=qcow2 "
        "-drive file=/work/recovery.raw,if=virtio,format=raw "
        "-drive file=/work/{seed},media=cdrom,readonly=on "
        "-drive file=/work/{inputs},media=cdrom,readonly=on "
        "-netdev user,id=net0 -device virtio-net-pci,netdev=net0 "
        "-display none -serial stdio -monitor none -no-reboot"
    )
    source_qemu = ("qemu-system-x86_64 " + qemu_flags
        + common.format(overlay="source-guest.qcow2", seed=source_seed.name,
                        inputs=inputs_iso.name)
        + " > /work/source-serial.log 2>&1")
    replacement_qemu = ("qemu-system-x86_64 " + qemu_flags
        + common.format(overlay="replacement-guest.qcow2", seed=replacement_seed.name,
                        inputs=inputs_iso.name)
        + " > /work/replacement-serial.log 2>&1")
    shell = (
        "apk update >/dev/null; "
        "apk add --no-cache qemu=10.1.5-r0 qemu-img=10.1.5-r0 "
        "qemu-system-x86_64=10.1.5-r0 seabios=1.17.0-r0 >/dev/null; "
        "apk list -I > /work/apk-installed.txt; "
        "for p in qemu-10.1.5-r0 qemu-img-10.1.5-r0 qemu-system-x86_64-10.1.5-r0 seabios-1.17.0-r0; do "
        "grep -F \"$p \" /work/apk-installed.txt >/dev/null || { echo \"missing pinned $p\"; exit 21; }; done; "
        "qemu-img create -q -f raw /work/recovery.raw 256M; "
        "qemu-img create -q -f qcow2 -F qcow2 -b /ubuntu.img /work/source-guest.qcow2 16G; "
        "qemu-img create -q -f qcow2 -F qcow2 -b /ubuntu.img /work/replacement-guest.qcow2 16G; "
        f"{source_qemu}; "
        "grep -Fq 'SOURCE_COMPLETE {' /work/source-serial.log || "
        "{ echo 'source guest ended without completion proof'; exit 22; }; "
        f"{replacement_qemu}"
    )
    docker_command = [
        "docker", "run", "--rm", "--name", name, "--platform=linux/amd64",
        *(["--device=/dev/kvm:/dev/kvm:rwm"] if accelerator == "kvm" else []),
        "--mount", f"type=bind,src={stage},dst=/work",
        "--mount", f"type=bind,src={image},dst=/ubuntu.img,readonly",
        ALPINE_IMAGE, "sh", "-ec", shell,
    ]
    try:
        result = _run(docker_command, timeout=timeout)
        container_output = result.stdout or ""
        apk_log = stage / "apk-installed.txt"
        if apk_log.exists() and apk_log.stat().st_uid == os.geteuid():
            apk_log.chmod(0o600)
        logs = []
        for role in ("source", "replacement"):
            serial = stage / f"{role}-serial.log"
            if not serial.is_file():
                serial.write_text("", encoding="utf-8")
            if serial.stat().st_uid == os.geteuid():
                serial.chmod(0o600)
            logs.append(f"--- {role.upper()} GUEST ---\n" + serial.read_text(
                encoding="utf-8", errors="replace"))
        if result.returncode:
            raise RuntimeError(
                f"covered-root QEMU container exited with status {result.returncode}; see {stage}")
        return container_output + "\n" + "\n".join(logs)
    except RuntimeError:
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
    parser.add_argument("--scenario", choices=("timer-reboot", "covered-root"),
                        default="timer-reboot",
                        help="existing reboot drill or isolated covered-root replacement emulation")
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
    parser.add_argument("--accelerator", choices=("kvm", "tcg"), default="kvm",
                        help="KVM hardware virtualization or slower TCG software emulation")
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
    if args.scenario == "covered-root":
        source_seed = _create_covered_root_seed(
            stage, args.wheel_sha256, args.ubuntu_codename, "source")
        replacement_seed = _create_covered_root_seed(
            stage, args.wheel_sha256, args.ubuntu_codename, "replacement")
    else:
        seed = _create_seed(stage, args.wheel_sha256, args.ubuntu_codename)
    inputs_iso, input_digests = _prepare_inputs(stage, artifact, args.wheelhouse,
                                                args.requirements, restic, uv)
    _container_image()
    start = time.monotonic()
    if args.scenario == "covered-root":
        serial = _docker_covered_root_qualification(
            stage, image, source_seed, replacement_seed, inputs_iso, args.timeout_seconds,
            accelerator=args.accelerator)
        source_match = re.search(r"SOURCE_COMPLETE (\{.*\})", serial)
        replacement_match = re.search(r"COVERED_ROOT_RESULT (\{.*\})", serial)
        if source_match is None or replacement_match is None:
            failure_marker = re.search(r"FAIL [^\r\n]+", serial)
            reason = failure_marker.group(0) if failure_marker else "covered-root guest proof is incomplete"
            raise RuntimeError(f"covered-root emulation incomplete: {reason}; artifacts: {stage}")
        source = json.loads(source_match.group(1))
        evidence = json.loads(replacement_match.group(1))
        checks = evidence.get("checks")
        if (source.get("candidate_wheel_sha256") != args.wheel_sha256
                or evidence.get("candidate_wheel_sha256") != args.wheel_sha256
                or source.get("source_machine_digest") != evidence.get("source_machine_digest")
                or source.get("source_machine_digest") == evidence.get("replacement_machine_digest")
                or not isinstance(checks, dict) or not checks or not all(checks.values())
                or evidence.get("capture_manifest_sha256") != source.get("capture_manifest_sha256")
                or evidence.get("sqlite_export_sha256") != source.get("sqlite_export_sha256")
                or evidence.get("baseline_digest") != source.get("baseline_digest")
                or evidence.get("snapshot_id") != source.get("snapshot_id")
                or evidence.get("fixture_logical_bytes") != source.get("fixture_logical_bytes")
                or source.get("acl_applied") is not True
                or evidence.get("acl_applied") is not True
                or type(evidence.get("fixture_logical_bytes")) is not int
                or not 0 < evidence["fixture_logical_bytes"] < 1024 * 1024):
            raise RuntimeError(f"covered-root guest evidence failed validation; artifacts: {stage}")
        if (not re.fullmatch(r"[0-9a-f]{64}", source.get("source_machine_digest", ""))
                or not re.fullmatch(r"[0-9a-f]{64}", evidence.get("replacement_machine_digest", ""))
                or not re.fullmatch(r"[0-9a-f]{64}", source.get("baseline_digest", ""))
                or not re.fullmatch(r"[0-9a-f]{64}", source.get("candidate_digest", ""))
                or not re.fullmatch(r"[0-9a-f]{64}", source.get("capture_manifest_sha256", ""))
                or not re.fullmatch(r"[0-9a-f]{64}", source.get("sqlite_export_sha256", ""))
                or not isinstance(source.get("snapshot_id"), str) or not source["snapshot_id"]):
            raise RuntimeError(f"covered-root guest digests are malformed; artifacts: {stage}")
        timer_invocation = source.get("root_timer_invocation")
        if (not isinstance(timer_invocation, dict)
                or timer_invocation.get("unit") != "bbackup-covered.service"
                or timer_invocation.get("result") != "success"
                or timer_invocation.get("exit_status") != 0
                or timer_invocation.get("start_observed") is not True
                or timer_invocation.get("exit_observed") is not True):
            raise RuntimeError(f"covered-root root timer invocation is incomplete; artifacts: {stage}")
        evidence.update({"source_machine_digest": source["source_machine_digest"],
                         "baseline_digest": source["baseline_digest"],
                         "candidate_digest": source["candidate_digest"],
                         "snapshot_id": source["snapshot_id"],
                         "capture_manifest_sha256": source["capture_manifest_sha256"],
                         "sqlite_export_sha256": source["sqlite_export_sha256"],
                         "root_timer_invocation": source["root_timer_invocation"],
                         "acl_applied": source["acl_applied"]})
        evidence_files = ("source-serial.log", "replacement-serial.log", "apk-installed.txt",
                          "source-guest.qcow2", "replacement-guest.qcow2", "recovery.raw")
        for filename in evidence_files:
            path = stage / filename
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f"covered-root emulation artifact is missing: {filename}")
    else:
        serial = _docker_qualification(stage, image, seed, inputs_iso, args.timeout_seconds,
                                       accelerator=args.accelerator)
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
    elapsed = round(time.monotonic() - start, 1)
    private_evidence = {
        "schema_version": 1,
        "candidate": {"path_label": artifact.name, "sha256": args.wheel_sha256},
        "guest_image": {"distribution": f"Ubuntu {args.ubuntu_codename}", "codename": args.ubuntu_codename,
                        "sha256": _sha256(image), "signed_manifest": "Canonical gpgv valid"},
        "qemu_toolchain": {"image": ALPINE_IMAGE, "packages": list(ALPINE_PACKAGES),
                           "accelerator": args.accelerator,
                           "verified": "image digest and installed package versions checked at runtime"},
        "input_digests": input_digests,
        "elapsed_seconds": elapsed,
        "guest": evidence,
        "evidence_files": (list(evidence_files) if args.scenario == "covered-root" else
                            ["serial.log", "apk-installed.txt", "guest.qcow2", "qualification.json"]),
    }
    if args.scenario == "covered-root":
        source_instance_id = (stage / "covered-root-seed-source" / "meta-data").read_text(
            encoding="ascii").splitlines()[0].split(": ", 1)[1]
        replacement_instance_id = (stage / "covered-root-seed-replacement" / "meta-data").read_text(
            encoding="ascii").splitlines()[0].split(": ", 1)[1]
        if source_instance_id == replacement_instance_id:
            raise RuntimeError("source and replacement cloud-init identities must differ")
        private_evidence.update({"scenario": "covered-root",
            "qualification_classification": "local-emulation-only",
            "limitations": ["This is local VM emulation, not independent custody or a remote provider window."],
            "guest_topology": {"source_overlay": "source-guest.qcow2",
                "replacement_overlay": "replacement-guest.qcow2",
                "source_backing_image_sha256": _sha256(image),
                "replacement_backing_image_sha256": _sha256(image),
                "recovery_disk": "recovery.raw", "recovery_disk_size_bytes": 256 * 1024 * 1024,
                "source_cloud_init_instance_id": source_instance_id,
                "replacement_cloud_init_instance_id": replacement_instance_id,
                "separate_os_overlays": True, "shared_dedicated_recovery_disk": True}})
    evidence_path = stage / "host-evidence.json"
    evidence_path.write_text(json.dumps(private_evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    evidence_path.chmod(0o600)
    summary = {"success": True, "candidate_sha256": args.wheel_sha256,
               "guest": evidence.get("os"), "checks": evidence["checks"],
               "elapsed_seconds": elapsed, "private_evidence": str(evidence_path)}
    if args.scenario == "covered-root":
        summary.update({"scenario": "covered-root", "classification": "local-emulation-only"})
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"qualification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
