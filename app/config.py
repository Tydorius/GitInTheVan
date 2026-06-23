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

    cors_origins: str = "*"
    rate_limit_enabled: bool = True
    rate_limit_proxy_per_min: int = 60
    rate_limit_api_per_min: int = 120
    max_request_body_size: int = 10 * 1024 * 1024
    jwt_expiration_hours: int = 24
    min_password_length: int = 8

    @field_validator("default_endpoint_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/") if v else v

    @field_validator("cors_origins")
    @classmethod
    def parse_cors_origins(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    model_config = {"env_prefix": "GITV_"}


settings = Settings()
