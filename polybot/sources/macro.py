from __future__ import annotations

from .base import SourceReading


class MacroReleaseAdapter:
    """Stub for BLS / ISM scheduled releases.

    The sub-second macro hot path is explicitly out of scope until weather has
    demonstrated real fills in audited logs.
    """

    def poll(self) -> SourceReading | None:
        raise NotImplementedError("macro adapter is a stub until weather is proven")
