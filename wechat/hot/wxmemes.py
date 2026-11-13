"""Sticker library for the WeChat bot: keep the ones it sees, tag them, send them back.

Why screen capture and not the message database: WeChat 4.x stores local pictures in an
encrypted container (`msg/attach/**/Img/<md5>.dat`, keyed by an `aeskey` hidden inside a
zstd-compressed XML blob), and fetching the original from the CDN needs their private POST
protocol. The bot opens chats anyway to read and answer them - at that moment the sticker
is drawn on screen, so we crop it out of a screenshot of the message list. What we get is
the rendered picture (a bit smaller than the original), which is what gets sent back.

Everything else lives here: dedupe by pixels, a short LLM pass that tags each sticker from
the chatter around it, and `pick()` for when the model wants to answer with one.

    python3 /opt/wxhot/wxmemes.py --list     # what has been collected
"""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import random
import re
import threading
import time
import urllib.request

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

HOME = pathlib.Path.home()
STATE = HOME / '.wxbot'
MEME_DIR = STATE / 'memes'
INDEX = MEME_DIR / 'index.json'
LLM_CFG = STATE / 'llm.json'

MAX_ITEMS = 200          # keep the newest N stickers
MAX_BYTES = 400_000      # a cropped sticker is tiny; anything bigger was not a sticker
SIDE = 240               # stored at most this large
MIN_SIDE = 48            # ignore slivers (an emoji inside a line of text)
TRIM_MARGIN = 0.12       # ignore the outer 12% on each side: that is where avatars sit

_LOCK = threading.Lock()
_index: dict | None = None
_index_mtime: int = 0  # the admin page edits index.json too: reload when it changed


# ───────────────────────────── index ─────────────────────────────


def _mtime() -> int:
    try:
        return INDEX.stat().st_mtime_ns
    except OSError:
        return 0


def load_index() -> dict:
    global _index, _index_mtime
    if _index is not None and _mtime() == _index_mtime:
        return _index
    try:
        data = json.loads(INDEX.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('items'), list):
            _index = data
            _index_mtime = _mtime()
            return _index
    except (OSError, ValueError):
        pass
    _index = {'version': 2, 'items': []}
    return _index


def _save_index() -> None:
    idx = load_index()
    idx['items'] = idx['items'][-MAX_ITEMS:]
    try:
        MEME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = INDEX.with_suffix('.tmp')
        tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding='utf-8')
        tmp.replace(INDEX)
        global _index_mtime
        _index_mtime = _mtime()
    except OSError:
        pass


def find(md5: str) -> dict | None:
    for item in load_index()['items']:
        if item.get('md5') == md5:
            return item
    return None


def count() -> int:
    return len(load_index()['items'])


def summary(limit: int = 3) -> dict:
    items = load_index()['items']
    tags: dict[str, int] = {}
    for item in items:
        for tag in item.get('tags') or []:
            tags[tag] = tags.get(tag, 0) + 1
    top = sorted(tags.items(), key=lambda kv: -kv[1])[:8]
    return {
        'count': len(items),
        'tags': [{'tag': t, 'n': n} for t, n in top],
        'recent': [{'md5': i.get('md5'), 'tags': i.get('tags') or [], 'at': i.get('at')} for i in items[-limit:]],
    }


def _from_chat(item: dict, chat: str | None) -> bool:
    """Stolen stickers stay in the group they came from (chat=None: any group), unless the
    admin page marked them `shared` (any group). `disabled` ones are never used."""
    if item.get('disabled'):
        return False
    return chat is None or (bool(chat) and (bool(item.get('shared')) or item.get('chat') == chat[:32]))


def tag_list(limit: int = 8, chat: str | None = None) -> str:
    """'无语、得意、干饭' - the names the reply prompt offers the model."""
    tags: dict[str, int] = {}
    for item in load_index()['items']:
        if not _from_chat(item, chat):
            continue
        for tag in item.get('tags') or []:
            tags[tag] = tags.get(tag, 0) + 1
    return '、'.join(t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:limit])


# ───────────────────────────── capture ─────────────────────────────


def _magic(data: bytes) -> str | None:
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return None


def decode_xwd(data: bytes):
    """`xwd -root` output -> PIL image.

    PIL's XwdImagePlugin refuses the version x11-apps writes here, so the 100-byte header
    is parsed by hand. TrueColor, 32 bits per pixel, bytes BGRX (LSBFirst)."""
    if Image is None or np is None or len(data) < 120:
        return None
    import struct

    try:
        header_size = struct.unpack('>I', data[:4])[0]
        f = struct.unpack('>24I', data[4:100])
        width, height = f[3], f[4]
        bits_per_pixel, bytes_per_line = f[10], f[11]
        ncolors = f[18]
        if width < 1 or height < 1 or width > 8000 or height > 8000:
            return None
        off = header_size + ncolors * 12
        step = max(1, bits_per_pixel // 8)
        body = data[off : off + bytes_per_line * height]
        if len(body) < bytes_per_line * height:
            return None
        arr = np.frombuffer(body, dtype=np.uint8).reshape(height, bytes_per_line)
        arr = arr[:, : width * step].reshape(height, width, step)
        rgb = arr[:, :, [2, 1, 0]] if step >= 3 else arr[:, :, [0, 0, 0]]
        return Image.fromarray(np.ascontiguousarray(rgb), 'RGB')
    except Exception:
        return None


def trim(img):
    """Cut the sticker out of a screenshot crop: drop the flat background and avatars."""
    if Image is None:
        return None
    im = img.convert('RGB')
    w, h = im.size
    if w < 24 or h < 24:
        return None
    left, right = int(w * TRIM_MARGIN), int(w * (1 - TRIM_MARGIN))
    if right - left < 24:
        return None
    if np is None:
        return im.crop((left, 0, right, h))
    a = np.asarray(im).astype('int16')[:, left:right, :]
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    bg = np.median(border, axis=0)
    mask = np.abs(a - bg).sum(axis=2) > 36
    ys, xs = np.where(mask)
    if len(xs) < 400:
        return None
    box = (int(xs.min()) + left, int(ys.min()), int(xs.max()) + 1 + left, int(ys.max()) + 1)
    out = im.crop(box)
    ow, oh = out.size
    if ow < MIN_SIDE or oh < MIN_SIDE:
        return None
    ratio = ow / oh
    if ratio < 0.45 or ratio > 2.2:  # a sticker is roughly square; a photo is not
        return None
    return out


AVATAR_STRIP = 80  # px at each end of a message row that hold an avatar, never the sticker
SAME_HASH_BITS = 6  # dHash distance at or under which two crops are the same sticker


def _runs(flags, min_gap: int = 4) -> list[tuple[int, int]]:
    """[start, end) spans where flags is true, joining gaps shorter than min_gap."""
    spans: list[list[int]] = []
    for i, on in enumerate(flags):
        if not on:
            continue
        if spans and i - spans[-1][1] < min_gap:
            spans[-1][1] = i + 1
        else:
            spans.append([i, i + 1])
    return [(a, b) for a, b in spans]


def extract(row):
    """The sticker inside a screenshot of one whole message row, or None.

    An incoming row: avatar in the left strip, in groups the nickname as a thin text line,
    then the picture. (trim() cut a fixed 12% off each side - 111 px of a 926 px row - but
    stickers start ~86 px in, so it sliced them.) Rows with no avatar on the left are our
    own messages and are skipped: we do not collect what we sent ourselves."""
    if Image is None or np is None:
        return None
    im = row.convert('RGB')
    w, h = im.size
    if w < AVATAR_STRIP * 2 + MIN_SIDE or h < MIN_SIDE:
        return None
    a = np.asarray(im).astype('int16')
    bg = np.median(np.concatenate([a[0], a[-1]]), axis=0)
    mask = np.abs(a - bg).sum(axis=2) > 36
    if not mask[:, :AVATAR_STRIP].any():
        return None  # no avatar on the left: our own message
    body = mask[:, AVATAR_STRIP : w - AVATAR_STRIP]
    spans = _runs(body.any(axis=1))
    if not spans:
        return None
    y0, y1 = max(spans, key=lambda s: s[1] - s[0])  # the picture, not the nickname line
    cols = _runs(body[y0:y1].any(axis=0), min_gap=8)
    if not cols:
        return None
    x0, x1 = max(cols, key=lambda s: s[1] - s[0])
    out = im.crop((x0 + AVATAR_STRIP, y0, x1 + AVATAR_STRIP, y1))
    ow, oh = out.size
    if ow < MIN_SIDE or oh < MIN_SIDE:
        return None
    if not 0.45 <= ow / oh <= 2.2:  # a sticker is roughly square; a photo or text bubble is not
        return None
    return out


def dhash(img) -> int:
    g = img.convert('L').resize((9, 8))
    px = list(g.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | int(px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


def _same_as(h: int) -> dict | None:
    for item in load_index()['items']:
        other = item.get('hash')
        if isinstance(other, int) and bin(other ^ h).count('1') <= SAME_HASH_BITS:
            return item
    return None


def save(raw: bytes, chat: str = '', ctx: list[str] | None = None, sender: str = '') -> dict | None:
    """Store one cropped sticker. Returns the new item, or None if it was a duplicate."""
    if Image is None or not raw or len(raw) > MAX_BYTES * 8:  # raw is a whole message row
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None
    img = extract(img)
    if img is None:
        return None
    h = dhash(img)
    with _LOCK:
        seen = _same_as(h)
        if seen is not None:
            seen['seen'] = int(seen.get('seen', 1)) + 1  # others keep using it: a good one
            _save_index()
            return None
    try:
        img.thumbnail((SIDE, SIDE))
        buf = io.BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=88)
        data = buf.getvalue()
    except Exception:
        return None
    md5 = hashlib.sha1(data).hexdigest()
    with _LOCK:
        if find(md5):
            return None
        name = f'{md5[:16]}.jpg'
        try:
            MEME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            (MEME_DIR / name).write_bytes(data)
        except OSError:
            return None
        item = {
            'md5': md5,
            'file': name,
            'chat': chat[:32],
            'sender': (sender or '')[:24],
            'at': int(time.time()),
            'hash': h,
            'seen': 1,
            'tags': [],
            'note': '',
            'uses': 0,
            'last_used': 0,
            'ctx': [c[:80] for c in (ctx or [])][-6:],
        }
        load_index()['items'].append(item)
        _save_index()
    return item


# ───────────────────────── from WeChat's own database ─────────────────────────
#
# Screenshots gave half stickers and mistook our own for others'. Every sticker message
# (local_type 47) in message_0.db carries <emoji fromusername=… md5=… cdnurl=…>: the
# sender, and a plain CDN link to the original file (gif/png/jpg, md5-verified). The
# database is read through wxdb's decrypted copy; nothing is written to WeChat's files.

HERE = pathlib.Path(__file__).resolve().parent
DB_MAX_BYTES = 4_000_000  # an animated sticker can be a few MB
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def _db_conns():
    import sys

    for p in (str(HERE), str(HERE / 'pylib')):
        if p not in sys.path:
            sys.path.insert(0, p)
    import wxdb  # noqa: E402
    import zstandard  # noqa: E402

    keys = wxdb.load_keys()
    msg = next((p for p in keys if p.endswith('message/message_0.db')), None)
    contact = next((p for p in keys if p.endswith('contact/contact.db')), None)
    if not msg or not contact:
        raise RuntimeError('no database keys')
    m = re.search(r'/(wxid_[A-Za-z0-9]+?)_\d+/db_storage/', msg)
    own = m.group(1) if m else ''

    def copy(path: str):
        # One long-lived copier per database: it keeps the decrypted base and only redoes
        # work when WeChat's file or its -wal actually changed.
        c = _COPIES.get(path)
        if c is None or c.key != keys[path]:
            c = _COPIES[path] = wxdb.PlainCopy(path, keys[path])
        return c

    return (copy(msg).connect(), copy(contact).connect(), own, zstandard.ZstdDecompressor())


_COPIES: dict = {}


def _text(raw, dec) -> str:
    data = bytes(raw or b'') if not isinstance(raw, str) else raw.encode()
    if data[:4] == b'\x28\xb5\x2f\xfd':
        data = dec.decompress(data, max_output_size=1 << 20)
    return data.decode('utf-8', 'replace')


def harvest(limit: int = 15) -> int:
    """Collect stickers other people sent in groups since the last run. Returns how many
    new ones were stored."""
    msg_db, contact_db, own, dec = _db_conns()
    try:
        return _harvest(msg_db, contact_db, own, dec, limit)
    finally:
        # Open connections pin the old /dev/shm copies (30 MB each) after they are replaced.
        msg_db.close()
        contact_db.close()


def _harvest(msg_db, contact_db, own: str, dec, limit: int) -> int:
    import html

    groups = {
        r['username']: (r['remark'] or r['nick_name'] or '').strip()
        for r in contact_db.execute("select username, nick_name, remark from contact where username like '%@chatroom'")
    }
    idx = load_index()
    since = int(idx.get('db_since') or time.time() - 3600)  # first run: the last hour
    rows = []
    for (t,) in msg_db.execute("select name from sqlite_master where type='table' and name like 'Msg_%'"):
        rows += [
            (r['create_time'], r['message_content'])
            for r in msg_db.execute(
                f'select create_time, message_content from "{t}" where local_type = 47 and create_time > ? '
                'order by create_time limit 50',
                (since,),
            )
        ]
    rows.sort(key=lambda r: r[0])
    added = 0
    newest = since
    for ts, content in rows[: limit * 3]:
        newest = max(newest, ts)
        try:
            txt = _text(content, dec)
        except Exception:
            continue
        m = re.search(r'<emoji\b([^>]*)', txt)
        if not m:
            continue
        a = dict(_ATTR.findall(m.group(1)))
        room, sender, wx_md5 = a.get('tousername', ''), a.get('fromusername', ''), a.get('md5', '')
        chat = groups.get(room, '')
        if not room.endswith('@chatroom') or not chat or not sender or sender == own or not wx_md5:
            continue  # groups only, never our own stickers
        with _LOCK:
            known = next((i for i in load_index()['items'] if i.get('wx_md5') == wx_md5 and i.get('chat') == chat[:32]), None)
            if known is not None:
                known['seen'] = int(known.get('seen', 1)) + 1  # the group keeps using it
                continue
        url = html.unescape(a.get('cdnurl', ''))
        if not url.startswith('http'):
            continue
        try:
            data = urllib.request.urlopen(url, timeout=20).read(DB_MAX_BYTES + 1)
        except Exception:
            continue
        if len(data) > DB_MAX_BYTES or hashlib.md5(data).hexdigest() != wx_md5:
            continue
        ext = _magic(data)
        if ext is None or Image is None:
            continue
        try:
            first = Image.open(io.BytesIO(data))
            first.load()
            h = dhash(first.convert('RGB'))
        except Exception:
            continue
        name = f'{wx_md5[:16]}.{ext}'
        try:
            MEME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            (MEME_DIR / name).write_bytes(data)
        except OSError:
            continue
        with _LOCK:
            load_index()['items'].append({
                'md5': hashlib.sha1(data).hexdigest(),
                'wx_md5': wx_md5,
                'file': name,
                'chat': chat[:32],
                'sender': sender[:40],
                'at': int(ts),
                'hash': h,
                'seen': 1,
                'tags': [],
                'note': '',
                'uses': 0,
                'last_used': 0,
                'source': 'db',
            })
            _save_index()
        added += 1
        if added >= limit:
            break
    with _LOCK:
        load_index()['db_since'] = int(newest)
        _save_index()
    return added


# ───────────────────────────── tagging ─────────────────────────────


def _llm(messages: list[dict], max_tokens: int, temperature: float) -> str:
    try:
        cfg = json.loads(LLM_CFG.read_text(encoding='utf-8'))
        body = json.dumps(
            {'model': cfg['model'], 'messages': messages, 'max_tokens': max_tokens, 'temperature': temperature}
        ).encode()
        req = urllib.request.Request(
            cfg['base_url'].rstrip('/') + '/chat/completions',
            data=body,
            headers={'Authorization': 'Bearer ' + cfg['api_key'], 'Content-Type': 'application/json'},
        )
        resp = json.load(urllib.request.urlopen(req, timeout=45))
        return re.sub(r'<think>.*?</think>', '', resp['choices'][0]['message'].get('content') or '', flags=re.S).strip()
    except Exception:
        return ''


_TAG_PROMPT = (
    '你在帮聊天机器人整理表情包。下面是一张表情包被发出时群里的对话片段。'
    '请判断这张表情包大概是什么情绪/用途，输出 JSON：{{"tags": ["无语"], "note": "不超过20字"}}。\n'
    '要求：tags 1-3 个，从这些词里选：{cats}；实在判断不了就给 ["搞笑"]。'
    '不要输出 JSON 以外的任何内容。'
)

CATEGORIES = (
    '干饭、生气、得意、躺平、无语、嫌弃、加班、睡觉、比心、害羞、花痴、要钱、问号、'
    '震惊、偷看、求饶、飘了、OK、不要、猫爪、加载、惊吓、晕、早上好、搞笑'
)


VISION_MODEL = 'Qwen/Qwen3-VL-8B-Instruct'

_LOOK_PROMPT = (
    '这是一张微信表情包。下面还有它被发出时群里的几句对话（可能没有）。'
    '请看图判断它表达的情绪/用途，输出 JSON：{{"tags": ["无语"], "note": "什么时候适合发，不超过20字", "text": "图上的文字，没有就空", "safe": true, "why": ""}}。\n'
    '要求：tags 1-3 个，从这些词里选：{cats}。按画面里人物的表情和动作来选（大笑→搞笑/得意，'
    '哭→求饶/惊吓，张嘴瞪眼→震惊，摊手→无语）；不要默认选“无语”，只有画面真的是无语、汗颜、白眼才选它。'
    'safe：机器人会在群里随手发它，下面这些情况填 false 并在 why 里写原因（不超过10字）：'
    '真人照片或截图里有真人、色情低俗擦边、暴力血腥、侮辱歧视、政治敏感，'
    '或者画面里有机器人自己的形象（{me}），哪怕是别人二创、恶搞的图。其余填 true。'
    '不要输出 JSON 以外的任何内容。'
)


def _my_look() -> str:
    """The bot's own look, from the persona page (人设 → 形象)."""
    try:
        s = json.loads((pathlib.Path.home() / '.wxbot' / 'settings.json').read_text(encoding='utf-8'))
        look = ((s.get('persona_profile') or {}).get('appearance') or '').strip()
    except (OSError, ValueError, AttributeError):
        look = ''
    look = look or '橙色长发、头戴橙色花朵和黑色蝴蝶结的 Q 版二次元女孩'
    return look.split('，内置表情包')[0][:80]


def _look(path: pathlib.Path, ctx: list[str]) -> str:
    """Ask the vision model about the sticker image itself ('' on any failure)."""
    import base64

    try:
        cfg = json.loads(LLM_CFG.read_text(encoding='utf-8'))
        # Originals can be multi-MB animated gifs: send a small jpeg of the first frame.
        im = Image.open(path)
        im = im.convert('RGB')
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=85)
        img = base64.b64encode(buf.getvalue()).decode()
        body = json.dumps({
            'model': cfg.get('vision_model') or VISION_MODEL,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + img}},
                    {'type': 'text', 'text': _LOOK_PROMPT.format(cats=CATEGORIES, me=_my_look())
                     + '\n对话：\n' + ('\n'.join(ctx) if ctx else '（没有）')},
                ],
            }],
            'max_tokens': 200,
            'temperature': 0.2,
        }).encode()
        req = urllib.request.Request(
            cfg['base_url'].rstrip('/') + '/chat/completions',
            data=body,
            headers={'Authorization': 'Bearer ' + cfg['api_key'], 'Content-Type': 'application/json'},
        )
        resp = json.load(urllib.request.urlopen(req, timeout=45))
        return re.sub(r'<think>.*?</think>', '', resp['choices'][0]['message'].get('content') or '', flags=re.S).strip()
    except Exception:
        return ''


def _tag_one(item: dict) -> None:
    ctx = item.get('ctx') or []
    raw = _look(MEME_DIR / str(item.get('file') or ''), ctx) if item.get('file') else ''
    if not raw:  # no vision: guess from the chat around it
        raw = _llm(
            [
                {'role': 'system', 'content': _TAG_PROMPT.format(cats=CATEGORIES)},
                {'role': 'user', 'content': '对话片段：\n' + ('\n'.join(ctx) if ctx else '（没有上下文）')},
            ],
            200,
            0.2,
        )
    found = re.search(r'\{.*\}', raw, flags=re.S)
    tags: list[str] = []
    note = ''
    if found:
        try:
            data = json.loads(found.group(0))
            if isinstance(data.get('tags'), list):
                tags = [str(t)[:8] for t in data['tags'][:3] if str(t).strip()]
            if isinstance(data.get('note'), str):
                note = data['note'][:40]
            if isinstance(data.get('text'), str) and data['text'].strip():
                item['text'] = data['text'].strip()[:30]
            if data.get('safe') is False:
                # Kept (the admin can look and re-enable it), never sent on its own.
                item['disabled'] = True
                item['auto_off'] = str(data.get('why') or '').strip()[:12] or '不适合发'
        except ValueError:
            pass
    item['tags'] = tags or ['搞笑']
    item['note'] = note
    item.pop('ctx', None)


def tag_pending(limit: int = 3) -> int:
    done = 0
    for item in list(load_index()['items']):
        if done >= limit:
            break
        if item.get('tags'):
            continue
        try:
            _tag_one(item)
            with _LOCK:
                _save_index()
            done += 1
        except Exception:
            continue
    return done


# ───────────────────────────── using them ─────────────────────────────


def pick(tag: str = '', chat: str | None = None) -> dict | None:
    """Choose a sticker to send: one stolen in this same group (`chat`; '' = a private
    chat, which gets none) whose tag matches. No match: None, so the caller falls back
    to the hand-made set instead of sending some unrelated sticker."""
    items = [
        i for i in load_index()['items']
        if i.get('file') and (MEME_DIR / str(i['file'])).is_file() and _from_chat(i, chat)
    ]
    if not items:
        return None
    tag = (tag or '').strip()
    chosen: dict | None = None
    if tag:
        hits = [
            i for i in items
            if any(tag in t or t in tag for t in (i.get('tags') or [])) or tag in str(i.get('text') or '')
        ]
        if hits:
            # Popular with the group (seen many times) and not just used by us wins;
            # a little randomness so the same one does not come back every time.
            def score(i: dict) -> float:
                fresh = min(1.0, (time.time() - i.get('last_used', 0)) / 3600)
                return fresh * (1 + min(int(i.get('seen', 1)), 5) * 0.3) + random.random() * 0.3

            chosen = max(hits, key=score)
    if chosen is None:
        if tag:
            return None
        pool = [i for i in items if i.get('tags')] or items
        chosen = min(pool, key=lambda i: (i.get('last_used', 0), -i.get('at', 0)))
    path = MEME_DIR / str(chosen['file'])
    chosen['uses'] = int(chosen.get('uses', 0)) + 1
    chosen['last_used'] = int(time.time())
    with _LOCK:
        _save_index()
    return {'md5': chosen['md5'], 'path': str(path), 'tags': chosen.get('tags') or [], 'ext': 'jpg'}


def as_png(path: str) -> str | None:
    """xclip needs PNG: convert the stored jpg to a temp png next to it (cached)."""
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    if p.suffix.lower() == '.png' or Image is None:
        return str(p)
    out = p.with_suffix('.clip.png')
    try:
        if not out.is_file() or out.stat().st_mtime < p.stat().st_mtime:
            Image.open(p).convert('RGBA').save(out, format='PNG')
        return str(out)
    except Exception:
        return None


def diagnose() -> dict:
    return {
        'pil': Image is not None,
        'numpy': np is not None,
        'stored': count(),
        'summary': summary(),
        'dir': str(MEME_DIR),
    }


if __name__ == '__main__':
    import sys

    if '--list' in sys.argv:
        print(json.dumps(summary(limit=50), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(diagnose(), ensure_ascii=False))
