from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Animax API"
    VERSION: str = "1.0.0"
    API_PREFIX: str = "/api"
    
    # Security
    JWT_SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080
    TMDB_API_KEY: str = ""
    TMDB_ACCESS_TOKEN: str = ""
    RESOLVER_MICROSERVICE_URL: str = ""
    
    # Database Paths
    USERS_DB_URL: str = "sqlite:///./app/databases/users.db"
    MAPPING_DB_URL: str = "sqlite:///./app/databases/mapping.db"
    CACHE_DB_URL: str = "sqlite:///./app/databases/cache.db"
    PROXY_DB_PATH: str = "proxy.db"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    

settings = Settings()