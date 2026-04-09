"""Install bundled WeCode Claude Code skill into the workspace ``.claude/skills/`` tree."""

from __future__ import annotations

import shutil
from pathlib import Path

_BUNDLE_NAME = "wecode-server"


def wecode_skill_source_dir() -> Path:
    return Path(__file__).resolve().parent / "wecode_claude_skill" / _BUNDLE_NAME


def ensure_wecode_claude_skill(cwd: str) -> None:
    """Copy bundled ``wecode-server`` skill into ``cwd`` if missing or content differs."""
    src_root = wecode_skill_source_dir()
    if not src_root.is_dir():
        return

    dest_root = Path(cwd).expanduser().resolve() / ".claude" / "skills" / _BUNDLE_NAME
    dest_root.mkdir(parents=True, exist_ok=True)

    for path in sorted(src_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root)
        dest_file = dest_root / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if dest_file.is_file():
            try:
                if dest_file.read_bytes() == path.read_bytes():
                    continue
            except OSError:
                pass
        shutil.copy2(path, dest_file)
