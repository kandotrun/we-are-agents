"""Keep the English and Japanese READMEs linked, navigable, and in step."""

from __future__ import annotations

from pathlib import Path
import re
import unicodedata
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
READMES = {"en": ROOT / "README.md", "ja": ROOT / "README.ja.md"}
FENCE = re.compile(r"^\s*(```|~~~)\s*([\w-]*)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)|\b(?:src|href)=\"([^\"]+)\"")
# Diagrams and file trees carry translated labels; every other code block must match.
TRANSLATED_BLOCKS = {"mermaid", "text"}


def slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, keep word characters, spaces become hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", re.sub(r"<[^>]+>", "", heading)).lower()
    kept = (char for char in text
            if char in "-_ \u200c\u200d" or unicodedata.category(char)[0] in "LMN")
    return "".join(kept).replace(" ", "-")


def parse(path: Path) -> dict:
    headings, blocks, prose = [], [], []
    block = None
    for line in path.read_text(encoding="utf-8").splitlines():
        fence = FENCE.match(line)
        if block is not None:
            if fence and not fence.group(2):
                blocks.append(block)
                block = None
            else:
                block["lines"].append(line)
        elif fence:
            block = {"info": fence.group(2), "lines": []}
        else:
            heading = HEADING.match(line)
            if heading:
                headings.append((len(heading.group(1)), heading.group(2)))
            prose.append(re.sub(r"`[^`]*`", "", line))
    return {"headings": headings, "blocks": blocks, "prose": "\n".join(prose)}


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    result = set()
    for _, text in parse(path)["headings"]:
        base = slug(text)
        seen[base] = seen.get(base, -1) + 1
        result.add(base if seen[base] == 0 else f"{base}-{seen[base]}")
    return result


def links(prose: str) -> list[str]:
    return [markdown or html for markdown, html in LINK.findall(prose)]


def commands(block: dict) -> list[str]:
    return [line for line in block["lines"] if not line.strip().startswith(("#", "//"))]


class ReadmeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readmes = {language: parse(path) for language, path in READMES.items()}

    def test_each_language_links_to_the_other(self) -> None:
        self.assertIn("README.ja.md", links(self.readmes["en"]["prose"]))
        self.assertIn("README.md", links(self.readmes["ja"]["prose"]))

    def test_relative_links_and_anchors_resolve(self) -> None:
        for language, readme in self.readmes.items():
            for target in links(readme["prose"]):
                if re.match(r"[a-z][a-z0-9+.-]*:", target):
                    continue
                with self.subTest(language=language, target=target):
                    file, _, fragment = unquote(target).partition("#")
                    path = ROOT / file if file else READMES[language]
                    self.assertTrue(path.exists(), f"{target} does not exist")
                    if fragment and path.suffix == ".md":
                        self.assertIn(fragment, anchors(path), f"{target} matches no heading")

    def test_languages_share_structure_and_commands(self) -> None:
        en, ja = self.readmes["en"], self.readmes["ja"]
        self.assertEqual([level for level, _ in en["headings"]], [level for level, _ in ja["headings"]])
        self.assertEqual([block["info"] for block in en["blocks"]], [block["info"] for block in ja["blocks"]])
        for index, (english, japanese) in enumerate(zip(en["blocks"], ja["blocks"])):
            if english["info"] not in TRANSLATED_BLOCKS:
                with self.subTest(block=index, info=english["info"]):
                    self.assertEqual(commands(english), commands(japanese))


class SlugTests(unittest.TestCase):
    def test_slug_matches_github_for_emoji_and_japanese_headings(self) -> None:
        # Anchors observed from GitHub's Markdown renderer.
        for heading, expected in {
            "🚀 Quick start": "-quick-start",
            "Install & update": "install--update",
            "✅ What's verified — and what isn't": "-whats-verified--and-what-isnt",
            "⚠️ Security notes": "\ufe0f-security-notes",
            "🧑‍⚖️ People keep the final say": "\u200d\ufe0f-people-keep-the-final-say",
            "日本語・テスト（括弧）": "日本語テスト括弧",
            "Step 1: `waa` relay (VPN)": "step-1-waa-relay-vpn",
        }.items():
            with self.subTest(heading=heading):
                self.assertEqual(slug(heading), expected)


if __name__ == "__main__":
    unittest.main()
