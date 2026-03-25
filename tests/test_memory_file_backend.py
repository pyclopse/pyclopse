"""Tests for FileMemoryBackend (per-agent structure)."""
import pytest
from pathlib import Path

from pyclawops.memory.file_backend import FileMemoryBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_dir(tmp_path) -> Path:
    """Simulates ~/.pyclawops/agents/myagent/."""
    d = tmp_path / "agents" / "myagent"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mem(agent_dir) -> FileMemoryBackend:
    return FileMemoryBackend(base_dir=str(agent_dir))


# ---------------------------------------------------------------------------
# Directory structure
# ---------------------------------------------------------------------------

class TestDirectoryStructure:
    def test_daily_files_in_memory_subdir(self, agent_dir, mem):
        """Daily files must live in {agent_dir}/memory/, not in agent_dir."""
        assert (agent_dir / "memory").is_dir()

    @pytest.mark.asyncio
    async def test_write_creates_file_in_memory_subdir(self, agent_dir, mem):
        await mem.write("k", {"content": "v"})
        assert any((agent_dir / "memory").glob("????-??-??.md"))

    def test_curated_path_is_in_agent_dir(self, agent_dir, mem):
        assert mem.curated_path == agent_dir / "MEMORY.md"

    def test_read_curated_uses_agent_dir(self, agent_dir, mem):
        (agent_dir / "MEMORY.md").write_text("# curated\n")
        assert mem.read_curated() == "# curated\n"

    def test_daily_files_not_at_agent_dir_root(self, agent_dir, mem):
        """Glob in agent_dir root should never find daily files."""
        assert list(agent_dir.glob("????-??-??.md")) == []


# ---------------------------------------------------------------------------
# write / read
# ---------------------------------------------------------------------------

class TestWriteRead:
    @pytest.mark.asyncio
    async def test_write_and_read(self, mem):
        await mem.write("my-key", {"content": "Hello world"})
        entry = await mem.read("my-key")
        assert entry is not None
        assert entry["key"] == "my-key"
        assert entry["content"] == "Hello world"

    @pytest.mark.asyncio
    async def test_read_nonexistent_returns_none(self, mem):
        assert await mem.read("no-such-key") is None

    @pytest.mark.asyncio
    async def test_write_with_tags(self, mem):
        await mem.write("tagged", {"content": "stuff", "tags": ["a", "b"]})
        entry = await mem.read("tagged")
        assert entry["tags"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_write_with_string_tags(self, mem):
        await mem.write("tagged2", {"content": "stuff", "tags": "x, y"})
        entry = await mem.read("tagged2")
        assert entry["tags"] == ["x", "y"]

    @pytest.mark.asyncio
    async def test_update_existing_key_no_duplicate(self, mem, agent_dir):
        await mem.write("updatable", {"content": "v1"})
        await mem.write("updatable", {"content": "v2"})
        entry = await mem.read("updatable")
        assert entry["content"] == "v2"
        daily_files = list((agent_dir / "memory").glob("????-??-??.md"))
        text = daily_files[0].read_text()
        assert text.count("## updatable") == 1

    @pytest.mark.asyncio
    async def test_write_multiple_keys(self, mem):
        await mem.write("alpha", {"content": "aaa"})
        await mem.write("beta", {"content": "bbb"})
        assert (await mem.read("alpha"))["content"] == "aaa"
        assert (await mem.read("beta"))["content"] == "bbb"

    @pytest.mark.asyncio
    async def test_date_recorded_correctly(self, mem, agent_dir):
        await mem.write("dated", {"content": "c"})
        entry = await mem.read("dated")
        # date field matches the filename stem (YYYY-MM-DD)
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2}", entry["date"])


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_existing(self, mem):
        await mem.write("to-delete", {"content": "bye"})
        assert await mem.delete("to-delete") is True
        assert await mem.read("to-delete") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, mem):
        assert await mem.delete("ghost") is False

    @pytest.mark.asyncio
    async def test_delete_leaves_other_keys(self, mem):
        await mem.write("keep", {"content": "stay"})
        await mem.write("remove", {"content": "gone"})
        await mem.delete("remove")
        assert (await mem.read("keep"))["content"] == "stay"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestList:
    @pytest.mark.asyncio
    async def test_list_empty(self, mem):
        assert await mem.list() == []

    @pytest.mark.asyncio
    async def test_list_all_keys(self, mem):
        await mem.write("key-a", {"content": "a"})
        await mem.write("key-b", {"content": "b"})
        keys = await mem.list()
        assert set(keys) == {"key-a", "key-b"}

    @pytest.mark.asyncio
    async def test_list_with_prefix(self, mem):
        await mem.write("user-name", {"content": "Alice"})
        await mem.write("user-pref", {"content": "dark"})
        await mem.write("project-x", {"content": "stuff"})
        keys = await mem.list(prefix="user-")
        assert set(keys) == {"user-name", "user-pref"}
        assert "project-x" not in keys

    @pytest.mark.asyncio
    async def test_list_deduplicates_across_files(self, agent_dir):
        mem = FileMemoryBackend(str(agent_dir))
        mem_dir = agent_dir / "memory"
        mem_dir.mkdir(exist_ok=True)
        (mem_dir / "2026-03-09.md").write_text(
            "# Memory — 2026-03-09\n\n## shared-key\n\nold\n\n---\n\n"
        )
        (mem_dir / "2026-03-10.md").write_text(
            "# Memory — 2026-03-10\n\n## shared-key\n\nnew\n\n---\n\n"
        )
        keys = await mem.list()
        assert keys.count("shared-key") == 1


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearch:
    @pytest.mark.asyncio
    async def test_search_finds_keyword(self, mem):
        await mem.write("python-tip", {"content": "Python is great for scripting"})
        await mem.write("java-tip", {"content": "Java is verbose but fast"})
        results = await mem.search("Python scripting")
        assert "python-tip" in [r["key"] for r in results]

    @pytest.mark.asyncio
    async def test_search_ordered_by_relevance(self, mem):
        await mem.write("high", {"content": "python python python"})
        await mem.write("low", {"content": "python"})
        results = await mem.search("python")
        assert results[0]["key"] == "high"

    @pytest.mark.asyncio
    async def test_search_empty_query(self, mem):
        await mem.write("k", {"content": "some content"})
        assert await mem.search("") == []

    @pytest.mark.asyncio
    async def test_search_no_match(self, mem):
        await mem.write("k", {"content": "hello world"})
        assert await mem.search("xyzzy_not_present") == []

    @pytest.mark.asyncio
    async def test_search_respects_limit(self, mem):
        for i in range(10):
            await mem.write(f"key-{i}", {"content": "target word target"})
        results = await mem.search("target", limit=3)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_search_includes_curated_sections(self, agent_dir):
        mem = FileMemoryBackend(str(agent_dir))
        (agent_dir / "MEMORY.md").write_text(
            "# My Memory\n\n## curated-fact\n\nSpecial curated knowledge\n\n---\n\n"
        )
        results = await mem.search("curated knowledge")
        keys = [r["key"] for r in results]
        assert "curated-fact" in keys
        match = next(r for r in results if r["key"] == "curated-fact")
        assert match["date"] == "MEMORY.md"

    @pytest.mark.asyncio
    async def test_search_result_no_score_field(self, mem):
        await mem.write("k", {"content": "something"})
        results = await mem.search("something")
        for r in results:
            assert "score" not in r


# ---------------------------------------------------------------------------
# read_curated
# ---------------------------------------------------------------------------

class TestReadCurated:
    def test_returns_none_when_missing(self, mem):
        assert mem.read_curated() is None

    def test_returns_content_when_present(self, agent_dir):
        mem = FileMemoryBackend(str(agent_dir))
        (agent_dir / "MEMORY.md").write_text("# Curated\n\nSome notes.\n")
        assert mem.read_curated() == "# Curated\n\nSome notes.\n"


# ---------------------------------------------------------------------------
# Per-agent isolation
# ---------------------------------------------------------------------------

class TestPerAgentIsolation:
    @pytest.mark.asyncio
    async def test_two_agents_separate_memory(self, tmp_path):
        agent_a = tmp_path / "agents" / "alice"
        agent_b = tmp_path / "agents" / "bob"
        agent_a.mkdir(parents=True)
        agent_b.mkdir(parents=True)

        mem_a = FileMemoryBackend(str(agent_a))
        mem_b = FileMemoryBackend(str(agent_b))

        await mem_a.write("secret", {"content": "alice's secret"})
        await mem_b.write("secret", {"content": "bob's secret"})

        a_entry = await mem_a.read("secret")
        b_entry = await mem_b.read("secret")

        assert a_entry["content"] == "alice's secret"
        assert b_entry["content"] == "bob's secret"

    @pytest.mark.asyncio
    async def test_agent_b_cannot_see_agent_a_keys(self, tmp_path):
        agent_a = tmp_path / "agents" / "alice"
        agent_b = tmp_path / "agents" / "bob"
        agent_a.mkdir(parents=True)
        agent_b.mkdir(parents=True)

        mem_a = FileMemoryBackend(str(agent_a))
        mem_b = FileMemoryBackend(str(agent_b))

        await mem_a.write("only-alice", {"content": "private"})

        assert await mem_b.read("only-alice") is None
        assert await mem_b.list() == []


# ---------------------------------------------------------------------------
# File parsing edge cases
# ---------------------------------------------------------------------------

class TestParsing:
    @pytest.mark.asyncio
    async def test_parse_existing_daily_file(self, agent_dir):
        mem = FileMemoryBackend(str(agent_dir))
        mem_dir = agent_dir / "memory"
        mem_dir.mkdir(exist_ok=True)
        (mem_dir / "2026-03-10.md").write_text(
            "# Memory — 2026-03-10\n\n"
            "## user-name\n\nAlice\n\n---\n\n"
            "## user-age\n\n30\n\nTags: personal\n\n---\n\n"
        )
        entry = await mem.read("user-name")
        assert entry["content"] == "Alice"
        assert entry["date"] == "2026-03-10"

    @pytest.mark.asyncio
    async def test_tags_stripped_from_content(self, agent_dir):
        mem = FileMemoryBackend(str(agent_dir))
        mem_dir = agent_dir / "memory"
        mem_dir.mkdir(exist_ok=True)
        (mem_dir / "2026-03-10.md").write_text(
            "# Memory — 2026-03-10\n\n"
            "## tagged-entry\n\nSome content.\n\nTags: foo, bar\n\n---\n\n"
        )
        entry = await mem.read("tagged-entry")
        assert entry["tags"] == ["foo", "bar"]
        assert "Tags:" not in entry["content"]

    @pytest.mark.asyncio
    async def test_newest_file_wins(self, agent_dir):
        mem = FileMemoryBackend(str(agent_dir))
        mem_dir = agent_dir / "memory"
        mem_dir.mkdir(exist_ok=True)
        (mem_dir / "2026-03-09.md").write_text(
            "# Memory — 2026-03-09\n\n## shared\n\nold value\n\n---\n\n"
        )
        (mem_dir / "2026-03-10.md").write_text(
            "# Memory — 2026-03-10\n\n## shared\n\nnew value\n\n---\n\n"
        )
        entry = await mem.read("shared")
        assert entry["content"] == "new value"
        assert entry["date"] == "2026-03-10"

    def test_parse_empty_file(self, agent_dir):
        mem = FileMemoryBackend(str(agent_dir))
        empty = agent_dir / "memory" / "2026-03-10.md"
        empty.parent.mkdir(exist_ok=True)
        empty.write_text("")
        assert mem._parse_daily(empty) == {}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestMemoryConfigSchema:
    def test_default_backend_is_file(self):
        from pyclawops.config.schema import MemoryConfig
        assert MemoryConfig().backend == "file"

    def test_inject_curated_default_true(self):
        from pyclawops.config.schema import MemoryConfig
        assert MemoryConfig().file.inject_curated is True

    def test_inject_curated_camelcase(self):
        from pyclawops.config.schema import MemoryConfig
        cfg = MemoryConfig.model_validate({"file": {"injectCurated": False}})
        assert cfg.file.inject_curated is False

    def test_no_base_dir_field(self):
        from pyclawops.config.schema import FileMemoryConfig
        assert not hasattr(FileMemoryConfig(), "base_dir")
