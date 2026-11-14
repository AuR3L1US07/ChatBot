"""Auto-accept WeChat friend requests, the way a person would.

WeChat has no bot API, so this drives the UI. In WeChat 4.x the pending requests live
inline in 通讯录 → 新的朋友: a collapsible section whose rows read like

    星 / 小孙 / 接受          -> still waiting, click 接受 on the right
    星 / 小孙 / 已添加        -> already through

The status on the right is plain text inside the row (no separate accessible button),
so a row is accepted by clicking where that text is drawn. Everything is read from the
accessibility tree (AT-SPI) and clicked with xdotool; WeChat's own files are untouched.

A row is only reported as accepted after WeChat's own label changes to 已添加, so a
mis-aimed click can never look like a success.

Standalone (read-only / one pass):

    python3 wxfriends.py --dump      # what the 新的朋友 section looks like right now
    python3 wxfriends.py --accept    # one pass, printing what happened
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

os.environ.setdefault('DISPLAY', ':1')

import pyatspi  # noqa: E402

BUTTON_ROLES = {'push button', 'toggle button', 'button', 'menu item'}
TAB_ROLES = BUTTON_ROLES | {'page tab', 'list item', 'table row', 'label'}
NAV_CONTACTS = re.compile(r'^(通讯录|Contacts)$')
NAV_CHATS = re.compile(r'^(微信|WeChat|聊天|Chats)$')
NEW_FRIENDS = re.compile(r'^(新的朋友|朋友验证|好友申请|New Friends)\s*[（(]?\d*[)）]?$')
CONTACTS_LIST = '通讯录'
# Row status, right-aligned in the 新的朋友 row. 接受/通过 means "waiting for me".
# WeChat 4.x (Linux) labels an incoming, still-open request 「等待验证」 - it waits for
# *my* verification - and the row itself is plain text. The first version filed 等待验证
# under "done", so every real request was skipped silently. Older builds say 「接受」.
ACCEPT_STATUS = re.compile(r'(等待验证|前往验证|去验证|接受|通过|同意|添加为好友|Accept|Verify)$')
DONE_STATUS = re.compile(r'(已添加|已通过|已过期|已发送|已拒绝|Added|Expired)')
STATUS_SUFFIX = re.compile(
    r'(前往验证|去验证|验证|接受|通过|同意|添加为好友|已添加|已通过|等待验证|已过期|已发送|已拒绝)+$'
)
CONFIRM = re.compile(r'^(确定|完成|通过|确定添加|通过验证|OK|Done|Confirm)$')
WINDOW_NAMES = ('wechat', 'weixin', '微信')
STATUS_MARGIN = 28  # px from the row's right edge to the middle of the status text
MAX_NODES = 20000  # a full tree walk costs one D-Bus round trip per node


def log(msg: str) -> None:
    print(f'[wxfriends] {msg}', file=sys.stderr, flush=True)


def is_friend_entry(name: str) -> bool:
    """True for a chat-list entry that carries friend requests ("新的朋友"), if the build
    happens to show one there."""
    return bool(NEW_FRIENDS.match((name or '').strip()))


# ───────────────────────── accessibility helpers ─────────────────────────


def _walk(root, limit: int = MAX_NODES):
    """Depth-first over the accessibility tree, skipping nodes that raise (Qt builds
    hand out stale handles while a page is being rebuilt)."""
    stack = [root]
    seen = 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        yield node
        try:
            for i in range(node.childCount):
                stack.append(node.getChildAtIndex(i))
        except Exception:
            continue


def _name(node) -> str:
    try:
        return (node.name or '').strip()
    except Exception:
        return ''


def _role(node) -> str:
    try:
        return node.getRoleName()
    except Exception:
        return ''


def _showing(node) -> bool:
    try:
        return node.getState().contains(pyatspi.STATE_SHOWING)
    except Exception:
        return True


def _size(node) -> tuple[int, int]:
    try:
        e = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        return e.width, e.height
    except Exception:
        return 0, 0


def _extents(node):
    try:
        return node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
    except Exception:
        return None


def _clickable(node) -> bool:
    w, h = _size(node)
    return w > 2 and h > 2


def _wechat_apps() -> list:
    """The WeChat window(s) - dialogs are separate frames under the same application."""
    out = []
    try:
        desktop = pyatspi.Registry.getDesktop(0)
    except Exception:
        return out
    for i in range(desktop.childCount):
        try:
            app = desktop.getChildAtIndex(i)
            if (app.name or '').strip().lower() in WINDOW_NAMES:
                out.append(app)
        except Exception:
            continue
    return out


def _xdo(*args: str) -> str:
    try:
        return subprocess.run(['xdotool', *args], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


_SCALE = {'v': 1.0}


def calibrate() -> float:
    """AT-SPI reports WeChat's coordinates in Qt's logical pixels, which on this screen
    are ~4% smaller than X pixels (main window: 1228x768 in AT-SPI, 1280x800 on screen).
    Rows are big enough to survive that, the 确定 button is not - a click aimed at its
    middle lands above it. Scale = X width / AT-SPI width of the main window."""
    scale = 1.0
    try:
        main = None
        for app in _wechat_apps():
            for i in range(app.childCount):
                k = app.getChildAtIndex(i)
                if _role(k) == 'frame' and _name(k) in ('微信', 'WeChat', 'Weixin'):
                    main = k
                    break
        e = _extents(main) if main is not None else None
        widths = []
        for wid in _xdo('search', '--onlyvisible', '--name', '^(微信|WeChat|Weixin)$').split():
            geo = _xdo('getwindowgeometry', '--shell', wid)
            if 'WIDTH=' in geo:
                widths.append(int(geo.split('WIDTH=')[1].split()[0]))
        if e is not None and e.width > 200 and widths:
            s = max(widths) / e.width
            if 0.8 < s < 2.5:
                scale = s
    except Exception:
        pass
    _SCALE['v'] = scale
    _SCALE['done'] = True
    return scale


_NAV: dict = {}


def _contacts_nav():
    """The 通讯录 button of the left navigation bar (the wide one, not its icon).

    Finding it means walking WeChat's whole UI tree (thousands of D-Bus round trips,
    which WeChat has to answer), and the badge check asks for it twice a minute around
    the clock. The button never goes away while WeChat runs, so it is remembered and
    only looked up again when it stops answering as the 通讯录 button."""
    node = _NAV.get('node')
    if node is not None:
        e = _extents(node)
        if _role(node) in BUTTON_ROLES and NAV_CONTACTS.match(_name(node)) and e is not None and 0 < e.width and e.x < 80:
            return node
        _NAV.clear()
    best = None
    for node in _find(_wechat_apps(), NAV_CONTACTS, BUTTON_ROLES):
        e = _extents(node)
        if e is not None and e.x < 80 and (best is None or e.width > _extents(best).width):
            best = node
    if best is not None:
        _NAV['node'] = best
    return best


def _is_badge_red(px) -> bool:
    r, g, b = px[:3]
    return r >= 200 and g <= 130 and b <= 130 and r - g >= 90


def contacts_badge(image=None) -> str:
    """Why 通讯录 looks like it has news (its red count badge), or '' when it does not.

    Every nav button holds a ~17x17 slot at its top-right corner where WeChat draws the
    red number. The slot is read from the accessibility tree first (a name or a child
    there); failing that, the screen pixels of that corner are checked for badge red.
    `image` is a PIL screenshot of the whole X screen, or a function returning one
    (called only if needed), or None to skip the pixel check."""
    nav = _contacts_nav()
    if nav is None:
        return ''
    slot = None
    for node in _walk(nav, 40):
        if node is nav:
            continue
        e = _extents(node)
        name = _name(node)
        if name and name != _name(nav) and re.search(r'\d|new|新', name, re.I):
            return f'atspi:{name}'
        if e is not None and 8 <= e.width <= 22 and 8 <= e.height <= 22 and _role(node) in ('filler', 'label'):
            slot = slot or node
            try:
                if node.childCount:
                    return 'atspi:slot_child'
            except Exception:
                pass
    if callable(image):  # taken only now that the accessibility tree had nothing
        try:
            image = image()
        except Exception:
            image = None
    if image is None:
        return ''
    if _SCALE.get('done') is None:
        calibrate()
    s = _SCALE['v']
    box = _extents(slot) if slot is not None else None
    ne = _extents(nav)
    if box is None and ne is None:
        return ''
    if box is not None:  # the slot, a little larger: a two-digit badge overflows it
        x0, y0, x1, y1 = box.x - 6, box.y - 6, box.x + box.width + 6, box.y + box.height + 6
    else:  # no slot node: the right half of the button's top
        x0, y0, x1, y1 = ne.x + ne.width // 2, ne.y, ne.x + ne.width, ne.y + ne.height // 2
    try:
        crop = image.convert('RGB').crop((round(x0 * s), round(y0 * s), round(x1 * s), round(y1 * s)))
        red = sum(1 for px in crop.getdata() if _is_badge_red(px))
    except Exception:
        return ''
    return f'pixels:{red}' if red >= 12 else ''


def _click_at(x: int, y: int) -> bool:
    """Click at AT-SPI coordinates (converted to X pixels with the calibrated scale)."""
    s = _SCALE['v']
    x, y = round(x * s), round(y * s)
    _xdo('mousemove', str(int(x)), str(int(y)))
    time.sleep(0.1)
    _xdo('click', '1')
    return True


def _activate(node) -> bool:
    """Click a node. The AT-SPI action is tried first (no pointer involved); the mouse is
    the fallback."""
    try:
        action = node.queryAction()
        if action.nActions > 0:
            action.doAction(0)
            return True
    except Exception:
        pass
    e = _extents(node)
    if e is None or e.width <= 0 or e.height <= 0:
        return False
    return _click_at(e.x + e.width // 2, e.y + e.height // 2)


def _find(apps, pattern, roles) -> list:
    out = []
    for app in apps:
        for node in _walk(app):
            if _role(node) not in roles:
                continue
            if not pattern.match(_name(node)):
                continue
            if not _showing(node) or not _clickable(node):
                continue
            out.append(node)
    return out


def _in_dialog(node) -> bool:
    cur = node
    for _ in range(6):
        try:
            cur = cur.parent
        except Exception:
            return False
        if cur is None:
            return False
        if _role(cur) in ('dialog', 'alert'):
            return True
    return False


# ───────────────────────── the 新的朋友 section ─────────────────────────


def _contacts_list():
    """The visible 通讯录 list. WeChat keeps hidden copies of the page (zero-sized) for
    the other tabs, so the one with a real size is the open page."""
    apps = _wechat_apps()
    for node in _walk(apps[0]) if apps else []:
        if _role(node) == 'list' and _name(node) == CONTACTS_LIST and _clickable(node):
            return node
    return None


def _section_rows(lst=None) -> list[tuple]:
    """(node, text) of the rows inside 新的朋友 - everything between that header and the
    next section header ("联系人N"). Empty when the section is collapsed."""
    rows: list[tuple] = []
    lst = lst if lst is not None else _contacts_list()
    if lst is None:
        return rows
    try:
        items = [lst.getChildAtIndex(i) for i in range(lst.childCount)]
    except Exception:
        return rows
    inside = False
    for item in items:
        name = _name(item)
        if NEW_FRIENDS.match(name):
            inside = True
            continue
        if inside and re.match(r'^(联系人|群聊|标签|公众号|企业微信联系人)', name):
            break
        if inside and name and _showing(item) and _clickable(item):
            rows.append((item, name))
    return rows


def pending_rows(lst=None) -> list[tuple]:
    """Request rows still waiting for me: their text ends with 接受/通过."""
    lst = lst if lst is not None else _contacts_list()
    return [(node, text) for node, text in _section_rows(lst) if ACCEPT_STATUS.search(text)]


def done_rows(lst=None) -> list[tuple]:
    lst = lst if lst is not None else _contacts_list()
    return [(node, text) for node, text in _section_rows(lst) if DONE_STATUS.search(text)]


def _row_label(text: str) -> str:
    """'星小孙接受' -> '星 / 小孙' (the status suffix dropped) for logs."""
    return STATUS_SUFFIX.sub('', text).strip()[:100] or '（未知申请人）'


def _click_status(node) -> bool:
    """Click the status text at the row's right edge - the row body opens the profile."""
    e = _extents(node)
    if e is None or e.width <= 0:
        return False
    return _click_at(e.x + e.width - STATUS_MARGIN, e.y + e.height // 2)


def _top_frame(node):
    """The application's direct child (its window) that holds `node`."""
    cur = node
    for _ in range(60):
        try:
            parent = cur.parent
        except Exception:
            return None
        if parent is None:
            return None
        if _role(parent) == 'application':
            return cur
        cur = parent
    return None


def _confirm_dialog(main_frame=None) -> bool:
    """WeChat 4.x opens 通过朋友验证 (备注/标签/权限) after 前往验证; click its 确定.
    Qt draws that popup as its own top-level window, often with role 'frame' rather
    than 'dialog', so a button counts when it is in a dialog OR in a window other than
    the main one (never a 确定 inside the main window's own pages)."""
    apps = _wechat_apps()
    for app in apps:
        for node in _walk(app):
            if _role(node) not in BUTTON_ROLES or not CONFIRM.match(_name(node)):
                continue
            if not _showing(node) or not _clickable(node):
                continue
            # ONLY a 确定 inside a window titled 通过朋友验证. WeChat's other popups say 确定
            # too - 「确定退出登录？」 among them - and must never be clicked from here.
            frame = _top_frame(node)
            if main_frame is None or frame is None or frame is main_frame:
                continue
            if not VERIFY_FRAME.match(_name(frame)):
                continue
            if _mouse(node):
                time.sleep(0.4)
                return True
    return False


# ── WeChat 4.x flow (seen live 2026-11-13) ──
# row "张三 / 我是张三 / 等待验证" (plain text) → click the row body → the right pane
# shows the request with a 「前往验证」 push button → that opens the top-level frame
# 「通过朋友验证」 (备注 / 标签 / 朋友权限) → its 「确定」 finishes it; the row turns 已添加.
# Qt reuses that frame and AT-SPI keeps reporting it as showing after it closed (it still
# held the previous person's 备注), so it is only trusted once its contents name the
# person being accepted, or at least changed after 前往验证 was clicked.
VERIFY_BUTTON = re.compile(r'^(前往验证|去验证|通过验证)$')
VERIFY_FRAME = re.compile(r'^(通过朋友验证|朋友验证)')
DIALOG_FIXED = {'备注', '标签', '修改备注', '修改标签', '朋友权限', '朋友圈和状态', '仅聊天',
                '确定', '取消', '通过朋友验证', '搜索或创建标签...', '不看他（她）', '不让他（她）看',
                '聊天、朋友圈、微信运动等'}


def _right_of(node, x_min: int) -> bool:
    e = _extents(node)
    return e is not None and e.x >= x_min


def _pane_nodes(main_frame, x_min: int) -> list:
    """Showing nodes of the main window to the right of the contacts list."""
    if main_frame is None:
        return []
    return [n for n in _walk(main_frame) if _showing(n) and _clickable(n) and _right_of(n, x_min)]


def _verify_frames() -> list:
    """Every 通过朋友验证 window. WeChat opens a NEW one per 前往验证 and the old ones stay
    in the tree (still 'showing', with the earlier person's 备注)."""
    out = []
    for app in _wechat_apps():
        try:
            kids = [app.getChildAtIndex(i) for i in range(app.childCount)]
        except Exception:
            continue
        out.extend(k for k in kids if VERIFY_FRAME.match(_name(k)))
    return out


def _verify_frame():
    frames = _verify_frames()
    return frames[-1] if frames else None


def _mouse(node) -> bool:
    """WeChat's Qt buttons expose only a 'SetFocus' action, so they need a real click."""
    e = _extents(node)
    if e is None or e.width <= 2 or e.height <= 2:
        return False
    return _click_at(e.x + e.width // 2, e.y + e.height // 2)


def _frame_texts(frame) -> set:
    if frame is None:
        return set()
    return {_name(n) for n in _walk(frame, 400) if _role(n) in ('label', 'text') and _name(n)}


def _accept_via_pane(node, text: str, main_frame, x_min: int, log_fn) -> bool | None:
    """The 4.x path. None = this build has no 前往验证 pane (use the old status click)."""
    label = _row_label(text)
    e = _extents(node)
    if e is None:
        return False
    _click_at(e.x + e.width // 2, e.y + e.height // 2)  # row body, not the status
    time.sleep(1.2)
    pane = _pane_nodes(main_frame, x_min)
    button = next((n for n in pane if _role(n) in BUTTON_ROLES and VERIFY_BUTTON.match(_name(n))), None)
    if button is None:
        return None
    # The pane must be this person's request (its avatar button / name label).
    if not any(_name(n) and text.startswith(_name(n)) for n in pane if _name(n) not in DIALOG_FIXED):
        log_fn(f'{label}: the open pane is someone else\'s request, skipped')
        return False
    # pyatspi hands out a new wrapper per lookup, so old windows are known by content.
    before = [frozenset(_frame_texts(f)) for f in _verify_frames()]
    if not _mouse(button):
        return False
    frame = None
    for _ in range(6):
        time.sleep(0.5)
        frames = _verify_frames()
        # Newest first: the window naming this person, else one that is new or changed.
        for f in reversed(frames):
            texts = _frame_texts(f)
            if any(text.startswith(t) for t in texts - DIALOG_FIXED):
                frame = f
                break
        if frame is None:
            for f in reversed(frames):
                texts = _frame_texts(f)
                if texts and frozenset(texts) not in before:
                    frame = f
                    break
        if frame is not None:
            break
    if frame is None:
        # No key presses here: Escape on the main window opens 「确定退出登录？」.
        log_fn(f'{label}: 通过朋友验证 did not open')
        return False
    ok = next((n for n in _walk(frame, 400) if _role(n) in BUTTON_ROLES and CONFIRM.match(_name(n))), None)
    if ok is None or not _mouse(ok):
        log_fn(f'{label}: no 确定 in 通过朋友验证')
        cancel = next((n for n in _walk(frame, 400) if _role(n) in BUTTON_ROLES and _name(n) == '取消'), None)
        if cancel is not None:
            _mouse(cancel)  # close only this dialog, by its own 取消
        return False
    time.sleep(1.5)
    return True


def accept_all(limit: int = 6, log_fn=log) -> list[str]:
    """Accept every waiting row. A row counts as accepted only once WeChat relabels it."""
    accepted: list[str] = []
    tried: set = set()
    for _ in range(limit):
        rows = [(n, t) for n, t in pending_rows() if _row_label(t) not in tried]
        if not rows:
            break
        node, text = rows[0]
        label = _row_label(text)
        tried.add(label)
        lst = _contacts_list()
        main_frame = _top_frame(lst) if lst is not None else None
        le = _extents(lst) if lst is not None else None
        x_min = (le.x + le.width) if le is not None else 0
        confirmed = _accept_via_pane(node, text, main_frame, x_min, log_fn)
        if confirmed is None:  # older builds: a clickable 接受 at the row's right edge
            if not _click_status(node):
                log_fn(f'could not click the status of {label}')
                break
            time.sleep(1.0)
            confirmed = _confirm_dialog(main_frame)
            time.sleep(0.8 if confirmed else 0.6)
        # Success only when the list is readable again and WeChat itself relabelled the
        # row (or dropped it). An unreadable list - e.g. a dialog still in front - is a
        # failure, not a silent success.
        # WeChat relabels the row only after its server answers - seen live taking longer
        # than 1.5 s - so give a confirmed request up to ~10 s before calling it a failure.
        deadline = time.time() + (10.0 if confirmed else 0.0)
        while True:
            lst = _contacts_list()
            rows_now = _section_rows(lst) if lst is not None else []
            still = any(_row_label(t) == label and ACCEPT_STATUS.search(t) for _n, t in rows_now)
            if (lst is not None and not still) or time.time() >= deadline:
                break
            time.sleep(1.0)
        if lst is None:
            log_fn(f'{label}: list not readable after the click (confirm={confirmed})')
            break
        if still:
            log_fn(f'{label}: still waiting after the click (confirm={confirmed})')
            break
        accepted.append(label)
        log_fn(f'accepted: {label}')
    return accepted


def open_contacts(tries: int = 2) -> bool:
    """Switch to 通讯录, and only report success once its list is really readable. A reply
    or a notification can yank the UI back to a chat right after the click, so this is
    tried more than once - otherwise one unlucky collision wastes the whole pass."""
    entry = _find(_wechat_apps(), NAV_CONTACTS, TAB_ROLES)
    if not entry:
        return False
    for attempt in range(tries):
        if _activate(entry[0]):
            time.sleep(1.0 if attempt == 0 else 1.4)
            if _contacts_list() is not None:
                return True
        entry = _find(_wechat_apps(), NAV_CONTACTS, TAB_ROLES) or entry
    return False


def expand_new_friends() -> bool:
    """Make sure the section is unfolded. Clicking the header toggles it, so only click
    when nothing inside is visible yet, and confirm the rows really showed up."""
    for _ in range(2):
        if _section_rows() or done_rows():
            return True
        lst = _contacts_list()
        if lst is None:
            return False
        try:
            items = [lst.getChildAtIndex(i) for i in range(lst.childCount)]
        except Exception:
            return False
        header = next((i for i in items if NEW_FRIENDS.match(_name(i))), None)
        if header is None or not _activate(header):
            return False
        time.sleep(1.2)
    return bool(_section_rows() or done_rows())


def back_to_chats() -> None:
    entry = _find(_wechat_apps(), NAV_CHATS, TAB_ROLES)
    if entry and _activate(entry[0]):
        time.sleep(0.6)


def tick(session_entry: str | None = None, click_chat=None, reset=None) -> dict:
    """One pass. Normally: 通讯录 → 新的朋友 → click 接受 on each waiting row. When the
    build does list requests as a chat, `session_entry` takes that cheaper path.
    `reset()` drops the caller's caches because the page changes underneath it."""
    result: dict = {'accepted': [], 'waiting': 0, 'path': 'none'}
    if not _wechat_apps():
        result['error'] = 'no_wechat_window'
        return result
    result['scale'] = round(calibrate(), 4)
    try:
        if session_entry and click_chat is not None and click_chat(session_entry):
            result['path'] = 'chat'
            time.sleep(1.0)
            result['accepted'] = accept_all()
            return result

        result['path'] = 'contacts'
        if not open_contacts():
            result['error'] = 'no_contacts_button'
            return result
        if not expand_new_friends():
            result['error'] = 'no_new_friends_entry'
            return result
        # What the section really says, so a label this code does not know shows up in
        # the bot log instead of failing silently as "0 waiting".
        result['rows'] = [t[:60] for _n, t in _section_rows()][:8]
        result['waiting'] = len(pending_rows())
        result['accepted'] = accept_all()
    finally:
        back_to_chats()
        if reset is not None:
            reset()
    return result


# ───────────────────────── standalone debugging ─────────────────────────


def _dump() -> None:
    print('apps:', [_name(a) for a in _wechat_apps()])
    lst = _contacts_list()
    print('通讯录 list:', 'found' if lst is not None else 'not open')
    if lst is None:
        print('open 通讯录 first: python3 wxfriends.py --contacts')
        return
    print('rows:')
    for node, text in _section_rows(lst):
        e = _extents(node)
        geo = f'{e.x},{e.y} {e.width}x{e.height}' if e else '-'
        print(f'  {geo:<22} {text[:60]!r}')
    print('pending:', [_row_label(t) for _n, t in pending_rows(lst)])
    print('accept status clicks would work on:', len(pending_rows(lst)))


if __name__ == '__main__':
    if '--dump' in sys.argv:
        _dump()
    elif '--contacts' in sys.argv:
        print('open contacts:', open_contacts(), 'expand:', expand_new_friends())
    elif '--accept' in sys.argv:
        print(tick())
    else:
        print(__doc__)
