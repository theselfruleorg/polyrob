"""`anysite` tool: query AnySite's 200+ sources / 1,200+ endpoints via the
official CLI (pip: anysite-cli). Replaces the legacy anysite-via-MCP path."""
import logging
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.anysite.client import (
    build_api_argv, run_anysite, ensure_configured, binary_available,
)

_INSTALL_HINT = (
    "anysite CLI not available — install with `pip install anysite-cli` and set "
    "ANYSITE_API_KEY (or run `anysite config set api_key <KEY>`)."
)


class AnysiteApiParams(BaseModel):
    endpoint: str = Field(..., description="API path, e.g. '/api/linkedin/user' or '/api/yc/companies'")
    params: Optional[dict] = Field(None, description="Endpoint params as key=value pairs, e.g. {'user': 'satyanadella'}")
    output_format: str = Field("json", description="Output format: json | jsonl | csv | table")


class AnysiteSchemaParams(BaseModel):
    pass


class AnysiteTool(BaseTool):
    def __init__(self, name: str = "anysite", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._configured = False

    @staticmethod
    def _ok(content):
        from tools.controller.types import ActionResult
        return ActionResult(extracted_content=content)

    @staticmethod
    def _err(msg):
        from tools.controller.types import ActionResult
        return ActionResult(error=msg)

    def _prepare(self):
        if not binary_available():
            return False
        if not self._configured:
            ensure_configured()
            self._configured = True
        return True

    @BaseTool.action(
        "Query AnySite (200+ sources / 1,200+ endpoints: LinkedIn, Twitter/X, Reddit, "
        "YouTube, GitHub, SEC, Google, a universal web scraper, and more). Pass an API "
        "path and key=value params, e.g. endpoint='/api/linkedin/user', params={'user':'satyanadella'}.",
        param_model=AnysiteApiParams,
    )
    async def anysite_api(self, params: AnysiteApiParams, execution_context=None):
        try:
            if not self._prepare():
                return self._err(_INSTALL_HINT)
            argv = build_api_argv(params.endpoint, params.params, params.output_format)
            result = await run_anysite(argv)
            if result.timed_out:
                return self._err("anysite CLI timed out")
            if result.exit_code != 0:
                return self._err(f"anysite api failed (exit {result.exit_code}): {result.stderr or result.stdout}")
            return self._ok(result.stdout or "(empty response)")
        except ValueError as e:
            return self._err(f"invalid argument: {e}")
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"anysite_api failed: {e}")
            return self._err(f"anysite_api failed: {e}")

    @BaseTool.action(
        "Refresh the local AnySite endpoint/schema cache (run once if endpoints look stale).",
        param_model=AnysiteSchemaParams,
    )
    async def anysite_schema_update(self, params: AnysiteSchemaParams, execution_context=None):
        try:
            if not self._prepare():
                return self._err(_INSTALL_HINT)
            result = await run_anysite(["anysite", "--non-interactive", "schema", "update"], timeout=60.0)
            if result.exit_code != 0:
                return self._err(f"schema update failed: {result.stderr or result.stdout}")
            return self._ok(result.stdout or "schema updated")
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"anysite_schema_update failed: {e}")
            return self._err(f"anysite_schema_update failed: {e}")
