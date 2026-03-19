from sqlalchemy import Column, Integer, DateTime, func
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()  # type: ignore


class BaseModel(Base):  # type: ignore
    __abstract__ = True

    id = Column("Id", Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(
        "CreatedAt", DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column("UpdatedAt", DateTime(timezone=True), onupdate=func.now())
