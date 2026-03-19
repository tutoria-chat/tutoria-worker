"""
ProviderKey Model - AI provider API key management

Stores encrypted API keys for AI providers (OpenAI, Anthropic, Bedrock)
with round-robin rotation, failure tracking, and cooldown support.
"""
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, CheckConstraint
from .base import BaseModel


class ProviderKey(BaseModel):
    __tablename__ = "ProviderKeys"

    provider = Column("Provider", String(50), nullable=False)
    key_name = Column("KeyName", String(100), nullable=False)
    encrypted_api_key = Column("EncryptedApiKey", Text, nullable=False)
    region = Column("Region", String(50), nullable=True)
    priority = Column("Priority", Integer, default=0, nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False)
    last_used_at = Column("LastUsedAt", DateTime(timezone=True), nullable=True)
    failure_count = Column("FailureCount", Integer, default=0, nullable=False)
    last_failure_at = Column("LastFailureAt", DateTime(timezone=True), nullable=True)
    cooldown_until = Column("CooldownUntil", DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "Provider IN ('openai', 'anthropic', 'bedrock', 'deepseek', 'gemini', 'xai')",
            name="CK_ProviderKeys_Provider",
        ),
    )

    def __repr__(self):
        return f"<ProviderKey(id={self.id}, provider={self.provider}, key_name={self.key_name}, is_active={self.is_active})>"
