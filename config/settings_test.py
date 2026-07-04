"""Test settings: config.settings with the production environment neutralized.

`config.settings` loads `.env` at import, so once the real deployment values land
there (CF_ACCESS_*, RESEND_*, DATA_DIR=/data …) a bare `uv run pytest` would run the
suite against production config — Access middleware active, /data unwritable. Tests
must always exercise the unconfigured defaults, so force them *before* the import;
`load_dotenv` never overrides variables that already exist.
"""

import os
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent

os.environ["DEBUG"] = "1"
os.environ["DATA_DIR"] = str(_BASE_DIR / "data")
for _var in (
    "CF_ACCESS_TEAM_DOMAIN",
    "CF_ACCESS_AUD",
    "RESEND_API_KEY",
    "RESEND_WEBHOOK_SECRET",
    "EMAIL_FROM",
    "EMAIL_REPLY_TO",
    "ALLOWED_HOSTS",
):
    os.environ[_var] = ""

from config.settings import *  # noqa: E402,F403
