"""Gunicorn entry point; importing the application factory has no side effects."""

from jarvis.api.web_app import create_app


app = create_app()
