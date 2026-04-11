import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.1-8b-instant")

IRCTC_RAPIDAPI_KEY = os.getenv("IRCTC_RAPIDAPI_KEY")
IRCTC_RAPIDAPI_HOST = os.getenv("IRCTC_RAPIDAPI_HOST", "irctc1.p.rapidapi.com")
IRCTC_API_BASE_URL = os.getenv("IRCTC_API_BASE_URL", "https://irctc1.p.rapidapi.com")
IRCTC_TRAIN_BETWEEN_PATH = os.getenv("IRCTC_TRAIN_BETWEEN_PATH", "/api/v3/trainBetweenStations")
IRCTC_PNR_PATH = os.getenv("IRCTC_PNR_PATH", "/api/v3/getPNRStatus")
IRCTC_STATION_SEARCH_PATH = os.getenv("IRCTC_STATION_SEARCH_PATH", "/api/v1/searchStation")