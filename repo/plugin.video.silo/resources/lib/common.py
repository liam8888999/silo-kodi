"""Shared imports and constants for the Silo Kodi addon modules.

Keep third-party/Kodi imports here so feature modules can focus on behaviour.
Runtime values supplied by Kodi for a single invocation live in runtime.py.
"""

import re
import sys
import time
import threading
import json
import datetime
import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, urlencode, urlparse

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
import xbmcaddon

from resources.lib.silo import (
    SiloClient,
    SiloError,
    _hide_login_loading,
    log,
)

PLAYABLE = (
    "movie",
    "episode",
    "video",
)

DIRECTORY_PAGE_SIZE = 200
SEARCH_PAGE_SIZE = 100

MIN_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

__all__ = [
    "re", "sys", "time", "threading", "json", "datetime", "base64",
    "hashlib", "os", "socket", "ssl", "struct", "quote",
    "ThreadPoolExecutor", "as_completed", "parse_qsl", "urlencode", "urlparse",
    "xbmc", "xbmcgui", "xbmcplugin", "xbmcvfs", "xbmcaddon",
    "SiloClient", "SiloError", "_hide_login_loading", "log",
    "PLAYABLE", "DIRECTORY_PAGE_SIZE", "SEARCH_PAGE_SIZE",
    "MIN_PAGE_SIZE", "MAX_PAGE_SIZE",
]
