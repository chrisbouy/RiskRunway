
from pathlib import Path
from dotenv import load_dotenv
import os

# Load .env from root directory
load_dotenv()
# LLM_PROVIDER = "groq" 
LLM_PROVIDER = "gemini" 
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
# Email Configuration
# Email Configuration

