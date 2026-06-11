#!/usr/bin/env python3
from __future__ import annotations

from review_suite_runtime_bootstrap import bootstrap_from_installed_cache

bootstrap_from_installed_cache(__file__)

from review_suite_core import emit_error


def main() -> int:
    return emit_error(
        "review_t2.py is retired as a direct agent entrypoint; use review.py instead.",
        status="usage_error",
    )


if __name__ == "__main__":
    raise SystemExit(main())
