"""Tests for wxagent.stats(): the dashboard's numbers must match the bot's log.

No X server, no WeChat: only ~/.wxbot/log.jsonl is exercised. Run with:

    python3 tests/test_wxagent_stats.py
"""

import importlib.util
import json
import pathlib
import sys
import tempfile
import time

AGENT = pathlib.Path(__file__).resolve().parent.parent / 'wxagent.py'


def _load():
    spec = importlib.util.spec_from_file_location('wxagent', AGENT)
    module = importlib.util.module_from_spec(spec)
    sys.modules['wxagent'] = module
    spec.loader.exec_module(module)
    return module


def _write(rows):
    state = pathlib.Path(tempfile.mkdtemp()) / '.wxbot'
    state.mkdir(parents=True)
    log = state / 'log.jsonl'
    with open(log, 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    return state, log


NOW = int(time.time())

ROWS = [
    # today: 4 messages seen, 2 answered (one only dry-run), 1 ignored on purpose, 1 failed
    {'time': NOW - 300, 'chat': '张三', 'text': '在吗', 'reply': '在', 'sent': True, 'took': 2.0},
    {'time': NOW - 200, 'chat': '群A', 'group': True, 'text': 'hi', 'judged': 'skip'},
    {'time': NOW - 120, 'chat': '群A', 'group': True, 'text': '@小克', 'reply': '来了', 'dry_run': True, 'took': 4.0},
    {'time': NOW - 60, 'chat': '李四', 'text': '??', 'error': 'llm: URLError'},
    {'time': NOW - 30, 'chat': '新的朋友', 'text': 'x', 'reply': '已自动通过好友申请', 'sent': True, 'auto_accept': True},
    # three days ago: answered once
    {'time': NOW - 3 * 86400, 'chat': '张三', 'text': '早', 'reply': '早', 'sent': True, 'took': 1.0},
]


def test_daily_counters():
    agent = _load()
    state, log = _write(ROWS)
    agent._LOG_FILE = log
    agent._MEME_INDEX = state / 'memes' / 'index.json'
    stats = agent.stats()

    today = stats['today']
    assert today['in'] == 4, today  # friend approvals are not chat traffic
    assert today['out'] == 2, today
    assert today['chats'] == 3, today  # 张三 / 群A / 李四
    assert today['skipped'] == 1, today
    assert today['errors'] == 1, today
    assert today['friends'] == 1, today
    assert today['avg_took'] == 3.0, today

    assert stats['total'] == {'in': 5, 'out': 3, 'chats': 3, 'since': NOW - 3 * 86400}, stats['total']
    assert len(stats['daily']) == 7, stats['daily']
    assert stats['daily'][-1]['in'] == 4, stats['daily']
    assert stats['daily'][-1]['out'] == 2, stats['daily']
    assert stats['daily'][3]['in'] == 1, stats['daily']
    assert stats['last_at'] >= NOW - 40, stats['last_at']


def test_empty_and_missing_log():
    agent = _load()
    state, log = _write([])
    agent._LOG_FILE = log
    agent._MEME_INDEX = state / 'memes' / 'index.json'
    stats = agent.stats()
    assert stats['today']['in'] == 0 and stats['total']['in'] == 0, stats
    assert len(stats['daily']) == 7, stats['daily']

    agent._LOG_FILE = state / 'nope.jsonl'
    agent._ENTRY_CACHE = None
    agent._ENTRY_CACHE_KEY = None
    assert agent.stats()['total']['in'] == 0


def test_tail_read_keeps_latest_line():
    agent = _load()
    state, log = _write(ROWS)
    agent._LOG_FILE = log
    agent._MEME_INDEX = state / 'memes' / 'index.json'
    entries = agent.log_entries()
    assert len(entries) == len(ROWS), entries
    assert entries[-1]['chat'] == '张三', entries[-1]

    # A tail cut in the middle of a line drops that half line but keeps the rest.
    agent._ENTRY_CACHE = None
    agent._ENTRY_CACHE_KEY = None
    tail = agent.log_entries(tail_bytes=len(json.dumps(ROWS[-1], ensure_ascii=False)) + 40)
    assert tail and tail[-1]['chat'] == '张三', tail


if __name__ == '__main__':
    test_daily_counters()
    test_empty_and_missing_log()
    test_tail_read_keeps_latest_line()
    print('wxagent stats: ok')
