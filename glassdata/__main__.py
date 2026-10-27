import sys

# 同 main.py：交付目录里不留 __pycache__
sys.dont_write_bytecode = True

from .cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
