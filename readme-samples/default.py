# Harness for README fragments (scripts/readme-samples.mjs): each fragment becomes the body of
# {{NAME}}, with these names in scope. Not linted by ruff; mypy --strict checks the rendered files.
from __future__ import annotations

from desktopaccountingapi import DesktopAccountingApi


def save(value: object) -> None: ...


def show_to_end_user(message: str) -> None: ...


def {{NAME}}(client: DesktopAccountingApi) -> None:
    {{SAMPLE}}
