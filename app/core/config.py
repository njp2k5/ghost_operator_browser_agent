from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    GROQ_API_KEY: str
    SUPERMEMORY_API_KEY: str
    SECRET_KEY: str
    BASE_URL: str = "http://localhost:8000"
    TEAMMATE_WEBHOOK_URL: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
