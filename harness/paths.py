"""Where potluck's data and machine-local state live.

potluck supports two install modes, and this module is the single place that
knows the difference:

  * clone      — you `git clone` and run `./potluck` / `./setup.sh`. Data
                 (profiles/, base/) sits next to the `harness` package in the repo.
  * uv tool    — `uv tool install git+…`. The `harness` package (with profiles/
                 and base/ shipped as `harness/_data/`) lives in uv's tool venv.

Data lookup auto-detects which mode is active. Machine-local state — the
resolved model plan — always lives in the user config dir (XDG), never in the
source tree: writing machine paths into site-packages (or the repo) is a smell,
and it must survive a tool reinstall.
"""

from __future__ import annotations

import os
from pathlib import Path


def _packaged_data() -> Path:
    # Populated by the wheel build (see pyproject force-include).
    return Path(__file__).parent / "_data"


def _repo_root() -> Path:
    return Path(__file__).parent.parent


def data_dir() -> Path:
    """Dir containing profiles/ and base/ — packaged copy if installed, else repo."""
    pkg = _packaged_data()
    if (pkg / "profiles").is_dir():
        return pkg
    return _repo_root()


def profiles_dir() -> Path:
    return data_dir() / "profiles"


def base_dir() -> Path:
    return data_dir() / "base"


def config_dir() -> Path:
    """User config dir: $XDG_CONFIG_HOME/potluck or ~/.config/potluck (not created)."""
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(root) / "potluck"


def resolved_path() -> Path:
    """Machine-local resolved model plan. Lives in the config dir for both modes."""
    return config_dir() / "resolved.toml"
