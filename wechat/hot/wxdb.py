"""Read WeChat 4.x (Linux) encrypted databases with keys found by wxlauncher.py.

Each database is SQLCipher 4: 4096-byte pages, AES-256-CBC, 80 reserved bytes per page
(16-byte IV + 64-byte HMAC-SHA512). The first 16 bytes of the file are the salt. We
decrypt pages ourselves (python3-cryptography) into a plain SQLite file under /dev/shm
and query it with the standard sqlite3 module. The write-ahead log (-wal) is applied on
every refresh so new messages are visible within seconds, without waiting for WeChat to
checkpoint. Nothing is ever written to WeChat's own files.
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
        self.key = key
        PLAIN_DIR.mkdir(mode=0o700, exist_ok=True)
        name = hashlib.sha1(str(self.src).encode()).hexdigest()[:12] + '_' + self.src.name
        self.dst = PLAIN_DIR / name
        self._base: bytearray | None = None
        self._base_sig: tuple | None = None

    def _load_base(self) -> bytearray:
        st = os.stat(self.src)
        sig = (st.st_mtime_ns, st.st_size)
        if self._base is None or sig != self._base_sig:
            data = self.src.read_bytes()
            out = bytearray()
            for i in range(len(data) // PAGE):
                out += _decrypt_page(self.key, data[i * PAGE : (i + 1) * PAGE], i + 1)
            self._base, self._base_sig = out, sig
        return self._base

    def _apply_wal(self, db: bytearray) -> bytearray:
        wal = self.src.with_name(self.src.name + '-wal')
        try:
            data = wal.read_bytes()
        except OSError:
            return db
        if len(data) < 32:
            return db
        salt1, salt2 = data[16:20], data[20:24]
        pending: dict[int, bytes] = {}
        committed: dict[int, bytes] = {}
        size_pages = len(db) // PAGE
        off = 32
        while off + 24 + PAGE <= len(data):
            hdr = data[off : off + 24]
            pgno, commit_size = struct.unpack('>II', hdr[:8])
            if hdr[8:12] != salt1 or hdr[12:16] != salt2:
                break  # stale frames from an older WAL generation
            pending[pgno] = data[off + 24 : off + 24 + PAGE]
            if commit_size:
                committed.update(pending)
                pending.clear()
                size_pages = commit_size
            off += 24 + PAGE
        if not committed:
            return db
        out = bytearray(db)
        if size_pages * PAGE > len(out):
            out += bytes(size_pages * PAGE - len(out))
        for pgno, page in committed.items():
            out[(pgno - 1) * PAGE : pgno * PAGE] = _decrypt_page(self.key, page, pgno)
        return out[: size_pages * PAGE]

    def _sig(self) -> tuple:
        out = []
        for f in (self.src, self.src.with_name(self.src.name + '-wal')):
            try:
                st = os.stat(f)
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def refresh(self) -> pathlib.Path:
        # Nothing changed since the last copy: reuse it. Decrypting and writing a 30 MB
        # database every pass kept ~100 MB churning in RAM and /dev/shm.
        sig = self._sig()
        if sig == getattr(self, '_last_sig', None) and self.dst.exists():
            return self.dst
        db = self._apply_wal(self._load_base())
        # Mark the plain copy as a normal rollback-journal database.
        db[18:20] = b'\x01\x01'
        tmp = self.dst.with_suffix('.tmp')
        tmp.write_bytes(db)
        os.chmod(tmp, 0o600)
        tmp.replace(self.dst)
        self._last_sig = sig
        return self.dst

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f'file:{self.refresh()}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        return conn
