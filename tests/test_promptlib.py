"""Finding and loading saved prompt files."""

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from agent_orchestrator.shell import promptlib
from agent_orchestrator.shell.promptlib import PromptError


class LibraryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, relative, text="do the thing"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def files(self, settings=None):
        return promptlib.discover(settings or {}, self.root)

    def names(self, settings=None):
        return sorted(f.name for f in self.files(settings))


class Discovery(LibraryTestCase):
    def test_markdown_and_text_in_the_current_directory(self):
        self.write("task.md")
        self.write("notes.txt")
        self.write("readme.rst")       # not a prompt suffix
        self.assertEqual(self.names(), ["notes", "task"])

    def test_the_current_directory_is_not_walked(self):
        """A repository root is far too noisy to search recursively."""
        self.write("top.md")
        self.write("src/deep/buried.md")
        self.assertEqual(self.names(), ["top"])

    def test_docs_prompts_is_walked(self):
        self.write("docs/prompts/scope.md")
        self.write("docs/prompts/area/nested.md")
        self.assertEqual(self.names(), ["nested", "scope"])

    def test_docs_prompts_is_labelled_separately(self):
        self.write("here.md")
        self.write("docs/prompts/there.md")
        sources = {f.name: f.source for f in self.files()}
        self.assertEqual(sources["here"], "current directory")
        self.assertEqual(sources["there"], "docs/prompts")

    def test_a_configured_root_is_searched(self):
        other = Path(self.tmp.name) / "elsewhere"
        (other / "sub").mkdir(parents=True)
        (other / "sub" / "extra.md").write_text("x", encoding="utf-8")
        found = self.names({"prompt_paths": [str(other)]})
        self.assertIn("extra", found)

    def test_a_configured_root_that_is_gone_is_ignored(self):
        self.assertEqual(self.names({"prompt_paths": ["/nope/missing"]}), [])

    def test_noisy_directories_are_skipped(self):
        self.write("docs/prompts/node_modules/junk.md")
        self.write("docs/prompts/.hidden/secret.md")
        self.write("docs/prompts/real.md")
        self.assertEqual(self.names(), ["real"])

    def test_the_same_file_is_not_listed_twice(self):
        self.write("docs/prompts/one.md")
        found = self.files({"prompt_paths": [str(self.root / "docs" / "prompts")]})
        self.assertEqual(len(found), 1)

    def test_nothing_found_is_an_empty_list(self):
        self.assertEqual(self.files(), [])


class References(LibraryTestCase):
    def setUp(self):
        super().setUp()
        self.write("docs/prompts/scope-permissions.md")
        self.write("docs/prompts/scope-fields.md")
        self.write("docs/prompts/rate-limiting.md")

    def resolve(self, ref):
        return promptlib.resolve(ref, self.files(), self.root)

    def test_an_exact_stem_matches(self):
        self.assertEqual(self.resolve("rate-limiting").name, "rate-limiting.md")

    def test_matching_ignores_case(self):
        self.assertEqual(self.resolve("RATE-LIMITING").name, "rate-limiting.md")

    def test_a_unique_prefix_matches(self):
        self.assertEqual(self.resolve("rate").name, "rate-limiting.md")

    def test_an_ambiguous_prefix_lists_the_candidates(self):
        with self.assertRaises(PromptError) as ctx:
            self.resolve("scope")
        message = str(ctx.exception)
        self.assertIn("scope-permissions", message)
        self.assertIn("scope-fields", message)

    def test_an_exact_stem_beats_a_prefix(self):
        self.write("docs/prompts/scope.md")
        self.assertEqual(self.resolve("scope").name, "scope.md")

    def test_an_unknown_name_says_so(self):
        with self.assertRaises(PromptError) as ctx:
            self.resolve("nothing-like-this")
        self.assertIn("/prompts", str(ctx.exception))

    def test_a_path_is_used_directly(self):
        self.assertEqual(
            self.resolve("docs/prompts/scope-fields.md").name, "scope-fields.md"
        )

    def test_a_missing_path_says_so(self):
        with self.assertRaises(PromptError):
            self.resolve("docs/prompts/absent.md")

    def test_an_empty_reference_is_refused(self):
        with self.assertRaises(PromptError):
            self.resolve("")


class Loading(LibraryTestCase):
    def test_content_is_read_and_stripped(self):
        written = self.write("task.md", "\n\n# Title\n\nBody.\n\n")
        path, text = promptlib.load(str(written))
        self.assertEqual(path, written)
        self.assertEqual(text, "# Title\n\nBody.")

    def test_quotes_around_a_pasted_path_are_stripped(self):
        written = self.write("task.md", "content")
        for raw in (f'"{written}"', f"'{written}'", f"  {written}  "):
            self.assertEqual(promptlib.load(raw)[1], "content")

    def test_environment_variables_expand(self):
        self.write("task.md", "content")
        with unittest.mock.patch.dict(os.environ, {"PDIR": str(self.root)}):
            self.assertEqual(promptlib.load("%PDIR%/task.md")[1], "content")

    def test_a_missing_file_is_reported(self):
        with self.assertRaises(PromptError) as ctx:
            promptlib.load(str(self.root / "nope.md"))
        self.assertIn("no such file", str(ctx.exception))

    def test_a_directory_is_refused(self):
        with self.assertRaises(PromptError) as ctx:
            promptlib.load(str(self.root))
        self.assertIn("directory", str(ctx.exception))

    def test_an_empty_file_is_refused(self):
        written = self.write("empty.md", "   \n\n")
        with self.assertRaises(PromptError) as ctx:
            promptlib.load(str(written))
        self.assertIn("empty", str(ctx.exception))

    def test_a_binary_file_is_refused(self):
        binary = self.root / "image.png"
        binary.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        with self.assertRaises(PromptError) as ctx:
            promptlib.load(str(binary))
        self.assertIn("not UTF-8", str(ctx.exception))

    def test_non_ascii_survives(self):
        written = self.write("task.md", "acentuação e emoji \U0001f600")
        self.assertIn("acentuação", promptlib.load(str(written))[1])


if __name__ == "__main__":
    unittest.main()
