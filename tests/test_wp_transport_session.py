"""Mocked tests for live WordPress transport — ZERO live WordPress requests."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from newsagent_v2.wordpress.adapter import (
    NEWSAGENT_WP_USER_AGENT,
    WordPressPublishError,
    build_live_wordpress_transport,
    is_siteground_captcha_response,
    publish_frozen_story,
)
from newsagent_v2.wordpress.config import WordPressConfig


def _fake_response(
    *,
    status_code: int,
    text: str = "",
    json_data: Any = None,
    headers: dict[str, str] | None = None,
    json_raises: bool = False,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    if json_raises:

        def _boom() -> Any:
            raise ValueError("No JSON")

        resp.json = _boom
    else:
        resp.json = MagicMock(return_value=json_data)
    return resp


class TestSiteGroundCaptchaDetection(unittest.TestCase):
    def test_header_marker(self) -> None:
        self.assertTrue(
            is_siteground_captcha_response(
                status_code=202,
                headers={"sg-captcha": "challenge"},
                body_text="",
            )
        )

    def test_body_well_known_path(self) -> None:
        html = '<html><a href="/.well-known/sgcaptcha/">challenge</a></html>'
        self.assertTrue(
            is_siteground_captcha_response(
                status_code=202,
                headers={"Content-Type": "text/html"},
                body_text=html,
            )
        )

    def test_normal_json_200_not_captcha(self) -> None:
        self.assertFalse(
            is_siteground_captcha_response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body_text='{"id":1}',
            )
        )


class TestLiveWordPressTransport(unittest.TestCase):
    def test_uses_persistent_session_and_newsagent_user_agent(self) -> None:
        with patch("requests.Session") as session_cls:
            session = MagicMock()
            session.headers = {}
            session_cls.return_value = session
            session.request.return_value = _fake_response(
                status_code=201,
                json_data={"id": 42, "link": "https://example.com/?p=42"},
            )

            transport = build_live_wordpress_transport(timeout_seconds=5)
            result = transport(
                "POST",
                "https://example.com/wp-json/wp/v2/posts",
                json={"title": "t", "status": "draft"},
                auth=("user", "pass"),
            )

            self.assertEqual(session.headers.get("User-Agent"), NEWSAGENT_WP_USER_AGENT)
            session.request.assert_called_once()
            kwargs = session.request.call_args.kwargs
            self.assertEqual(kwargs["auth"], ("user", "pass"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["payload"]["id"], 42)

            # Same session reused on second call
            session.request.return_value = _fake_response(
                status_code=200,
                json_data=[{"id": 1}],
            )
            transport("GET", "https://example.com/wp-json/wp/v2/categories", auth=("user", "pass"))
            self.assertEqual(session.request.call_count, 2)
            session_cls.assert_called_once()

    def test_sg_captcha_202_html_is_not_success(self) -> None:
        with patch("requests.Session") as session_cls:
            session = MagicMock()
            session.headers = {}
            session_cls.return_value = session
            session.request.return_value = _fake_response(
                status_code=202,
                text='<!DOCTYPE html><html>sg-captcha challenge at /.well-known/sgcaptcha/</html>',
                headers={"Content-Type": "text/html", "sg-captcha": "challenge"},
                json_raises=True,
            )

            transport = build_live_wordpress_transport()
            result = transport(
                "POST",
                "https://example.com/wp-json/wp/v2/posts",
                json={"title": "t"},
                auth=("user", "pass"),
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result.get("error_code"), "sg_captcha")
            self.assertIsNone(result.get("payload"))

    def test_non_json_2xx_is_not_success(self) -> None:
        with patch("requests.Session") as session_cls:
            session = MagicMock()
            session.headers = {}
            session_cls.return_value = session
            session.request.return_value = _fake_response(
                status_code=200,
                text="<html>oops</html>",
                headers={"Content-Type": "text/html"},
                json_raises=True,
            )

            transport = build_live_wordpress_transport()
            result = transport("GET", "https://example.com/wp-json/wp/v2/users/me", auth=("u", "p"))
            self.assertFalse(result["ok"])
            self.assertEqual(result.get("error_code"), "non_json")


class TestPublishFrozenStoryPostId(unittest.TestCase):
    def setUp(self) -> None:
        self.config = WordPressConfig(
            base_url="https://example.com",
            username="editor",
            app_password="xxxx xxxx xxxx xxxx xxxx xxxx",
        )

    def test_success_requires_valid_post_id(self) -> None:
        def transport(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "payload": {"id": 14327, "link": "https://example.com/?p=14327"},
            }

        out = publish_frozen_story(
            config=self.config,
            article={"headline": "H", "article_body": "Body", "slug": "h"},
            image_path=None,
            transport=transport,
            status="draft",
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["post_id"], 14327)

    def test_missing_post_id_raises(self) -> None:
        def transport(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "payload": {"link": "https://example.com/?p=1"},
            }

        with self.assertRaises(WordPressPublishError) as ctx:
            publish_frozen_story(
                config=self.config,
                article={"headline": "H", "article_body": "Body"},
                image_path=None,
                transport=transport,
            )
        self.assertEqual(ctx.exception.code, "missing_post_id")

    def test_sg_captcha_transport_failure_surfaces_as_post_failed(self) -> None:
        def transport(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "error": "SiteGround sg-captcha challenge (not a WordPress REST response)",
                "error_code": "sg_captcha",
                "payload": None,
            }

        with self.assertRaises(WordPressPublishError) as ctx:
            publish_frozen_story(
                config=self.config,
                article={"headline": "H", "article_body": "Body"},
                image_path=None,
                transport=transport,
            )
        self.assertEqual(ctx.exception.code, "post_failed")


if __name__ == "__main__":
    unittest.main()
