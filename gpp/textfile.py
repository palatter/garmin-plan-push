"""Reading the text files people hand gpp, however Windows saved them."""

from __future__ import annotations

import codecs
from pathlib import Path


class TextError(ValueError):
    """A file that is not text gpp can read."""


def read_text(path: str | Path) -> str:
    """UTF-8 with or without a byte-order mark, or UTF-16 with one.

    Notepad can add the mark to UTF-8, and PowerShell 5.1's `>` writes
    UTF-16, so `gpp export ... > plan.json` comes back that way there.
    """
    path = Path(path)
    with path.open("rb") as handle:
        head = handle.read(2)
    utf16 = head in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
    try:
        return path.read_text(encoding="utf-16" if utf16 else "utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TextError(f"{path} is not a text file gpp can read; save it as UTF-8") from exc
