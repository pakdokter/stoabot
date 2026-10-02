from decimal import Decimal
from datetime import date
from typing import Optional
from sqlalchemy import select, func, and_, case
from sqlalchemy.ext.asyncio import AsyncSession
from bot.models import Transaction


async def get_running_balance(
    session: AsyncSession,
    user_id: Optional[int] = None,
) -> Decimal:
    """Hitung saldo transaksi. Jika user_id diberikan, filter per user."""
    conditions = [Transaction.is_deleted == False]
    if user_id is not None:
        conditions.append(Transaction.user_id == user_id)

    result = await session.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (Transaction.type == "masuk", Transaction.amount),
                        else_=-Transaction.amount
                    )
                ),
                0
            )
        ).where(and_(*conditions))
    )
    return result.scalar_one()


async def get_summary(
    session: AsyncSession,
    user_id: Optional[int] = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Ringkasan per user untuk rentang tanggal tertentu."""
    conditions = [Transaction.is_deleted == False]
    if user_id is not None:
        conditions.append(Transaction.user_id == user_id)
    if date_from:
        conditions.append(Transaction.transaction_date >= date_from)
    if date_to:
        conditions.append(Transaction.transaction_date <= date_to)

    result = await session.execute(
        select(
            func.coalesce(
                func.sum(
                    case((Transaction.type == "masuk", Transaction.amount), else_=0)
                ), 0
            ).label("total_masuk"),
            func.coalesce(
                func.sum(
                    case((Transaction.type == "keluar", Transaction.amount), else_=0)
                ), 0
            ).label("total_keluar"),
            func.count(Transaction.id).label("jumlah"),
        ).where(and_(*conditions))
    )
    row = result.one()
    return {
        "total_masuk": Decimal(str(row.total_masuk)),
        "total_keluar": Decimal(str(row.total_keluar)),
        "saldo": Decimal(str(row.total_masuk)) - Decimal(str(row.total_keluar)),
        "jumlah": row.jumlah,
    }
