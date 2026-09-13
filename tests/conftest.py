import os
import tempfile

# settings reads DATABASE_URL once, at import. Point it at a throwaway SQLite
# file before any test imports the app, so the suite never writes to the live
# macs.db or to Supabase. A real env var wins over .env for both load_dotenv
# and pydantic-settings.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="macs-tests-"), "test.db")
