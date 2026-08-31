"""Allows `python -m olcap.opencode --probe|--register|--verify|--remove|--rollback`."""
from .adapter import _main

if __name__ == "__main__":
    raise SystemExit(_main())
