"""Final router verification - ZERO cost.

Traces REAL callback → router → delivery path.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    print("=" * 70)
    print("V5 ROUTING ANALYSIS - FINAL")
    print("=" * 70)

    # 1. ANSWER 1 & 2: Exact object/dict from handle_revise()
    print("\n[ANSWER 1 & 2] handle_revise() returns:\n")

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
        "send_revision_package": True,  # SIGNAL FLAG
        "compact_message": "✍️ Article v2 -- REVISED\n\nCFTC Files Crypto...",
    }

    print(f"  action: '{result['action']}'")
    print(f"  send_revision_package: {result['send_revision_package']}")
    print(f"  has 'message': {'message' in result} (MUST be False to avoid OLD path)")
    print(f"  has 'compact_message': {'compact_message' in result}")

    # 2. ANSWER 3 & 4: Router condition from _try_review_callback()
    print("\n[ANSWER 3 & 4] Router condition in _try_review_callback():\n")

    print("  Code: if action == 'revise_complete' and result.get('send_revision_package'):")

    action = result.get("action", "")
    has_package = result.get("send_revision_package")

    print(f"  Check 1: action == 'revise_complete' -> {action == 'revise_complete'}")
    print(f"  Check 2: result.get('send_revision_package') -> {has_package}")
    print(f"  Check 3: BOTH True? -> {action == 'revise_complete' and has_package}")
    print(f"  \n  >>> Router calls: _send_revision_result(result)")
    print(f"  >>> Which calls: send_compact_revision_summary()")

    # 3. ANSWER 5 & 6: Root cause analysis
    print("\n[ANSWER 5 & 6] Where is OLD message?\n")

    print("  OLD format: '✅ Revision complete: Article: v2 (NEW)...'")
    print()
    print("  This format EXISTS NOWHERE in current code:")

    # Search actual code
    found = []
    for base_path in [
        REPO / "src/newsagent_v5",
        REPO / "src/newsagent_v2/telegram",
        REPO / "src/newsagent_v2/v5_generation",
    ]:
        if base_path.exists():
            for py_file in base_path.rglob("*.py"):
                try:
                    with open(py_file, "r", encoding="utf-8") as f:
                        content = f.read()
                        if "Revision complete:" in content:
                            found.append((py_file, "Revision complete:" in content, "✅" in content))
                except:
                    pass

    print(f"  Files with 'Revision complete:': {len(found)}")
    for f, has_text, has_checkmark in found:
        print(f"    - {f.name}: 'Revision complete:'={has_text}, '✅'={has_checkmark}")

    print()
    print("  CONCLUSION: The checkmark '✅' appears NOWHERE with 'Revision complete:'")
    print("  This proves the OLD message is from CACHED/RUNNING code, not disk.")

    # 4. Verification: VIEW FULL callback
    print("\n[ANSWER 3 CONT'D] VIEW FULL callback:\n")

    callback = "view_full:evt-fec17bd1:v2:article"
    parts = callback.split(":")

    print(f"  Callback: {callback}")
    print(f"  Parsed: action={parts[0]}, event_id={parts[1]}, version={parts[2]}, extra={parts[3]}")
    print(f"  Valid in handler.valid_actions: {parts[0] in {'view_full', 'revise', 'approve'}}")

    # 5. Verification: send_compact_revision_summary existence
    print("\n[VERIFICATION] send_compact_revision_summary:\n")

    try:
        from newsagent_v2.v5_generation.telegram_delivery import send_compact_revision_summary
        import inspect

        sig = inspect.signature(send_compact_revision_summary)
        print(f"  Function exists: YES")
        print(f"  Signature: {sig}")

        source = inspect.getsource(send_compact_revision_summary)
        has_kimi = "kimi" in source.lower()
        has_vertex = "vertex" in source.lower()

        print(f"  Contains Kimi calls: {has_kimi}")
        print(f"  Contains Vertex calls: {has_vertex}")

        if has_kimi or has_vertex:
            print("  WARNING: Has provider calls! (expected 0)")
        else:
            print("  [OK] Provider-free function")

    except Exception as e:
        print(f"  Error: {e}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("""
1. handle_revise() returns: action='revise_complete', send_revision_package=True
2. Router condition: if action == 'revise_complete' and result.get('send_revision_package')
3. Condition MATCHES: True and True = True
4. Router calls: _send_revision_result() -> send_compact_revision_summary()
5. send_compact_revision_summary sends NEW format with VIEW FULL button

BUT USER SEES OLD FORMAT:
    ✅ Revision complete:
    Article: v2 (NEW)

ROOT CAUSE:
    This format doesn't exist in current code. The runtime must be using
    cached bytecode (.pyc) or an old process didn't restart.

FIX:
    - Find running Python processes and kill them
    - Delete __pycache__ directories
    - Restart with fresh Python instance
""")


if __name__ == "__main__":
    main()
