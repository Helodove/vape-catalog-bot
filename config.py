from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    moysklad_token: str
    admin_chat_id: int

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
