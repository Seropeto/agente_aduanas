import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
CHROMA_DIR = DATA_DIR / "chroma_db"
UPLOADS_DIR = DATA_DIR / "uploads"
LOGS_DIR = DATA_DIR / "logs"

# Create dirs
for d in [DATA_DIR, CHROMA_DIR, UPLOADS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "contacto@toxirodigital.cloud")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Tesseract OCR
tessdata = os.getenv("TESSDATA_PREFIX", "")
if tessdata:
    os.environ["TESSDATA_PREFIX"] = tessdata
MODEL_NAME = "claude-sonnet-4-6"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
TOP_K_RESULTS = 3
