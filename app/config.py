from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    secret_key: str = "change-me-in-production"
    database_url: str = "sqlite+aiosqlite:///./data/gitinthevan.db"
    log_level: str = "INFO"

    default_endpoint_url: str = ""
    default_endpoint_api_key: str = ""
    default_endpoint_model: str = ""
    default_endpoint_api_base_path: str = ""
    request_timeout: int = 300

    @field_validator("default_endpoint_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/") if v else v

    model_config = {"env_prefix": "GITV_"}


settings = Settings()
