"""Tests for skills — frontmatter parsing, skill scanning, agent attachments."""

from hermes_bridge import skills
from tests.conftest import write_skill


# ── Frontmatter parser ────────────────────────────────────────────────


def test_parse_frontmatter_basic():
    content = "---\nname: foo\ndescription: a thing\n---\n\nBody here."
    fm, body = skills._parse_frontmatter(content)
    assert fm["name"] == "foo"
    assert fm["description"] == "a thing"
    assert "Body here." in body


def test_parse_frontmatter_no_frontmatter():
    content = "# Just markdown\n\nNo frontmatter."
    fm, body = skills._parse_frontmatter(content)
    assert fm == {}
    assert body == content


def test_parse_frontmatter_list_value():
    content = "---\nname: foo\ntags: ['a', 'b', 'c']\n---\nbody"
    fm, _ = skills._parse_frontmatter(content)
    assert fm["tags"] == ["a", "b", "c"]


def test_parse_frontmatter_malformed_list_falls_back_to_string():
    content = "---\nname: foo\ntags: [unclosed\n---\nbody"
    fm, _ = skills._parse_frontmatter(content)
    assert fm["tags"] == "[unclosed"


# ── Skill scanning ────────────────────────────────────────────────────


def test_list_skills_empty_when_no_dir(temp_skills_dir, monkeypatch):
    import shutil
    shutil.rmtree(temp_skills_dir)
    assert skills.list_skills() == []


def test_list_skills_finds_categorized_skill(temp_skills_dir):
    write_skill(temp_skills_dir, "devops/mytool", "mytool", "Does devops things")
    result = skills.list_skills()
    assert len(result) == 1
    assert result[0]["name"] == "mytool"
    assert result[0]["category"] == "devops"
    assert result[0]["description"] == "Does devops things"


def test_list_skills_uncategorized(temp_skills_dir):
    write_skill(temp_skills_dir, "loose", "loose", "Top level skill")
    result = skills.list_skills()
    assert len(result) == 1
    assert result[0]["category"] == ""


def test_list_skills_dedupes_by_name(temp_skills_dir):
    write_skill(temp_skills_dir, "cat1/dup", "samename", "First")
    write_skill(temp_skills_dir, "cat2/dup", "samename", "Second")
    result = skills.list_skills()
    names = [s["name"] for s in result]
    assert names.count("samename") == 1


def test_list_skills_sorted_by_category_then_name(temp_skills_dir):
    write_skill(temp_skills_dir, "zcat/b", "bbb", "d")
    write_skill(temp_skills_dir, "acat/a", "aaa", "d")
    result = skills.list_skills()
    cats = [s["category"] for s in result]
    assert cats == sorted(cats)


def test_get_skill_returns_full_content(temp_skills_dir):
    write_skill(temp_skills_dir, "devops/tool", "tool", "desc",
                extra_frontmatter="tags: ['x', 'y']\n", body="Full body content.")
    detail = skills.get_skill("tool")
    assert detail is not None
    assert detail["name"] == "tool"
    assert detail["tags"] == ["x", "y"]
    assert "Full body content." in detail["content"]


def test_get_skill_none_when_missing(temp_skills_dir):
    assert skills.get_skill("nonexistent") is None


def test_get_skill_lists_linked_files(temp_skills_dir):
    skill_dir = write_skill(temp_skills_dir, "devops/tool", "tool", "desc")
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "api.md").write_text("ref content")
    detail = skills.get_skill("tool")
    assert detail is not None
    assert "references" in detail["linked_files"]
    assert "api.md" in detail["linked_files"]["references"]


def test_get_skill_linked_file_content(temp_skills_dir):
    skill_dir = write_skill(temp_skills_dir, "devops/tool", "tool", "desc")
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "api.md").write_text("the reference body")
    content = skills.get_skill_linked_file("tool", "references/api.md")
    assert content == "the reference body"


def test_get_skill_linked_file_missing_returns_none(temp_skills_dir):
    write_skill(temp_skills_dir, "devops/tool", "tool", "desc")
    assert skills.get_skill_linked_file("tool", "references/nope.md") is None


# ── Agent skill attachments (SQLite) ──────────────────────────────────


def test_attach_skill_to_agent(temp_db):
    rec = skills.attach_skill_to_agent("agent1", "skillA")
    assert rec["skill_name"] == "skillA"
    assert "attached_at" in rec


def test_attach_is_idempotent(temp_db):
    first = skills.attach_skill_to_agent("agent1", "skillA")
    second = skills.attach_skill_to_agent("agent1", "skillA")
    # Same attached_at — second attach returns existing record, no duplicate.
    assert first["attached_at"] == second["attached_at"]


def test_detach_skill(temp_db):
    skills.attach_skill_to_agent("agent1", "skillA")
    assert skills.detach_skill_from_agent("agent1", "skillA") is True
    # Second detach: nothing to remove.
    assert skills.detach_skill_from_agent("agent1", "skillA") is False


def test_get_agent_skills_handles_missing_on_disk(temp_db, temp_skills_dir):
    # Attached skill that no longer exists on disk should still be listed
    # with a clear placeholder description.
    skills.attach_skill_to_agent("agent1", "ghost-skill")
    result = skills.get_agent_skills("agent1")
    assert len(result) == 1
    assert result[0]["skill_name"] == "ghost-skill"
    assert "no longer available" in result[0]["description"]


def test_get_agent_skills_enriches_from_disk(temp_db, temp_skills_dir):
    write_skill(temp_skills_dir, "devops/real", "real-skill", "A real one")
    skills.attach_skill_to_agent("agent1", "real-skill")
    result = skills.get_agent_skills("agent1")
    assert result[0]["description"] == "A real one"
    assert result[0]["category"] == "devops"


def test_agent_skills_isolated_by_agent(temp_db):
    skills.attach_skill_to_agent("agent1", "skillA")
    skills.attach_skill_to_agent("agent2", "skillB")
    a1 = [s["skill_name"] for s in skills.get_agent_skills("agent1")]
    a2 = [s["skill_name"] for s in skills.get_agent_skills("agent2")]
    assert a1 == ["skillA"]
    assert a2 == ["skillB"]
