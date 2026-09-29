"""Back up and restore everything gpp keeps on this machine (#184).

Profile(s), the library, the history database, push receipts and recent
plans go into one zip. The Garmin login tokens do not, unless asked: they
are credentials, and a backup that travels should not carry them. Nor does
the debug log, which is a support aid, not data, and may predate redaction.
"""

from __future__ import annotations

import datetime as dt
import os
import zipfile
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "gpp"
TOKEN_DIR = Path.home() / ".garminconnect"


SKIPPED = ("logs",)


def backup(
    target: Path | None = None,
    with_tokens: bool = False,
    config_dir: Path | None = None,
    token_dir: Path | None = None,
) -> Path:
    config_dir = config_dir or CONFIG_DIR
    token_dir = token_dir or TOKEN_DIR
    target = target or Path(f"gpp-backup-{dt.date.today().isoformat()}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        if config_dir.exists():
            for path in sorted(config_dir.rglob("*")):
                rel = path.relative_to(config_dir)
                if path.is_file() and rel.parts[0] not in SKIPPED:
                    archive.write(path, f"config/{rel.as_posix()}")
        if with_tokens and token_dir.exists():
            for path in sorted(token_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, f"tokens/{path.relative_to(token_dir).as_posix()}")
    return target


def _inside(root: Path, rel: str) -> Path | None:
    """Where `rel` lands under `root`, or None if it would land anywhere else.

    Catches `..`, absolute names (`config//tmp/x`), Windows drive and UNC
    names, and symlinks already under `root` that point out of it.
    """
    if not rel or "\\" in rel or ":" in rel or Path(rel).is_absolute() or rel.startswith("/"):
        return None
    base = root.resolve()
    destination = (base / rel).resolve()
    return destination if base in destination.parents else None


def _write_private(path: Path, data: bytes) -> None:
    """Owner-only, like garminconnect writes its own token file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    if os.name == "posix":
        path.chmod(0o600)


def restore(
    archive_path: Path,
    force: bool = False,
    config_dir: Path | None = None,
    token_dir: Path | None = None,
) -> list[str]:
    """Restore into the config directory; existing files are kept unless `force`.

    Entries that would land outside their folder are skipped: a zip someone
    sent must not be able to write anywhere else on the machine.
    """
    config_dir = config_dir or CONFIG_DIR
    token_dir = token_dir or TOKEN_DIR
    written: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            if member.startswith("config/"):
                root, rel = config_dir, member[len("config/") :]
            elif member.startswith("tokens/"):
                root, rel = token_dir, member[len("tokens/") :]
            else:
                continue
            destination = _inside(root, rel)
            if destination is None:
                continue
            if destination.exists() and not force:
                continue
            if root is token_dir:
                _write_private(destination, archive.read(member))
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member))
            written.append(str(destination))
    return written
