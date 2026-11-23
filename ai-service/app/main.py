import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / '.env', interpolate=False)


mode = os.getenv('SUBTITLE_DB_MODE', 'sqlite').lower()
if mode == 'mysql':
    from .mysql_api import app, worker_app
    from .admin import router as admin_router
    from .editor import router as editor_router
    from .live import router as live_router

    app.include_router(editor_router)
    app.include_router(admin_router)
    app.include_router(live_router)
elif mode == 'sqlite':
    from .demo import app
else:
    raise RuntimeError('SUBTITLE_DB_MODE must be mysql or sqlite')

from .accounts import router as accounts_router

app.include_router(accounts_router)

if mode == 'mysql':
    app.mount('/', worker_app)
