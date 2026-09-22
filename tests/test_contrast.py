"""Measured contrast for the design tokens (#170): the pairs the UI actually
puts text on must clear WCAG AA (4.5:1), in the light theme and the dark one."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "gpp" / "web" / "static"


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block))


def _blocks(css: str) -> tuple[dict, dict]:
    light = re.search(r":root \{(.*?)\n\}", css, re.S).group(1)
    dark = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S).group(1)
    return _tokens(light), _tokens(dark)


def _hsl(value: str) -> tuple[float, float, float] | None:
    m = re.match(r"hsl\((\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%", value.strip())
    if not m:
        return None
    h, s, lum = float(m.group(1)), float(m.group(2)) / 100, float(m.group(3)) / 100
    c = (1 - abs(2 * lum - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m0 = lum - c / 2
    r, g, b = {0: (c, x, 0), 1: (x, c, 0), 2: (0, c, x), 3: (0, x, c), 4: (x, 0, c), 5: (c, 0, x)}[
        int(h // 60) % 6
    ]
    return (r + m0, g + m0, b + m0)


def _luminance(rgb):
    def chan(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(_hsl(a)), _luminance(_hsl(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [
    ("--text", "--surface"),
    ("--text", "--bg"),
    ("--text-soft", "--surface"),
    ("--text-faint", "--bg-sunk"),
    ("--text-faint", "--surface"),
    ("--danger", "--surface"),
    ("--ok", "--surface"),
    ("--accent-text", "--accent"),
]


def test_base_tokens_clear_aa_in_both_themes():
    light, dark = _blocks((STATIC / "style.css").read_text(encoding="utf-8"))
    failures = []
    for theme, tokens in (("light", light), ("dark", dark)):
        for fg, bg in PAIRS:
            ratio = contrast(tokens[fg], tokens[bg])
            if ratio < 4.5:
                failures.append(f"{theme} {fg} on {bg}: {ratio:.2f}")
    assert failures == []


def test_warning_token_clears_aa_on_the_surfaces_it_sits_on():
    light, dark = _blocks((STATIC / "style.css").read_text(encoding="utf-8"))
    review = (STATIC / "review.css").read_text(encoding="utf-8")
    warn_light = _tokens(re.search(r":root \{(.*?)\}", review, re.S).group(1))["--warn"]
    warn_dark = _tokens(re.search(r':root\[data-theme="dark"\] \{(.*?)\}', review, re.S).group(1))[
        "--warn"
    ]
    assert contrast(warn_light, light["--surface"]) >= 4.5
    assert contrast(warn_light, light["--bg-sunk"]) >= 4.5
    assert contrast(warn_dark, dark["--surface"]) >= 4.5
