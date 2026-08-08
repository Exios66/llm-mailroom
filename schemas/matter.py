from pydantic import BaseModel, Field
from datetime import datetime


class Matter(BaseModel):
    matter_id: str
    name: str
    client_name: str
    practice_area: str = "transactional"
    opened_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
