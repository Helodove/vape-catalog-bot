from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    moysklad_token: str
    admin_chat_id: int
    bot_base_url: str = ""          # https://your-bot.railway.app
    miniapp_origin: str = ""        # https://thevaper-miniapp.vercel.app
    supabase_url: str = ""          # https://<project>.supabase.co
    supabase_service_key: str = ""  # service_role ключ

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
