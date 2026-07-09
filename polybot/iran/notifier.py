from __future__ import annotations

import sys

from polybot.core import notifier as _notifier

sys.modules[__name__] = _notifier
