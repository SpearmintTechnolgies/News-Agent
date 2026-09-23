from unittest.mock import MagicMock

from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler
from newsagent_v2.telegram.v5_canonical_runtime import CanonicalV5Integration


def _runtime_for_callback_test() -> CanonicalV5Integration:
    runtime = CanonicalV5Integration.__new__(CanonicalV5Integration)
    runtime.callback_handler = V5CallbackHandler()
    runtime.callback_handler.handle_run_story = MagicMock(
        return_value={"ok": True, "action": "run_story", "event_id": "evt-0449fa13"}
    )
    runtime.review_callback_handler = MagicMock()
    runtime.review_callback_handler.parse_callback.return_value = None
    runtime.client = MagicMock()
    runtime.config = MagicMock(test_chat_id="-123")
    return runtime


def test_canonical_run_story_callback_contract() -> None:
    runtime = _runtime_for_callback_test()
    callback = "run:evt-0449fa13"

    parsed = runtime.callback_handler.parse_callback(callback)
    assert parsed == {"action": "run", "event_id": "evt-0449fa13"}

    result = runtime.handle_callback(callback)

    assert result["event_id"] == "evt-0449fa13"
    runtime.callback_handler.handle_run_story.assert_called_once_with("evt-0449fa13")
    runtime.review_callback_handler.handle.assert_not_called()

    malformed = "run:evt-0449fa13:unexpected:extra"
    rejected = runtime.handle_callback(malformed)

    assert rejected["ok"] is False
    assert rejected["reason"] == "invalid_callback"
    runtime.callback_handler.handle_run_story.assert_called_once_with("evt-0449fa13")