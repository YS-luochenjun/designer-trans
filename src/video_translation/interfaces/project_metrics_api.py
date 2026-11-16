"""Read-only statistics, isolated from dispatch and production endpoints."""

from datetime import datetime

from fastapi import APIRouter, HTTPException

from video_translation.application.project_metrics import read_project_metrics


def project_metrics_routes(store):
    router = APIRouter(prefix="/api")

    @router.get("/project-metrics")
    def project_metrics(start: datetime | None = None, end: datetime | None = None):
        if (start is None) != (end is None):
            raise HTTPException(422, "start and end must be provided together")
        if start is not None and end is not None:
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise HTTPException(422, "Use timezone-aware start < end")
        return read_project_metrics(store.path, start=start, end=end)

    return router
