"""V2 Top-5 TEST batch CLI. Default: no live LLM, image, or Telegram calls."""

from __future__ import annotations

import argparse
import sys

from newsagent_v2.telegram.contract import LIVE_SEND_ENABLED

LIVE_BATCH_COMMAND = (
    "python -m newsagent_v2.batch --test-top5 --live-llm --live-image --live-telegram"
)

HELP = f"""
NewsAgent V2 Top-5 TEST batch.

Default: NO live LLM calls, NO live image calls, NO Telegram send.

A later TEST Top-5 batch (TEST chat only) would be:

  {LIVE_BATCH_COMMAND}

Required env for that later TEST send (do not paste values):
  NEWSAGENT_V2_TELEGRAM_BOT_TOKEN
  NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID

LIVE_SEND_ENABLED default={LIVE_SEND_ENABLED}
Production Telegram is not available on this path.
""".strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--test-top5", action="store_true")
    parser.add_argument("--live-llm", action="store_true")
    parser.add_argument("--live-image", action="store_true")
    parser.add_argument("--live-telegram", action="store_true")
    args = parser.parse_args(argv)

    live_any = bool(args.live_llm or args.live_image or args.live_telegram)
    if not args.test_top5 or not live_any:
        print(HELP)
        print("Refusing live Top-5 batch.")
        return 2

    print("STOP. Live Top-5 execution is not enabled in this implementation task.")
    print(f"Recorded command: {LIVE_BATCH_COMMAND}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
