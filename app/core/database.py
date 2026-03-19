import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.models.base import Base

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,                 # Never log every SQL statement in production
    pool_size=10,               # Persistent connections kept open
    max_overflow=20,            # Extra connections allowed under burst load
    pool_timeout=30,            # Seconds to wait for a free connection before raising
    pool_recycle=1800,          # Recycle connections after 30 min (avoids stale TCP drops)
    pool_pre_ping=True,         # Test connection health before use — prevents "lost connection" crashes
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    Base.metadata.create_all(bind=engine)
