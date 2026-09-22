"""The page's structure, checked against the real HTML (#169): every control
labelled, every dialog headed, no duplicate ids, images described."""

from html.parser import HTMLParser
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "gpp" / "web" / "static" / "index.html"


class Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []
        self.problems: list[str] = []
        self.label_stack: list[bool] = []
        self.labelled_ids: set[str] = set()
        self.controls: list[tuple[str, dict]] = []
        self.dialog_headed: dict[str, bool] = {}
        self.current_dialog: str | None = None
        self.in_button: dict | None = None
        self.button_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "label":
            self.label_stack.append(True)
            if a.get("for"):
                self.labelled_ids.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden",):
            self.controls.append((tag, a, bool(self.label_stack)))
        if tag == "dialog":
            self.current_dialog = a.get("id", "?")
            self.dialog_headed[self.current_dialog] = False
        if tag in ("h2", "h1") and self.current_dialog:
            self.dialog_headed[self.current_dialog] = True
        if tag == "button":
            self.in_button = a
            self.button_text = ""
        if (
            tag in ("img", "svg")
            and tag == "img"
            and not (a.get("alt") is not None or a.get("aria-label"))
        ):
            self.problems.append(f"img without alt: {a}")
        if a.get("role") == "img" and not (a.get("aria-label") or a.get("aria-labelledby")):
            self.problems.append(f"role=img without a label: {a.get('id') or a.get('class')}")

    def handle_endtag(self, tag):
        if tag == "label" and self.label_stack:
            self.label_stack.pop()
        if tag == "dialog":
            self.current_dialog = None
        if tag == "button" and self.in_button is not None:
            a = self.in_button
            if not (self.button_text.strip() or a.get("aria-label") or a.get("title")):
                self.problems.append(f"button without a name: {a}")
            self.in_button = None

    def handle_data(self, data):
        if self.in_button is not None:
            self.button_text += data


def audit() -> Audit:
    parser = Audit()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser


def test_ids_are_unique():
    seen = audit().ids
    duplicates = sorted({i for i in seen if seen.count(i) > 1})
    assert duplicates == []


def test_every_control_has_a_label():
    unlabelled = []
    for tag, a, inside_label in audit().controls:
        labelled = (
            inside_label
            or a.get("id") in audit().labelled_ids
            or a.get("aria-label")
            or a.get("aria-labelledby")
        )
        if not labelled:
            unlabelled.append(f"{tag}#{a.get('id')}")
    assert unlabelled == []


def test_every_button_has_a_name_and_every_dialog_a_heading():
    parser = audit()
    assert [p for p in parser.problems if "button" in p] == []
    assert [d for d, ok in parser.dialog_headed.items() if not ok] == []


def test_images_and_role_img_are_described():
    assert [p for p in audit().problems if "img" in p] == []
