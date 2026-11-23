from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / '.env', interpolate=False)

from .mysql_api import worker_app
