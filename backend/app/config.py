from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "OmniFit Sync API"
    database_url: str = "postgresql://omnifit_user:omnifit_dev_password@localhost:5432/omnifit_sync"
    seed_dev_user: bool = True
    jwt_secret_key: str = "omnifit-dev-secret-change-in-production-min-32-bytes"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
