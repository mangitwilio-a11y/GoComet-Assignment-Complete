"""Test config: point the DB at a throwaway temp file and provide dummy provider
env BEFORE any app module imports settings. The LLM is never actually called in
tests — every test patches get_client with a fake."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="nova_test_")
os.environ["DB_PATH"] = os.path.join(_TMP, "nova_test.db")
os.environ.setdefault("LLM_PROVIDER", "azure")
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com")

import pytest  # noqa: E402

from app.db import store  # noqa: E402

DB_PATH = os.environ["DB_PATH"]


@pytest.fixture
def fresh_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    store.init_db()
    yield
