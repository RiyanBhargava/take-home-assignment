from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_CONFIG = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")


class Settings(BaseSettings):
    model_config = ENV_CONFIG

    inventory_path: Path = Path("Copy_of_sample_cars_dataset.xlsx")
    inventory_sheet: str = "raw dataset"
    runtime_dir: Path = Path(".runtime")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.1-flash-lite"


class ClientSettings(BaseSettings):
    model_config = ENV_CONFIG

    api_base_url: str = "http://127.0.0.1:8000"
