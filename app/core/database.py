from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import event
from .config import settings

# --- WAL Mode Injector ---
# This ensures SQLite doesn't lock up during simultaneous reads/writes
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

def create_database_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


# --- Engines ---
users_engine = create_database_engine(settings.USERS_DB_URL)
mapping_engine = create_database_engine(settings.MAPPING_DB_URL)
cache_engine = create_database_engine(settings.CACHE_DB_URL)

# Attach WAL pragmas
for engine, url in (
    (users_engine, settings.USERS_DB_URL),
    (mapping_engine, settings.MAPPING_DB_URL),
    (cache_engine, settings.CACHE_DB_URL),
):
    if url.startswith("sqlite"):
        event.listen(engine, 'connect', set_sqlite_pragma)

# --- Session Makers ---
UsersSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=users_engine)
MappingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=mapping_engine)
CacheSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cache_engine)

# --- Base Models ---
UsersBase = declarative_base()
MappingBase = declarative_base()
CacheBase = declarative_base()

# --- Dependency Injections (For FastAPI Routes) ---
def get_users_db():
    db = UsersSessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_mapping_db():
    db = MappingSessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_cache_db():
    db = CacheSessionLocal()
    try:
        yield db
    finally:
        db.close()