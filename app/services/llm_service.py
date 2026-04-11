from groq import Groq
from core.config import GROQ_API_KEY, MODEL_NAME

class LLMService:
    def __init__(self):
        self.client = None

    def _ensure_client(self):
        if self.client is None:
            if not GROQ_API_KEY:
                raise RuntimeError("GROQ_API_KEY environment variable is required")
            self.client = Groq(api_key=GROQ_API_KEY)
        return self.client

    def generate(self, messages, temperature: float = 0.7, max_tokens: int = 300):
        client = self._ensure_client()
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )
        return completion.choices[0].message.content or ""

llm_service = LLMService()