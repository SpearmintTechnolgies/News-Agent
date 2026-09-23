"""Test exact router behavior - NO cost.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    print("=" * 70)
    print("ROUTER BEHAVIOR TEST")
    print("=" * 70)

    # 1. Simulate handle_revise return
    print("\n1. handle_revise SUCCESS return:")
    result = {
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
        "send_revision_package": True,
        "compact_message": "✍️ Article v2 -- REVISED\n\ncftc...",
    }
    print(f"   Keys: {list(result.keys())}")
    print(f"   Has 'message': {'message' in result}")

    # 2. Simulate router
    print("\n2. Router evaluation:")
    if result.get("ok"):
        action = result.get("action", "")
        print(f"   action = '{action}'")

        # Check each condition
        print(f"\n   Checking conditions in ORDER:")
        print(f"   1. action == 'revise_complete': {action == 'revise_complete'}")
        print(f"   2. result.get('send_revision_package'): {result.get('send_revision_package')}")

        cond1 = (action == "revise_complete") and result.get("send_revision_package")
        print(f"   3. CONDITION 1 MATCHES: {cond1}")

        if cond1:
            print("   >>> Router calls: _send_revision_result()")
            print("   >>> Which calls: send_compact_revision_summary()")
        else:
            # Fall through to other conditions
            has_compact = result.get("compact_message")
            if has_compact:
                print(f"   >>> Router WOULD call: send_message(compact_message)")
            elif result.get("message"):
                print(f"   >>> Router WOULD call: send_message(message) - OLD PATH")

    # 3. Check if OLD message format exists in code
    print("\n3. Checking for old message format in code:")
    controller_path = REPO / "src/newsagent_v2/v5_generation/revision_controller.py"
    with open(controller_path, "r") as f:
        content = f.read()
        if "Revision complete:" in content and "✅" not in content:
            print(f"   [CORRECT] 'Revision complete:' found WITHOUT checkmark")
            print(f"   This means formatting is controller's internal logic")

    print("\n" + "=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    print("""
The NEW code SHOULD:
1. handle_revise() returns action='revise_complete', send_revision_package=True
2. Router calls _send_revision_result() which calls send_compact_revision_summary()
3. send_compact_revision_summary() sends NEW format with VIEW FULL button

If OLD format is appearing:
- Code was not reloaded after restart
- Cached Python bytecode is being used
- Multiple processes running with different code versions
""")


if __name__ == "__main__":
    main()
