
from pathlib import Path
from dotenv import load_dotenv
import os

# Load .env from root directory
load_dotenv()

# LLM provider selection. Use .env to switch without editing code:
# LLM_PROVIDER=groq
# LLM_PROVIDER=bedrock
# LLM_PROVIDER=gemini
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()

GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
# GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
#BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6")

BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
# Email Configuration
# Email Configuration
