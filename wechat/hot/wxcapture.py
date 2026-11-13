"""Capture the WeChat 4.x WCDB passphrase and derive per-database keys (Linux).

WeChat 4.1+ no longer keeps each database's derived key in memory; it keeps only a
32-byte passphrase, and every database key is PBKDF2-HMAC-SHA512(passphrase, salt,
256000). So the passphrase is captured once at the function that configures the WCDB
cipher, then keys are derived per database and verified against page 1's HMAC.

The breakpoint address is found by static analysis of the WeChat ELF (no symbols):
locate the anchor string "com.Tencent.WCDB.Config.Cipher" in .rodata, find the
`lea rsi,[rip+disp]` that loads it in .text, require a `lea rdi,[rip+disp]` right
before it (the cipher-name argument), follow that rdi target to a second lea, then
scan backwards for the function prologue `55 41 57` (push rbp; push r15).

find_hook_offset() is pure byte parsing (safe to run anytime). capture() drives gdb to
read the passphrase from registers when the breakpoint fires, which happens while the
database is opened — i.e. during login. Needs CAP_SYS_PTRACE on the container.

This is an independent reimplementation; approach verified against the MIT-licensed
wcdb-key-tool (github.com/TANGandXUE/wcdb-key-tool).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import struct
import subprocess
import tempfile

KEY_SZ = 32
ANCHOR = b'com.Tencent.WCDB.Config.Cipher'
LEA_RSI = b'\x48\x8d\x35'  # lea rsi,[rip+disp32]
LEA_RDI = b'\x48\x8d\x3d'  # lea rdi,[rip+disp32]
FUNC_HEAD = b'\x55\x41\x57'  # push rbp; push r15
STATE = pathlib.Path.home() / '.wxbot'


def _sections(data: bytes) -> dict:
    if data[:4] != b'\x7fELF' or data[4] != 2:
        raise RuntimeError('not an ELF64 file')
    shoff = struct.unpack_from('<Q', data, 40)[0]
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 58)
    raw = []
    for i in range(shnum):
        off = shoff + i * shentsize
        name, _t, _f, addr, soff, size = struct.unpack_from('<IIQQQQ', data, off)
        raw.append((name, addr, soff, size))
    stroff, strsize = raw[shstrndx][2], raw[shstrndx][3]
    strtab = data[stroff : stroff + strsize]
    out = {}
    for name, addr, soff, size in raw:
        end = strtab.find(b'\0', name)
        nm = strtab[name : end if end >= 0 else None].decode('utf-8', 'replace')
        if nm:
            out[nm] = (addr, data[soff : soff + size])
    return out


def _refs(text_addr: int, text: bytes, opcode: bytes, target_va: int):
    hits = []
    i = text.find(opcode)
    while i != -1:
        if i + 7 <= len(text):
            disp = struct.unpack_from('<i', text, i + 3)[0]
            if text_addr + i + 7 + disp == target_va:
                hits.append(i)
        i = text.find(opcode, i + 1)
    return hits


def find_hook_offset(binary_path: str) -> int:
    # Cache the result: the ELF scan is memory-heavy and the binary rarely changes.
    cache = STATE / 'hook.json'
    try:
        c = json.loads(cache.read_text())
        if c.get('mtime') == pathlib.Path(binary_path).stat().st_mtime_ns:
            return c['offset']
    except (OSError, ValueError, KeyError):
        pass
    off = _scan_hook_offset(binary_path)
    STATE.mkdir(mode=0o700, exist_ok=True)
    cache.write_text(json.dumps({'offset': off, 'mtime': pathlib.Path(binary_path).stat().st_mtime_ns}))
    return off


def _scan_hook_offset(binary_path: str) -> int:
    data = pathlib.Path(binary_path).read_bytes()
    secs = _sections(data)
    rodata_addr, rodata = secs['.rodata']
    text_addr, text = secs['.text']
    candidates = set()
    pos = rodata.find(ANCHOR)
    while pos != -1:
        anchor_va = rodata_addr + pos
        for r1 in _refs(text_addr, text, LEA_RSI, anchor_va):
            if r1 >= 7 and text[r1 - 7 : r1 - 4] == LEA_RDI:
                unk_disp = struct.unpack_from('<i', text, r1 - 4)[0]
                unk_va = text_addr + r1 + unk_disp
                for r2 in _refs(text_addr, text, LEA_RSI, unk_va):
                    for c in range(r2, max(0, r2 - 0x500) - 1, -1):
                        if text[c : c + 3] == FUNC_HEAD:
                            candidates.add(text_addr + c)
                            break
        pos = rodata.find(ANCHOR, pos + 1)
    if not candidates:
        raise RuntimeError('breakpoint not found (unsupported WeChat version)')
    return sorted(candidates)[0]


def runtime_base(pid: int, binary_name: str) -> int:
    for line in pathlib.Path(f'/proc/{pid}/maps').read_text().splitlines():
        p = line.split()
        if len(p) >= 6 and p[5].endswith('/' + binary_name) and 'x' in p[1]:
            return int(p[0].split('-')[0], 16)
    raise RuntimeError('runtime base not found')


# gdb LAUNCHES WeChat as its own child, so the kernel's restricted-ptrace policy
# (yama ptrace_scope=1: a tracer may trace only its descendants) permits it without
# any capability or host change. WeChat runs free until the breakpoint fires at login;
# then we read the passphrase, detach (WeChat keeps running, reparented to the launcher)
# and quit.
_GDB = """set pagination off
set height 0
set follow-fork-mode parent
set detach-on-fork on
set follow-exec-mode same
file {binary}
starti
python
import gdb
OFF = {off}
pid = gdb.selected_inferior().pid
# PIE load base = the lowest mapping of the binary (file offset 0), not the exec segment.
starts = [int(l.split('-')[0], 16) for l in open('/proc/%d/maps' % pid)
          if l.rstrip().endswith('/{name}')]
base = min(starts)
# Let the loader map and relocate the main image, then plant the real breakpoint.
ehdr = open('{binary}', 'rb').read(64)
import struct as _s
entry = base + _s.unpack_from('<Q', ehdr, 24)[0]
gdb.execute('tbreak *%d' % entry)
gdb.execute('continue')

# Self-verifying: at each hit, read 32-byte blobs from argument registers (and one level
# of pointer indirection) and keep the one that actually derives this database's key.
# This avoids depending on the exact calling convention of this WeChat build.
import hashlib, hmac
SALT = bytes.fromhex("{salt}")
BODY = bytes.fromhex("{body}")
EXPECT = bytes.fromhex("{expect}")
MAC_SALT = bytes(b ^ 0x3A for b in SALT)
inf = gdb.selected_inferior()

def _is_pp(cand):
    if len(cand) != 32 or len(set(cand)) < 8:
        return False
    enc = hashlib.pbkdf2_hmac('sha512', cand, SALT, 256000, 32)
    mk = hashlib.pbkdf2_hmac('sha512', enc, MAC_SALT, 2, 32)
    return hmac.compare_digest(hmac.new(mk, BODY, hashlib.sha512).digest(), EXPECT)

def _read(addr, n):
    try:
        return inf.read_memory(addr, n).tobytes()
    except Exception:
        return b''

def _ptr(addr):
    d = _read(addr, 8)
    return int.from_bytes(d, 'little') if len(d) == 8 else 0

class BP(gdb.Breakpoint):
    def stop(self):
        try:
            regs = {{r: int(gdb.parse_and_eval('$' + r)) for r in
                     ('rdi', 'rsi', 'rdx', 'rcx', 'r8', 'r9', 'rax')}}
            seen = set()
            print("WXHIT")
            for v in regs.values():
                if not (0x1000 < v < 0x7fffffffffff):
                    continue
                for addr in (v, _ptr(v), _ptr(v + 8)):
                    if addr and addr not in seen and 0x1000 < addr < 0x7fffffffffff:
                        seen.add(addr)
                        cand = _read(addr, 32)
                        if len(cand) == 32 and _is_pp(cand):
                            print("WXKEY=" + cand.hex())
                            gdb.execute("detach"); gdb.execute("quit"); return True
        except Exception as e:
            print("WXERR=" + str(e))
        return False

BP("*%d" % (base + OFF))
end
continue
quit
"""


def capture(binary_path: str, targets: dict, timeout: int = 300, log=print) -> str | None:
    """Start WeChat under gdb and wait for the breakpoint (fires when the user logs in).
    targets: {db_path: (mac_salt, body, expected)} — one is enough to verify the passphrase."""
    off = find_hook_offset(binary_path)
    name = pathlib.Path(binary_path).name
    mac_salt, body, expected = next(iter(targets.values()))
    salt = bytes(b ^ 0x3A for b in mac_salt)
    log(f'launching WeChat under gdb, breakpoint offset {off:#x}')
    with tempfile.NamedTemporaryFile('w', suffix='.gdb', delete=False) as f:
        f.write(_GDB.format(
            binary=binary_path, off=off, name=name,
            salt=salt.hex(), body=body.hex(), expect=expected.hex(),
        ))
        script = f.name
    try:
        proc = subprocess.run(
            ['gdb', '-batch', '-nx', '-x', script], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        # Kill gdb AND the orphaned WeChat, so the supervisor re-arms a fresh gdb.
        subprocess.run(['pkill', '-9', 'gdb'], capture_output=True)
        subprocess.run(['pkill', '-9', '-x', 'wechat'], capture_output=True)
        log('capture: timed out, re-arming')
        return None
    finally:
        pathlib.Path(script).unlink(missing_ok=True)
    hits = 0
    for line in proc.stdout.splitlines():
        if line.startswith('WXKEY='):
            return line[6:].strip()
        if line == 'WXHIT':
            hits += 1
        if line.startswith('WXERR='):
            log('gdb: ' + line[6:])
    tail = ' | '.join((proc.stdout[-300:] + proc.stderr[-300:]).split('\n'))[-300:]
    log(f'capture: no key (rc={proc.returncode}, breakpoint hits={hits}) {tail}')
    return None


def save_passphrase(hex_key: str) -> None:
    STATE.mkdir(mode=0o700, exist_ok=True)
    p = STATE / 'passphrase.json'
    p.write_text(json.dumps({'passphrase': hex_key}))
    p.chmod(0o600)


def load_passphrase() -> str | None:
    try:
        return json.loads((STATE / 'passphrase.json').read_text())['passphrase']
    except (OSError, ValueError, KeyError):
        return None


def derive_keys(passphrase_hex: str, read_targets, log=print) -> dict:
    """passphrase + each db's page-1 HMAC -> {db_path: enc_key_hex}."""
    passphrase = bytes.fromhex(passphrase_hex)
    out = {}
    for path, target in read_targets().items():
        mac_salt, body, expected = target
        salt = bytes(b ^ 0x3A for b in mac_salt)
        enc = hashlib.pbkdf2_hmac('sha512', passphrase, salt, 256000, KEY_SZ)
        mac_key = hashlib.pbkdf2_hmac('sha512', enc, mac_salt, 2, 32)
        if hmac.compare_digest(hmac.new(mac_key, body, hashlib.sha512).digest(), expected):
            out[path] = enc.hex()
    log(f'derived {len(out)}/{len(read_targets())} keys from passphrase')
    return out
