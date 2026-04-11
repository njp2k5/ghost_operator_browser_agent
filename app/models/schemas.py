from pydantic import BaseModel

class ChatMessage(BaseModel):
    sender: str
    message: str