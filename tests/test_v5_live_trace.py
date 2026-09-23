"""Live trace test - simulates exact runtime flow.

ANSWERS:
1. EXACT object/dict handle_revise() returns
2. EXACT keys/action values
3. Condition in _try_review_callback()
4. Does condition match?
5. Where is OLD message sent?
6. Why old path wins?
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def simulate_router(result):
    """Simulate exact router logic from _try_review_callback."""
    # From v5_canonical_runtime.py:270-312

    print(f"\nRouter processing result with keys: {list(result.keys())}")
    print(f"  ok={result.get('ok')}")
    print(f"  action={result.get('action')}")
    print(f"  send_revision_package={result.get('send_revision_package')}")

    if result.get("ok"):
        action = result.get("action", "")

        # Handle revision completion - send compact revision package
        if action == "revise_complete" and result.get("send_revision_package"):
            print("  [BRANCH] -> _send_revision_result()")
            return "SEND_REVISION_RESULT"

        # Handle view full article - send complete article (read-only)
        elif action == "view_full_article":
            print("  [BRANCH] -> _send_view_full_article()")
            return "VIEW_FULL_ARTICLE"

        # Handle view full image - send image (read-only)
        elif action == "view_full_image":
            print("  [BRANCH] -> _send_view_full_image()")
            return "VIEW_FULL_IMAGE"

        # Standard message + reply_markup
        elif result.get("reply_markup"):
            print("  [BRANCH] -> send_message with reply_markup")
            return "SEND_MESSAGE_WITH_MARKUP"

        elif result.get("compact_message"):
            print("  [BRANCH] -> send_message(compact_message)")
            return "SEND_COMPACT_MESSAGE"

        elif result.get("message"):
            print("  [BRANCH] -> send_message(message) [OLD PATH]")
            return "SEND_MESSAGE_FALLBACK_OLD"

    else:
        print(f"  [BRANCH] -> send_message with error: {result.get('message') or result.get('reason')}")
        return "ERROR"

    print("  [BRANCH] -> NO ACTION TAKEN")
    return "NO_ACTION"


def simulate_handle_revise_success():
    """Simulate what handle_revise() returns on success."""

    # Actual return from v5_review_callbacks.py:317-337
    return {
        "ok": True,
        "action": "revise_complete",
        "article_revised": True,
        "image_revised": False,
        "article_version": "v2",
        "image_version": "v1",
        "article_hash": "abc123",
        "image_hash": "def456",
        "article_calls": 1,
        "image_calls": 0,
        "event_id": "evt-fec17bd1",
        "canonical_title": "CFTC Files Crypto Rules",
        # Signal to runtime that full revision package should be sent
        "send_revision_package": True,
        "compact_message": "✍️ Article v2 -- REVISED\n\ncftc...",
    }


def check_old_message_sources():
    """Search for sources of old 'Revision complete:' message."""

    results = []

    # 1. Check revision_controller.py
    controller_path = REPO / "src/newsagent_v2/v5_generation/revision_controller.py"
    with open(controller_path, "r") as f:
        content = f.read()
        if "Revision complete:" in content:
            results.append(f"Found 'Revision complete:' in {controller_path}")
        if "✅ Revision" in content:
            results.append(f"Found checkmark in {controller_path}")

    # 2. Check v5_review_callbacks.py
    callbacks_path = REPO / "src/newsagent_v2/telegram/v5_review_callbacks.py"
    with open(callbacks_path, "r") as f:
        content = f.read()
        if "Revision complete:" in content:
            results.append(f"Found 'Revision complete:' in {callbacks_path}")

    # 3. Check runtime
    runtime_path = REPO / "src/newsagent_v2/telegram/v5_canonical_runtime.py"
    with open(runtime_path, "r") as f:
        content = f.read()
        if "Revision complete:" in content:
            results.append(f"Found 'Revision complete:' in {runtime_path}")
        if "✅ Revision" in content:
            results.append(f"Found checkmark in {runtime_path}")

    return results


def check_send_compact_called():
    """Verify send_compact_revision_summary is imported and can be called."""

    # Check if function exists and has right signature
    from newsagent_v2.v5_generation.telegram_delivery import send_compact_revision_summary
    import inspect

    sig = inspect.signature(send_compact_revision_summary)
    return str(sig)


def main():
    print("=" * 70)
    print("V5 ROUTING TRACE REPORT")
    print("=" * 70)

    # Question 1 & 2: EXACT object/dict and keys
    print("\n" + "=" * 70)
    print("ANSWER 1 & 2: handle_revise() RETURN OBJECT")
    print("=" * 70)

    result = simulate_handle_revise_success()
    print(f"\nExact return dict keys: {list(result.keys())}")
    print(f"\nExact action value: '{result['action']}'")
    print(f"Exact send_revision_package value: {result['send_revision_package']}")
    print(f"Has 'message' key: {'message' in result}")
    print(f"Has 'compact_message' key: {'compact_message' in result}")

    # Question 3 & 4: Condition in router and matching
    print("\n" + "=" * 70)
    print("ANSWER 3 & 4: ROUTER CONDITION AND MATCHING")
    print("=" * 70)

    print("\nRouter condition: if action == 'revise_complete' and result.get('send_revision_package'):")
    print("  - action == 'revise_complete':", result.get('action') == 'revise_complete')
    print("  - result.get('send_revision_package'):", result.get('send_revision_package'))
    print("  - BOTH TRUE:", result.get('action') == 'revise_complete' and bool(result.get('send_revision_package')))

    branch = simulate_router(result)
    print(f"\n>>> Router takes branch: {branch}")

    # Question 5: Old message source
    print("\n" + "=" * 70)
    print("ANSWER 5: OLD 'Revision complete:' MESSAGE SOURCE")
    print("=" * 70)

    old_sources = check_old_message_sources()
    for src in old_sources:
        print(f"  -> {src}")

    # Question 6: Why old path wins
    print("\n" + "=" * 70)
    print("ANSWER 6: WHY OLD PATH WINS")
    print("=" * 70)

    print("""
ANALYSIS:
=========
1. handle_revise() returns a dict WITHOUT 'message' key on success.
2. The router checks for 'revise_complete' + 'send_revision_package' FIRST.
3. If those match, it should call _send_revision_result() which calls
   send_compact_revision_summary().

POSSIBLE CAUSES OF OLD MESSAGE:
- Old code loaded at runtime
- Different code path: maybe generation_worker or another module
  is sending the old message?
- Callback NOT routing through v5_review_callbacks -> handle_revise
  but through a different handler?

THE OLD MESSAGE FORMAT:
  ✅ Revision complete:
  Article: v2 (NEW)
  Image: None (reused)

This matches build_simple_message in generation_worker.py which
was the old pattern.
""")

    # Verify send_compact exists
    print("\n" + "=" * 70)
    print("VERIFY send_compact_revision_summary EXISTS")
    print("=" * 70)
    try:
        sig = check_send_compact_called()
        print(f"  Function signature: {sig}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n" + "=" * 70)
    print("END OF REPORT")
    print("=" * 70)


if __name__ == "__main__":
    main()
