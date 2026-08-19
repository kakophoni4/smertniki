from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    admin_ids: str = ""
    check_cron: str = "0 10,18 * * *"
    request_delay_sec: float = 3.0
    http_timeout_sec: float = 30.0
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    timezone: str = "Europe/Moscow"
    user_agent: str = "Mozilla/5.0 (compatible; LavkiMonitor/1.0)"
    # одна актуальная выписка на компанию: data/vypiski/{ogrn}.pdf (перезапись)
    vypiski_dir: str = "./data/vypiski"
    # сколько последних check_results хранить на лавку (остальное удаляем)
    keep_check_results: int = 5
    # через сколько дней недостоверности начинать еженедельно пинать
    stale_ticket_days: int = 60
    # cron еженедельного пинга (по умолчанию пн 11:00)
    stale_nag_cron: str = "0 11 * * 1"
    # кому слать пинги Декстера (telegram id через запятую). Пусто = никому (не всем подряд)
    stale_nag_ids: str = ""
    crm_api_token: str = ""
    crm_api_host: str = "0.0.0.0"
    crm_api_port: int = 8088

    @property
    def admin_id_list(self) -> list[int]:
        if not self.admin_ids.strip():
            return []
        return [int(x.strip()) for x in self.admin_ids.split(",") if x.strip()]

    @property
    def stale_nag_id_list(self) -> list[int]:
        if not self.stale_nag_ids.strip():
            return []
        return [int(x.strip()) for x in self.stale_nag_ids.split(",") if x.strip()]


settings = Settings()
