from __future__ import annotations

from .base import SourceReading


class BoxOfficeAdapter:
    """Stub for The Numbers / Box Office Mojo final figures.

    Box office is intentionally out of MVP scope until weather dry-run and live
    logs demonstrate real fills.
    """

    def poll(self) -> SourceReading | None:
        raise NotImplementedError("box office adapter is a stub until weather is proven")
