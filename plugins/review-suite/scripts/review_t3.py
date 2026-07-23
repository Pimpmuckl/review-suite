#!/usr/bin/env python3
from __future__ import annotations

import sys

_previous_dont_write_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True

from review_suite_runtime_bootstrap import bootstrap_from_installed_cache

bootstrap_from_installed_cache(__file__)
sys.dont_write_bytecode = _previous_dont_write_bytecode

from review_suite_core import emit_error


def main() -> int:
    return emit_error(
        "review_t3.py is retired as a direct agent entrypoint; use review.py instead.",
        status="usage_error",
    )


if __name__ == "__main__":
    raise SystemExit(main())
