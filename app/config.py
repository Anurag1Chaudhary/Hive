from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://hive:hive@localhost:5433/hive"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    llm_max_retries: int = 2
    raw_data_path: str = "data/campaigns_raw.json"


settings = Settings()
