from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.daemon_runtime import start_daemon_process


_ROOT = Path(__file__).resolve().parent.parent
WEB_ENTRY = _ROOT / "app" / "web.py"


def main(argv: list[str] | None = None) -> int:
    streamlit_args = list(sys.argv[1:] if argv is None else argv)
    ok, message = start_daemon_process()
    if not ok:
        print(message, file=sys.stderr)
        return 1
    if message:
        print(message)

    command = [sys.executable, "-m", "streamlit", "run", str(WEB_ENTRY), *streamlit_args]
    return subprocess.call(command, cwd=str(_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
