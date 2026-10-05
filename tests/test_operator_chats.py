"""Several Telegram chats can operate the bot and receive the same cards."""

from newsagent_v2.telegram.config import CHAT_ENV, TOKEN_ENV, load_telegram_config
from newsagent_v2.telegram.operators import broadcast_message


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


def test_broadcast_reaches_every_operator_and_returns_the_primary():
    config = load_telegram_config({TOKEN_ENV: "123456:test-token", CHAT_ENV: "111,222"})
    client = _Client()
    result = broadcast_message(client, config, text="hello", parse_mode="HTML")
    assert client.sent == ["111", "222"]
    assert result["message_id"] == 1
