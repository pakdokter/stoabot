from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from bot.config import settings
from loguru import logger

def _make_engine():
    url = settings.database_url
    kwargs = dict(pool_size=5, max_overflow=10, pool_pre_ping=True, echo=settings.debug)
    if "rlwy.net" in url:
        import ssl
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        kwargs["connect_args"] = {"ssl": ssl_ctx}
    return create_async_engine(url, **kwargs)

engine = _make_engine()
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

class Base(DeclarativeBase):
    pass

async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

async def init_db():
    from bot import models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")
