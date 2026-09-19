from __future__ import annotations

from .input import DEFAULT_OUTPUT_PATH, write_editorial_input


def main() -> int:
    path = write_editorial_input()
    print(f"Wrote {path}")
    print(f"Default path: {DEFAULT_OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
