import os

CNR = "KABC010220432024"
BASE_URL = "https://services.ecourts.gov.in/ecourtindia_v6/"
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
NVIDIA_OCR_URL = "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2"
MAX_RETRIES = 10
OUTPUT_FILE = "ecourt_scraper/result.txt"
CAPTCHA_DEBUG_DIR = "ecourt_scraper/captcha_debug"
