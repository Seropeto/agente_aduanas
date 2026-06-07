import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
LOGS_DIR = DATA_DIR / "logs"

# Create dirs
for d in [DATA_DIR, UPLOADS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "contacto@toxirodigital.cloud")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Tesseract OCR
tessdata = os.getenv("TESSDATA_PREFIX", "")
if tessdata:
    os.environ["TESSDATA_PREFIX"] = tessdata
MODEL_NAME = "claude-sonnet-4-6"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
TOP_K_RESULTS = 3
# Temperature 0.0 = determinismo absoluto, sin creatividad especulativa
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.0"))
