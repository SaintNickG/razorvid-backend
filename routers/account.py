"""Authenticated account lifecycle endpoints."""

import os
from urllib.parse import urlparse

import boto3
from fastapi import APIRouter, Depends, HTTPException

from multicam_pipeline.auth import principal_id_from_claims, require_auth
from multicam_pipeline.config import AWS_REGION, DYNAMODB_TABLE, IS_AWS
from multicam_pipeline.routers.projects import remove_account_projects

router = APIRouter(prefix="/account", tags=["account"])


def _delete_s3_uri(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        return
    boto3.client("s3", region_name=AWS_REGION).delete_object(
        Bucket=parsed.netloc,
        Key=parsed.path.lstrip("/"),
    )


def _delete_related_jobs(media_refs: list[str], deleted_project_ids: set[str]) -> int:
    if not IS_AWS or not DYNAMODB_TABLE:
        return 0

    media_set = set(media_refs)
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DYNAMODB_TABLE)
    response = table.scan()
    jobs = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        jobs.extend(response.get("Items", []))

    deleted = 0
    for job in jobs:
        video_paths = set(job.get("video_paths") or [])
        if job.get("project_id") not in deleted_project_ids and not video_paths.intersection(media_set):
            continue
        output_paths = job.get("output_parts") or [job.get("output_path", "")]
        for uri in {*video_paths, *output_paths}:
            _delete_s3_uri(str(uri))
        table.delete_item(Key={"job_id": job["job_id"]})
        deleted += 1
    return deleted


@router.delete("")
async def delete_account(claims: dict = Depends(require_auth)):
    """Permanently delete the authenticated user's account and managed data."""
    actor_id = principal_id_from_claims(claims)
    username = str(claims.get("cognito:username") or "").strip()
    if not actor_id or not username:
        raise HTTPException(status_code=401, detail="A Cognito user identity is required to delete this account.")

    media_refs, deleted_project_ids = remove_account_projects(actor_id)
    for uri in media_refs:
        _delete_s3_uri(uri)
    deleted_jobs = _delete_related_jobs(media_refs, deleted_project_ids)

    if IS_AWS:
        user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "").strip()
        if not user_pool_id:
            raise HTTPException(status_code=503, detail="Account deletion is not configured.")
        boto3.client("cognito-idp", region_name=AWS_REGION).admin_delete_user(
            UserPoolId=user_pool_id,
            Username=username,
        )

    return {
        "deleted": True,
        "deleted_projects": len(deleted_project_ids),
        "deleted_uploads": len(media_refs),
        "deleted_jobs": deleted_jobs,
    }