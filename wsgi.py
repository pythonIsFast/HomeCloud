"""WSGI entry point for gunicorn.

    gunicorn --workers 2 --bind 127.0.0.1:6002 wsgi:app
"""

from app import create_app

app = create_app()
