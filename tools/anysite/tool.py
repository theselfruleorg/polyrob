"""`anysite` tool: query AnySite's 200+ sources / 1,200+ endpoints via the
official CLI (pip: anysite-cli). Replaces the legacy anysite-via-MCP path."""
import logging
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.anysite.client import (
    build_api_argv, build_describe_argv, run_anysite, ensure_configured,
    binary_available, binary_path, missing_requirement,
)

_INSTALL_HINT = (
    "anysite CLI not available — install with `pip install anysite-cli` and set "
    "ANYSITE_API_KEY (or run `anysite config set api_key <KEY>`)."
)


def _unavailable_reason() -> str:
    """Name the ONE thing that is missing.

    Prod 2026-09-12 ran for months with the package installed and the key absent,
    while this tool reported "install with pip install anysite-cli" — so the
    operator checked the package, found it present, and concluded the capability
    was broken rather than unconfigured. One message for two different failures
    sends people to the wrong place.
    """
    what = missing_requirement()
    if what == "binary":
        return ("anysite CLI is not installed — `pip install anysite-cli` "
                "(it is a declared dependency, so a normal install includes it).")
    if what == "key":
        return ("anysite CLI is installed at "
                f"{binary_path()} but no credential is set — set ANYSITE_API_KEY "
                "(or ANYSITE_ACCESS_TOKEN) in the environment, then restart.")
    return _INSTALL_HINT


class AnysiteApiParams(BaseModel):
    endpoint: str = Field(..., description="API path, e.g. '/api/linkedin/user' or '/api/yc/companies'")
    params: Optional[dict] = Field(None, description="Endpoint params as key=value pairs, e.g. {'user': 'satyanadella'}")
    output_format: str = Field("json", description="Output format: json | jsonl | csv | table")


class AnysiteSchemaParams(BaseModel):
    pass


class AnysiteDescribeParams(BaseModel):
    endpoint: Optional[str] = Field(
        default=None,
        description=("One endpoint to describe in full (input params + output "
                     "fields), e.g. '/api/twitter/search/posts' or "
                     "'twitter.search.posts'."))
    search: Optional[str] = Field(
        default=None,
        description=("Keyword to find endpoints, e.g. 'twitter', 'company', "
                     "'github'. Returns the matching paths only."))


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
        """True only when the CLI can actually make an authenticated call.

        It used to return True whenever the BINARY existed, ignoring
        `ensure_configured`'s answer — so a keyless install proceeded and failed
        with the vendor's error instead of ours, which is how "anysite is broken"
        rather than "anysite is unconfigured" reached the owner.
        """
        if missing_requirement() is not None:
            return False
        if not self._configured:
            if not ensure_configured():
                return False
            self._configured = True
        return True

    @BaseTool.action(
        "Query AnySite (200+ sources / 1,200+ endpoints: LinkedIn, Twitter/X, Reddit, "
        "YouTube, GitHub, SEC, Google, a universal web scraper, and more). Pass an API "
        "path and key=value params, e.g. endpoint='/api/linkedin/user', params={'user':'satyanadella'}. "
        "Do NOT guess an endpoint path — run anysite_describe first (search=<keyword> to list "
        "paths, then endpoint=<path> for its exact params).",
        param_model=AnysiteApiParams,
    )
    async def anysite_api(self, params: AnysiteApiParams, execution_context=None):
        try:
            if not self._prepare():
                return self._err(_unavailable_reason())
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
        "Discover AnySite endpoints BEFORE calling anysite_api: pass search=<keyword> to list "
        "matching endpoint paths, then endpoint=<path> to read that endpoint's exact input "
        "params and output fields. Guessing paths wastes steps on Not Found and "
        "param-validation errors.",
        param_model=AnysiteDescribeParams,
    )
    async def anysite_describe(self, params: AnysiteDescribeParams, execution_context=None):
        try:
            endpoint = (params.endpoint or "").strip()
            search = (params.search or "").strip()
            if not endpoint and not search:
                return self._err(
                    "anysite_describe needs a search keyword (to list matching endpoints) "
                    "or an endpoint path (to describe one endpoint) — pass one of the two."
                )
            if not self._prepare():
                return self._err(_unavailable_reason())
            argv = build_describe_argv(endpoint=endpoint or None, search=search or None)
            result = await run_anysite(argv)
            if result.timed_out:
                return self._err("anysite CLI timed out")
            if result.exit_code != 0:
                return self._err(f"anysite describe failed (exit {result.exit_code}): {result.stderr or result.stdout}")
            return self._ok(result.stdout or "(no matching endpoints)")
        except ValueError as e:
            return self._err(f"invalid argument: {e}")
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"anysite_describe failed: {e}")
            return self._err(f"anysite_describe failed: {e}")

    @BaseTool.action(
        "Refresh the local AnySite endpoint/schema cache (run once if endpoints look stale).",
        param_model=AnysiteSchemaParams,
    )
    async def anysite_schema_update(self, params: AnysiteSchemaParams, execution_context=None):
        try:
            if not self._prepare():
                return self._err(_unavailable_reason())
            result = await run_anysite([binary_path() or "anysite", "--non-interactive",
                                        "schema", "update"], timeout=60.0)
            if result.exit_code != 0:
                return self._err(f"schema update failed: {result.stderr or result.stdout}")
            return self._ok(result.stdout or "schema updated")
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"anysite_schema_update failed: {e}")
            return self._err(f"anysite_schema_update failed: {e}")
