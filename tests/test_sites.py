"""/start lists websites and can add another."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from newsagent_v2.control import sites as sites_mod
from newsagent_v2.control.sites import (
    ADD_CALLBACK,
    FINISH_PREFIX,
    Site,
    begin_add,
    begin_logo,
    is_ready,
    list_sites,
    logo_step,
    menu_keyboard,
    normalize_base_url,
    onboarding_hides_text,
    probe_site,
    save_site,
    take_reply,
)
from newsagent_v2.wordpress.config import BASE_ENV, PASSWORD_ENV, USER_ENV


@pytest.fixture(autouse=True)
def _isolated_wizard(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sites_mod, "WIZARD_PATH", tmp_path / "wizard.json")
    monkeypatch.setattr(sites_mod, "_pending", {})


def test_start_lists_the_saved_site_and_an_add_button(tmp_path: Path):
    path = tmp_path / "sites.json"
    environ = {
        BASE_ENV: "https://coinnetwork.info",
        USER_ENV: "editor",
        PASSWORD_ENV: "app-secret",
    }
    sites = list_sites(path, environ)
    assert [site.name for site in sites] == ["coinnetwork.info"]
    buttons = [button["text"] for row in menu_keyboard(sites, active=sites[0].id)["inline_keyboard"] for button in row]
    assert buttons == ["✓ coinnetwork.info", "ADD WEBSITE"]
    assert menu_keyboard(sites)["inline_keyboard"][-1][0]["callback_data"] == ADD_CALLBACK
    again = list_sites(path, {})
    assert again[0].base_url == "https://coinnetwork.info"


def test_adding_a_site_checks_wordpress_and_keeps_the_password_out_of_the_prompt(tmp_path: Path):
    path = tmp_path / "sites.json"
    chat = "100"
    assert "example.com" in begin_add(chat)
    user_prompt = take_reply(chat, "example.com")["text"]
    assert "username" in user_prompt.lower()
    ready = take_reply(chat, "abcd efgh ijkl mnop")
    assert ready["ready"] is True
    assert ready["username"] == ""
    assert ready["app_password"] == "abcdefghijklmnop"
    assert "abcdefghijklmnop" not in ready.get("text", "")

    def transport(method, url, **kwargs):
        assert method == "GET"
        if url.endswith("/wp-json/"):
            return {"ok": True, "payload": {"name": "Example News"}}
        assert kwargs["auth"] == ("editor", "abcdefghijklmnop")
        return {"ok": True, "payload": {"id": 1}}

    site, message = probe_site(ready["base_url"], "editor", ready["app_password"], transport)
    assert message == "Added Example News."
    save_site(site, path)
    assert list_sites(path)[0].name == "Example News"
    replaced, _ = probe_site("https://example.com/", "editor", "abcdefghijklmnop", transport)
    save_site(replaced, path)
    assert len(list_sites(path)) == 1


def test_adding_a_site_accepts_editable_username(tmp_path: Path):
    chat = "150"
    begin_add(chat)
    user_prompt = take_reply(chat, "https://coinography.com/wp-admin/")["text"]
    assert "username" in user_prompt.lower()
    assert "email" in user_prompt.lower()
    pass_prompt = take_reply(chat, "custom_editor")["text"]
    assert "custom_editor" in pass_prompt
    assert "application password" in pass_prompt.lower()
    ready = take_reply(chat, "abcd efgh ijkl mnop qrst uvwx")
    assert ready["ready"] is True
    assert ready["username"] == "custom_editor"
    assert ready["app_password"] == "abcdefghijklmnopqrstuvwx"


def test_adding_a_site_accepts_an_account_email():
    chat = "151"
    begin_add(chat)
    take_reply(chat, "https://news.example.com")
    echoed = take_reply(chat, "editor@example.com")["text"]
    assert echoed.startswith("Email: editor@example.com")
    ready = take_reply(chat, "abcd efgh ijkl mnop qrst uvwx")
    assert ready["username"] == "editor@example.com"


def test_email_login_falls_back_to_the_wordpress_username():
    from newsagent_v2.control.sites import open_site

    def transport(method, url, **kwargs):
        if url.endswith("/wp-json/"):
            return {"ok": True, "payload": {"name": "Example News"}}
        if "per_page=100" in url:
            return {"ok": True, "payload": [{"slug": "editor", "name": "Editor"}]}
        login = kwargs["auth"][0]
        if login == "editor":
            return {"ok": True, "payload": {"id": 4}}
        return {"ok": False, "payload": {"code": "rest_not_logged_in"}}

    site, message = open_site(
        "https://news.example.com",
        "editor@example.com",
        "app-secret",
        transport,
    )
    assert message == "Added Example News."
    assert site is not None
    assert site.username == "editor"


def test_email_finds_the_account_when_the_mailbox_name_is_not_the_login():
    from newsagent_v2.control.sites import open_site

    tried: list[str] = []

    def transport(method, url, **kwargs):
        if url.endswith("/wp-json/"):
            return {"ok": True, "payload": {"name": "Example News"}}
        if "per_page=100" in url:
            return {"ok": True, "payload": [{"slug": "newsdesk", "name": "desk@example.com"}]}
        tried.append(kwargs["auth"][0])
        if kwargs["auth"][0] == "desk@example.com":
            return {"ok": True, "payload": {"id": 9}}
        return {"ok": False, "payload": {"code": "rest_not_logged_in"}}

    site, message = open_site("https://news.example.com", "someone@example.com", "app-secret", transport)
    assert message == "Added Example News."
    assert site is not None and site.username == "desk@example.com"
    assert tried[0] == "someone@example.com"
    assert "someone" in tried
    assert "desk@example.com" in tried


def test_two_factor_rejection_asks_for_an_application_password():
    def transport(method, url, **kwargs):
        if url.endswith("/wp-json/"):
            return {"ok": True, "payload": {"name": "Example News"}}
        return {
            "ok": False,
            "payload": {"code": "wfls_twofactor_required", "message": "CODE REQUIRED: enter your 2FA code"},
        }

    refused, reason = probe_site("https://example.com", "editor", "account-password", transport)
    assert refused is None
    assert "2FA" in reason
    assert "Application Passwords" in reason


def test_a_bad_address_or_password_is_refused():
    assert normalize_base_url("HTTPS://News.Example.com/blog/") == "https://News.Example.com/blog"
    assert normalize_base_url("https://coinography.com/wp-admin/") == "https://coinography.com"
    refused, reason = probe_site("https://example.com", "editor", "secret-password", lambda *args, **kwargs: {"ok": False})
    assert refused is None
    assert "WordPress" in reason


def test_telegram_add_uses_the_password_not_its_name(tmp_path: Path):
    from newsagent_v2.control.sites import match_site, save_site

    chat = "300"
    begin_add(chat)
    assert "username" in take_reply(chat, "https://coinography.com/wp-admin/")["text"].lower()
    ready = take_reply(chat, "Newsagent\nabcd efgh ijkl mnop qrst uvwx")
    assert ready["username"] == ""
    assert ready["app_password"] == "abcdefghijklmnopqrstuvwx"
    assert ready["base_url"] == "https://coinography.com"
    tried: list[str] = []

    def transport(method, url, **kwargs):
        if url.endswith("/wp-json/"):
            return {"ok": True, "payload": {"name": "Coinography"}}
        if "per_page=100" in url:
            return {"ok": True, "payload": [
                {"slug": "ahmed-falah", "name": "Ahmed Falah"},
                {"slug": "dmicoinography-com", "name": "DMI@Coinography.com"},
            ]}
        tried.append(kwargs["auth"][0])
        if kwargs["auth"][0] == "DMI@Coinography.com":
            return {"ok": True, "payload": {"id": 19}}
        return {"ok": False, "payload": {"code": "rest_not_logged_in"}}

    site, message = match_site(ready["base_url"], ready["app_password"], transport)
    assert tried == ["DMI@Coinography.com"]
    assert message == "Added Coinography."
    path = tmp_path / "sites.json"
    path.write_text('{"active":"keep","sites":[{"id":"aaaaaaaa","name":"coinnetwork.info","base_url":"https://coinnetwork.info","username":"editor","app_password":"app-secret"}]}', encoding="utf-8")
    sites = save_site(site, path)
    labels = [button["text"] for row in menu_keyboard(sites, active="keep")["inline_keyboard"] for button in row]
    assert labels == ["coinnetwork.info", "FINISH SETUP Coinography", "ADD WEBSITE"]
    finish = menu_keyboard(sites)["inline_keyboard"][1][0]["callback_data"]
    assert finish == f"{FINISH_PREFIX}{site.id}"


def test_start_cancels_adding():
    chat = "200"
    begin_add(chat)
    cancelled = take_reply(chat, "/start")
    assert cancelled["cancel"] is True
    assert take_reply(chat, "https://example.com") is None


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), (20, 40, 180)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_wizard_asks_for_address_password_and_logo_before_listing(tmp_path: Path):
    from newsagent_v2.control.sites import attach_logo, confirm_text

    chat = "400"
    assert "website address" in begin_add(chat)
    user_step = take_reply(chat, "https://news.example.com/wp-admin/")
    assert "username" in user_step["text"].lower()
    ready = take_reply(chat, "abcd efgh ijkl mnop qrst uvwx")
    assert ready["ready"] is True
    assert ready["base_url"] == "https://news.example.com"
    assert ready["app_password"] == "abcdefghijklmnopqrstuvwx"
    site = Site(
        id="bbbbbbbb",
        name="Example News",
        base_url=ready["base_url"],
        username="editor@example.com",
        app_password=ready["app_password"],
    )
    assert is_ready(site) is False
    prompt = begin_logo(chat, site)
    assert "logo" in prompt.lower()
    assert logo_step(chat)
    assert sites_mod._load_wizard()[chat]["step"] == "logo"
    sites_mod._pending.clear()
    sites_mod._pending.update(sites_mod._load_wizard())
    assert logo_step(chat)
    assert take_reply(chat, "not a photo").get("awaiting_logo") is True

    path = tmp_path / "sites.json"
    saved, reason = attach_logo(chat, _png(), path=path, directory=tmp_path / "logos")
    assert reason == ""
    assert saved is not None
    assert Path(saved.logo_path).is_file()
    assert is_ready(saved)
    buttons = [button["text"] for row in menu_keyboard([saved])["inline_keyboard"] for button in row]
    assert buttons == ["Example News", "ADD WEBSITE"]
    text = confirm_text(saved, 52, 8)
    assert "Example News" in text
    assert "editor@example.com" in text
    assert "Categories: 52" in text
    assert "Authors: 8" in text
    assert "Logo saved." in text
    assert ready["app_password"] not in text


def test_missing_upload_right_blocks_the_logo_and_rank_math_is_only_a_warning():
    from newsagent_v2.control.sites import confirm_text, publishing_rights, rights_message

    def without_upload(method, url, **kwargs):
        if url.endswith("/wp-json/"):
            return {"ok": True, "payload": {"namespaces": ["rankmath/v1", "wp/v2"]}}
        return {"ok": True, "payload": {"capabilities": {"edit_posts": True, "upload_files": False}}}

    missing, warning = publishing_rights("https://news.example.com", "editor", "app-secret", without_upload)
    assert missing == ["upload_files"]
    assert warning == ""
    assert "upload files" in rights_message(missing)

    def without_rank_math(method, url, **kwargs):
        if url.endswith("/wp-json/"):
            return {"ok": True, "payload": {"namespaces": ["wp/v2"]}}
        return {"ok": True, "payload": {"capabilities": {"edit_posts": True, "upload_files": True}}}

    missing, warning = publishing_rights("https://news.example.com", "editor", "app-secret", without_rank_math)
    assert missing == []
    assert "Rank Math" in warning
    site = Site("bbbbbbbb", "Example News", "https://news.example.com", "editor", "app-secret", "logo.png")
    text = confirm_text(site, 2, 1, 10, warning)
    assert "Logo saved." in text
    assert "Rank Math" in text


def test_replace_logo_keeps_the_account_and_remove_drops_the_site(tmp_path, monkeypatch):
    from newsagent_v2.control import site_flow
    from newsagent_v2.control.sites import attach_logo, begin_logo, remove_site

    monkeypatch.setattr(sites_mod, "LOGO_DIR", tmp_path / "logos")
    monkeypatch.setattr(site_flow, "FLOW_PATH", tmp_path / "flow.json")
    path = tmp_path / "sites.json"
    site = Site(
        id="bbbbbbbb",
        name="Example News",
        base_url="https://news.example.com",
        username="editor@example.com",
        app_password="app-secret",
    )
    save_site(site, path)
    begin_logo("9", site, replacing=True)
    saved, reason = attach_logo("9", _png(), path=path, directory=tmp_path / "logos")
    assert reason == ""
    assert saved is not None
    assert saved.username == "editor@example.com"
    assert saved.app_password == "app-secret"
    assert Path(saved.logo_path).is_file()
    removed = remove_site(saved.id, path)
    assert removed is not None and removed.name == "Example News"
    labels = [button["text"] for row in menu_keyboard(list_sites(path))["inline_keyboard"] for button in row]
    assert labels == ["ADD WEBSITE"]


def test_replace_username_in_settings(tmp_path):
    from newsagent_v2.control.sites import begin_replace_username

    path = tmp_path / "sites.json"
    site = Site(
        id="dddddddd",
        name="Example News",
        base_url="https://news.example.com",
        username="old_user",
        app_password="app-secret",
    )
    save_site(site, path)
    prompt = begin_replace_username("12", site)
    assert "old_user" in prompt
    assert "email" in prompt.lower()
    res = take_reply("12", "new_user")
    assert res["ready"] is True
    assert res["username"] == "new_user"
    assert res["app_password"] == "app-secret"


def test_article_image_uses_the_site_logo(tmp_path: Path, monkeypatch):
    from newsagent_v2.image.logo_derive import ensure_compositor_logo
    from newsagent_v2.image.vertex_make_image import logo_rejection, resolve_article_logo

    logo = tmp_path / "logos" / "site.png"
    logo.parent.mkdir()
    logo.write_bytes(_png())
    store = tmp_path / "sites.json"
    monkeypatch.setattr(sites_mod, "DEFAULT_PATH", store)
    site = Site(
        id="cccccccc",
        name="Example News",
        base_url="https://news.example.com",
        username="editor",
        app_password="app-secret",
        logo_path=str(logo),
    )
    save_site(site, store)
    resolved = resolve_article_logo({"NEWSAGENT_ACTIVE_SITE_ID": site.id})
    assert resolved == logo
    assert resolved.name != "coinnetwork_logo.png"
    assert logo_rejection(resolved) is None
    placed, _meta = ensure_compositor_logo(logo)
    assert placed.is_file()
