"""Utility helpers (tier 0, ranked with ``core``).

Import the module you need directly (``from utils.time_utils import
time_execution_async``); this package deliberately re-exports nothing — the old
package-level re-exports had no importer and only made ``import utils.x`` pull in
every sibling (GIF/PIL included).
"""

__all__: list = []
