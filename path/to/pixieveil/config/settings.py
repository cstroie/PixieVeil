import yaml
from pathlib import Path
from typing import Dict, Any
from pydantic import BaseModel, Field

class Settings(BaseModel):
    dicom_server: Dict[str, Any] = Field(default_factory=dict)
    anonymization: Dict[str, Any] = Field(default_factory=dict)
    storage: Dict[str, Any] = Field(default_factory=dict)
    http_server: Dict[str, Any] = Field(default_factory=dict)
    study: Dict[str, Any] = Field(default_factory=dict)
    series_filter: Dict[str, Any] = Field(default_factory=dict)
    logging: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, config_path: Path = None) -> "Settings":
        if config_path is None:
            config_path = Path("config/settings.yaml")
            if not config_path.exists():
                config_path = Path("config/settings.yaml.example")

        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)

        return cls(**config_data)
