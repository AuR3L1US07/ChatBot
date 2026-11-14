"""Read WeChat 4.x (Linux) encrypted databases with keys found by wxlauncher.py.

Each database is SQLCipher 4: 4096-byte pages, AES-256-CBC, 80 reserved bytes per page
(16-byte IV + 64-byte HMAC-SHA512). The first 16 bytes of the file are the salt. We
decrypt pages ourselves (python3-cryptography) into a plain SQLite file under /dev/shm
and query it with the standard sqlite3 module. The write-ahead log (-wal) is applied on
every refresh so new messages are visible within seconds, without waiting for WeChat to
checkpoint. Nothing is ever written to WeChat's own files.

Memory: the copy is built by streaming (1 MB at a time) and afterwards only the pages
that newly committed WAL frames touch are rewritten in place. Nothing database-sized is
ever held in this process - an earlier version kept the whole decrypted file in RAM and
copied it on every pass, which left the bot at ~200 MB.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE = 4096
RESERVE = 80
SQLITE_HEADER = b'SQLite format 3\x00'
KEY_FILE = pathlib.Path.home() / '.wxbot' / 'keys.json'
PLAIN_DIR = pathlib.Path('/dev/shm/wxbot')
WAL_HEADER = 32
FRAME = 24 + PAGE
CHUNK_PAGES = 256  # pages decrypted per read while building the base copy (1 MB)


def load_keys() -> dict[str, bytes]:
    try:
        return {k: bytes.fromhex(v) for k, v in json.loads(KEY_FILE.read_text()).items()}
    except (OSError, ValueError):
        return {}


def _decrypt_page(key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE - RESERVE : PAGE - RESERVE + 16]
    start = 16 if pgno == 1 else 0
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(page[start : PAGE - RESERVE]) + dec.finalize()
    head = SQLITE_HEADER if pgno == 1 else b''
    return head + plain + page[PAGE - RESERVE :]


class PlainCopy:
    """A decrypted, periodically refreshed copy of one WeChat database."""

    def __init__(self, src: str, key: bytes):
        self.src = pathlib.Path(src)
        self.wal = self.src.with_name(self.src.name + '-wal')
        self.key = key
        PLAIN_DIR.mkdir(mode=0o700, exist_ok=True)
        name = hashlib.sha1(str(self.src).encode()).hexdigest()[:12] + '_' + self.src.name
        self.dst = PLAIN_DIR / name
        self._last_sig: tuple | None = None
        # What self.dst holds: which base file, which WAL generation (its salts) and how
        # far into that WAL (the offset just past the last applied commit frame).
        self._state: dict | None = None

    def _sig(self) -> tuple:
        out = []
        for f in (self.src, self.wal):
            try:
                st = os.stat(f)
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def _write_base(self, tmp: pathlib.Path) -> None:
        pgno = 0
        with open(self.src, 'rb') as src, open(tmp, 'wb') as out:
            os.chmod(tmp, 0o600)
            while True:
                chunk = src.read(PAGE * CHUNK_PAGES)
                if len(chunk) < PAGE:
                    break
                for i in range(len(chunk) // PAGE):
                    pgno += 1
                    out.write(_decrypt_page(self.key, chunk[i * PAGE : (i + 1) * PAGE], pgno))

    def _wal_frames(self, start: int):
        """Committed frames of the WAL from byte `start` on.
        -> (salts, {pgno: encrypted page}, database size in pages after the last commit
        (0: no commit), offset just past that commit); None if there is no usable WAL."""
        try:
            with open(self.wal, 'rb') as f:
                head = f.read(WAL_HEADER)
                if len(head) < WAL_HEADER:
                    return None
                f.seek(0, 2)
                if f.tell() < start:
                    return None  # shorter than what was already applied: a new log
                f.seek(start)
                data = f.read()
        except OSError:
            return None
        salts = head[16:24]
        pending: dict[int, bytes] = {}
        committed: dict[int, bytes] = {}
        size_pages, end, off = 0, start, 0
        while off + FRAME <= len(data):
            pgno, commit_size = struct.unpack_from('>II', data, off)
            if data[off + 8 : off + 16] != salts:
                break  # stale frames from an older WAL generation
            pending[pgno] = data[off + 24 : off + FRAME]
            off += FRAME
            if commit_size:
                committed.update(pending)
                pending.clear()
                size_pages, end = commit_size, start + off
        return salts, committed, size_pages, end

    def _patch(self, path: pathlib.Path, committed: dict[int, bytes], size_pages: int) -> None:
        with open(path, 'r+b') as f:
            if size_pages:
                for pgno, page in committed.items():
                    if 1 <= pgno <= size_pages:
                        f.seek((pgno - 1) * PAGE)
                        f.write(_decrypt_page(self.key, page, pgno))
                f.truncate(size_pages * PAGE)  # the commit says how big the database is now
            # Mark the plain copy as a normal rollback-journal database.
            f.seek(0, 2)
            if f.tell() >= 20:
                f.seek(18)
                f.write(b'\x01\x01')

    def refresh(self) -> pathlib.Path:
        sig = self._sig()
        if sig == self._last_sig and self.dst.exists():
            return self.dst  # nothing changed since the last copy
        st = self._state
        if st is not None and st['base'] == sig[0] and st['salts'] is not None and self.dst.exists():
            wal = self._wal_frames(st['off'])
            if wal is not None and wal[0] == st['salts']:
                # Same base file, same log, just longer: rewrite only the new pages.
                # (In place - the only reader is this process, between two refreshes.)
                _salts, committed, size_pages, end = wal
                if size_pages:
                    self._state = None  # a crash half-way must lead to a full rebuild
                    self._patch(self.dst, committed, size_pages)
                    self._state = {**st, 'off': end}
                self._last_sig = sig
                return self.dst
        # First time, a checkpoint rewrote the base file, or the log started over.
        tmp = self.dst.with_suffix('.tmp')
        self._write_base(tmp)
        wal = self._wal_frames(WAL_HEADER)
        salts, committed, size_pages, end = wal if wal is not None else (None, {}, 0, WAL_HEADER)
        self._patch(tmp, committed, size_pages)
        tmp.replace(self.dst)
        self._state = {'base': sig[0], 'salts': salts, 'off': end}
        self._last_sig = sig
        return self.dst

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f'file:{self.refresh()}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        return conn
