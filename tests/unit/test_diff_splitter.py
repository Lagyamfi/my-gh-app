"""Tests for app.services._diff_splitter."""
from app.services._diff_splitter import FileDiff, split_unified_diff


SAMPLE_DIFF = """diff --git a/foo.py b/foo.py
index 1111..2222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 line one
+added line
 line two
diff --git a/bar/baz.py b/bar/baz.py
index 3333..4444 100644
--- a/bar/baz.py
+++ b/bar/baz.py
@@ -10,1 +10,1 @@
-old
+new
diff --git a/README.md b/README.md
index 5555..6666 100644
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-Hello
+Hello world
"""


class TestSplitUnifiedDiff:
    def test_splits_three_files(self):
        files = split_unified_diff(SAMPLE_DIFF)
        assert len(files) == 3
        assert [f.path for f in files] == ["foo.py", "bar/baz.py", "README.md"]

    def test_each_section_starts_with_diff_git(self):
        files = split_unified_diff(SAMPLE_DIFF)
        for f in files:
            assert f.content.startswith("diff --git ")

    def test_concatenated_sections_equal_original(self):
        files = split_unified_diff(SAMPLE_DIFF)
        assert "".join(f.content for f in files) == SAMPLE_DIFF

    def test_empty_input_returns_empty_list(self):
        assert split_unified_diff("") == []

    def test_input_without_diff_header_returns_empty_list(self):
        # Defensive: gh always emits headers, but malformed input shouldn't crash.
        assert split_unified_diff("not a diff\nat all\n") == []

    def test_path_with_subdirectory_is_preserved(self):
        files = split_unified_diff(SAMPLE_DIFF)
        assert files[1].path == "bar/baz.py"
