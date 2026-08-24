"""Compatibility entry point for deployments that run `python reference/server.py`.

The implementation moved into the `jnaapakam` package (src/jnaapakam/). This shim
keeps the historical path working — notably the MCPize deployment, whose manifest
invokes this file directly — whether or not the package has been pip-installed.

The platform contract is honored by jnaapakam.config: it binds the host from
MEMORY_HOST (defaulted to 0.0.0.0 below), the port from PORT (default 8889), and
refuses a public bind unless MEMORY_AUTH_TOKEN is set.
"""

import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jnaapakam.cli import main  # noqa: E402

if __name__ == "__main__":
    # Hosted platforms route to a public interface, so default the bind address
    # accordingly and honor the platform-assigned PORT via Config.from_env().
    # Config.validate() then requires MEMORY_AUTH_TOKEN before it will expose
    # /clear and /restore to the internet.
    os.environ.setdefault("MEMORY_HOST", "0.0.0.0")
    argv = sys.argv[1:] or ["serve"]
    if argv and argv[0].startswith("-"):
        argv = ["serve", *argv]
    raise SystemExit(main(argv))
