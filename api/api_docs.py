"""Authenticated API schema and documentation routes."""
from fastapi import Depends
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

from api.dependencies import get_user_id


def install_docs(app):
    @app.get('/openapi.json', include_in_schema=False)
    async def schema(user_id: str = Depends(get_user_id)):
        return app.openapi()

    @app.get('/docs', include_in_schema=False)
    async def swagger(user_id: str = Depends(get_user_id)):
        return get_swagger_ui_html(openapi_url='/openapi.json', title=app.title)

    @app.get('/redoc', include_in_schema=False)
    async def redoc(user_id: str = Depends(get_user_id)):
        return get_redoc_html(openapi_url='/openapi.json', title=app.title)
