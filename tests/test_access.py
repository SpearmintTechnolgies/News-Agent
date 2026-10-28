import pytest

from newsagent_v2.telegram import access, access_menu


class FakeClient:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []

    def send_message(self, **kw):
        self.sent.append(kw)
        return {"ok": True}

    def edit_message_text(self, **kw):
        self.edits.append(kw)
        return {"ok": True}

    def answer_callback_query(self, **kw):
        self.answers.append(kw)
        return {"ok": True}

    def _post(self, method, json_body):
        return {"ok": True, "payload": {"result": {"title": "Newsroom"}}}


@pytest.fixture(autouse=True)
def state(tmp_path):
    access.configure(tmp_path, ["100"])
    access._NOTICES.clear()
    yield
    access.configure(None, [])


def test_admin_user_group_rules():
    assert access.allows("100", "100") and access.allows("-5", "100", admin=True)
    assert not access.allows("200", "200")
    access.add_user(200, 100, "Sam")
    assert access.allows("200", "200") and not access.allows("200", "200", admin=True)
    assert not access.allows("-5", "200")          # approved user, unapproved group
    access.add_group(-5, 100, "Desk")
    assert access.allows("-5", "300") and not access.allows("-5", "300", admin=True)
    access.block(200)
    assert not access.allows("200", "200")
    assert access.remove_user(100) is False        # super admin cannot be removed


def test_request_approve_flow():
    c = FakeClient()
    cq = {"id": "1", "data": "ac:req", "from": {"id": 200, "first_name": "Sam"}, "message": {"message_id": 7, "chat": {"id": 200}}}
    access_menu.handle_callback(c, cq)
    assert "200" in access.snapshot()["pending_users"]
    assert c.sent[0]["chat_id"] == "100"           # the admin got the card
    approve = {"id": "2", "data": "ac:ok:200", "from": {"id": 100}, "message": {"message_id": 8, "chat": {"id": 100}}}
    access_menu.handle_callback(c, approve)
    assert access.is_user(200)
    # a non-admin cannot approve
    access.remove_user(200)
    forged = {"id": "3", "data": "ac:ok:200", "from": {"id": 300}, "message": {"message_id": 9, "chat": {"id": 300}}}
    access_menu.handle_callback(c, forged)
    assert not access.is_user(200)


def test_group_added_by_admin_is_auto_approved_and_leaving_removes():
    c = FakeClient()
    upd = {"chat": {"id": -9, "type": "supergroup", "title": "Desk"}, "from": {"id": 100}, "new_chat_member": {"status": "member"}}
    access_menu.handle_my_chat_member(c, upd)
    assert access.is_group(-9)
    access_menu.handle_my_chat_member(c, {**upd, "new_chat_member": {"status": "kicked"}})
    assert not access.is_group(-9)


def test_group_added_by_stranger_asks_admin():
    c = FakeClient()
    upd = {"chat": {"id": -9, "type": "group", "title": "Desk"}, "from": {"id": 300, "first_name": "X"}, "new_chat_member": {"status": "member"}}
    access_menu.handle_my_chat_member(c, upd)
    assert not access.is_group(-9) and "-9" in access.snapshot()["pending_groups"]
    assert any(m["chat_id"] == "100" for m in c.sent)


def test_admin_adds_user_by_id():
    c = FakeClient()
    access.set_awaiting(100, "user")
    msg = {"from": {"id": 100}, "chat": {"id": 100, "type": "private"}, "text": "555"}
    assert access_menu.handle_admin_input(c, msg)
    assert access.is_user(555)


def test_daily_limit_and_blocked_management():
    access.add_user(200, 100, "Sam")
    access.set_daily_limit(2)
    assert access.check_quota(200)[0]
    access.record_story(200, "evt-1"); access.record_story(200, "evt-2")
    ok, why = access.check_quota(200)
    assert not ok and "2/2" in why
    assert access.check_quota(100)[0]              # super admin is never limited
    access.set_daily_limit(0)
    assert access.check_quota(200)[0]
    access.block(200)
    assert access.is_blocked(200) and access.unblock(200) and not access.is_blocked(200)


def test_limit_command_and_screens():
    c = FakeClient()
    msg = {"from": {"id": 100}, "chat": {"id": 100, "type": "private"}}
    access_menu.handle_command(c, "/limit", "7", msg)
    assert access.daily_limit() == 7
    access.block(300)
    text, rows = access_menu.blocked_screen()
    assert "300" in text and rows[0][0][1] == "ac:ub:300"
    assert "ac:b" in str(access_menu.main_menu()[1])


def test_feature_switches_per_user_and_group():
    access.add_user(200, 100, "Sam")
    access.add_group(-5, 100, "Desk")
    assert not access.can("publish", 200, "200", "private")
    assert access.set_feature("user", 200, "publish", True)
    assert access.can("publish", 200, "200", "private") and not access.can("autopilot", 200, "200", "private")
    assert not access.can("publish", 300, "-5", "supergroup")
    access.set_feature("group", -5, "publish", True)
    assert access.can("publish", 300, "-5", "supergroup")
    assert access.can("sites", 100, "-5", "supergroup") or access.can("sites", 100, "100", "private")   # super admin: everything
    assert not access.set_feature("user", 999, "publish", True)       # not approved
    assert not access.set_feature("user", 200, "nonsense", True)
    access.remove_user(200)
    assert not access.can("publish", 200, "200", "private")


def test_features_menu_toggles_via_callbacks():
    c = FakeClient()
    access.add_user(200, 100, "Sam")
    cb = lambda data, who=100: {"id": "1", "data": data, "from": {"id": who}, "message": {"message_id": 5, "chat": {"id": who}}}
    access_menu.handle_callback(c, cb("ac:tu:200:p"))
    assert access.can("publish", 200, "200")
    access_menu.handle_callback(c, cb("ac:tu:200:p"))
    assert not access.can("publish", 200, "200")
    access_menu.handle_callback(c, cb("ac:tu:200:p", who=300))        # not an admin
    assert not access.can("publish", 200, "200")
    assert "ac:fu:200" in str(access_menu.users_screen()[1])


def test_story_ownership():
    access.add_user(200, 100, "Sam"); access.add_user(201, 100, "Kim")
    access.add_group(-5, 100, "Desk")
    access.record_story(200, "evt-a", "200")
    access.record_story(200, "evt-g", "-5")
    assert access.can_manage_story(200, "200", "evt-a")
    assert not access.can_manage_story(201, "201", "evt-a")           # someone else's story
    assert access.can_manage_story(100, "100", "evt-a")               # super admin
    assert access.can_manage_story(201, "-5", "evt-g")                # same approved group
    assert not access.can_manage_story(201, "201", "evt-g")
    assert access.can_manage_story(201, "201", "evt-unknown")         # unrecorded stories are not restricted


def test_home_screen_is_shaped_by_role():
    from newsagent_v2.telegram import home_menu
    access.add_user(200, 100, "Sam")
    base = {"inline_keyboard": [[{"text": "Site", "callback_data": "site:abc"}],
                                [{"text": "A", "callback_data": "idle:menu"}, {"text": "B", "callback_data": "site:add"}]]}
    flat = lambda m: str(m)
    user = flat(home_menu.decorate(base, 200, "200"))
    assert "idle:menu" not in user and "site:add" not in user and "ac:m" not in user and "hm:my" in user
    access.set_feature("user", 200, "autopilot", True)
    assert "idle:menu" in flat(home_menu.decorate(base, 200, "200")) and "site:add" not in flat(home_menu.decorate(base, 200, "200"))
    admin = flat(home_menu.decorate(base, 100, "100"))
    assert "idle:menu" in admin and "site:add" in admin and "ac:m" in admin and "hm:r" in admin
    assert "Super admin" in home_menu.header(100, "100") and "Approved user" in home_menu.header(200, "200")


def test_home_buttons_are_gated():
    from newsagent_v2.telegram import home_menu
    c = FakeClient()
    access.add_user(200, 100, "Sam")
    cq = lambda data, who: {"id": "1", "data": data, "from": {"id": who}, "message": {"message_id": 5, "chat": {"id": who, "type": "private"}}}
    home_menu.handle_callback(c, cq("hm:id", 777), "help")                 # a stranger may look up their ID
    assert "777" in c.sent[-1]["text"]
    n = len(c.sent)
    home_menu.handle_callback(c, cq("hm:my", 777), "help")                 # but nothing else
    home_menu.handle_callback(c, cq("hm:r", 200), "help")                  # report not switched on
    assert len(c.sent) == n
    home_menu.handle_callback(c, cq("hm:my", 200), "help")
    assert "MY STORIES" in c.sent[-1]["text"]


def test_pasted_secrets_are_recognised():
    assert access_menu.looks_like_secret("1234567890:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
    assert not access_menu.looks_like_secret("write a story about bitcoin today")
    assert not access_menu.looks_like_secret("/start")


def _runtime():
    from types import SimpleNamespace
    from newsagent_v2.telegram.config import TelegramConfig
    c = FakeClient()
    return SimpleNamespace(config=TelegramConfig(bot_token="fake", test_chat_id="555"), client=c), c


def _cb(data, who, chat=None):
    return {"update_id": 1, "callback_query": {"id": "1", "data": data, "from": {"id": who, "is_bot": False},
                                                 "message": {"message_id": 5, "chat": {"id": chat or who, "type": "private" if not chat else "supergroup"}}}}


def test_loop_gate_feature_and_ownership(monkeypatch):
    import start_v5_bot as bot
    rt, c = _runtime()
    access.add_user(200, 100, "Sam"); access.add_user(201, 100, "Kim")
    access.record_story(200, "evt-a", "200")
    # approved user: normal actions pass, publish / autopilot / site setup need the switch
    assert bot._authorized(rt, _cb("run:evt-a", 200))
    assert not bot._authorized(rt, _cb("publish:evt-a", 200))
    assert not bot._authorized(rt, _cb("idle:menu", 200))
    access.set_feature("user", 200, "publish", True)
    assert bot._authorized(rt, _cb("publish:evt-a", 200)) and not bot._authorized(rt, _cb("idle:menu", 200))
    # the super admin passes everything; strangers nothing
    assert bot._authorized(rt, _cb("idle:menu", 100)) and not bot._authorized(rt, _cb("run:evt-a", 999))
    # somebody else's story is refused by the gate; the owner and the admin pass
    assert bot._handle_access_update(rt, _cb("cancel:evt-a", 201), {"username": "b"}) is True
    assert c.answers and "belongs to someone else" in c.answers[-1]["text"]
    assert bot._handle_access_update(rt, _cb("cancel:evt-a", 200), {"username": "b"}) is False
    assert bot._handle_access_update(rt, _cb("cancel:evt-a", 100), {"username": "b"}) is False


def test_website_choice_per_user_and_group():
    access.add_user(200, 100, "Sam"); access.add_group(-5, 100, "Desk")
    assert access.allowed_sites(200, "200") is None and access.site_allowed(200, "200", "s1")     # default: every website
    access.toggle_site("user", 200, "s1", ["s1", "s2", "s3"])                                       # first flip starts from "all"
    assert access.allowed_sites(200, "200") == {"s2", "s3"}
    assert not access.site_allowed(200, "200", "s1") and access.site_allowed(200, "200", "s2")
    assert access.site_allowed(100, "100", "s1")                                                    # super admin: everything
    access.set_sites("group", -5, ["s3"])
    assert access.site_allowed(300, "-5", "s3") and not access.site_allowed(300, "-5", "s2")
    access.set_sites("user", 200, None)
    assert access.allowed_sites(200, "200") is None


def test_website_menu_and_gate(monkeypatch):
    import start_v5_bot as bot
    from types import SimpleNamespace
    from newsagent_v2.telegram import home_menu
    from newsagent_v2.control import sites as sites_mod
    sites = [SimpleNamespace(id="s1", name="One"), SimpleNamespace(id="s2", name="Two")]
    monkeypatch.setattr(sites_mod, "list_sites", lambda *a, **k: sites)
    access.add_user(200, 100, "Sam")
    c = FakeClient()
    cb = lambda data, who=100: {"id": "1", "data": data, "from": {"id": who}, "message": {"message_id": 5, "chat": {"id": who}}}
    access_menu.handle_callback(c, cb("ac:ws:u:200:s1"))
    assert access.allowed_sites(200, "200") == {"s2"}
    text, rows = access_menu.websites_screen("u", "200")
    assert "ac:ws:u:200:s2" in str(rows) and "ac:wa:u:200" in str(rows)
    assert "ac:wl:u:200" in str(access_menu.features_screen("u", "200")[1])
    base = {"inline_keyboard": [[{"text": "One", "callback_data": "site:s1"}], [{"text": "Two", "callback_data": "site:s2"}]]}
    shown = str(home_menu.decorate(base, 200, "200"))
    assert "site:s2" in shown and "site:s1" not in shown
    rt, _ = _runtime()
    assert bot._site_ok(_cb("site:s2", 200)) and not bot._site_ok(_cb("site:s1", 200))
    assert not bot._site_ok(_cb("c:s1:xyz", 200)) and bot._site_ok(_cb("c:s1:xyz", 100))
    assert bot._handle_access_update(rt, _cb("site:s1", 200), {"username": "b"}) is True


def test_test_bot_can_block_publishing_while_live_is_unaffected(monkeypatch):
    import start_v5_bot as bot
    rt, c = _runtime()
    access.add_user(200, 100, "Sam"); access.set_feature("user", 200, "publish", True)
    assert bot._handle_access_update(rt, _cb("approve:evt-x", 200), {"username": "b"}) is False        # live: normal flow
    monkeypatch.setenv("NEWSAGENT_TEST_BLOCK_PUBLISH", "true")
    assert bot._handle_access_update(rt, _cb("approve:evt-x", 200), {"username": "b"}) is True         # test bot: refused
    assert "blocked on the test bot" in c.answers[-1]["text"]
    assert bot._handle_access_update(rt, _cb("revise:evt-x", 200), {"username": "b"}) is False         # other actions still work
