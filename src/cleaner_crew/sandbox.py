"""Running commands over code the crew (or an upgraded dependency) wrote.

Test, lint and mutation runs execute agent-written code: a test file, a `conftest.py`, a
`package.json` script. Those runs must not see the crew's own credentials, and with
`run.isolate_network` they get no network either, so such code can't phone home.

  * `secret_env_names()` lists the variables stripped from every such run: the tracker and
    code host tokens, Claude credentials, and anything loaded from `secrets.env`.
  * `offline_argv()` wraps a command in a fresh, empty network namespace (Linux
    `unshare`, no root needed) with only loopback up, so local test servers still work.
    When that isn't possible it returns None and the caller fails closed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

# Credentials Claude Code itself needs. Agents keep these (the `claude` process can't run
# without them); every command that runs code the agents wrote loses them.
CLAUDE_AUTH_ENV = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                             "CLAUDE_CODE_OAUTH_TOKEN"})

# The crew's own credentials under their usual names, whatever config.yml calls them.
CREW_SECRET_ENV = frozenset({"GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "CLEANER_CREW_TOKEN",
                             "LINEAR_API_KEY", "JIRA_API_TOKEN", "JIRA_EMAIL"}) | CLAUDE_AUTH_ENV

# Names loaded from .cleaner-crew/secrets.env (filled in by config.load_secrets).
LOADED_SECRET_NAMES: set[str] = set()


def secret_env_names(cfg: Config | None = None) -> frozenset[str]:
    names = set(CREW_SECRET_ENV) | LOADED_SECRET_NAMES
    if cfg is not None:
        names |= {cfg.tracker.token_env, cfg.tracker.email_env, cfg.code_host.token_env}
    return frozenset(n for n in names if n)


# Runs inside the new namespace: bring loopback up (it starts down), then exec the command.
# `-I` so nothing in the worktree (a planted socket.py, PYTHONPATH) is imported.
_BOOTSTRAP = """\
import fcntl, os, socket, struct, sys
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    flags = struct.unpack("16sH22x", fcntl.ioctl(s, 0x8913, struct.pack("16sH22x", b"lo", 0)))[1]
    fcntl.ioctl(s, 0x8914, struct.pack("16sH22x", b"lo", flags | 1))
except OSError:
    pass
os.execvp("sh", ["sh", "-c", sys.argv[1]])
"""

# Prefer keeping the caller's uid; older util-linux only knows --map-root-user.
_UNSHARE_VARIANTS = (["--user", "--map-current-user", "--net"], ["--user", "--map-root-user",
                                                                 "--net"])


@lru_cache(maxsize=1)
def _unshare_flags() -> tuple[str, ...] | None:
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return None
    for flags in _UNSHARE_VARIANTS:
        try:
            probe = subprocess.run(["unshare", *flags, "true"], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return tuple(flags)
    return None


def isolation_available() -> bool:
    return _unshare_flags() is not None


def offline_argv(cmd: str) -> list[str] | None:
    """argv running `cmd` through `sh -c` with no network, or None if that's impossible."""
    flags = _unshare_flags()
    if flags is None:
        return None
    return ["unshare", *flags, sys.executable, "-I", "-c", _BOOTSTRAP, cmd]


UNAVAILABLE = ("run.isolate_network is on, but this machine can't create an unprivileged "
               "network namespace (needs Linux `unshare` and user namespaces; Ubuntu 24.04+ "
               "restricts them via AppArmor). Refusing to run untrusted code with network "
               "access. Turn isolation off in config.yml or run the crew where it's allowed.")
