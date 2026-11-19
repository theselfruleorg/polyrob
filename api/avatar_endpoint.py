"""The instance avatar image on the API origin — ``GET``/``HEAD /avatar.png``.

The ERC-8004 registration file publishes ``{A2A_BASE_URL}/avatar.png`` as the
agent's ``image`` (``modules/eip8004/registration.py::_avatar``), and
``A2A_BASE_URL`` names THIS origin. A consumer of an on-chain card cannot
authenticate, so the path is public (``api.auth_constants.PUBLIC_PATHS_EXACT``):
the image is the agent's public face and nothing else is served here.

The response parts come from ``core.avatar.image_response_parts`` — the same
function the console route calls, so the two origins cannot differ.
"""
from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

router = APIRouter()


@router.api_route("/avatar.png", methods=["GET", "HEAD"], include_in_schema=False)
async def avatar_image():
    """The slot image with its stored content type (the name stays ``.png`` for
    every format), or 404 when no readable image is set."""
    from core.avatar import image_response_parts
    from core.instance import resolve_instance_id
    from core.runtime_paths import resolve_data_home
    parts = image_response_parts(resolve_data_home(), resolve_instance_id())
    if parts is None:
        return Response(status_code=404)
    path, media_type, headers = parts
    return FileResponse(str(path), media_type=media_type, headers=headers)
