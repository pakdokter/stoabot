import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from bot.models import AuditLog, Transaction


def _tx_to_dict(tx: Transaction) -> dict:
    return {
        "id": str(tx.id),
        "type": tx.type,
        "amount": float(tx.amount),
        "description": tx.description,
        "category": tx.category,
        "transaction_date": str(tx.transaction_date),
    }


async def log_create(session: AsyncSession, user_id: int, tx: Transaction):
    session.add(AuditLog(
        user_id=user_id,
        action="create",
        table_name="transactions",
        record_id=tx.id,
        old_values=None,
        new_values=_tx_to_dict(tx),
    ))


async def log_update(session: AsyncSession, user_id: int, old: dict, tx: Transaction):
    session.add(AuditLog(
        user_id=user_id,
        action="update",
        table_name="transactions",
        record_id=tx.id,
        old_values=old,
        new_values=_tx_to_dict(tx),
    ))


async def log_delete(session: AsyncSession, user_id: int, tx: Transaction):
    session.add(AuditLog(
        user_id=user_id,
        action="delete",
        table_name="transactions",
        record_id=tx.id,
        old_values=_tx_to_dict(tx),
        new_values=None,
    ))
