"""
routers/contributions.py
------------------------
Contribution analytics endpoint for completed renders.

Route:
    GET /api/project/{project_id}/render/{render_id}/contributions

The current backend stores project and upload metadata in a JSON-backed store.
Contribution percentages are computed from persisted render timeline summaries
(see routers/projects.py record_render_contributions).
"""

from fastapi import APIRouter, Depends, HTTPException

from multicam_pipeline.auth import principal_id_from_claims, require_auth
from multicam_pipeline.routers.projects import get_project_record, get_render_contributions

router = APIRouter(prefix="/api/project", tags=["contributions"])


@router.get("/{project_id}/render/{render_id}/contributions")
async def get_render_contributor_breakdown(
    project_id: str,
    render_id: str,
    _claims: dict = Depends(require_auth),
):
    project = get_project_record(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    actor_id = principal_id_from_claims(_claims)
    if actor_id and actor_id not in (project.get("members") or []):
        raise HTTPException(status_code=403, detail="You are not a member of this project.")

    summary = get_render_contributions(project_id, render_id)
    if not summary:
        return {
            "projectId": project_id,
            "renderId": render_id,
            "totalDuration": 0.0,
            "contributions": [],
        }

    total_duration = float(summary.get("total_duration", 0.0))
    if total_duration <= 0.0:
        return {
            "projectId": project_id,
            "renderId": render_id,
            "totalDuration": 0.0,
            "contributions": [],
        }

    duration_by_user = summary.get("duration_by_user", {}) or {}
    contributor_names = summary.get("contributor_names", {}) or {}

    contributions = []
    for user_id, duration in duration_by_user.items():
        pct = (float(duration) / total_duration) * 100.0
        contributions.append({
            "contributorId": user_id,
            "contributorName": contributor_names.get(user_id, user_id),
            "percentage": round(pct, 2),
            "duration": round(float(duration), 3),
        })

    contributions.sort(key=lambda c: c["percentage"], reverse=True)

    return {
        "projectId": project_id,
        "renderId": render_id,
        "totalDuration": round(total_duration, 3),
        "contributions": contributions,
    }
