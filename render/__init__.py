"""The render step: Jinja2 template + headless Chromium -> paper.pdf."""

from .render import DEFAULT_LOOK, RenderResult, render  # noqa: F401

__all__ = ["render", "RenderResult", "DEFAULT_LOOK"]
