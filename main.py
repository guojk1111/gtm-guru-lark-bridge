try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from asgiref.wsgi import WsgiToAsgi
from app import app as flask_app

# ASGI wrapper for deployment platforms that expect FastAPI/ASGI-style `main:app`.
# The production bridge logic remains in app.py.
app = WsgiToAsgi(flask_app)
