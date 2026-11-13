"""Tests for hot/wxfriends.py against a fake accessibility tree.

No X server, no WeChat: pyatspi is stubbed and a small tree mimics WeChat 4.x's
通讯录 list, where 新的朋友 is a collapsible section of plain text rows:

    新的朋友                   <- section header (click to unfold)
    星小孙接受                 <- waiting for me
    当时明月在我…已添加        <- already through
    联系人6                    <- next section

Run with:  python3 tests/test_wxfriends.py
"""

import pathlib
import sys
import types

HOT = pathlib.Path(__file__).resolve().parent.parent / 'hot'


class _State:
    def contains(self, _state):
        return True


class _Comp:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h

    def getExtents(self, _coords):  # noqa: N802 - mimics pyatspi
        return self


class _Action:
    def __init__(self, node):
        self.node = node
        self.nActions = 1 if node.on_click else 0

    def doAction(self, _i):
        if self.node.on_click:
            self.node.on_click()


class Node:
    def __init__(self, name, role='panel', x=0, y=0, w=200, h=30, on_click=None):
        self.name, self.role = name, role
        self.x, self.y, self.width, self.height = x, y, w, h
        self.on_click = on_click
        self.children = []
        self.parent = None

    @property
    def childCount(self):
        return len(self.children)

    def getChildAtIndex(self, i):
        return self.children[i]

    def getRoleName(self):
        return self.role

    def getState(self):
        return _State()

    def queryComponent(self):
        return _Comp(self.x, self.y, self.width, self.height)

    def queryAction(self):
        return _Action(self)

    def add(self, node):
        node.parent = self
        self.children.append(node)
        return node


DESKTOP = [None]


def install_stub():
    pyatspi = types.ModuleType('pyatspi')

    class Registry:
        @staticmethod
        def getDesktop(_i):
            return DESKTOP[0]

    pyatspi.Registry = Registry
    pyatspi.STATE_SHOWING = 'showing'
    pyatspi.DESKTOP_COORDS = 'desktop'
    sys.modules['pyatspi'] = pyatspi


def build(row_names, header=True):
    """A WeChat window on 通讯录, 新的朋友 section unfolded (unless header=False)."""
    app = Node('wechat', 'application')
    app.add(Node('微信', 'push button', w=60, h=36))
    app.add(Node('通讯录', 'push button', w=60, h=36))
    lst = app.add(Node('通讯录', 'list', x=61, y=81, w=240, h=686))
    lst.add(Node('', 'list item', x=61, y=95, w=240, h=52))
    if header:
        lst.add(Node('新的朋友', 'list item', x=61, y=147, w=240, h=36))
    y = 183
    for text in row_names:
        lst.add(Node(text, 'list item', x=61, y=y, w=240, h=70))
        y += 70
    lst.add(Node('联系人6', 'list item', x=61, y=y + 20, w=240, h=36))
    lst.add(Node('当时明月在', 'list item', x=61, y=y + 60, w=240, h=54))
    DESKTOP[0] = Node('desktop', 'desktop')
    DESKTOP[0].add(app)
    return app


def run():
    install_stub()
    sys.path.insert(0, str(HOT))
    import wxfriends

    failures = []

    def check(name, cond, detail=''):
        if not cond:
            failures.append(f'{name}: {detail}')
        print(f'{"OK  " if cond else "FAIL"} {name}{"" if cond else "  <- " + detail}')

    def accept_at(x, y):
        """Stand-in for the real mouse click: flips the row whose right edge was hit."""
        for node, _text in wxfriends._section_rows():
            if x > node.x + node.width - 40 and node.y < y < node.y + node.height:
                node.name = node.name.replace('接受', '已添加')
        return True

    # 1. chat-list entry detection (some builds list requests as a chat)
    check('is_friend_entry 新的朋友', wxfriends.is_friend_entry('新的朋友'))
    check('is_friend_entry 群', not wxfriends.is_friend_entry('相亲相爱一家人'))

    # 2. only rows between 新的朋友 and 联系人N belong to the section
    build(['星小孙接受', '当时明月在我…已添加'])
    rows = [t for _n, t in wxfriends._section_rows()]
    check('section rows parsed', rows == ['星小孙接受', '当时明月在我…已添加'], str(rows))
    check('only 接受 is pending', [t for _n, t in wxfriends.pending_rows()] == ['星小孙接受'])
    check('label drops the status', wxfriends._row_label('星小孙接受') == '星小孙')

    # 3. a collapsed section is unfolded by clicking the header
    build([], header=True)
    lst = wxfriends._contacts_list()
    header = [lst.getChildAtIndex(i) for i in range(lst.childCount) if lst.getChildAtIndex(i).name == '新的朋友'][0]

    def unfold():
        lst.children.insert(2, Node('李四接受', 'list item', x=61, y=183, w=240, h=70))

    header.on_click = unfold
    check('header click unfolds the section', wxfriends.expand_new_friends() is True)
    check('rows visible after unfolding', [t for _n, t in wxfriends.pending_rows()] == ['李四接受'])

    # 4. accepting flips WeChat's own label, and that is what counts as success
    build(['星小孙接受', '王五接受'])
    clicked = []
    wxfriends._click_at = lambda x, y: (clicked.append((x, y)), accept_at(x, y))[1]
    accepted = wxfriends.accept_all()
    check('accepts both waiting rows', sorted(accepted) == ['星小孙', '王五'], str(accepted))
    check('one status click per row', len([c for c in clicked if c[0] > 250]) == 2, str(clicked))
    check('nothing pending afterwards', wxfriends.pending_rows() == [])

    # 5. a click that changes nothing is reported, not looped on, and not counted
    build(['赵六接受'])
    wxfriends._click_at = lambda x, y: True
    check('dead click is not accepted', wxfriends.accept_all() == [])

    # 6. tick(): contacts path end to end, and the chat list is restored
    build(['孙八接受'])
    wxfriends._click_at = accept_at
    went_back = []
    wxfriends.back_to_chats = lambda: went_back.append(True)
    result = wxfriends.tick()
    check('tick uses the contacts path', result['path'] == 'contacts', str(result))
    check('tick accepted one', result['accepted'] == ['孙八'], str(result))
    check('tick returns to the chat list', went_back == [True], str(went_back))

    # 7. WeChat 4.x: 「前往验证」 opens 通过朋友验证 as a separate frame; 确定 finishes it
    app = build(['小王你好我是小王前往验证'])
    popup = []

    def confirm():
        for node, _t in wxfriends._section_rows():
            node.name = node.name.replace('前往验证', '已添加')
        app.children.remove(popup[0])

    def open_popup(x, y):
        """Older-build fallback: the status click opens the popup, a mouse click on its
        确定 confirms (its AT-SPI action does nothing, as in WeChat)."""
        if popup and popup[0] in app.children and 500 <= x <= 580 and 450 <= y <= 480:
            confirm()
        elif x > 250 and not (popup and popup[0] in app.children):
            frame = app.add(Node('通过朋友验证', 'frame', x=300, y=200, w=400, h=300))
            frame.add(Node('确定', 'push button', x=500, y=450, w=80, h=30))
            popup[:] = [frame]
        return True

    # main window wrapped in its own frame, as in the real tree
    main = Node('微信', 'frame')
    for child in list(app.children):
        main.add(child)
    app.children = []
    app.add(main)
    wxfriends._click_at = open_popup
    check('前往验证 is pending', [wxfriends._row_label(t) for _n, t in wxfriends.pending_rows()] == ['小王你好我是小王'])
    accepted = wxfriends.accept_all()
    check('前往验证 + 确定 accepted', accepted == ['小王你好我是小王'], str(accepted))
    check('popup closed', popup[0] not in app.children)

    # 8. a 确定 inside the main window is never clicked as a confirm
    build(['钱九前往验证'])
    hits = []
    DESKTOP[0].children[0].add(Node('确定', 'push button', w=80, h=30, on_click=lambda: hits.append(1)))
    wxfriends._click_at = lambda x, y: True
    check('dead click with stray 确定 not accepted', wxfriends.accept_all() == [])
    check('main-window 确定 untouched', hits == [], str(hits))

    # 9. the live 4.x layout: row "…等待验证" → pane with 前往验证 → 通过朋友验证 → 确定.
    #    A stale 通过朋友验证 frame (previous person's 备注) is already "showing".
    check('等待验证 is pending, not done', wxfriends.ACCEPT_STATUS.search('孙金辰我是孙金辰等待验证') is not None
          and not wxfriends.DONE_STATUS.search('孙金辰我是孙金辰等待验证'))
    app = build(['孙金辰我是孙金辰等待验证', '草上白羊我是草上白羊已添加'])
    main = Node('微信', 'frame', w=1228, h=768)
    for child in list(app.children):
        main.add(child)
    app.children = []
    app.add(main)
    def verify_frame(remark_name, on_ok):
        f = app.add(Node('通过朋友验证', 'frame', x=433, y=63, w=360, h=640))
        f.add(Node('备注', 'label', x=470, y=128, w=300, h=19))
        f.add(Node(remark_name, 'label', x=470, y=167, w=288, h=28))
        # mouse only: like WeChat, the AT-SPI action of this button does nothing
        f.add(Node('确定', 'push button', x=489, y=648, w=112, h=30)).mouse = on_ok
        return f

    events = []
    verify_frame('草上白羊', lambda: events.append('stale 确定'))  # left over, still "showing"

    def do_confirm():
        events.append('确定')
        for node, _t in wxfriends._section_rows():
            node.name = node.name.replace('等待验证', '已添加')

    def go_verify():
        events.append('前往验证')
        verify_frame('孙金辰', do_confirm)  # WeChat opens a NEW window

    def mouse_click(x, y):
        for win in reversed(DESKTOP[0].children[0].children):  # newest window is on top
            hits = [n for n in wxfriends._walk(win)
                    if getattr(n, 'mouse', None) and n.x <= x <= n.x + n.width and n.y <= y <= n.y + n.height]
            if hits:
                hits[0].mouse()
                return True
        if x < 302:  # a row body
            events.append('row')
            if not any(n.name == '前往验证' for n in main.children):
                main.add(Node('孙金辰', 'push button', x=608, y=121, w=60, h=60))
                main.add(Node('前往验证', 'push button', x=708, y=352, w=112, h=30)).mouse = go_verify
        return True

    wxfriends._click_at = mouse_click
    accepted = wxfriends.accept_all()
    check('4.x pane flow accepted', accepted == ['孙金辰我是孙金辰'], str(accepted))
    check('4.x order row→前往验证→确定', events == ['row', '前往验证', '确定'], str(events))

    # 10. 前往验证 that does not refresh the stale dialog must not click its 确定
    app = build(['李雷我是李雷等待验证'])
    main = Node('微信', 'frame', w=1228, h=768)
    for child in list(app.children):
        main.add(child)
    app.children = []
    app.add(main)
    stale = app.add(Node('通过朋友验证', 'frame', x=433, y=63, w=360, h=640))
    stale.add(Node('草上白羊', 'label', x=470, y=167, w=288, h=28))
    hits = []
    stale.add(Node('确定', 'push button', x=489, y=648, w=112, h=30)).mouse = lambda: hits.append(1)
    main.add(Node('李雷', 'push button', x=608, y=121, w=60, h=60))
    main.add(Node('前往验证', 'push button', x=708, y=352, w=112, h=30)).mouse = lambda: None
    wxfriends._click_at = mouse_click
    wxfriends._xdo = lambda *a: ''
    check('stale dialog: nothing accepted', wxfriends.accept_all() == [])
    check('stale dialog: 确定 untouched', hits == [], str(hits))

    # 12. the 通讯录 red badge decides whether the page is opened at all
    app = build([])
    nav = [n for n in app.children if n.name == '通讯录' and n.role == 'push button'][0]
    nav.x, nav.y, nav.width, nav.height = 1, 145, 60, 36
    slot = nav.add(Node('', 'filler', x=34, y=147, w=17, h=17))
    nav.add(Node('', 'filler', x=19, y=151, w=24, h=24)).add(Node('', 'push button', x=19, y=151, w=24, h=24))
    wxfriends._SCALE.update(v=1.0, done=True)
    check('no badge, no image', wxfriends.contacts_badge(None) == '')
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image is not None:
        blank = Image.new('RGB', (1280, 800), (240, 240, 240))
        check('no badge in pixels', wxfriends.contacts_badge(blank) == '')
        red = blank.copy()
        red.paste((250, 81, 81), (36, 149, 50, 163))  # WeChat's badge red, inside the slot
        got = wxfriends.contacts_badge(lambda: red)
        check('red badge in pixels', got.startswith('pixels:'), got)
        grey = blank.copy()
        grey.paste((120, 120, 120), (36, 149, 50, 163))
        check('grey dot is not a badge', wxfriends.contacts_badge(grey) == '')
    slot.add(Node('1', 'label', x=38, y=149, w=9, h=12))
    got = wxfriends.contacts_badge(None)
    check('badge in the accessibility tree', got.startswith('atspi:'), got)

    # 11. a 「确定退出登录？」 popup (its own window, no 通过朋友验证 title) is never clicked
    app = build(['韩梅梅接受'])
    main = Node('微信', 'frame', w=1228, h=768)
    for child in list(app.children):
        main.add(child)
    app.children = []
    app.add(main)
    logout = app.add(Node('', 'frame', x=473, y=299, w=282, h=170))
    out = []
    logout.add(Node('确定', 'push button', x=519, y=423, w=116, h=30)).mouse = lambda: out.append('LOGOUT')
    wxfriends._click_at = mouse_click
    wxfriends.accept_all()
    check('logout 确定 never clicked', out == [], str(out))

    print()
    if failures:
        print(f'{len(failures)} FAILED')
        for f in failures:
            print(' -', f)
        return 1
    print('all passed')
    return 0


if __name__ == '__main__':
    sys.exit(run())
