import os

import pytest

# Set required environment before any app module is imported. A file-backed SQLite
# database (rather than :memory:) is used so the separate connections SQLAlchemy opens
# per session share the same schema and data. APP_SECRET must be a non-placeholder value
# or app.security._secret() refuses to sign tokens.
_TEST_DB = os.path.join(os.path.dirname(__file__), "_test_straddleup.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_DB}")
os.environ.setdefault("APP_SECRET", "test-secret-that-is-sufficiently-long-1234567890")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db():
    yield
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TEST_DB + suffix)
        except FileNotFoundError:
            pass
