"""Admin server for the WeChat bot (stdlib only).

Listens on :6081 inside the Docker network; the Caddy proxy publishes it over HTTPS.
Serves the one-page admin UI (admin.html) and a small JSON API under /api/.
Everything except login and first-time setup requires a signed session cookie.
"""

import base64
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = pathlib.Path.home()
ENV = {**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':1')}
# The login/QR window is small (about 292x396); the main chat window is much larger.
MAIN_WINDOW_MIN_WIDTH = 500


def _run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5, env=ENV).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _wechat_running() -> bool:
    return bool(_run('pgrep', '-x', 'wechat').strip())


def _windows() -> list[dict]:
    result = []
    for wid in _run('xdotool', 'search', '--onlyvisible', '--name', '.').split():
        name = _run('xdotool', 'getwindowname', wid).strip()
        geo = _run('xdotool', 'getwindowgeometry', '--shell', wid)
        values = dict(line.split('=', 1) for line in geo.splitlines() if '=' in line)
        try:
            result.append({'name': name, 'width': int(values['WIDTH']), 'height': int(values['HEIGHT'])})
        except (KeyError, ValueError):
            continue
    return result


def _accounts() -> list[str]:
    root = HOME / 'xwechat_files'
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith('wxid_') and (p / 'db_storage').is_dir())


def status() -> dict:
    running = _wechat_running()
    windows = [w for w in _windows() if w['name'] in ('微信', 'WeChat', 'Weixin')] if running else []
    main = max(windows, key=lambda w: w['width'], default=None)
    return {
        'running': running,
        'logged_in': bool(main and main['width'] >= MAIN_WINDOW_MIN_WIDTH),
        'window': main,
        'accounts': _accounts(),
    }


def _window_ids() -> list[tuple[str, dict]]:
    out = []
    for wid in _run('xdotool', 'search', '--onlyvisible', '--name', '^(微信|WeChat|Weixin)$').split():
        geo = _run('xdotool', 'getwindowgeometry', '--shell', wid)
        values = dict(line.split('=', 1) for line in geo.splitlines() if '=' in line)
        try:
            out.append((wid, {k: int(values[k]) for k in ('X', 'Y', 'WIDTH', 'HEIGHT')}))
        except (KeyError, ValueError):
            continue
    return out


def _fit_windows_forever() -> None:
    """Fill the virtual screen with the main window (no black border on the admin page)
    and keep the small login/QR window centred. The screen resolution never changes."""
    while True:
        try:
            screen = _run('xdotool', 'getdisplaygeometry').split()
            sw, sh = int(screen[0]), int(screen[1])
            for wid, g in _window_ids():
                if g['WIDTH'] >= MAIN_WINDOW_MIN_WIDTH:
                    if (g['X'], g['Y'], g['WIDTH'], g['HEIGHT']) != (0, 0, sw, sh):
                        _run('xdotool', 'windowmove', wid, '0', '0', 'windowsize', wid, str(sw), str(sh))
                else:
                    cx, cy = (sw - g['WIDTH']) // 2, (sh - g['HEIGHT']) // 2
                    if abs(g['X'] - cx) > 4 or abs(g['Y'] - cy) > 4:
                        _run('xdotool', 'windowmove', wid, str(cx), str(cy))
        except (ValueError, IndexError):
            pass
        time.sleep(3)


def relogin() -> None:
    # The launcher starts WeChat again within a few seconds, showing a new QR code.
    _run('pkill', '-x', 'wechat')


_LOG_FILE = HOME / '.wxbot' / 'log.jsonl'
_SETTINGS_FILE = HOME / '.wxbot' / 'settings.json'
_CHATS_FILE = HOME / '.wxbot' / 'chats.json'


def read_log(limit: int = 300) -> list:
    try:
        lines = _LOG_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()  # newest first
    return out


_ENTRY_CACHE = None
_ENTRY_CACHE_KEY = None


def log_entries(limit: int = 20000, tail_bytes: int = 4 * 1024 * 1024) -> list:
    """Parsed log entries, re-read only when the file changed (the dashboard polls).

    Only the tail of the file is decoded: the log is append-only and grows forever,
    while the dashboard never needs the very first day it ever ran.
    """
    global _ENTRY_CACHE, _ENTRY_CACHE_KEY
    try:
        st = _LOG_FILE.stat()
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    if _ENTRY_CACHE is not None and _ENTRY_CACHE_KEY == key:
        return _ENTRY_CACHE
    try:
        with open(_LOG_FILE, 'rb') as f:
            f.seek(max(0, st.st_size - tail_bytes))
            raw = f.read()
    except OSError:
        return []
    lines = raw.decode('utf-8', 'ignore').splitlines()
    if st.st_size > tail_bytes and lines:
        lines = lines[1:]  # the first line is cut in half by the seek above
    out = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    _ENTRY_CACHE = out
    _ENTRY_CACHE_KEY = key
    return out


def _day(ts: float) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(ts))


def stats() -> dict:
    """Today's / last-7-days / all-time traffic, straight from the bot's own log.

    One log line is one message the bot noticed; `sent` / `dry_run` mark the ones it
    answered, `judged` the ones it deliberately ignored, `auto_accept` friend approvals
    (which are not chat traffic and are counted apart).
    """
    now = time.time()
    days = [_day(now - i * 86400) for i in range(6, -1, -1)]
    buckets = {d: {'date': d, 'in': 0, 'out': 0} for d in days}
    today = {
        'date': days[-1],
        'in': 0,
        'out': 0,
        'skipped': 0,
        'errors': 0,
        'friends': 0,
        'chats': set(),
        '_took': [],
    }
    total = {'in': 0, 'out': 0, 'chats': set(), 'since': 0}
    last_at = 0

    for entry in log_entries():
        try:
            ts = float(entry.get('time') or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        last_at = max(last_at, ts)
        friend = bool(entry.get('auto_accept'))
        replied = bool(entry.get('reply')) and bool(
            entry.get('sent') or entry.get('dry_run')
        )
        day = _day(ts)

        if not friend:
            total['in'] += 1
            if replied:
                total['out'] += 1
            total['chats'].add(str(entry.get('chat') or ''))
            if not total['since'] or ts < total['since']:
                total['since'] = int(ts)
            bucket = buckets.get(day)
            if bucket is not None:
                bucket['in'] += 1
                if replied:
                    bucket['out'] += 1

        if day != today['date']:
            continue
        if friend:
            today['friends'] += 1
            continue
        today['in'] += 1
        today['chats'].add(str(entry.get('chat') or ''))
        if replied:
            today['out'] += 1
        if entry.get('judged'):
            today['skipped'] += 1
        if entry.get('error'):
            today['errors'] += 1
        took = entry.get('took')
        if isinstance(took, (int, float)) and took > 0:
            today['_took'].append(float(took))

    took_list = today.pop('_took')
    today['chats'] = len([c for c in today['chats'] if c])
    today['avg_took'] = round(sum(took_list) / len(took_list), 1) if took_list else 0

    return {
        'generated_at': int(now),
        'last_at': int(last_at),
        'today': today,
        'total': {
            'in': total['in'],
            'out': total['out'],
            'chats': len([c for c in total['chats'] if c]),
            'since': total['since'],
        },
        'daily': [buckets[d] for d in days],
        'memes': read_memes()['count'],
    }


def read_chats() -> list:
    """The bot's latest view of WeChat's chat list: [{name, group, preview, at}, ...]."""
    try:
        data = json.loads(_CHATS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    chats = data.get('chats') if isinstance(data, dict) else None
    return chats if isinstance(chats, list) else []


def read_settings() -> dict:
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


_MEME_INDEX = HOME / '.wxbot' / 'memes' / 'index.json'


def read_memes() -> dict:
    """How many stickers the bot has stolen, and the tags it gave them."""
    try:
        data = json.loads(_MEME_INDEX.read_text(encoding='utf-8'))
        items = data.get('items') if isinstance(data, dict) else None
    except (OSError, ValueError):
        items = None
    if not isinstance(items, list):
        return {'count': 0, 'tags': [], 'recent': []}
    tags: dict[str, int] = {}
    for item in items:
        for tag in (item.get('tags') or []) if isinstance(item, dict) else []:
            tags[str(tag)] = tags.get(str(tag), 0) + 1
    top = sorted(tags.items(), key=lambda kv: -kv[1])[:8]
    return {
        'count': len(items),
        'tags': [{'tag': t, 'n': n} for t, n in top],
        'recent': [
            {'tags': i.get('tags') or [], 'at': i.get('at'), 'chat': str(i.get('chat', ''))[:16]}
            for i in items[-3:]
            if isinstance(i, dict)
        ],
    }




# ───────────────────────── settings ─────────────────────────

GLOBAL_BOOL_KEYS = (
    'enabled',
    'dry_run',
    'reply_private',
    'reply_group_at',
    'reply_group_smart',
    'learn',
    'steal_meme',
    'use_meme',
    'auto_accept_friend',
)
CHAT_BOOL_KEYS = ('enabled', 'use_meme', 'learn', 'read_media', 'read_voice', 'reply_pat')
ACTIVITY_LEVELS = ('quiet', 'normal', 'active', 'chatty')


def _write_private_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_settings(patch: dict) -> dict:
    cur = read_settings()
    for k in GLOBAL_BOOL_KEYS:
        if k in patch:
            cur[k] = bool(patch[k])
    if isinstance(patch.get('persona'), str):
        cur['persona'] = patch['persona'][:2000]
    if patch.get('group_activity') in ACTIVITY_LEVELS:
        cur['group_activity'] = patch['group_activity']
    _write_private_json(_SETTINGS_FILE, cur)
    return cur


def write_chat_settings(name: str, over) -> dict:
    """Per-chat overrides; None (or nothing set) removes them so the chat follows the global settings."""
    cur = read_settings()
    chats = cur.get('chats') if isinstance(cur.get('chats'), dict) else {}
    clean = {}
    if isinstance(over, dict):
        for k in CHAT_BOOL_KEYS:
            if isinstance(over.get(k), bool):
                clean[k] = over[k]
        if over.get('group_activity') in ACTIVITY_LEVELS:
            clean['group_activity'] = over['group_activity']
        if over.get('group_reply') in ('at', 'smart'):
            clean['group_reply'] = over['group_reply']
        if isinstance(over.get('persona'), str) and over['persona'].strip():
            clean['persona'] = over['persona'].strip()[:2000]
    if clean:
        chats[name[:200]] = clean
    else:
        chats.pop(name, None)
    cur['chats'] = chats
    _write_private_json(_SETTINGS_FILE, cur)
    return cur


# ───────────────────────── model / API key ─────────────────────────

_LLM_FILE = HOME / '.wxbot' / 'llm.json'
DEFAULT_BASE_URL = 'https://api.siliconflow.cn/v1'
DEFAULT_MODEL = 'deepseek-ai/DeepSeek-V4-Flash'
DEFAULT_FALLBACKS = ['deepseek-ai/DeepSeek-V3.2', 'Pro/deepseek-ai/DeepSeek-V3.2']

# Platforms with an OpenAI-compatible /chat/completions API. `vision` is the image
# model used for pictures and stickers ('' = this platform cannot read images).
# Model names are only starting points: "测试" lists what the key can really use.
PROVIDERS = [
    {'id': 'siliconflow', 'name': '硅基流动', 'base_url': 'https://api.siliconflow.cn/v1',
     'model': DEFAULT_MODEL, 'vision': 'Qwen/Qwen3-VL-8B-Instruct', 'photo_vision': 'Qwen/Qwen3-VL-30B-A3B-Instruct',
     'fallbacks': DEFAULT_FALLBACKS, 'key_url': 'https://cloud.siliconflow.cn/account/ak', 'note': '推荐：一个 Key 就能聊天和看图'},
    {'id': 'deepseek', 'name': 'DeepSeek 官方', 'base_url': 'https://api.deepseek.com/v1',
     'model': 'deepseek-chat', 'vision': '', 'key_url': 'https://platform.deepseek.com/api_keys', 'note': '不支持看图'},
    {'id': 'bailian', 'name': '通义千问（阿里云百炼）', 'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     'model': 'qwen-plus', 'vision': 'qwen-vl-plus', 'key_url': 'https://bailian.console.aliyun.com/?apiKey=1'},
    {'id': 'moonshot', 'name': 'Kimi（月之暗面）', 'base_url': 'https://api.moonshot.cn/v1',
     'model': 'kimi-latest', 'vision': 'moonshot-v1-8k-vision-preview', 'key_url': 'https://platform.moonshot.cn/console/api-keys'},
    {'id': 'zhipu', 'name': '智谱 GLM', 'base_url': 'https://open.bigmodel.cn/api/paas/v4',
     'model': 'glm-4-flash', 'vision': 'glm-4v-flash', 'key_url': 'https://open.bigmodel.cn/usercenter/apikeys'},
    {'id': 'volcark', 'name': '豆包（火山方舟）', 'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
     'model': '', 'vision': '', 'key_url': 'https://console.volcengine.com/ark', 'note': '模型名填你在方舟创建的接入点 ID'},
    {'id': 'openrouter', 'name': 'OpenRouter', 'base_url': 'https://openrouter.ai/api/v1',
     'model': 'deepseek/deepseek-chat', 'vision': 'qwen/qwen2.5-vl-72b-instruct', 'key_url': 'https://openrouter.ai/keys'},
    {'id': 'openai', 'name': 'OpenAI', 'base_url': 'https://api.openai.com/v1',
     'model': 'gpt-4o-mini', 'vision': 'gpt-4o-mini', 'key_url': 'https://platform.openai.com/api-keys'},
    {'id': 'anthropic', 'name': 'Claude（Anthropic）', 'base_url': 'https://api.anthropic.com/v1',
     'model': 'claude-haiku-5-5', 'vision': 'claude-haiku-5-5', 'key_url': 'https://console.anthropic.com/settings/keys'},
    {'id': 'ollama', 'name': 'Ollama（本地模型）', 'base_url': 'http://host.docker.internal:11434/v1',
     'model': '', 'vision': '', 'no_key': True, 'note': '本机运行的模型，不需要 Key'},
    {'id': 'custom', 'name': '自定义（兼容 OpenAI 的接口）', 'base_url': '', 'model': '', 'vision': ''},
]
_PROVIDER = {p['id']: p for p in PROVIDERS}


def _provider_of(cfg: dict) -> str:
    if cfg.get('provider') in _PROVIDER:
        return cfg['provider']
    base = str(cfg.get('base_url') or DEFAULT_BASE_URL).rstrip('/')
    for p in PROVIDERS:
        if p['base_url'] and p['base_url'].rstrip('/') == base:
            return p['id']
    return 'custom'


def read_llm() -> dict:
    try:
        data = json.loads(_LLM_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def llm_public() -> dict:
    cfg = read_llm()
    key = str(cfg.get('api_key') or '')
    provider = _provider_of(cfg)
    return {
        'configured': bool(key) or bool(cfg.get('base_url') and _PROVIDER[provider].get('no_key')),
        'provider': provider,
        'providers': PROVIDERS,
        'base_url': cfg.get('base_url') or DEFAULT_BASE_URL,
        'model': cfg.get('model') or DEFAULT_MODEL,
        'vision_model': cfg.get('vision_model', _PROVIDER[provider].get('vision', '')),
        'key_hint': ('…' + key[-4:]) if len(key) >= 8 else '',
        'search_hint': ('…' + str(cfg.get('search_key'))[-4:]) if len(str(cfg.get('search_key') or '')) >= 8 else '',
    }


def save_search_key(patch: dict) -> dict:
    # Optional 博查 web-search key; empty = the free Bing search the bot uses by default.
    cur = read_llm()
    key = str(patch.get('search_key') or '').strip()
    if key and (len(key) < 8 or len(key) > 200 or any(c.isspace() for c in key)):
        raise ValueError('搜索 Key 格式不对')
    cur['search_key'] = key
    _write_private_json(_LLM_FILE, cur)
    return llm_public()


def list_models(base_url: str, api_key: str) -> list[str]:
    """Ask the provider for its models; doubles as the API-key check."""
    req = urllib.request.Request(base_url.rstrip('/') + '/models', headers={'Authorization': 'Bearer ' + api_key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    items = data.get('data') if isinstance(data, dict) else None
    return sorted(str(m.get('id')) for m in (items or []) if isinstance(m, dict) and m.get('id'))


def save_llm(patch: dict) -> dict:
    cur = read_llm()
    old_provider = _provider_of(cur)
    provider = patch.get('provider') if patch.get('provider') in _PROVIDER else old_provider
    preset = _PROVIDER[provider]
    base_url = str(patch.get('base_url') or (cur.get('base_url') if provider == old_provider else '') or preset['base_url']).strip()
    if not base_url.startswith(('https://', 'http://')):
        raise ValueError('请填写接口地址（以 http:// 或 https:// 开头）')
    key = str(patch.get('api_key') or '').strip()
    if not key and provider == old_provider:
        key = str(cur.get('api_key') or '')
    if not key:
        if not preset.get('no_key'):
            raise ValueError('换了平台，请填写这个平台的 API Key' if provider != old_provider else '请填写 API Key')
        key = 'none'
    model = str(patch.get('model') or (cur.get('model') if provider == old_provider else '') or preset['model']).strip()
    if not model:
        raise ValueError('请选择或填写聊天模型（先点“测试”可以列出可用模型）')
    vision = patch.get('vision_model')
    if not isinstance(vision, str):
        vision = cur.get('vision_model', preset.get('vision', '')) if provider == old_provider else preset.get('vision', '')
    cur.update({'provider': provider, 'base_url': base_url, 'api_key': key, 'model': model, 'vision_model': vision.strip()})
    if provider != old_provider:
        cur['photo_vision_model'] = preset.get('photo_vision', '')
        cur['fallback_models'] = list(preset.get('fallbacks') or [])
    else:
        cur.setdefault('photo_vision_model', preset.get('photo_vision', ''))
    _write_private_json(_LLM_FILE, cur)
    return llm_public()


# ───────────────────────── server info & one-key cleanup ─────────────────────────

_CPU_LAST: list = []  # [(total, idle)] from the previous /proc/stat read
_CGROUP = pathlib.Path('/sys/fs/cgroup')
_BOT_CMD = 'python3 /opt/wxhot/wxbot.py'


def _cpu_times() -> tuple[int, int] | None:
    try:
        parts = pathlib.Path('/proc/stat').read_text().split('\n', 1)[0].split()[1:]
        vals = [int(x) for x in parts]
    except (OSError, ValueError):
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def _cpu_percent() -> float | None:
    now = _cpu_times()
    if now is None:
        return None
    prev = _CPU_LAST[0] if _CPU_LAST else None
    if prev is None:
        time.sleep(0.4)
        prev, now = now, _cpu_times() or now
    _CPU_LAST[:] = [now]
    total, idle = now[0] - prev[0], now[1] - prev[1]
    return round(100 * (1 - idle / total), 1) if total > 0 else 0.0


def _meminfo() -> dict:
    out = {}
    try:
        for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
            k, v = line.split(':', 1)
            out[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return out


def _read_int(path: pathlib.Path) -> int | None:
    try:
        v = path.read_text().strip()
        return None if v == 'max' else int(v)
    except (OSError, ValueError):
        return None


def _procs() -> list[dict]:
    """(name, rss) of the processes in this container, biggest first."""
    out = []
    for d in pathlib.Path('/proc').iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'ignore').strip()
            rss = 0
            for line in (d / 'status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError):
            continue
        if cmd and rss:
            out.append({'cmd': cmd, 'rss': rss})
    return sorted(out, key=lambda p: -p['rss'])


def _proc_label(cmd: str) -> str:
    base = cmd.split()[0].rsplit('/', 1)[-1]
    if cmd.startswith(_BOT_CMD):
        return '机器人'
    if base == 'wechat':
        return '微信'
    if base in ('wxocr', 'wxplayer', 'wxutility', 'crashpad_handler'):
        return '微信辅助进程'
    if base in ('Xvfb', 'openbox'):
        return '虚拟屏幕'
    if 'wxagent.py' in cmd or 'wxlauncher.py' in cmd:
        return '后台'
    if 'websockify' in cmd or base == 'x11vnc':
        return '远程桌面'
    return '其他'


def _uptime() -> int:
    try:
        return int(float(pathlib.Path('/proc/uptime').read_text().split()[0]))
    except (OSError, ValueError):
        return 0


def system_info() -> dict:
    mem = _meminfo()
    try:
        st = os.statvfs(str(HOME))
        disk = {'total': st.f_blocks * st.f_frsize, 'used': (st.f_blocks - st.f_bfree) * st.f_frsize}
    except OSError:
        disk = {'total': 0, 'used': 0}
    groups: dict[str, int] = {}
    for p in _procs():
        label = _proc_label(p['cmd'])
        groups[label] = groups.get(label, 0) + p['rss']
    try:
        load = [float(x) for x in pathlib.Path('/proc/loadavg').read_text().split()[:3]]
    except (OSError, ValueError):
        load = []
    return {
        'cpu': _cpu_percent(),
        'cores': os.cpu_count() or 1,
        'load': load,
        'mem': {'total': mem.get('MemTotal', 0), 'used': mem.get('MemTotal', 0) - mem.get('MemAvailable', 0)},
        'swap': {'total': mem.get('SwapTotal', 0), 'used': mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)},
        'bot_mem': {'used': _read_int(_CGROUP / 'memory.current') or 0, 'limit': _read_int(_CGROUP / 'memory.max') or 0},
        'disk': disk,
        'uptime': _uptime(),
        'procs': sorted(({'name': k, 'rss': v} for k, v in groups.items()), key=lambda x: -x['rss']),
        'cleanable': sum(size for _path, size in _cleanable()),
    }


def _old_files(folder: pathlib.Path, days: float) -> list[tuple[pathlib.Path, int]]:
    cutoff = time.time() - days * 86400
    out = []
    try:
        for f in folder.rglob('*'):
            try:
                st = f.lstat()
                if f.is_file() and not f.is_symlink() and st.st_mtime < cutoff:
                    out.append((f, st.st_size))
            except OSError:
                continue
    except OSError:
        pass
    return out


def _cleanable() -> list[tuple[pathlib.Path, int]]:
    """Files that are safe to delete: WeChat's own logs (older than a day), its crash
    reports, image thumbnails it can fetch again (older than 3 days) and stale temp
    files. Never chats, login data, stickers or the bot's settings and memory."""
    files = _old_files(HOME / '.xwechat' / 'log', 1)
    files += _old_files(HOME / '.xwechat' / 'crashinfo', 0)
    for acc in (HOME / 'xwechat_files').glob('*'):
        files += _old_files(acc / 'cache', 3)
    files += [(f, s) for f, s in _old_files(pathlib.Path('/tmp'), 1) if f.parent == pathlib.Path('/tmp') and f.suffix in ('.png', '.jpg', '.py', '.txt', '.xwd')]
    return files


def optimize() -> dict:
    freed = 0
    removed = 0
    for f, size in _cleanable():
        try:
            f.unlink()
            freed += size
            removed += 1
        except OSError:
            continue
    # The bot slowly piles up memory (screenshots, image libraries); a restart gives it
    # back. The launcher starts it again within a few seconds, and @-mentions that
    # arrive meanwhile are still answered.
    before = sum(p['rss'] for p in _procs() if p['cmd'].startswith(_BOT_CMD))
    restarted = False
    if before > 120 * 1024 * 1024:
        _run('pkill', '-f', '^' + _BOT_CMD)
        restarted = True
    return {'files': removed, 'disk_freed': freed, 'bot_restarted': restarted, 'bot_mem_before': before}

# ───────────────────────── admin login ─────────────────────────

_ADMIN_FILE = HOME / '.wxbot' / 'admin.json'
_SETUP_CODE_FILE = HOME / '.wxbot' / 'setup-code.txt'
SESSION_SECONDS = 30 * 86400
_login_failures: list[float] = []


def _admin() -> dict:
    try:
        data = json.loads(_ADMIN_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200_000).hex()


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError('密码至少 8 位')
    salt = secrets.token_hex(16)
    # A new secret also signs every older session out.
    _write_private_json(_ADMIN_FILE, {'salt': salt, 'hash': _hash_password(password, salt), 'secret': secrets.token_hex(32)})
    try:
        _SETUP_CODE_FILE.unlink()
    except OSError:
        pass


def check_password(password: str) -> bool:
    admin = _admin()
    if not admin.get('hash'):
        return False
    return hmac.compare_digest(_hash_password(password, admin['salt']), admin['hash'])


def setup_code() -> str:
    """One-time code that proves whoever finishes setup can read the container log."""
    try:
        code = _SETUP_CODE_FILE.read_text().strip()
        if code:
            return code
    except OSError:
        pass
    code = secrets.token_hex(4)
    _SETUP_CODE_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    _SETUP_CODE_FILE.write_text(code)
    os.chmod(_SETUP_CODE_FILE, 0o600)
    return code


def make_session() -> str:
    exp = str(int(time.time()) + SESSION_SECONDS)
    sig = hmac.new(bytes.fromhex(_admin()['secret']), exp.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{exp}.{sig}'.encode()).decode()


def valid_session(token: str) -> bool:
    secret = _admin().get('secret')
    if not token or not secret:
        return False
    try:
        exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split('.', 1)
        good = hmac.new(bytes.fromhex(secret), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except (ValueError, UnicodeDecodeError):
        return False


# ───────────────────────── persona ─────────────────────────

# The default persona, as a set of fields; "恢复小克预设" on the admin page puts these back.
PERSONA_PRESET = {
    'name': '小克',
    'developer': 'AuR3L1US',
    'identity': '微信聊天助手',
    'personality': '随和、真诚、有点小幽默，乐于帮忙，偶尔会吐槽但不刻薄',
    'style': '像真人朋友一样聊天，回复简短口语化，一般一两句话；别用列表或 Markdown，别长篇大论',
    'catchphrases': '',
    'likes': '',
    'avoid': '不说教、不卖弄、不假装是真人',
    'extra': '',
}
PERSONA_FIELDS = tuple(PERSONA_PRESET)
_PERSONA_LIMITS = {'name': 12, 'developer': 24, 'identity': 40}


def compose_persona(p: dict) -> str:
    """Fields -> the persona text the bot puts at the top of every prompt."""
    name = p.get('name') or PERSONA_PRESET['name']
    out = [f'你叫"{name}"，是由 {p.get("developer") or PERSONA_PRESET["developer"]} 开发的{p.get("identity") or "微信聊天助手"}。']
    for key, label in (
        ('personality', '性格'),
        ('style', '说话方式'),
        ('catchphrases', '口头禅（偶尔自然地用，别每句都用）'),
        ('likes', '兴趣和喜好'),
        ('avoid', '不要做的事'),
    ):
        if p.get(key):
            out.append(f'{label}：{p[key]}。'.replace('。。', '。'))
    if p.get('extra'):
        out.append(p['extra'])
    return '\n'.join(out)


def persona_view() -> dict:
    cur = read_settings()
    profile = cur.get('persona_profile') if isinstance(cur.get('persona_profile'), dict) else None
    return {
        'profile': {**PERSONA_PRESET, **(profile or {})},
        'custom_text': None if profile else cur.get('persona'),  # set by hand before this page existed
        'preset': PERSONA_PRESET,
        'intro': cur.get('intro') or '',
    }


def save_persona(patch: dict) -> dict:
    src = patch.get('profile') if isinstance(patch.get('profile'), dict) else {}
    profile = {}
    for k in PERSONA_FIELDS:
        v = str(src.get(k) or '').strip()
        profile[k] = v[: _PERSONA_LIMITS.get(k, 400)]
    if not profile['name']:
        raise ValueError('请填写机器人的名字')
    cur = read_settings()
    cur['persona_profile'] = profile
    cur['persona'] = compose_persona(profile)
    cur['bot_name'] = profile['name']          # @ / 拍一拍 / 叫名字 are matched against this
    cur['developer'] = profile['developer'] or PERSONA_PRESET['developer']
    if isinstance(patch.get('intro'), str):
        cur['intro'] = patch['intro'].strip()[:600]
    _write_private_json(_SETTINGS_FILE, cur)
    return persona_view()


# ───────────────────────── stickers ─────────────────────────

_MEME_DIR = HOME / '.wxbot' / 'memes'
_CUSTOM_STICKERS = HOME / '.wxbot' / 'stickers'
_BUILTIN_STICKERS = pathlib.Path(os.environ.get('WX_HOT_DIR', '/opt/wxhot')) / 'stickers'
_IMG_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}
_MAX_UPLOAD = 2 * 1024 * 1024


def _meme_index() -> dict:
    try:
        data = json.loads(_MEME_INDEX.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('items'), list):
            return data
    except (OSError, ValueError):
        pass
    return {'version': 2, 'items': []}


def _save_meme_index(data: dict) -> None:
    _MEME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = _MEME_INDEX.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(_MEME_INDEX)


def _sticker_name_ok(name: str) -> str:
    name = (name or '').strip()
    if not name or len(name) > 8 or any(c in name for c in '/\\.[]［］【】:：\0') or name.startswith('-'):
        raise ValueError('名称 1~8 个字，不能带 / \\ . [ ] : 等符号')
    return name


def list_stickers() -> dict:
    off = set(read_settings().get('sticker_disabled') or [])
    named: dict[str, dict] = {}
    for kind, folder in (('builtin', _BUILTIN_STICKERS), ('custom', _CUSTOM_STICKERS)):
        try:
            files = sorted(folder.glob('*.png'))
        except OSError:
            files = []
        for f in files:
            if f.name.endswith('.clip.png'):
                continue
            named[f.stem] = {'name': f.stem, 'kind': kind, 'disabled': f.stem in off,
                             'url': f'/api/sticker-img?kind={kind}&f={urllib.parse.quote(f.name)}'}
    collected = []
    for i in reversed(_meme_index()['items']):
        if not isinstance(i, dict) or not i.get('file'):
            continue
        collected.append({
            'md5': i.get('md5'), 'tags': i.get('tags') or [], 'note': i.get('note') or '',
            'chat': i.get('chat') or '', 'at': i.get('at') or 0, 'uses': i.get('uses') or 0,
            'seen': i.get('seen') or 1, 'shared': bool(i.get('shared')), 'disabled': bool(i.get('disabled')),
            'url': f'/api/sticker-img?kind=collected&f={urllib.parse.quote(str(i["file"]))}',
        })
    return {'named': sorted(named.values(), key=lambda x: (x['kind'] != 'custom', x['name'])), 'collected': collected}


def sticker_file(kind: str, name: str) -> pathlib.Path | None:
    folder = {'builtin': _BUILTIN_STICKERS, 'custom': _CUSTOM_STICKERS, 'collected': _MEME_DIR}.get(kind)
    base = pathlib.PurePosixPath(name).name
    if folder is None or not base or base != name or base.startswith('.') or pathlib.Path(base).suffix.lower() not in _IMG_TYPES:
        return None
    f = folder / base
    return f if f.is_file() else None


def update_collected(patch: dict) -> dict:
    md5 = str(patch.get('md5') or '')
    data = _meme_index()
    for item in data['items']:
        if item.get('md5') == md5:
            if isinstance(patch.get('tags'), list):
                item['tags'] = [str(t).strip()[:8] for t in patch['tags'] if str(t).strip()][:6]
            if isinstance(patch.get('note'), str):
                item['note'] = patch['note'].strip()[:60]
            for k in ('shared', 'disabled'):
                if isinstance(patch.get(k), bool):
                    item[k] = patch[k]
            _save_meme_index(data)
            return {'ok': True}
    raise ValueError('找不到这个表情包')


def delete_collected(md5: str) -> dict:
    data = _meme_index()
    keep = [i for i in data['items'] if i.get('md5') != md5]
    if len(keep) == len(data['items']):
        raise ValueError('找不到这个表情包')
    for i in data['items']:
        if i.get('md5') == md5 and i.get('file'):
            f = sticker_file('collected', str(i['file']))
            for p in (f, f.with_suffix('.clip.png') if f else None):
                if p is not None:
                    try:
                        p.unlink()
                    except OSError:
                        pass
    data['items'] = keep
    _save_meme_index(data)
    return {'ok': True}


def toggle_named(name: str, disabled: bool) -> dict:
    cur = read_settings()
    off = [n for n in (cur.get('sticker_disabled') or []) if n != name]
    if disabled:
        off.append(name)
    cur['sticker_disabled'] = off
    _write_private_json(_SETTINGS_FILE, cur)
    return {'ok': True}


def upload_named(name: str, b64: str) -> dict:
    name = _sticker_name_ok(name)
    try:
        raw = base64.b64decode(b64.split(',', 1)[-1], validate=False)
    except (ValueError, TypeError):
        raise ValueError('图片数据不对')
    if not raw or len(raw) > _MAX_UPLOAD:
        raise ValueError('图片太大（最大 2 MB）')
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw))
        img.load()
        img.thumbnail((360, 360))
        _CUSTOM_STICKERS.mkdir(mode=0o700, parents=True, exist_ok=True)
        img.convert('RGBA').save(_CUSTOM_STICKERS / f'{name}.png', format='PNG')
    except ImportError:
        raise ValueError('服务器缺少图片处理组件（Pillow）')
    except Exception:
        raise ValueError('这不是能识别的图片（支持 png / jpg / gif / webp）')
    return {'ok': True}


def delete_named(name: str) -> dict:
    f = _CUSTOM_STICKERS / f'{_sticker_name_ok(name)}.png'
    if not f.is_file():
        raise ValueError('只能删除自己上传的表情包；自带的可以停用')
    f.unlink()
    toggle_named(name, False)
    return {'ok': True}


# ───────────────────────── HTTP ─────────────────────────

_ADMIN_HTML = pathlib.Path(__file__).with_name('admin.html')
_MAX_BODY = 64 * 1024
_MAX_BODY_AUTHED = 4 * 1024 * 1024  # sticker uploads arrive as base64 JSON


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _cookie(self, name: str) -> str:
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == name:
                return v
        return ''

    def _authed(self) -> bool:
        return valid_session(self._cookie('wxadmin'))

    def _session_cookie(self, value: str, max_age: int) -> dict:
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        return {'Set-Cookie': f'wxadmin={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}'}

    def _login_ok(self) -> None:
        self._send(200, {'ok': True}, self._session_cookie(make_session(), SESSION_SECONDS))

    def _body(self) -> dict:
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length > (_MAX_BODY_AUTHED if self._authed() else _MAX_BODY):
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _setup_code_ok(self, body: dict) -> bool:
        return not _admin().get('hash') and hmac.compare_digest(str(body.get('code') or '').strip(), setup_code())

    def do_GET(self):  # noqa: N802
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            try:
                data = _ADMIN_HTML.read_bytes()
            except OSError:
                return self._send(500, {'error': 'admin.html missing'})
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == '/api/session':
            needs_setup = not _admin().get('hash')
            if needs_setup:
                print(f'[wxadmin] first-time setup code: {setup_code()}', flush=True)
            return self._send(200, {'needs_setup': needs_setup, 'authed': self._authed()})
        if path == '/api/auth-check':  # the proxy asks this before showing the WeChat screen
            return self._send(200 if self._authed() else 401, {})
        if not self._authed():
            return self._send(401, {'error': 'login required'})
        if path == '/api/sticker-img':
            q = urllib.parse.parse_qs(self.path.split('?', 1)[1] if '?' in self.path else '')
            f = sticker_file((q.get('kind') or [''])[0], (q.get('f') or [''])[0])
            if f is None:
                return self._send(404, {'error': 'not found'})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', _IMG_TYPES[f.suffix.lower()])
            self.send_header('Cache-Control', 'private, max-age=300')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        routes = {
            '/api/status': status,
            '/api/log': lambda: {'entries': read_log(), 'settings': read_settings(), 'chats': read_chats()},
            '/api/settings': read_settings,
            '/api/memes': read_memes,
            '/api/stats': stats,
            '/api/system': system_info,
            '/api/llm': llm_public,
            '/api/stickers': list_stickers,
            '/api/persona': persona_view,
        }
        if path in routes:
            self._send(200, routes[path]())
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):  # noqa: N802
        path = self.path.split('?', 1)[0]
        if 'application/json' not in (self.headers.get('Content-Type') or ''):
            return self._send(415, {'error': 'json only'})  # also blocks cross-site form posts
        body = self._body()
        try:
            if path == '/api/setup':
                if _admin().get('hash'):
                    return self._send(403, {'error': '已经设置过了，请直接登录'})
                if not self._setup_code_ok(body):
                    return self._send(403, {'error': '设置码不对（在服务器上运行 docker logs wechat_linux 查看）'})
                if len(str(body.get('password') or '')) < 8:
                    raise ValueError('密码至少 8 位')
                if body.get('api_key'):
                    save_llm(body)
                set_password(str(body.get('password')))
                return self._login_ok()
            if path == '/api/login':
                now = time.time()
                _login_failures[:] = [t for t in _login_failures if now - t < 600]
                if len(_login_failures) >= 10:
                    return self._send(429, {'error': '尝试次数太多，请 10 分钟后再试'})
                if not check_password(str(body.get('password') or '')):
                    _login_failures.append(now)
                    time.sleep(1)
                    return self._send(403, {'error': '密码不对'})
                return self._login_ok()
            if path == '/api/logout':
                return self._send(200, {'ok': True}, self._session_cookie('', 0))
            if path == '/api/llm/test':
                if not (self._authed() or self._setup_code_ok(body)):
                    return self._send(401, {'error': 'login required'})
                cur = read_llm()
                base = str(body.get('base_url') or cur.get('base_url') or DEFAULT_BASE_URL).strip()
                same = base.rstrip('/') == str(cur.get('base_url') or '').rstrip('/')
                key = str(body.get('api_key') or '').strip() or (str(cur.get('api_key') or '') if same else '')
                if not base.startswith(('https://', 'http://')):
                    return self._send(200, {'ok': False, 'error': '接口地址需要以 http(s):// 开头'})
                try:
                    models = list_models(base, key)
                except urllib.error.HTTPError as e:
                    return self._send(200, {'ok': False, 'error': 'API Key 无效' if e.code in (401, 403) else f'接口返回 {e.code}'})
                except (urllib.error.URLError, OSError, ValueError) as e:
                    return self._send(200, {'ok': False, 'error': f'连不上接口：{e}'})
                return self._send(200, {'ok': True, 'models': models})
            if not self._authed():
                return self._send(401, {'error': 'login required'})
            if path == '/api/relogin':
                relogin()
                self._send(200, {'ok': True})
            elif path == '/api/settings':
                self._send(200, write_settings(body))
            elif path == '/api/chat-settings':
                name = str(body.get('name') or '').strip()
                if not name:
                    return self._send(400, {'error': 'name required'})
                self._send(200, write_chat_settings(name, body.get('settings')))
            elif path == '/api/llm':
                self._send(200, save_llm(body))
            elif path == '/api/optimize':
                self._send(200, optimize())
            elif path == '/api/search-key':
                self._send(200, save_search_key(body))
            elif path == '/api/persona':
                self._send(200, save_persona(body))
            elif path == '/api/stickers/collected':
                self._send(200, update_collected(body))
            elif path == '/api/stickers/collected/delete':
                self._send(200, delete_collected(str(body.get('md5') or '')))
            elif path == '/api/stickers/named/toggle':
                self._send(200, toggle_named(_sticker_name_ok(str(body.get('name') or '')), bool(body.get('disabled'))))
            elif path == '/api/stickers/named/upload':
                self._send(200, upload_named(str(body.get('name') or ''), str(body.get('data') or '')))
            elif path == '/api/stickers/named/delete':
                self._send(200, delete_named(str(body.get('name') or '')))
            elif path == '/api/password':
                if not check_password(str(body.get('old') or '')):
                    return self._send(403, {'error': '原密码不对'})
                set_password(str(body.get('new') or ''))
                self._login_ok()
            else:
                self._send(404, {'error': 'not found'})
        except ValueError as e:
            self._send(400, {'error': str(e)})

    def log_message(self, *args):  # keep container logs quiet
        pass


if __name__ == '__main__':
    threading.Thread(target=_fit_windows_forever, daemon=True).start()
    if not _admin().get('hash'):
        print(f'[wxadmin] first-time setup code: {setup_code()}', flush=True)
    try:  # under memory pressure the kernel should kill this page server, not WeChat
        pathlib.Path('/proc/self/oom_score_adj').write_text('500')
    except OSError:
        pass
    ThreadingHTTPServer(('0.0.0.0', 6081), Handler).serve_forever()
