"""Back up and restore everything gpp keeps on this machine (#184).

Profile(s), the library, the history database, push receipts and recent
plans go into one zip. The Garmin login tokens do not, unless asked: they
are credentials, and a backup that travels should not carry them.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "gpp"
TOKEN_DIR = Path.home() / ".garminconnect"


def backup(
    target: Path | None = None, with_tokens: bool = False, config_dir: Path | None = None
) -> Path:
    config_dir = config_dir or CONFIG_DIR
    target = target or Path(f"gpp-backup-{dt.date.today().isoformat()}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        if config_dir.exists():
            for path in sorted(config_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, f"config/{path.relative_to(config_dir).as_posix()}")
        if with_tokens and TOKEN_DIR.exists():
            for path in sorted(TOKEN_DIR.rglob("*")):
                if path.is_file():
                    archive.write(path, f"tokens/{path.relative_to(TOKEN_DIR).as_posix()}")
    return target


def restore(archive_path: Path, force: bool = False, config_dir: Path | None = None) -> list[str]:
    """Restore into the config directory; existing files are kept unless `force`."""
    config_dir = config_dir or CONFIG_DIR
    written: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            if member.startswith("config/"):
                root, rel = config_dir, member[len("config/") :]
            elif member.startswith("tokens/"):
                root, rel = TOKEN_DIR, member[len("tokens/") :]
            else:
                continue
            if ".." in Path(rel).parts:
                continue  # a hostile archive must not write outside the folders
            destination = root / rel
            if destination.exists() and not force:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
            written.append(str(destination))
    return written
