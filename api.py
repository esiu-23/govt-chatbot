"""
api.py — entrypoint shim.
All logic lives in app/. This file exists so gunicorn can find `api:app`.
"""
from dotenv import load_dotenv
load_dotenv()

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    import os
    from app.resources import load_resources
    load_resources()
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, host="0.0.0.0", port=port, use_reloader=False, threaded=True)
