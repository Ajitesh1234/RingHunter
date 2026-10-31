import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    paypal_client_id = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_secret = os.getenv("PAYPAL_SECRET", "")
    paypal_webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "")
    paypal_base = "https://api-m.sandbox.paypal.com"  # sandbox only, by design
    admin_token = os.getenv("ADMIN_TOKEN", "")  # empty = open (local dev)
    groq_key = os.getenv("GROQ_API_KEY", "")
    groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    database_url = (os.getenv("DATABASE_URL", "sqlite:///./ringhunter.db")
                    .replace("postgres://", "postgresql+psycopg://", 1)
                    .replace("postgresql://", "postgresql+psycopg://", 1))


settings = Settings()
