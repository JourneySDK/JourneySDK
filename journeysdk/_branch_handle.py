"""Assigned branch handle support."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from .errors import InvalidBranchUsageError
from .session import get_session

BranchSiteKey = tuple[int, int]


@dataclass(frozen=True)
class BranchHandle:
    definition_site: BranchSiteKey
    name: str
    start_from: object | None

    def __bool__(self) -> bool:
        session = get_session()
        if session is None:
            raise InvalidBranchUsageError(
                "Assigned branch handles can only be checked while a journey is being planned or executed.",
                hint="Use assigned branch handles inside the journey function that created them.",
            )

        selector = getattr(session, "branch_handle", None)
        if not callable(selector):
            raise InvalidBranchUsageError(
                "Assigned branch handles are not available in this journey context.",
                hint="Use branch handles only inside a journey plan or execution.",
            )

        caller = inspect.currentframe()
        caller_frame = caller.f_back if caller is not None else None
        if caller_frame is None:
            raise InvalidBranchUsageError(
                "Assigned branch handle could not determine where it was checked.",
                hint="Use the branch handle directly as the whole condition in an if/elif chain.",
            )
        return bool(selector(handle=self, frame=caller_frame))
