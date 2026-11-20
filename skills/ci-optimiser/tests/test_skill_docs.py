"""Packaging checks: frontmatter, size, and every local Markdown link + anchor resolves."""
from __future__ import annotations

import re
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```.*?^```", re.S | re.M)


def slug(heading: str) -> str:
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text())
    return {slug(m.group(1)) for m in re.finditer(r"^#{1,6} (.+)$", text, re.M)}


def source_root() -> Path | None:
    """Installed bundles do not depend on checkout-only README/CI/hook files."""
    candidate = ROOT.parent.parent
    if ROOT.name == "ci-optimiser" and ROOT.parent.name == "skills" and (candidate / ".git").exists():
        return candidate
    return None


def broken_links(root: Path, *, boundary: Path | None = None) -> list[str]:
    problems = []
    for md in sorted(root.rglob("*.md")):
        if ".git" in md.parts or ".ci-audit" in md.parts or ".claude" in md.parts:
            continue
        for target in LINK.findall(FENCE.sub("", md.read_text())):
            if re.match(r"^[a-z]+:", target) or target.startswith("<"):
                continue
            file_part, _, anchor = target.partition("#")
            dest = (md.parent / file_part).resolve() if file_part else md
            rel = md.relative_to(root)
            if boundary is not None and not dest.is_relative_to(boundary.resolve()):
                problems.append(f"{rel}: link leaves installed bundle: {target}")
                continue
            if not dest.exists():
                problems.append(f"{rel}: missing {target}")
            elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
                problems.append(f"{rel}: missing anchor {target}")
    return problems


class SkillDocsTest(unittest.TestCase):
    def test_frontmatter(self):
        text = (ROOT / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\n"))
        front = text.split("---", 2)[1]
        self.assertRegex(front, r"(?m)^name: ci-optimiser$")
        self.assertRegex(front, r"(?m)^description: >-\n  Use when ")
        desc = " ".join(l.strip() for l in front.split("description: >-", 1)[1].splitlines())
        self.assertLessEqual(len(desc), 1024)

    def test_skill_md_size(self):
        core = ROOT / "SKILL.md"
        self.assertLessEqual(len(core.read_text().splitlines()), 200)
        self.assertLessEqual(len(core.read_bytes()), 13 * 1024)

    def test_links_resolve(self):
        self.assertEqual(broken_links(ROOT, boundary=ROOT), [])
        repository = source_root()
        if repository is not None:
            self.assertEqual(broken_links(repository), [])

    def test_bundle_entrypoint_license_and_source_scaffolding(self):
        self.assertEqual(list(ROOT.rglob("SKILL.md")), [ROOT / "SKILL.md"])
        self.assertTrue((ROOT / "LICENSE").is_file())
        repository = source_root()
        if repository is not None:
            self.assertFalse((repository / "SKILL.md").exists())
            for path in ("README.md", "AGENTS.md", "LICENSE", ".githooks/pre-push",
                         ".github/workflows/ci.yml", ".github/dependabot.yml"):
                self.assertTrue((repository / path).is_file(), path)
            self.assertTrue(os.access(repository / ".githooks/pre-push", os.X_OK))
            self.assertEqual((ROOT / "LICENSE").read_bytes(), (repository / "LICENSE").read_bytes())

    def test_checker_can_fail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a.md").write_text("# Top\n[x](b.md) [y](a.md#nope) [z](a.md#top) [w](https://e.x)\n")
            self.assertEqual(broken_links(p), ["a.md: missing b.md", "a.md: missing anchor a.md#nope"])


if __name__ == "__main__":
    unittest.main()
