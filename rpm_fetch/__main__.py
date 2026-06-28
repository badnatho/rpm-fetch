"""Entry point for ``python -m rpm_fetch``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
