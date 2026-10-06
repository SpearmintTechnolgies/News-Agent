"""Group chat replies only when the bot is tagged. Other text goes to Hermes."""

from start_v5_bot import is_group_chat, mentions_bot, strip_bot_mention


def test_a_private_chat_is_not_a_group():
    update = {"message": {"chat": {"id": 6877637692, "type": "private"}, "text": "hey"}}
    assert is_group_chat(update, "6877637692") is False


def test_a_supergroup_is_a_group_even_without_the_type():
    assert is_group_chat({}, "-1004400759917") is True
    update = {"message": {"chat": {"id": -1004400759917, "type": "supergroup"}, "text": "hey"}}
    assert is_group_chat(update, "-1004400759917") is True


def test_a_group_message_counts_only_when_this_bot_is_tagged():
    update = {"message": {"text": "hey @NewsagentzohaBot what is this"}}
    assert mentions_bot(update, "NewsagentzohaBot") is True
    assert mentions_bot({"message": {"text": "hey everyone"}}, "NewsagentzohaBot") is False
    assert mentions_bot({"message": {"text": "@someone else"}}, "NewsagentzohaBot") is False


def test_the_tag_is_removed_before_hermes_sees_the_question():
    assert strip_bot_mention("@NewsagentzohaBot how do I publish", "NewsagentzohaBot") == "how do I publish"
    assert strip_bot_mention("hey", "NewsagentzohaBot") == "hey"
