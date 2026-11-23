"""GET /api/cases  Every case with its progress, who it waits for and its spend, plus counts."""

from __future__ import annotations

from ... import api
from .common import pipeline_summary, response, store


def handler(event, context):
    return response(200, api.list_view(store(), pipeline_summary()))
