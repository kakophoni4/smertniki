from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_ids: str = ""
    check_cron: str = "0 10,18 * * *"
    request_delay_sec: float = 3.0
    http_timeout_sec: float = 30.0
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    timezone: str = "Europe/Moscow"
    user_agent: str = "Mozilla/5.0 (compatible; LavkiMonitor/1.0)"

    @property
    def admin_id_list(self) -> list[int]:
        if not self.admin_ids.strip():
            return []
        return [int(x.strip()) for x in self.admin_ids.split(",") if x.strip()]


settings = Settings()
