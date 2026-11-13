"""Starts and supervises WeChat, and can read its database keys.

Why a launcher: Ubuntu's Yama policy (ptrace_scope=1) only lets a process read the
memory of its own descendants. This launcher starts WeChat and registers as a child
subreaper, so WeChat (and anything it forks) stays below it and the launcher may read
/proc/<pid>/mem without extra capabilities or host-wide changes.

Key extraction (on SIGUSR1, or at most once per minute while keys are missing):
WeChat 4.x stores chats in SQLCipher 4 databases (4096-byte pages, HMAC-SHA512,
PBKDF2-HMAC-SHA512). Every file starts with a random 16-byte salt and the running
process keeps each open database's *derived* 32-byte key in memory. A candidate is
checked cheaply: mac_key = PBKDF2-SHA512(candidate, salt ^ 0x3a, 2 rounds), then the
HMAC of page 1 must match the one stored at the end of page 1.
Found keys are written to ~/.wxbot/keys.json (mode 600), keyed by database path.
"""

import ctypes
import hashlib
import hmac
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

import numpy as np

WECHAT = '/opt/wechat/wechat'
HOME = pathlib.Path.home()
DATA_ROOT = HOME / 'xwechat_files'
KEY_FILE = HOME / '.wxbot' / 'keys.json'
HOT_DIR = pathlib.Path('/opt/wxhot')
WANTED = ('message/message_0.db', 'contact/contact.db', 'session/session.db')

PAGE = 4096
SALT = 16
RESERVE = 80  # 16-byte IV + 64-byte HMAC-SHA512
CHUNK = 16 * 1024 * 1024

_extract_requested = True
_force = False


def log(msg: str) -> None:
    print(f'[wxlauncher] {msg}', file=sys.stderr, flush=True)


def _set_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_CHILD_SUBREAPER = 36
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        log('could not become subreaper')


def _wechat_pids() -> list[int]:
    pids = []
    for entry in pathlib.Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                if (entry / 'comm').read_text().strip() == 'wechat':
                    pids.append(int(entry.name))
            except OSError:
                pass
    return pids


# ───────────────────────── key extraction ─────────────────────────


def _targets() -> dict[str, tuple[bytes, bytes, bytes]]:
    """db path -> (mac_salt, page1 body to authenticate, expected hmac)."""
    targets = {}
    for account in DATA_ROOT.glob('wxid_*'):
        for rel in WANTED:
            path = account / 'db_storage' / rel
            try:
                with open(path, 'rb') as f:
                    page = f.read(PAGE)
            except OSError:
                continue
            if len(page) < PAGE:
                continue
            salt = page[:SALT]
            mac_salt = bytes(b ^ 0x3A for b in salt)
            body = page[SALT : PAGE - RESERVE + 16] + (1).to_bytes(4, 'little')
            expected = page[PAGE - 64 : PAGE]
            targets[str(path)] = (mac_salt, body, expected)
    return targets


def _check(candidate: bytes, target: tuple[bytes, bytes, bytes]) -> bool:
    mac_salt, body, expected = target
    mac_key = hashlib.pbkdf2_hmac('sha512', candidate, mac_salt, 2, 32)
    return hmac.compare_digest(hmac.new(mac_key, body, hashlib.sha512).digest(), expected)


def _regions(pid: int):
    with open(f'/proc/{pid}/maps') as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2 or parts[1][:2] != 'rw':
                continue
            path = parts[5] if len(parts) > 5 else ''
            if path and path != '[heap]':
                continue  # skip file-backed mappings and stacks
            start, end = (int(x, 16) for x in parts[0].split('-'))
            yield start, end


def _candidates(buf: bytes):
    """8-byte aligned 32-byte windows that look like random key material."""
    n = (len(buf) - 32) // 8
    if n <= 0:
        return
    arr = np.frombuffer(buf, dtype=np.uint8)
    windows = np.lib.stride_tricks.as_strided(arr, shape=(n, 32), strides=(8, 1))
    zeros = (windows == 0).sum(axis=1)
    # Random 32 bytes almost never have more than 2 zero bytes or long runs of one
    # value; pointers, integers and text nearly always do.
    first = windows[:, :8].astype(np.int16)
    spread = first.max(axis=1) - first.min(axis=1)
    mask = (zeros <= 2) & (spread > 40)
    for idx in np.nonzero(mask)[0]:
        window = buf[idx * 8 : idx * 8 + 32]
        if len(set(window)) >= 24:
            yield window


def extract_keys() -> int:
    targets = _targets()
    if not targets:
        log('no databases yet (not logged in?)')
        return 0
    try:
        known = json.loads(KEY_FILE.read_text())
    except (OSError, ValueError):
        known = {}
    # Drop keys that no longer match (e.g. after re-login), keep valid ones.
    for path in list(known):
        if path not in targets or not _check(bytes.fromhex(known[path]), targets[path]):
            known.pop(path)
    if len(known) < len(targets):
        cap = _hot('wxcapture')
        passphrase = cap.load_passphrase()
        if passphrase:
            for path, key_hex in cap.derive_keys(passphrase, _targets, log).items():
                if path in targets and _check(bytes.fromhex(key_hex), targets[path]):
                    known[path] = key_hex
    KEY_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = KEY_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(known))
    os.chmod(tmp, 0o600)
    tmp.replace(KEY_FILE)
    log(f'{len(known)}/{len(targets)} keys')
    return len(known)


def _hot(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, HOT_DIR / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_capture_thread = None


def _have_passphrase() -> bool:
    try:
        return _hot('wxcapture').load_passphrase() is not None
    except Exception:
        return False


def _start_under_gdb() -> None:
    """Start WeChat as gdb's child and capture the passphrase when it logs in. Runs in a
    thread: gdb keeps WeChat alive until the breakpoint fires (or a timeout recycles it)."""
    global _capture_thread
    if _capture_thread and _capture_thread.is_alive():
        return

    def worker():
        try:
            cap = _hot('wxcapture')
            pp = cap.capture(WECHAT, _targets(), timeout=1200, log=log)
            if pp:
                cap.save_passphrase(pp)
                log('passphrase captured and cached')
        except Exception as exc:
            log(f'capture failed: {type(exc).__name__}: {exc}')

    import threading

    _capture_thread = threading.Thread(target=worker, daemon=True)
    _capture_thread.start()


# ───────────────────────── supervision ─────────────────────────


BOT = HOT_DIR / 'wxbot.py'


def _supervise_bot() -> None:
    """Keep the bot running from the hot folder. To load new bot code, just kill it:
    `pkill -f /opt/wxhot/wxbot.py` - it is restarted here within a few seconds."""
    for entry in pathlib.Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                if str(BOT) in (entry / 'cmdline').read_bytes().decode(errors='ignore'):
                    return
            except OSError:
                pass
    if BOT.exists():
        log('starting bot')
        subprocess.Popen(['python3', str(BOT)], stdout=subprocess.DEVNULL)


def _on_usr1(signum, frame):
    global _extract_requested, _force
    _extract_requested = True
    _force = True


def main() -> None:
    global _extract_requested, _force
    _set_subreaper()
    signal.signal(signal.SIGUSR1, _on_usr1)
    (HOME / '.wxbot').mkdir(mode=0o700, exist_ok=True)
    (HOME / '.wxbot' / 'launcher.pid').write_text(str(os.getpid()))
    last_try = 0.0
    while True:
        # Reap any exited children/orphans (we are their subreaper).
        try:
            while os.waitpid(-1, os.WNOHANG)[0] > 0:
                pass
        except ChildProcessError:
            pass
        _supervise_bot()
        manual = (HOME / '.wxbot' / 'manual').exists()
        if not _wechat_pids() and not (_capture_thread and _capture_thread.is_alive()):
            if manual or _have_passphrase():
                # Manual mode (or key already cached): just keep WeChat running, never gdb.
                log('starting wechat')
                subprocess.Popen([WECHAT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                log('starting wechat under gdb (first-time key capture)')
                _start_under_gdb()
            time.sleep(20)
            _extract_requested = True
        if _extract_requested and (_force or time.time() - last_try > 60):
            _extract_requested = False
            _force = False
            last_try = time.time()
            try:
                if extract_keys() < len(_targets()):
                    _extract_requested = True  # retry later (e.g. not logged in yet)
            except Exception as exc:  # never let extraction kill the supervisor
                log(f'extract failed: {type(exc).__name__}: {exc}')
        time.sleep(3)


if __name__ == '__main__':
    main()
