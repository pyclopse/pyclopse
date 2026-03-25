"""Tests for pyclawops.self.loader.DocLoader."""

import pytest
from pathlib import Path
from unittest.mock import patch

from pyclawops.self.loader import DocLoader, _KNOWLEDGE_DIR, _PACKAGE_DIR


# ---------------------------------------------------------------------------
# topics()
# ---------------------------------------------------------------------------

def test_topics_returns_index_md_when_present():
    """topics() returns the contents of index.md when it exists."""
    loader = DocLoader()
    result = loader.topics()
    # index.md exists in the real knowledge dir
    assert "overview" in result
    assert "architecture" in result.lower()


def test_topics_fallback_when_no_index(tmp_path):
    """topics() builds a fallback index when index.md is missing."""
    (tmp_path / "foo.md").write_text("# Foo")
    (tmp_path / "bar.md").write_text("# Bar")

    loader = DocLoader()
    loader._knowledge_dir = tmp_path
    result = loader.topics()
    assert "foo" in result
    assert "bar" in result


def test_topics_empty_knowledge_dir(tmp_path):
    """topics() returns helpful message when knowledge dir is empty."""
    loader = DocLoader()
    loader._knowledge_dir = tmp_path
    result = loader.topics()
    assert "[EMPTY]" in result


# ---------------------------------------------------------------------------
# read()
# ---------------------------------------------------------------------------

def test_read_existing_topic():
    """read() returns content for a known topic."""
    loader = DocLoader()
    result = loader.read("overview")
    assert "pyclawops" in result.lower()
    assert not result.startswith("[NOT FOUND]")
    assert not result.startswith("[ERROR]")


def test_read_nested_topic():
    """read() resolves nested topics like 'architecture/gateway'."""
    loader = DocLoader()
    result = loader.read("architecture/gateway")
    assert not result.startswith("[NOT FOUND]")
    assert "Gateway" in result


def test_read_not_found_returns_message():
    """read() returns a [NOT FOUND] message for unknown topics."""
    loader = DocLoader()
    result = loader.read("nonexistent/topic/that/does/not/exist")
    assert result.startswith("[NOT FOUND]")


def test_read_not_found_includes_suggestion():
    """read() suggests related topics when a partial match exists."""
    loader = DocLoader()
    # 'gateway' appears in 'architecture/gateway'
    result = loader.read("gateway")
    # Either found it or suggested it
    assert "gateway" in result.lower()


def test_read_rejects_path_traversal():
    """read() rejects paths that would escape the knowledge directory."""
    loader = DocLoader()
    result = loader.read("../../pyclawops/__init__.py")
    assert result.startswith("[ERROR]")
    assert "escape" in result.lower()


def test_read_strips_leading_slash():
    """read() handles topics with leading slashes gracefully."""
    loader = DocLoader()
    result = loader.read("/overview")
    # Should find it the same as 'overview'
    assert not result.startswith("[NOT FOUND]")


def test_read_with_md_extension():
    """read() also accepts topic paths with .md extension."""
    loader = DocLoader()
    result = loader.read("overview.md")
    assert not result.startswith("[NOT FOUND]")


# ---------------------------------------------------------------------------
# source()
# ---------------------------------------------------------------------------

def test_source_returns_file_with_line_numbers():
    """source() returns source code with line numbers."""
    loader = DocLoader()
    result = loader.source("self/loader.py")
    assert "DocLoader" in result
    # Line numbers present
    assert "\t" in result
    # Header comment
    assert "# self/loader.py" in result


def test_source_gateway():
    """source() can read core/gateway.py."""
    loader = DocLoader()
    result = loader.source("core/gateway.py")
    assert not result.startswith("[NOT FOUND]")
    assert "Gateway" in result


def test_source_not_found():
    """source() returns [NOT FOUND] for missing modules."""
    loader = DocLoader()
    result = loader.source("does/not/exist.py")
    assert result.startswith("[NOT FOUND]")


def test_source_rejects_path_traversal():
    """source() rejects paths that escape the pyclawops package."""
    loader = DocLoader()
    result = loader.source("../../../etc/passwd")
    assert result.startswith("[ERROR]")
    assert "escape" in result.lower()


def test_source_directory_lists_contents():
    """source() on a directory returns its contents rather than erroring."""
    loader = DocLoader()
    result = loader.source("core")
    assert "is a directory" in result
    assert "gateway.py" in result


def test_source_line_numbers_are_correct():
    """source() line numbers start at 1 and are sequential."""
    loader = DocLoader()
    result = loader.source("self/__init__.py")
    lines = [l for l in result.splitlines() if "\t" in l]
    assert len(lines) > 0
    first_num = int(lines[0].split("\t")[0].strip())
    assert first_num == 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def test_list_topics_excludes_index():
    """_list_topics() does not include index.md in the topic list."""
    loader = DocLoader()
    topics = loader._list_topics()
    assert "index" not in topics
    assert all("index" not in t for t in topics)


def test_list_topics_uses_forward_slashes():
    """_list_topics() always uses forward slashes in topic paths."""
    loader = DocLoader()
    topics = loader._list_topics()
    for topic in topics:
        assert "\\" not in topic


def test_package_dir_exists():
    """_PACKAGE_DIR resolves to the actual pyclawops package directory."""
    assert _PACKAGE_DIR.exists()
    assert (_PACKAGE_DIR / "__init__.py").exists()


def test_knowledge_dir_exists():
    """_KNOWLEDGE_DIR resolves to the knowledge base directory."""
    assert _KNOWLEDGE_DIR.exists()
    assert (_KNOWLEDGE_DIR / "index.md").exists()
