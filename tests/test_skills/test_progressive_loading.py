"""L0-L3 I/O boundaries, caching, security and independent package switches."""

from pathlib import Path
import builtins

import pytest

from researchx.config.settings import Settings
from researchx.skills.loader import BUNDLED_SKILLS_DIR
from researchx.prompts.context import _build_skills_section
from researchx.skills.loader import load_skill_registry, load_skills_from_dirs
from researchx.skills.metadata import cached_content_hash, content_hash
from researchx.tools.base import ToolExecutionContext, ToolRegistry
from researchx.tools.file_read_tool import FileReadTool, FileReadToolInput
from researchx.tools.skill_tool import SkillTool, SkillToolInput


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return tmp_path


def make_skill(root, name="example"):
    base = root / name
    base.mkdir(parents=True)
    (base / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Small description\n---\nBODY_{name}\n"
    )
    for directory in ("references", "scripts", "templates", "assets"):
        (base / directory).mkdir()
        (base / directory / "resource.txt").write_text("RESOURCE_BODY_" + directory)
    return base


def test_cold_discovery_reads_frontmatter_only_and_never_hashes_resources(isolated, monkeypatch):
    base = make_skill(isolated / "skills")
    (base / "SKILL.md").write_text("---\nname: example\ndescription: Tiny\n---\n" + "正文" * 500000)
    original_open = Path.open
    returned = []

    class HeaderOnly:
        def __init__(self, stream):
            self.stream = stream
            self.markers = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def readline(self, size=-1):
            assert self.markers < 2, "discovery read past the closing frontmatter marker"
            line = self.stream.readline(size)
            if line.strip() == b"---":
                self.markers += 1
            returned.append(line)
            return line

    def guarded_open(path, *args, **kwargs):
        if path == base / "SKILL.md":
            return HeaderOnly(original_open(path, *args, **kwargs))
        assert not path.is_relative_to(base), f"Unexpected resource read: {path}"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    skills = load_skills_from_dirs([base.parent])
    assert len(skills) == 1 and skills[0].content is None
    assert skills[0].metadata.content_hash == ""
    assert b"\xe6\xad\xa3" not in b"".join(returned)


@pytest.mark.asyncio
async def test_selection_reads_one_entry_and_single_reference_reads_no_siblings(
    isolated, monkeypatch
):
    root = isolated / "skills"
    base = make_skill(root)
    other = make_skill(root, "other")
    read = []
    original = Path.read_bytes
    original_text = Path.read_text

    def bytes_read(path, *args, **kwargs):
        read.append(path)
        return original(path, *args, **kwargs)

    def text_read(path, *args, **kwargs):
        read.append(path)
        return original_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", bytes_read)
    monkeypatch.setattr(Path, "read_text", text_read)
    context = ToolExecutionContext(
        cwd=isolated, metadata={"extra_skill_dirs": [root], "skill_settings": Settings()}
    )
    prompt = _build_skills_section(isolated, extra_skill_dirs=[root], settings=Settings())
    assert "BODY_" not in prompt and not any(p.is_relative_to(root) for p in read)
    result = await SkillTool().execute(SkillToolInput(name="example"), context)
    assert (
        not result.is_error
        and "BODY_example" in result.output
        and "BODY_other" not in result.output
    )
    assert [p for p in read if p.is_relative_to(root)] == [base / "SKILL.md"]
    assert str(other) not in result.output and "resource.txt" not in result.output
    read.clear()
    reference = base / "references/resource.txt"
    result = await FileReadTool().execute(FileReadToolInput(path=str(reference)), context)
    assert not result.is_error and "RESOURCE_BODY_references" in result.output
    assert [p for p in read if p.is_relative_to(root)] == [reference]


def test_prompt_size_independent_of_resource_content(isolated):
    root = isolated / "skills"
    base = make_skill(root)
    before = _build_skills_section(isolated, extra_skill_dirs=[root], settings=Settings())
    (base / "references/resource.txt").write_text("Huge resource" * 100000)
    (base / "SKILL.md").write_text((base / "SKILL.md").read_text() + "Huge entry body" * 100000)
    assert _build_skills_section(isolated, extra_skill_dirs=[root], settings=Settings()) == before


@pytest.mark.asyncio
async def test_navigation_does_not_execute_and_file_reader_rejects_escape(isolated):
    base = make_skill(isolated / "skills")
    marker = isolated / "executed"
    (base / "scripts/execute.py").write_text(f"open({str(marker)!r}, 'w').write('EXECUTED')")
    outside = isolated / "outside.txt"
    outside.write_text("SECRET_OUTSIDE")
    (base / "references/outside.txt").symlink_to(outside)
    (base / "references/outside-directory").symlink_to(isolated, target_is_directory=True)
    context = ToolExecutionContext(
        cwd=isolated, metadata={"extra_skill_dirs": [base.parent], "skill_settings": Settings()}
    )
    result = await SkillTool().execute(SkillToolInput(name="example"), context)
    assert not result.is_error and "execute.py" not in result.output and not marker.exists()
    for path in (
        base / "references/outside.txt",
        base / "references/outside-directory/outside.txt",
    ):
        result = await FileReadTool().execute(FileReadToolInput(path=str(path)), context)
        assert result.is_error and "SECRET_OUTSIDE" not in result.output
    (base / "SKILL.md").write_text(
        (base / "SKILL.md").read_text() + "\n[escape](references/outside.txt)\n"
    )
    result = await SkillTool().execute(SkillToolInput(name="example"), context)
    assert result.is_error and "escapes" in result.output


def test_hash_cache_invalidation_persistence_and_explicit_verification(isolated, monkeypatch):
    import researchx.skills.metadata as metadata

    base = make_skill(isolated / "skills")
    entry = base / "SKILL.md"
    assert cached_content_hash(entry) == ""
    original = Path.read_bytes
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    first = content_hash(entry)
    assert len(first) == 64 and len(reads) == 5
    reads.clear()
    metadata._MEMORY_CACHE.clear()
    assert content_hash(entry) == first and not reads
    assert load_skills_from_dirs([base])[0].metadata.content_hash == first and not reads
    resource = base / "references/resource.txt"
    resource.write_text("Changed resource")
    assert cached_content_hash(entry) == ""  # Never expose a stale digest.
    second = content_hash(entry)
    assert second != first and reads
    reads.clear()
    resource.rename(base / "references/renamed.txt")
    assert content_hash(entry) != second
    reads.clear()
    assert len(content_hash(entry, verify=True)) == 64 and reads


def test_hash_cache_write_failure_and_read_only_skill_root(isolated, monkeypatch):
    import researchx.skills.metadata as metadata

    base = make_skill(isolated / "skills")
    monkeypatch.setattr(
        metadata, "atomic_write_text", lambda *a, **kw: (_ for _ in ()).throw(PermissionError())
    )
    first = content_hash(base / "SKILL.md")
    assert len(first) == 64 and cached_content_hash(base / "SKILL.md") == first
    assert load_skills_from_dirs([base])[0].metadata.content_hash == first


def test_each_skill_can_be_disabled_without_hiding_other_skills(isolated):
    names = {s.name for s in load_skill_registry(isolated, settings=Settings()).list_skills() if s.source == "builtin"}
    assert len(names) == 8
    for name in names:
        registry = load_skill_registry(isolated, settings=Settings(enabled_skills={name: False}))
        assert registry.get(name) is None
        assert all(registry.get(other) for other in names - {name})


@pytest.mark.asyncio
async def test_missing_required_tool_checked_before_entry_read(isolated, monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *a, **kw: (
            (_ for _ in ()).throw(AssertionError("entry read before dependency check"))
            if str(a[0]).endswith("SKILL.md")
            else builtins.open(a[0]).read()
        ),
    )
    result = await SkillTool().execute(
        SkillToolInput(name="earnings-forecast"),
        ToolExecutionContext(
            cwd=isolated, metadata={"skill_settings": Settings(), "tool_registry": registry}
        ),
    )
    assert result.is_error and "requires unavailable tools" in result.output


def test_all_bundled_metadata_and_relative_resource_links(isolated):
    import re
    import yaml
    from researchx.skills.resources import resolve_resource

    fields = {
        "name",
        "description",
        "skill_id",
        "version",
        "owner",
        "permissions",
        "required_tools",
        "optional_tools",
        "compatible_models",
        "scope",
        "status",
        "published_at",
        "deprecation",
        "content_hash",
    }
    entries = list(BUNDLED_SKILLS_DIR.glob("*/SKILL.md"))
    assert len(entries) == 8
    for entry in entries:
        text = entry.read_text()
        header = yaml.safe_load(text.split("---", 2)[1])
        assert fields <= header.keys() and "author" not in header
        assert header["content_hash"] is None and header["status"] == "active"
        for link in re.findall(r"\]\(([^)]+)\)", text):
            assert resolve_resource(entry.parent, link).is_file()
        for section in ("输入要求", "主流程", "完成标准", "失败处理"):
            assert section in text
    assert not (BUNDLED_SKILLS_DIR / "skill-authoring").exists()


def test_metadata_discovery_does_not_execute_scripts(isolated):
    root = isolated / "skills"
    base = make_skill(root)
    marker = isolated / "tool-imported"
    (base / "scripts/fixture.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    registry = load_skill_registry(isolated, settings=Settings(), extra_skill_dirs=[root])
    assert registry.get("example").path == str(base / "SKILL.md")
    assert not marker.exists()


@pytest.mark.asyncio
async def test_selected_resource_access_rechecks_switches_and_workspace_boundary(isolated):
    from types import SimpleNamespace

    base = make_skill(isolated / "skills")

    class ConfinedContext(ToolExecutionContext):
        def resolve_path(self, candidate=None, *, write=False):
            raise ValueError("workspace confinement")

    shared = SimpleNamespace(tool_metadata={})
    context = ConfinedContext(
        cwd=isolated / "workspace",
        metadata={
            "extra_skill_dirs": [base.parent],
            "skill_settings": Settings(),
            "query_context": shared,
        },
    )
    context.cwd.mkdir()
    reader = FileReadTool()
    result = await reader.execute(
        FileReadToolInput(path=str(base / "references/resource.txt")), context
    )
    assert result.is_error
    result = await SkillTool().execute(SkillToolInput(name="example"), context)
    assert not result.is_error
    result = await reader.execute(
        FileReadToolInput(path=str(base / "references/resource.txt")), context
    )
    assert not result.is_error
    context.metadata["skill_settings"] = Settings(enabled_skills={"example": False})
    result = await reader.execute(
        FileReadToolInput(path=str(base / "references/resource.txt")), context
    )
    assert result.is_error and "disabled" in result.output


def test_hash_same_size_write_preserved_mtime_and_corrupt_cache(isolated):
    import os
    import researchx.skills.metadata as metadata

    base = make_skill(isolated / "skills")
    entry = base / "SKILL.md"
    resource = base / "references/resource.txt"
    first = content_hash(entry)
    stat = resource.stat()
    resource.write_text("X" * stat.st_size)
    os.utime(resource, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert cached_content_hash(entry) == "" and content_hash(entry) != first
    record = metadata._MEMORY_CACHE[str(entry)]
    record["hash"] = 123
    assert cached_content_hash(entry) == ""
    assert len(content_hash(entry)) == 64


def test_bundled_shared_report_code_participates_in_hash_invalidation(isolated, monkeypatch):
    import researchx.skills.metadata as metadata

    app = isolated / "researchx"
    bundled = app / "skills/bundled"
    base = make_skill(bundled, "industry-commentary")
    helper = app / "skills/_shared/reporting.py"
    helper.parent.mkdir()
    (helper.parent / "__init__.py").write_text("")
    (app / "skills/runtime_support.py").write_text("support_version = 1\n")
    helper.write_text("report_validation_version = 1\n")
    monkeypatch.setattr(metadata, "__file__", str(app / "skills/metadata.py"))
    first = content_hash(base / "SKILL.md")
    assert helper in metadata.hash_resources(base / "SKILL.md")
    helper.write_text("report_validation_version = 2\n")
    assert cached_content_hash(base / "SKILL.md") == ""
    assert content_hash(base / "SKILL.md") != first


def test_hash_supports_compatible_root_symlink_and_rejects_entry_escape(isolated):
    base = make_skill(isolated / "skills")
    first = content_hash(base / "SKILL.md")
    alias = isolated / "compatible-skills"
    alias.symlink_to(base.parent, target_is_directory=True)
    assert content_hash(alias / "example/SKILL.md") == first
    assert load_skills_from_dirs([alias])[0].metadata.content_hash == first
    outside = isolated / "outside.md"
    outside.write_text("OUTSIDE")
    (base / "SKILL.md").unlink()
    (base / "SKILL.md").symlink_to(outside)
    assert load_skills_from_dirs([base.parent]) == []
    with pytest.raises(ValueError, match="escapes"):
        content_hash(base / "SKILL.md")


@pytest.mark.asyncio
async def test_skill_and_selected_reference_results_mark_dynamic_context(isolated):
    base = make_skill(isolated / "skills")
    context = ToolExecutionContext(
        cwd=isolated, metadata={"extra_skill_dirs": [base.parent], "skill_settings": Settings()}
    )
    skill = await SkillTool().execute(SkillToolInput(name="example"), context)
    assert not skill.is_error and skill.metadata["context_component"] == "dynamic_context"
    reference = await FileReadTool().execute(
        FileReadToolInput(path=str(base / "references/resource.txt")), context
    )
    assert not reference.is_error and reference.metadata["context_component"] == "dynamic_context"
    ordinary = isolated / "ordinary.txt"
    ordinary.write_text("ordinary result")
    result = await FileReadTool().execute(FileReadToolInput(path=str(ordinary)), context)
    assert "context_component" not in result.metadata
