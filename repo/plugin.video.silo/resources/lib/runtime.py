"""Per-invocation state supplied by Kodi's plugin entry point.

Feature modules import this module instead of keeping their own copies of the
Kodi handle/base URL/addon settings object.
"""

HANDLE = 0
BASE_URL = ""
ADDON = None
