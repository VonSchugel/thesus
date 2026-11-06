from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.history_order import history_order
from app.db.models import CheckpointORM


class CheckpointRepository:
    async def get(self, db: AsyncSession, checkpoint_id: str) -> CheckpointORM | None:
        return await db.get(CheckpointORM, checkpoint_id)

    async def list_checkpoints(self, db: AsyncSession, *, session_id: str, branch_id: str) -> list[CheckpointORM]:
        rows = await db.scalars(
            select(CheckpointORM)
            .where(CheckpointORM.session_id == session_id, CheckpointORM.branch_id == branch_id)
            .order_by(*history_order(db, CheckpointORM))
        )
        return list(rows)

    async def latest(self, db: AsyncSession, *, session_id: str, branch_id: str) -> CheckpointORM | None:
        return await db.scalar(select(CheckpointORM)
                               .where(CheckpointORM.session_id == session_id, CheckpointORM.branch_id == branch_id)
                               .order_by(*(column.desc() for column in history_order(db, CheckpointORM))).limit(1))

