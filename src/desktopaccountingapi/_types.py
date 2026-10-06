"""The NOT_GIVEN sentinel."""

from __future__ import annotations

from typing import ClassVar, Final, Optional

from typing_extensions import final


@final
class NotGiven:
    """Marks an omitted argument, so that ``None`` can mean an explicit JSON ``null``.

    Optional request fields default to ``NOT_GIVEN`` and are left out of the request. Passing
    ``None`` sends ``null``, which clears the field where the API allows it (fields documented
    as clearable).
    """

    _instance: ClassVar[Optional[NotGiven]] = None

    def __new__(cls) -> NotGiven:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NOT_GIVEN"

    def __copy__(self) -> NotGiven:
        return self

    def __deepcopy__(self, memo: object) -> NotGiven:
        return self


NOT_GIVEN: Final = NotGiven()
"""The default of every optional request argument: leave the field out of the request."""
