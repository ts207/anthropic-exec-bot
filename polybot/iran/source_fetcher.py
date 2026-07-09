from __future__ import annotations

import sys

from polybot.core import source_fetcher as _source_fetcher

sys.modules[__name__] = _source_fetcher
