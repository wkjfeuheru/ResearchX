"""Frozen internal deliveries and human review, outside agent tool authority."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from researchx.contracts.research import quality_from_coverage
from researchx.state.business_coverage import Profile, classify_request, evaluate_coverage
from researchx.state.errors import ResearchError
from researchx.state.models import ResearchMemory, SourceUsage, new_id
from researchx.state.source_policy import may_export
from researchx.storage.content import persist_object, read_object
from researchx.storage.database import current_database
from researchx.storage.research_records import scope
from researchx.storage.schema import collection_attempts, deliverables, review_decisions


@dataclass(frozen=True)
class TrustedReviewer:
    """Only a trusted server identity adapter may construct and pass this principal."""

    actor_id: str
    role: Literal["researcher", "compliance"]
    issuer: str


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def dependency_snapshot(
    memory: ResearchMemory,
    evidence_ids: list[str],
    artifact_id: str | None = None,
    answer_id: str | None = None,
    direct_source_ids: list[str] | None = None,
) -> dict[str, object]:
    evidence = {
        key: memory.evidence_pool[key].model_dump(mode="json")
        for key in sorted(set(evidence_ids))
        if key in memory.evidence_pool
    }
    source_ids = {str(item["source_id"]) for item in evidence.values()} | set(
        direct_source_ids or []
    )
    sources = {
        key: memory.sources[key].model_dump(mode="json")
        for key in sorted(source_ids)
        if key in memory.sources
    }
    artifact = memory.artifacts.get(artifact_id or "")
    plan = memory.plans.get(memory.research_state.current_plan_id or "")
    return {
        "evidence": evidence,
        "sources": sources,
        "artifact": artifact.model_dump(mode="json") if artifact else None,
        "answer": memory.answers.get(answer_id or ""),
        "superseding_evidence": sorted(
            item.id for item in memory.evidence_pool.values() if item.supersedes in evidence
        ),
        "source_corrections": sorted(
            item.id for item in memory.sources.values() if item.corrects_source_id in source_ids
        ),
        "reasoning": {
            key: item.model_dump(mode="json")
            for key, item in memory.reasoning_chain.items()
            if set(item.evidence_ids) & set(evidence)
        },
        "plan_assumptions": list(plan.assumptions) if plan else [],
    }


def dependencies_current(memory: ResearchMemory, manifest: dict[str, object]) -> bool:
    captured = manifest.get("dependencies")
    if not isinstance(captured, dict):
        return False
    evidence = captured.get("evidence", {})
    if not isinstance(evidence, dict):
        return False
    artifact = captured.get("artifact")
    artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
    answer = captured.get("answer")
    answer_id = answer.get("answer_id") if isinstance(answer, dict) else None
    sources = captured.get("sources", {})
    return captured == dependency_snapshot(
        memory,
        list(evidence),
        artifact_id,
        answer_id,
        list(sources) if isinstance(sources, dict) else [],
    )


class DeliveryService:
    def __init__(self, store: object) -> None:
        from researchx.state.store import ResearchStore

        if not isinstance(store, ResearchStore):
            raise TypeError("DeliveryService requires ResearchStore")
        self.store = store

    async def classify_source(
        self, source_id: str, usage: SourceUsage, *, principal: TrustedReviewer | None
    ) -> None:
        local = (
            principal is not None
            and principal.issuer == "local_workstation"
            and principal.role == "researcher"
        )
        institution = (
            principal is not None
            and principal.issuer == "trusted_identity_provider"
            and principal.role == "compliance"
        )
        if principal is None or not (local or institution):
            raise ResearchError("来源授权分类需要本机研究员或机构合规身份显式确认")
        if usage.category == "unclassified":
            raise ResearchError("Classification must provide a usable source category")
        if usage.category in {"licensed", "internal"} and usage.licence_status != "confirmed":
            if usage.model_use_allowed or usage.internal_export_allowed:
                raise ResearchError("Restricted source needs confirmed permission before use")
        async with self.store.transaction():
            memory = await self.store._load()
            source = memory.sources.get(source_id)
            if source is None:
                raise ResearchError("Unknown source")
            source.usage = usage
            await self.store._save(
                memory,
                "classify_source",
                {
                    "source_id": source_id,
                    "actor_id": principal.actor_id,
                    "category": usage.category,
                    "licence_status": usage.licence_status,
                },
            )

    async def freeze_answer(
        self, answer_id: str, user_request: str, *, profile: Profile | None = None
    ) -> dict[str, object] | None:
        profile = profile or classify_request(user_request)
        if profile == "other":
            return None
        async with self.store.transaction() as db:
            memory = await self.store._load()
            answer = memory.answers.get(answer_id)
            if answer is None:
                raise ResearchError("Unknown answer")
            evidence_ids = list(answer["citations"])
            return await self._freeze(
                db,
                memory,
                "answer",
                answer_id,
                answer["rendered"],
                evidence_ids,
                profile,
                user_request,
                bool(answer["invalid"]),
            )

    async def freeze_report(
        self, artifact_id: str, user_request: str, *, profile: Profile | None = None
    ) -> dict[str, object]:
        profile = profile or classify_request(user_request)
        async with self.store.transaction() as db:
            memory = await self.store._load()
            artifact = memory.artifacts.get(artifact_id)
            if artifact is None or artifact.kind != "report_draft":
                raise ResearchError("Report draft does not exist")
            body = await self.store.read_content(artifact.content_hash)
            if hashlib.sha256(body.encode()).hexdigest() != artifact.content_hash:
                raise ResearchError("Report content hash mismatch")
            evidence_ids = list(
                dict.fromkeys(
                    artifact.evidence_ids
                    + artifact.assumption_evidence_ids
                    + [
                        key
                        for finding_id in artifact.finding_ids
                        for key in memory.conclusions[finding_id].evidence_ids
                    ]
                )
            )
            from researchx.state.completion import CompletionPolicy, ResearchContext

            checks = await CompletionPolicy().artifact_checks(
                artifact, ResearchContext(memory, self.store)
            )
            failed_machine_check = any(item.blocking and not item.passed for item in checks)
            return await self._freeze(
                db,
                memory,
                "report_draft",
                artifact_id,
                body,
                evidence_ids,
                profile,
                user_request,
                failed_machine_check,
            )

    async def freeze_file(
        self, file_id: str, body: bytes, *, evidence_ids: list[str], source_ids: list[str]
    ) -> dict[str, object]:
        """Freeze a host-registered Skill report, including binary DOCX/XLSX exports."""
        async with self.store.transaction() as db:
            memory = await self.store._load()
            requests = sorted(
                (
                    item
                    for item in memory.sources.values()
                    if item.kind == "user" and item.locator.startswith("message:")
                ),
                key=lambda item: item.collected_at,
                reverse=True,
            )
            request = ""
            profile: Profile = "ambiguous"
            for source in requests:
                text = await self.store.read_content(source.content_hash)
                inferred = classify_request(text)
                if inferred in {"ambiguous", "other"} and source.request_profile:
                    inferred = source.request_profile
                if inferred != "other":
                    request, profile = text, inferred
                    break
            return await self._freeze(
                db,
                memory,
                "report_file",
                file_id,
                body,
                evidence_ids,
                profile,
                request,
                False,
                direct_source_ids=source_ids,
            )

    async def _freeze(
        self,
        db: object,
        memory: ResearchMemory,
        kind: str,
        origin_id: str,
        body: str | bytes,
        evidence_ids: list[str],
        profile: Profile,
        request: str,
        invalid_citation: bool,
        direct_source_ids: list[str] | None = None,
    ) -> dict[str, object]:
        from sqlalchemy.ext.asyncio import AsyncSession

        assert isinstance(db, AsyncSession)
        content = body.encode() if isinstance(body, str) else body
        existing = (
            (
                await db.execute(
                    select(deliverables).where(
                        scope(deliverables, self.store.workspace_id, self.store.session_id),
                        deliverables.c.kind == kind,
                        deliverables.c.origin_id == origin_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing:
            if (
                existing["content_hash"] != hashlib.sha256(content).hexdigest()
                or existing["manifest"]["request_hash"] != _digest(request)
                or existing["profile"] != profile
                or not dependencies_current(memory, existing["manifest"])
            ):
                raise ResearchError("Frozen delivery origin cannot be replaced")
            return self._public(existing)
        attempts = (
            (
                await db.execute(
                    select(collection_attempts).where(
                        scope(collection_attempts, self.store.workspace_id, self.store.session_id)
                    )
                )
            )
            .mappings()
            .all()
        )
        # Coverage belongs to this frozen delivery, not to every earlier task
        # in the session. Otherwise unrelated old evidence can fill its gaps.
        coverage_memory = memory.model_copy(
            update={
                "evidence_pool": {
                    key: value for key, value in memory.evidence_pool.items() if key in evidence_ids
                }
            }
        )
        coverage = evaluate_coverage(
            coverage_memory,
            profile,
            request,
            attempts=[
                {
                    key: row[key]
                    for key in (
                        "channel",
                        "scope",
                        "outcome",
                        "errors",
                        "window_start",
                        "window_end",
                        "created_at",
                    )
                }
                for row in attempts
            ],
        )
        dependencies = dependency_snapshot(
            memory,
            evidence_ids,
            origin_id if kind == "report_draft" else None,
            origin_id if kind == "answer" else None,
            direct_source_ids,
        )
        referenced = dependencies["sources"]
        assert isinstance(referenced, dict)
        unclassified = [
            key
            for key, raw in referenced.items()
            if SourceUsage.model_validate(raw.get("usage", {})).category == "unclassified"
        ]
        restricted = [
            key
            for key, raw in referenced.items()
            if not SourceUsage.model_validate(raw.get("usage", {})).model_use_allowed
        ]
        if unclassified:
            coverage.gaps.append("历史或未知资料须重新分类：" + ", ".join(unclassified))
        if restricted:
            coverage.gaps.append("资料使用权限不足：" + ", ".join(restricted))
        captured_evidence = dependencies["evidence"]
        assert isinstance(captured_evidence, dict)
        if any(
            memory.evidence_pool[key].status not in {"source_checked", "verified"}
            or memory.evidence_pool[key].needs_review
            or any(item.supersedes == key for item in memory.evidence_pool.values())
            for key in captured_evidence
        ):
            coverage.gaps.append("交付物引用了待核对、已更正或待复核的证据")
        if invalid_citation or not evidence_ids or len(captured_evidence) != len(set(evidence_ids)):
            coverage.gaps.append("引用缺失或不可核验")
        if coverage.gaps and coverage.status == "complete":
            coverage.status = "partial"
        if not evidence_ids or profile == "ambiguous":
            coverage.status = "blocked"
        quality = quality_from_coverage(
            f"{kind}:{origin_id}@1",
            status=coverage.status,
            gaps=list(coverage.gaps),
        )
        content_ref = await persist_object(current_database(), self.store.workspace_id, content)
        content_hash = content_ref.removeprefix("sha256:")
        manifest = {
            "content_ref": content_ref,
            "content_hash": content_hash,
            "dependencies": dependencies,
            "coverage": coverage.model_dump(mode="json"),
            "quality": quality.model_dump(mode="json"),
            "request_hash": _digest(request),
        }
        manifest_hash = _digest(manifest)
        existing = (
            (
                await db.execute(
                    select(deliverables).where(
                        scope(deliverables, self.store.workspace_id, self.store.session_id),
                        deliverables.c.kind == kind,
                        deliverables.c.origin_id == origin_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing:
            if existing["manifest_hash"] != manifest_hash:
                raise ResearchError("Frozen delivery origin cannot be replaced")
            return self._public(existing)
        timestamp = time.time()
        row = {
            "workspace_id": self.store.workspace_id,
            "session_id": self.store.session_id,
            "delivery_id": new_id("delivery"),
            "kind": kind,
            "origin_id": origin_id,
            "content_hash": content_hash,
            "manifest_hash": manifest_hash,
            "status": "awaiting_review" if quality.completeness == "complete" else "blocked",
            "profile": profile,
            "as_of": coverage.as_of,
            "coverage": coverage.model_dump(mode="json"),
            "manifest": manifest,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        await db.execute(insert(deliverables).values(**row))
        return self._public(row)

    @staticmethod
    def _public(row: object) -> dict[str, object]:
        from collections.abc import Mapping

        assert isinstance(row, Mapping)
        public = {
            key: row[key]
            for key in (
                "delivery_id",
                "kind",
                "origin_id",
                "content_hash",
                "manifest_hash",
                "status",
                "profile",
                "as_of",
                "coverage",
                "created_at",
                "updated_at",
            )
        }
        manifest = row.get("manifest")
        if isinstance(manifest, dict) and isinstance(manifest.get("quality"), dict):
            public["quality"] = manifest["quality"]
        return public

    async def list(self) -> list[dict[str, object]]:
        async with self.store.transaction() as db:
            memory = await self.store._load()
            rows = (
                (
                    await db.execute(
                        select(deliverables)
                        .where(scope(deliverables, self.store.workspace_id, self.store.session_id))
                        .order_by(deliverables.c.created_at)
                    )
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows:
                if row["status"] in {"approved", "awaiting_review"} and not dependencies_current(
                    memory, row["manifest"]
                ):
                    await db.execute(
                        update(deliverables)
                        .where(
                            scope(deliverables, self.store.workspace_id, self.store.session_id),
                            deliverables.c.delivery_id == row["delivery_id"],
                        )
                        .values(status="invalidated", updated_at=time.time())
                    )
                    items.append({**self._public(row), "status": "invalidated"})
                else:
                    items.append(self._public(row))
            return items

    async def decide(
        self,
        delivery_id: str,
        decision: Literal["approve", "changes_requested"],
        note: str,
        *,
        principal: TrustedReviewer | None,
        expected_manifest_hash: str,
        reviewed_claim_ids: Sequence[str] | None = None,
    ) -> dict[str, object]:
        if (
            principal is None
            or principal.issuer not in {"trusted_identity_provider", "local_workstation"}
            or not principal.actor_id
        ):
            raise ResearchError("可信审核人身份未接入；不能形成有效人工批准")
        if principal.role != "researcher":
            raise ResearchError("内部草稿须由研究员复核")
        if not note.strip():
            raise ResearchError("复核须填写意见")
        async with self.store.transaction() as db:
            row = (
                (
                    await db.execute(
                        select(deliverables)
                        .where(
                            scope(deliverables, self.store.workspace_id, self.store.session_id),
                            deliverables.c.delivery_id == delivery_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ResearchError("Unknown delivery")
            if row["status"] != "awaiting_review" or row["manifest_hash"] != expected_manifest_hash:
                raise ResearchError("Delivery changed or is not reviewable")
            memory = await self.store._load()
            if not dependencies_current(memory, row["manifest"]):
                raise ResearchError("Delivery evidence or source changed; freeze a new version")
            if row["coverage"]["status"] != "complete" or (
                isinstance(row["manifest"], dict)
                and isinstance(row["manifest"].get("quality"), dict)
                and row["manifest"]["quality"].get("completeness") not in {None, "complete"}
            ):
                raise ResearchError("Incomplete coverage cannot pass human review")
            content = await read_object(
                current_database(), self.store.workspace_id, row["manifest"]["content_ref"]
            )
            if hashlib.sha256(content).hexdigest() != row["content_hash"]:
                raise ResearchError("Frozen content no longer matches its hash")
            status = "approved" if decision == "approve" else "changes_requested"
            await db.execute(
                insert(review_decisions).values(
                    workspace_id=self.store.workspace_id,
                    session_id=self.store.session_id,
                    decision_id=new_id("review"),
                    delivery_id=delivery_id,
                    actor_id=principal.actor_id,
                    actor_role=principal.role,
                    decision=decision,
                    note=note.strip(),
                    manifest_hash=expected_manifest_hash,
                    reviewed_claim_ids=list(reviewed_claim_ids or []),
                    created_at=time.time(),
                )
            )
            await db.execute(
                update(deliverables)
                .where(
                    scope(deliverables, self.store.workspace_id, self.store.session_id),
                    deliverables.c.delivery_id == delivery_id,
                )
                .values(status=status, updated_at=time.time())
            )
            return {**self._public(row), "status": status}

    async def export_text(self, delivery_id: str) -> bytes:
        async with self.store.transaction() as db:
            row = (
                (
                    await db.execute(
                        select(deliverables).where(
                            scope(deliverables, self.store.workspace_id, self.store.session_id),
                            deliverables.c.delivery_id == delivery_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ResearchError("Unknown delivery")
            memory = await self.store._load()
            source_ids = row["manifest"]["dependencies"]["sources"]
            if any(
                key not in memory.sources or not may_export(memory.sources[key].usage)
                for key in source_ids
            ):
                raise ResearchError("Source licence does not allow export")
            if row["status"] == "invalidated" or not dependencies_current(memory, row["manifest"]):
                raise ResearchError("Frozen delivery is no longer current")
            return await read_object(
                current_database(), self.store.workspace_id, row["manifest"]["content_ref"]
            )
