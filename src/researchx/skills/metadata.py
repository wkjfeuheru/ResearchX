"""Stable SHA-256 verification, with a best-effort fingerprint cache.

Discovery never reads resource bodies. A missing/invalid cache yields an empty
hash until explicit verification; it never returns an outdated content digest.
"""

from hashlib import sha256
from pathlib import Path
from typing_extensions import TypedDict
from pydantic import TypeAdapter
import json
import re
from researchx.config.paths import get_data_dir
from researchx.skills._frontmatter import parse_skill_metadata
from researchx.skills.resources import runtime_resources
from researchx.skills.types import SkillMetadata
from researchx.storage.filesystem import atomic_write_text


class HashRecord(TypedDict):
    version: int
    fingerprint: list[list[str | int]]
    hash: str


_CACHE_VERSION = 2
_MEMORY_CACHE: dict[str, HashRecord] = {}


def hash_resources(entrypoint: Path) -> list[Path]:
    """Include the Skill tree plus trusted bundled package code used by adapters.

    Dependencies are hash inputs only, never navigable paths or read grants.
    Generic framework dependencies keep their application-version boundary.
    """
    paths = runtime_resources(entrypoint)
    skill_root = Path(__file__).resolve().parent
    bundled = skill_root / "bundled"
    if not entrypoint.is_relative_to(bundled):
        return paths
    dependencies = [skill_root / "runtime_support.py"]
    name = entrypoint.parent.name
    if name in {"financial-commentary", "industry-commentary", "industry-deep-dive"}:
        dependencies.extend(
            (skill_root / "_shared/__init__.py", skill_root / "_shared/reporting.py")
        )
    if name in {"earnings-forecast", "deep-investment-report"}:
        dependencies.append(skill_root / "_shared/__init__.py")
        dependencies.extend(
            skill_root / "_shared/forecasting" / filename
            for filename in ("__init__.py", "models.py", "forecast.py")
        )
    from researchx.skills.resources import resolve_resource

    for path in dependencies:
        resolve_resource(skill_root, path)
        paths.append(path)
    return sorted(paths, key=lambda path: _hash_name(entrypoint, path))


def _hash_name(entrypoint: Path, path: Path) -> str:
    if path.is_relative_to(entrypoint.parent):
        return path.relative_to(entrypoint.parent).as_posix()
    return "@shared/" + path.relative_to(Path(__file__).resolve().parent).as_posix()


def _installed_index_record(entrypoint: Path) -> HashRecord | None:
    """Seed installed-wheel digests from the build index and standard wheel RECORD.

    Resource bytes are never read here. RECORD authenticates the index's per-file
    digests; stat timestamps detect files modified since installation. Subsequent
    discoveries use the full local fingerprint including inode/ctime.
    """
    from base64 import urlsafe_b64decode
    from importlib.metadata import distribution, PackageNotFoundError

    bundled = Path(__file__).resolve().parent
    index = bundled / "skill-index.json"
    if not entrypoint.is_relative_to(bundled) or not index.is_file():
        return None
    try:
        index_data = index.read_bytes()
        manifest = json.loads(index_data)
        if manifest.get("version") != _CACHE_VERSION:
            return None
        entry = manifest["skills"][entrypoint.relative_to(bundled).as_posix()]
        dist = distribution("researchx-ai")
        files = {str(file): file for file in dist.files or []}
        index_file = files["researchx/skills/skill-index.json"]
        if (
            not index_file.hash
            or index_file.hash.mode != "sha256"
            or urlsafe_b64decode(index_file.hash.value + "==").hex()
            != sha256(index_data).hexdigest()
        ):
            return None
        record_file = next(
            file for name, file in files.items() if name.endswith(".dist-info/RECORD")
        )
        installed = Path(str(dist.locate_file(record_file))).stat()
        paths = hash_resources(entrypoint)
        if {path.relative_to(bundled).as_posix() for path in paths} != set(entry["resources"]):
            return None
        for path in paths:
            relative = path.relative_to(bundled).as_posix()
            expected = entry["resources"][relative]
            recorded = files["researchx/skills/" + relative]
            if not recorded.hash or recorded.hash.mode != "sha256":
                return None
            if urlsafe_b64decode(recorded.hash.value + "==").hex() != expected["sha256"]:
                return None
            stat = path.stat()
            if (
                stat.st_size != expected["size"]
                or stat.st_mtime_ns > installed.st_mtime_ns
                or stat.st_ctime_ns > installed.st_ctime_ns
            ):
                return None
        digest = entry["hash"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            return None
        return {
            "version": _CACHE_VERSION,
            "fingerprint": _fingerprint(entrypoint, paths),
            "hash": digest,
        }
    except (OSError, ValueError, KeyError, TypeError, StopIteration, PackageNotFoundError):
        return None


def _fingerprint(entrypoint: Path, paths: list[Path]) -> list[list[str | int]]:
    result: list[list[str | int]] = []
    for path in paths:
        stat = path.stat()
        # ctime/inode also invalidate same-size writes and atomic replacements.
        result.append(
            [
                _hash_name(entrypoint, path),
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_ino,
                stat.st_dev,
            ]
        )
    return result


def _cache_path(entrypoint: Path) -> Path:
    key = sha256(str(entrypoint).encode()).hexdigest()
    return get_data_dir() / "skill-hashes" / f"{key}.json"


def _record(entrypoint: Path) -> HashRecord | None:
    key = str(entrypoint)
    if key in _MEMORY_CACHE:
        return _MEMORY_CACHE[key]
    try:
        value = json.loads(_cache_path(entrypoint).read_text(encoding="utf-8"))
        if isinstance(value, dict):
            validated = TypeAdapter(HashRecord).validate_python(value)
            _MEMORY_CACHE[key] = validated
            return validated
    except (OSError, ValueError):
        pass
    record = _installed_index_record(entrypoint)
    if record:
        _MEMORY_CACHE[key] = record
    return record


def cached_content_hash(entrypoint: Path) -> str:
    """Return only a still-valid digest, without reading entry/resource content."""
    entrypoint = entrypoint.parent.resolve() / entrypoint.name
    record = _record(entrypoint)
    if not record or record.get("version") != _CACHE_VERSION:
        return ""
    try:
        fingerprint = _fingerprint(entrypoint, hash_resources(entrypoint))
    except (OSError, ValueError):
        return ""
    digest = record.get("hash", "")
    if (
        record.get("fingerprint") == fingerprint
        and isinstance(digest, str)
        and re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        return digest
    return ""


def content_hash(entrypoint: Path, *, verify: bool = False, cache: bool = True) -> str:
    """Verify runtime content. Reuse SHA-256 only when the stat fingerprint matches.

    Stat values are invalidation hints, never the content hash itself. Explicit
    verify=True bypasses the cache (including changes preserving filesystem stats).
    """
    entrypoint = entrypoint.parent.resolve() / entrypoint.name
    if cache and not verify and (cached := cached_content_hash(entrypoint)):
        return cached
    paths = hash_resources(entrypoint)
    before = _fingerprint(entrypoint, paths)
    digest = sha256()
    for path in paths:
        data = path.read_bytes()
        if path.suffix == ".md":
            text = data.decode("utf-8")
            if text.startswith("---\n") and (end := text.find("\n---\n", 4)) != -1:
                frontmatter = parse_skill_metadata(path.stem, text)["frontmatter"]
                frontmatter.pop("content_hash", None)
                if isinstance(frontmatter.get("metadata"), dict):
                    frontmatter["metadata"].pop("content_hash", None)
                data = (
                    json.dumps(frontmatter, sort_keys=True, ensure_ascii=False, default=str)
                    + text[end + 5 :]
                ).encode()
        for value in (_hash_name(entrypoint, path).encode(), data):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    # Never publish a cache for a tree changing during verification.
    if before != _fingerprint(entrypoint, hash_resources(entrypoint)):
        raise ValueError("Skill resources changed during hash verification; retry")
    record: HashRecord = {
        "version": _CACHE_VERSION,
        "fingerprint": before,
        "hash": digest.hexdigest(),
    }
    if not cache:
        return record["hash"]
    _MEMORY_CACHE[str(entrypoint)] = record
    try:
        cache_file = _cache_path(entrypoint)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cache_file, json.dumps(record))
    except OSError:
        pass  # Read-only installs/config dirs still work via the process cache.
    return record["hash"]


def read_metadata(name: str, frontmatter: dict[str, object], entrypoint: Path) -> SkillMetadata:
    nested = frontmatter.get("metadata")
    values = dict(nested) if isinstance(nested, dict) else {}
    values.update(
        {key: frontmatter[key] for key in SkillMetadata.model_fields if key in frontmatter}
    )
    values.setdefault("skill_id", name)
    if hasattr(values.get("published_at"), "isoformat"):
        values["published_at"] = getattr(values["published_at"], "isoformat")()
    values["content_hash"] = cached_content_hash(entrypoint)
    return SkillMetadata.model_validate(values)


def main() -> None:
    """Explicit install/development verification: python -m researchx.skills.metadata ROOT."""
    import argparse

    parser = argparse.ArgumentParser(description="Verify Skill SHA-256 hashes and refresh caches")
    parser.add_argument("root", type=Path)
    parser.add_argument("--verify", action="store_true", help="Bypass matching fingerprint caches")
    args = parser.parse_args()
    entries = [args.root] if args.root.is_file() else sorted(args.root.glob("bundled/*/SKILL.md"))
    if not entries and args.root.is_dir():
        entries = sorted(args.root.glob("skills/*/SKILL.md"))
    if not entries and args.root.is_dir():
        entries = sorted(args.root.glob("*/SKILL.md"))
        if (args.root / "SKILL.md").is_file():
            entries = [args.root / "SKILL.md"]
    print(
        json.dumps(
            {str(path): content_hash(path, verify=args.verify) for path in entries},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
