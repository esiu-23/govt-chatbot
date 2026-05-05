import os

MODEL_NAME      = "voyage-multilingual-2"
CLAUDE_PRIMARY  = "claude-haiku-4-5-20251001"
CLAUDE_FALLBACK = "claude-sonnet-4-6"
TOP_K           = 5
SCORE_THRESHOLD = 0.35
DATABASE_URL    = os.environ.get("DATABASE_URL")

# Illinois state data sources
LEGISCAN_API_KEY      = os.environ.get("LEGISCAN_API_KEY", "b2b1ed64d312fbf0c69f13b2c96a9e02")
LEGISCAN_BASE         = "https://api.legiscan.com/"
ILLINOIS_SOCRATA_BASE = "https://data.illinois.gov/resource"
