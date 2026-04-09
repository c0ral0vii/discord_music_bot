from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    PREFIX: str = "!"
    MESSAGE_CONTENT_INTENT: bool = True
    BOT_TOKEN: str
    APP_ID: int
    PUBLIC_KEY: str


settings = Settings()  # type: ignore
