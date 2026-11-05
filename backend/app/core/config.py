"""
Application configuration using Pydantic Settings
"""
from pydantic_settings import BaseSettings
from typing import List
import os
import secrets


class Settings(BaseSettings):
    """Application settings"""
    
    # App settings
    APP_NAME: str = "Gamja Bot"
    DEBUG: bool = True
    
    # Database
    DATABASE_URL: str = "sqlite:///./gamja_bot.db"
    
    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:5173"]
    
    # JWT
    SECRET_KEY: str = secrets.token_urlsafe(32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    # LLM Providers
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    
    class Config:
        env_file = ".env"


settings = Settings(_env_file=None if os.environ.get("GAMJA_APP_DATA_DIR") else ".env")
