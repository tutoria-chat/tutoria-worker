from sqlalchemy import Column, String, Integer, Boolean, DECIMAL, DateTime, CheckConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class AIModel(BaseModel):
    __tablename__ = "AIModels"

    model_name = Column("ModelName", String(100), nullable=False, unique=True)
    display_name = Column("DisplayName", String(100), nullable=False)
    provider = Column("Provider", String(50), nullable=False)
    max_tokens = Column("MaxTokens", Integer, nullable=False)
    supports_vision = Column("SupportsVision", Boolean, default=False, nullable=False)
    supports_function_calling = Column("SupportsFunctionCalling", Boolean, default=False, nullable=False)
    input_cost_per_1m = Column("InputCostPer1M", DECIMAL(10, 4), nullable=True)
    output_cost_per_1m = Column("OutputCostPer1M", DECIMAL(10, 4), nullable=True)
    required_tier = Column("RequiredTier", Integer, default=3, nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False)
    is_deprecated = Column("IsDeprecated", Boolean, default=False, nullable=False)
    deprecation_date = Column("DeprecationDate", DateTime(timezone=True), nullable=True)
    use_for_file_extraction = Column("UseForFileExtraction", Boolean, default=False, nullable=False)
    use_for_formatting = Column("UseForFormatting", Boolean, default=False, nullable=True)
    description = Column("Description", String(500), nullable=True)
    recommended_for = Column("RecommendedFor", String(200), nullable=True)

    __table_args__ = (
        CheckConstraint("\"Provider\" IN ('openai', 'anthropic', 'bedrock', 'deepseek', 'gemini', 'xai')", name="CK_AIModels_Provider"),
        CheckConstraint("RequiredTier BETWEEN 1 AND 3", name="CK_AIModels_RequiredTier"),
    )

    # Navigation properties
    modules = relationship("Module", back_populates="ai_model")
