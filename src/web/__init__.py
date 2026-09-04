"""Local web console for DeepResearch runs."""

from .app import create_app, run_web_server

__all__ = ["create_app", "run_web_server"]
