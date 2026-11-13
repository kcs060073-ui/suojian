"""Public copy stays neutral while implementation identifiers remain compatible."""
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
RESTRICTED = re.compile(r"园区|(?<![A-Za-z0-9_])cutroom(?![A-Za-z0-9_])", re.I)


class VisibleCopy(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "code"}:
            self.hidden += 1
        if not self.hidden:
            self.text.extend(value for key, value in attrs
                             if value and key in {"title", "aria-label", "alt", "placeholder"})

    def handle_endtag(self, tag):
        if tag in {"script", "style", "code"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden:
            self.text.append(value)


class PublicPresentationTests(unittest.TestCase):
    def test_current_interface_labels_are_neutral(self):
        folders = [ROOT / "main/suo_jian/ui/templates",
                   ROOT / "cutroom/web-ui/public", ROOT / "vibe/workbench/static"]
        for folder in folders:
            for page in folder.glob("*.html"):
                with self.subTest(page=page.relative_to(ROOT)):
                    parser = VisibleCopy()
                    parser.feed(page.read_text(encoding="utf-8-sig"))
                    self.assertIsNone(RESTRICTED.search("\n".join(parser.text)))

    def test_public_guides_use_neutral_prose(self):
        for name in ["README.md", "INSTALL.md", "main/README.md", "main/UNIFIED_WORKFLOW.md",
                     "docs/updates/2026-11-11.md", "cutroom/README.md", "cutroom/web-ui/README.md",
                     "cutroom/docs/user-guide/cutroom-user-guide.md"]:
            with self.subTest(guide=name):
                text = (ROOT / name).read_text(encoding="utf-8-sig")
                text = re.sub(r"```[\s\S]*?```", "", text)
                text = re.sub(r"`[^`]*`", "", text)
                text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
                self.assertIsNone(RESTRICTED.search(text))


if __name__ == "__main__":
    unittest.main()
