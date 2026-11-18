"""Shared services, created at startup by main.

Other modules use them as `rt.DB`, `rt.JOBS`, `rt.TG`, `rt.ENGINE`: a
single place to replace in tests.
"""

DB = None      # sqlite3.Connection
JOBS = None    # store.JobStore on the same connection
TG = None      # telegram.Telegram
ENGINE = None  # engine.LocalEngine
