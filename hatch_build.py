"""Bundle the optional web UI when it has been built; keep CLI installs independent."""

from __future__ import annotations
from typing import Any
from hatchling.builders.config import BuilderConfig
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
from hashlib import sha256

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface[BuilderConfig]):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        web_dist = Path(self.root) / "frontend" / "web" / "dist"
        if web_dist.is_dir():
            build_data["force_include"][str(web_dist)] = "researchx/_web"
        if self.target_name == "wheel":
            # Hashing belongs to build/install verification, never registry discovery.
            sys.path.insert(0, str(Path(self.root) / "src"))
            from researchx.skills.metadata import content_hash, hash_resources

            bundled = Path(self.root) / "src/researchx/skills"
            entries = {}
            for entry in sorted(bundled.glob("bundled/*/SKILL.md")):
                entries[entry.relative_to(bundled).as_posix()] = {
                    "hash": content_hash(entry, verify=True, cache=False),
                    "resources": {
                        path.relative_to(bundled).as_posix(): {
                            "size": path.stat().st_size,
                            "sha256": sha256(path.read_bytes()).hexdigest(),
                        }
                        for path in hash_resources(entry)
                    },
                }
            self._skill_index = TemporaryDirectory(prefix="researchx-skill-index-")
            index = Path(self._skill_index.name) / "skill-index.json"
            index.write_text(json.dumps({"version": 2, "skills": entries}, sort_keys=True))
            build_data["force_include"][str(index)] = "researchx/skills/skill-index.json"

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        if hasattr(self, "_skill_index"):
            self._skill_index.cleanup()
