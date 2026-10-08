"""Several Telegram chats can operate the bot and receive the same cards."""

from newsagent_v2.telegram.config import CHAT_ENV, TOKEN_ENV, load_telegram_config
from newsagent_v2.telegram.operators import broadcast_message, operator_ids, set_origin_chat


def test_comma_separated_chat_ids_are_all_allowed():
    config = load_telegram_config({
        TOKEN_ENV: "123456:test-token",
        CHAT_ENV: "6877637692, 8604260702",
    })
    assert config.test_chat_id == "6877637692"
    assert config.chat_ids == ("6877637692", "8604260702")
    assert config.allows("8604260702")
    assert config.allows("6877637692")
    assert config.allows("999") is False


class _Client:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs["chat_id"])
        return {"ok": True, "message_id": len(self.sent)}


def test_without_origin_only_the_primary_chat_gets_the_message():
    config = load_telegram_config({TOKEN_ENV: "123456:test-token", CHAT_ENV: "111,222"})
    set_origin_chat("")
    client = _Client()
    result = broadcast_message(client, config, text="hello", parse_mode="HTML")
    assert client.sent == ["111"]
    assert result["message_id"] == 1


def test_replies_go_to_the_chat_that_started_the_action():
    config = load_telegram_config({TOKEN_ENV: "123456:test-token", CHAT_ENV: "6877637692,-1004400759917"})
    assert config.allows("-1004400759917") and not config.allows("5480651602")
    token = set_origin_chat("-1004400759917")
    try:
        client = _Client()
        broadcast_message(client, config, text="card")
        assert client.sent == ["-1004400759917"]
        assert operator_ids(config) == ("-1004400759917",)
        set_origin_chat("5480651602")  # not allowed: never routed there
        assert operator_ids(config) == ("6877637692",)
    finally:
        set_origin_chat("")
