from __future__ import annotations

import hashlib
import math
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models import AuditEventModel, KnowledgeChunkModel, TaskModel
from app.schemas import AuditRecord, KnowledgeChunk, KnowledgeChunkCreate, RobotId, TaskRecord, TaskStatus

if TYPE_CHECKING:
    from app.cache import CacheManager


class TaskStore:
    """ORM repository with one bounded transaction per mutation and optional Redis caching."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], database_url: str, cache: CacheManager | None = None) -> None:
        self._sessions = sessions
        self._is_sqlite = database_url.startswith("sqlite")
        self._cache = cache

    async def create(
        self,
        pickup: str,
        destination: str,
        priority: int,
        assigned_robot_id: RobotId | None,
        payload_kg: float = 150.0,
        payload_size: str = "medium",
        urgency: str = "standard",
    ) -> TaskRecord:
        status: TaskStatus = "Assigned" if assigned_robot_id else "Queued"
        model = TaskModel(
            id=f"TASK-{uuid.uuid4().hex[:8].upper()}",
            pickup=pickup,
            destination=destination,
            priority=priority,
            status=status,
            payload_kg=payload_kg,
            payload_size=payload_size,
            urgency=urgency,
            assigned_robot_id=assigned_robot_id,
        )
        async with self._sessions.begin() as session:
            session.add(model)
            await session.flush()
        if self._cache:
            await self._cache.delete("edgefleet:tasks:list")
        return self._task_record(model)

    async def update(
        self,
        task_id: str,
        priority: int | None = None,
        payload_kg: float | None = None,
        payload_size: str | None = None,
        urgency: str | None = None,
        assigned_robot_id: RobotId | None = None,
        status: TaskStatus | None = None,
        unassign: bool = False,
        pickup: str | None = None,
        destination: str | None = None,
    ) -> TaskRecord | None:
        async with self._sessions.begin() as session:
            model = await session.get(TaskModel, task_id, with_for_update=not self._is_sqlite)
            if model is None:
                return None
            if unassign:
                model.assigned_robot_id = None
            if pickup is not None:
                model.pickup = pickup
            if destination is not None:
                model.destination = destination
            if priority is not None:
                model.priority = priority
            if payload_kg is not None:
                model.payload_kg = payload_kg
            if payload_size is not None:
                model.payload_size = payload_size
            if urgency is not None:
                model.urgency = urgency
            if assigned_robot_id is not None:
                model.assigned_robot_id = assigned_robot_id
            if status is not None:
                model.status = status
            await session.flush()
        if self._cache:
            await self._cache.delete("edgefleet:tasks:list")
        return self._task_record(model)

    async def delete(self, task_id: str) -> bool:
        async with self._sessions.begin() as session:
            model = await session.get(TaskModel, task_id, with_for_update=not self._is_sqlite)
            if model is None:
                return False
            await session.delete(model)
            await session.flush()
        if self._cache:
            await self._cache.delete("edgefleet:tasks:list")
        return True

    async def list(self) -> list[TaskRecord]:
        if self._cache:
            cached = await self._cache.get_json("edgefleet:tasks:list")
            if cached is not None:
                return [TaskRecord.model_validate(item) for item in cached]
        async with self._sessions() as session:
            models = (await session.scalars(select(TaskModel).order_by(TaskModel.created_at.desc()))).all()
        records = [self._task_record(model) for model in models]
        if self._cache:
            await self._cache.set_json("edgefleet:tasks:list", [r.model_dump(mode="json") for r in records], ttl=60)
        return records

    async def complete(self, task_id: str) -> TaskRecord | None:
        async with self._sessions.begin() as session:
            model = await session.get(TaskModel, task_id, with_for_update=not self._is_sqlite)
            if model is None:
                return None
            model.status = "Completed"
            await session.flush()
        if self._cache:
            await self._cache.delete("edgefleet:tasks:list")
        return self._task_record(model)

    async def requeue_orphans(self) -> int:
        """Robots do not survive a restart, so tasks they were executing go back to the queue."""
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(TaskModel).where(TaskModel.status.in_(("Assigned", "In Progress"))).values(status="Queued", assigned_robot_id=None)
            )
        if self._cache:
            await self._cache.delete("edgefleet:tasks:list")
        return result.rowcount or 0

    async def count_completed(self) -> int:
        async with self._sessions() as session:
            return int(await session.scalar(select(func.count()).select_from(TaskModel).where(TaskModel.status == "Completed")) or 0)

    async def knowledge_count(self) -> int:
        async with self._sessions() as session:
            return int(await session.scalar(select(func.count()).select_from(KnowledgeChunkModel)) or 0)

    async def add_audit(self, rows: list[dict]) -> None:
        async with self._sessions.begin() as session:
            session.add_all([AuditEventModel(**row) for row in rows])

    async def list_audit(self, limit: int, category: str | None = None) -> list[AuditRecord]:
        statement = select(AuditEventModel).order_by(AuditEventModel.id.desc()).limit(limit)
        if category:
            statement = statement.where(AuditEventModel.category == category)
        async with self._sessions() as session:
            models = (await session.scalars(statement)).all()
        return [
            AuditRecord(id=m.id, created_at=m.created_at, actor=m.actor, category=m.category, event_type=m.event_type, sim_time=m.sim_time, message=m.message)
            for m in models
        ]

    async def add_knowledge(self, item: KnowledgeChunkCreate) -> KnowledgeChunk:
        model = KnowledgeChunkModel(id=uuid.uuid4(), source_name=item.source_name, content=item.content, metadata_json=item.metadata, embedding=item.embedding)
        async with self._sessions.begin() as session:
            session.add(model)
            await session.flush()
        if self._cache:
            await self._cache.delete_pattern("edgefleet:knowledge:search:*")
        return self._knowledge_record(model)

    async def search_knowledge(self, embedding: list[float], limit: int) -> list[KnowledgeChunk]:
        cache_key = None
        if self._cache:
            embed_hash = hashlib.sha256(str(embedding[:16]).encode()).hexdigest()[:12]
            cache_key = f"edgefleet:knowledge:search:{embed_hash}:{limit}"
            cached = await self._cache.get_json(cache_key)
            if cached is not None:
                return [KnowledgeChunk.model_validate(item) for item in cached]

        async with self._sessions() as session:
            if self._is_sqlite:
                models = list((await session.scalars(select(KnowledgeChunkModel))).all())
                scored = sorted(((self._cosine(embedding, list(model.embedding)), model) for model in models), key=lambda pair: -pair[0])[:limit]
            else:
                distance = KnowledgeChunkModel.embedding.cosine_distance(embedding)
                statement = select(KnowledgeChunkModel, distance).order_by(distance).limit(limit)
                scored = [(1.0 - float(d), model) for model, d in (await session.execute(statement)).all()]
        records = [self._knowledge_record(model).model_copy(update={"similarity": round(score, 4)}) for score, model in scored]
        if self._cache and cache_key:
            await self._cache.set_json(cache_key, [r.model_dump(mode="json") for r in records], ttl=300)
        return records

    @staticmethod
    def _task_record(model: TaskModel) -> TaskRecord:
        return TaskRecord(
            id=model.id,
            pickup=model.pickup,
            destination=model.destination,
            priority=model.priority,
            status=model.status,
            payload_kg=getattr(model, "payload_kg", 150.0),
            payload_size=getattr(model, "payload_size", "medium"),
            urgency=getattr(model, "urgency", "standard"),
            assigned_robot_id=model.assigned_robot_id,
            created_at=model.created_at,
        )

    @staticmethod
    def _knowledge_record(model: KnowledgeChunkModel) -> KnowledgeChunk:
        return KnowledgeChunk(id=str(model.id), source_name=model.source_name, content=model.content, metadata=model.metadata_json, embedding=list(model.embedding), created_at=model.created_at)

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        denominator = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
        return sum(x * y for x, y in zip(a, b, strict=True)) / denominator if denominator else 0.0
