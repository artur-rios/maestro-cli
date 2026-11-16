"""Finding and loading saved prompt files."""

import sys
from pathlib import Path

import pytest

from maestro.shell import promptlib
from maestro.shell.promptlib import PromptError, PromptFile


class LibraryBase:
    @pytest.fixture(autouse=True)
    def _root(self, tmp_path):
        self.root = tmp_path

    def write(self, relative, text="do the thing"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def files(self, settings=None):
        return promptlib.discover(settings or {}, self.root)

    def names(self, settings=None):
        return sorted(f.name for f in self.files(settings))


class TestDiscovery(LibraryBase):
    def test_given_markdown_and_text_files_when_discovered_then_only_prompt_suffixes_count(self):
        self.write("task.md")
        self.write("notes.txt")
        self.write("long.markdown")
        self.write("UPPER.MD")
        self.write("readme.rst")       # not a prompt suffix
        assert self.names() == ["UPPER", "long", "notes", "task"]

    def test_given_files_in_subdirectories_when_the_current_directory_is_searched_then_it_is_not_walked(self):
        """A repository root is far too noisy to search recursively."""
        self.write("top.md")
        self.write("src/deep/buried.md")
        assert self.names() == ["top"]

    def test_given_docs_prompts_when_discovered_then_it_is_walked(self):
        self.write("docs/prompts/scope.md")
        self.write("docs/prompts/area/nested.md")
        assert self.names() == ["nested", "scope"]

    def test_given_a_file_deeper_than_the_walk_limit_when_discovered_then_it_is_left_out(self):
        self.write("docs/prompts/a/b/c/shallow.md")
        self.write("docs/prompts/a/b/c/d/deep.md")
        assert self.names() == ["shallow"]

    def test_given_both_roots_when_discovered_then_each_file_is_labelled_with_its_root(self):
        self.write("here.md")
        self.write("docs/prompts/there.md")
        sources = {f.name: f.source for f in self.files()}
        assert sources["here"] == "current directory"
        assert sources["there"] == "docs/prompts"

    def test_given_a_configured_root_when_discovered_then_it_is_searched(self):
        other = self.root / "elsewhere"
        (other / "sub").mkdir(parents=True)
        (other / "sub" / "extra.md").write_text("x", encoding="utf-8")
        found = self.files({"prompt_paths": [str(other)]})
        assert [(f.name, f.source) for f in found] == [("extra", str(other))]

    def test_given_a_configured_root_that_is_gone_when_discovered_then_it_is_ignored(self):
        assert self.names({"prompt_paths": [str(self.root / "missing")]}) == []

    def test_given_noisy_and_hidden_directories_when_discovered_then_they_are_skipped(self):
        self.write("docs/prompts/node_modules/junk.md")
        self.write("docs/prompts/.hidden/secret.md")
        self.write("docs/prompts/real.md")
        assert self.names() == ["real"]

    def test_given_the_same_directory_twice_when_discovered_then_each_file_is_listed_once(self):
        self.write("docs/prompts/one.md")
        found = self.files({"prompt_paths": [str(self.root / "docs" / "prompts")]})
        assert len(found) == 1

    def test_given_no_prompt_files_when_discovered_then_the_list_is_empty(self):
        assert self.files() == []

    def test_given_files_in_mixed_case_when_discovered_then_they_are_sorted_ignoring_case(self):
        self.write("b.md")
        self.write("A.md")
        self.write("c.md")
        assert [f.name for f in self.files()] == ["A", "b", "c"]

    def test_given_a_directory_that_cannot_be_read_when_scanned_then_nothing_is_returned(self):
        assert promptlib._scan(self.root / "not-there", recursive=False) == []

    def test_given_no_cwd_when_roots_are_listed_then_the_working_directory_is_used(self, monkeypatch):
        monkeypatch.chdir(self.root)
        assert promptlib.roots({})[0] == ("current directory", self.root, False)


class TestLabels:
    def test_given_a_file_under_the_working_directory_when_labelled_then_the_path_is_relative(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert PromptFile(tmp_path / "docs" / "a.md", "x").label == str(Path("docs") / "a.md")

    def test_given_a_file_elsewhere_when_labelled_then_the_full_path_is_shown(self, tmp_path, monkeypatch):
        (tmp_path / "cwd").mkdir()
        monkeypatch.chdir(tmp_path / "cwd")
        outside = tmp_path / "other" / "a.md"
        assert PromptFile(outside, "x").label == str(outside)


class TestReferences(LibraryBase):
    @pytest.fixture(autouse=True)
    def _prompts(self, _root):
        self.write("docs/prompts/scope-permissions.md")
        self.write("docs/prompts/scope-fields.md")
        self.write("docs/prompts/rate-limiting.md")

    def resolve(self, ref):
        return promptlib.resolve(ref, self.files(), self.root)

    def test_given_an_exact_stem_when_resolved_then_it_matches(self):
        assert self.resolve("rate-limiting").name == "rate-limiting.md"

    def test_given_a_stem_in_another_case_when_resolved_then_it_still_matches(self):
        assert self.resolve("RATE-LIMITING").name == "rate-limiting.md"

    def test_given_a_unique_prefix_when_resolved_then_it_matches(self):
        assert self.resolve("rate").name == "rate-limiting.md"

    def test_given_an_ambiguous_prefix_when_resolved_then_the_candidates_are_listed(self):
        with pytest.raises(PromptError) as ctx:
            self.resolve("scope")
        message = str(ctx.value)
        assert "scope-permissions" in message
        assert "scope-fields" in message

    def test_given_two_files_with_the_same_stem_when_resolved_then_it_is_ambiguous(self):
        self.write("other/rate-limiting.md")
        files = self.files({"prompt_paths": [str(self.root / "other")]})
        with pytest.raises(PromptError) as ctx:
            promptlib.resolve("rate-limiting", files, self.root)
        assert "matches several prompts" in str(ctx.value)

    def test_given_an_exact_stem_that_is_also_a_prefix_when_resolved_then_the_exact_one_wins(self):
        self.write("docs/prompts/scope.md")
        assert self.resolve("scope").name == "scope.md"

    def test_given_an_unknown_name_when_resolved_then_it_points_at_prompts(self):
        with pytest.raises(PromptError) as ctx:
            self.resolve("nothing-like-this")
        assert "/prompts" in str(ctx.value)

    def test_given_a_relative_path_when_resolved_then_it_is_used_directly(self):
        assert self.resolve("docs/prompts/scope-fields.md").name == "scope-fields.md"

    def test_given_an_absolute_path_when_resolved_then_it_is_used_directly(self):
        target = self.root / "docs" / "prompts" / "scope-fields.md"
        assert self.resolve(str(target)) == target

    def test_given_a_missing_path_when_resolved_then_it_says_so(self):
        with pytest.raises(PromptError) as ctx:
            self.resolve("docs/prompts/absent.md")
        assert "no such prompt file" in str(ctx.value)

    @pytest.mark.parametrize("ref", ["", "   ", None])
    def test_given_an_empty_reference_when_resolved_then_it_is_refused(self, ref):
        with pytest.raises(PromptError):
            self.resolve(ref)


class TestLoading(LibraryBase):
    def test_given_a_file_with_padding_when_loaded_then_its_content_is_stripped(self):
        written = self.write("task.md", "\n\n# Title\n\nBody.\n\n")
        path, text = promptlib.load(str(written))
        assert path == written
        assert text == "# Title\n\nBody."

    @pytest.mark.parametrize("wrap", ['"{}"', "'{}'", "  {}  "])
    def test_given_a_pasted_path_in_quotes_when_loaded_then_the_quotes_are_stripped(self, wrap):
        written = self.write("task.md", "content")
        assert promptlib.load(wrap.format(written))[1] == "content"

    def test_given_a_dollar_variable_in_the_path_when_loaded_then_it_is_expanded(self, monkeypatch):
        self.write("task.md", "content")
        monkeypatch.setenv("PDIR", str(self.root))
        assert promptlib.load("$PDIR/task.md")[1] == "content"

    @pytest.mark.skipif(sys.platform != "win32", reason="%VAR% is Windows syntax; os.path.expandvars only reads it there")
    def test_given_a_percent_variable_on_windows_when_loaded_then_it_is_expanded(self, monkeypatch):
        self.write("task.md", "content")
        monkeypatch.setenv("PDIR", str(self.root))
        assert promptlib.load("%PDIR%/task.md")[1] == "content"

    def test_given_a_home_relative_path_when_loaded_then_the_tilde_is_expanded(self, monkeypatch):
        self.write("task.md", "content")
        monkeypatch.setenv("HOME", str(self.root))
        monkeypatch.setenv("USERPROFILE", str(self.root))
        assert promptlib.load("~/task.md")[1] == "content"

    @pytest.mark.parametrize("raw", ["", "   ", '""'])
    def test_given_no_path_when_loaded_then_it_is_refused(self, raw):
        with pytest.raises(PromptError) as ctx:
            promptlib.load(raw)
        assert "no path given" in str(ctx.value)

    def test_given_a_missing_file_when_loaded_then_it_is_reported(self):
        with pytest.raises(PromptError) as ctx:
            promptlib.load(str(self.root / "nope.md"))
        assert "no such file" in str(ctx.value)

    def test_given_a_directory_when_loaded_then_it_is_refused(self):
        with pytest.raises(PromptError) as ctx:
            promptlib.load(str(self.root))
        assert "directory" in str(ctx.value)

    def test_given_an_empty_file_when_loaded_then_it_is_refused(self):
        written = self.write("empty.md", "   \n\n")
        with pytest.raises(PromptError) as ctx:
            promptlib.load(str(written))
        assert "empty" in str(ctx.value)

    def test_given_a_binary_file_when_loaded_then_it_is_refused(self):
        binary = self.root / "image.png"
        binary.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        with pytest.raises(PromptError) as ctx:
            promptlib.load(str(binary))
        assert "not UTF-8" in str(ctx.value)

    def test_given_a_file_that_cannot_be_read_when_loaded_then_the_reason_is_reported(self, monkeypatch):
        written = self.write("task.md", "content")

        def denied(*args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_text", denied)
        with pytest.raises(PromptError) as ctx:
            promptlib.load(str(written))
        assert "could not read task.md" in str(ctx.value)

    def test_given_non_ascii_text_when_loaded_then_it_survives(self):
        written = self.write("task.md", "acentuação e emoji \U0001f600")
        assert "acentuação" in promptlib.load(str(written))[1]

