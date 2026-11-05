import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    paypal_client_id = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_secret = os.getenv("PAYPAL_SECRET", "")
    paypal_webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "")
    paypal_base = "https://api-m.sandbox.paypal.com"  # sandbox only, by design
    screening = os.getenv("SCREENING", "off").lower() in ("on", "1", "true")  # authorize-then-screen checkout
    product_price = os.getenv("PRODUCT_PRICE", "25.00")   # the server decides the price; the browser cannot
    currency = "USD"
    strict_order_check = os.getenv("STRICT_ORDER_CHECK", "on").lower() in ("on", "1", "true")
    # On Render (or when REQUIRE_ADMIN=1) the app refuses state-changing requests unless ADMIN_TOKEN is set (fail closed)
    admin_required = bool(os.getenv("RENDER")) or os.getenv("REQUIRE_ADMIN", "").lower() in ("1", "true", "on")
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
