from pydantic import BaseModel
from typing import Optional


class Source(BaseModel):
    page: int
    text: str


class ExtractedField(BaseModel):
    field_name: str
    value: Optional[str]
    confidence: float
    source: Optional[Source]
    method: str
