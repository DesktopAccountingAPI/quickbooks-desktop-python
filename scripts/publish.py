"""Builds, publishes and verifies a PyPI release (used by .github/workflows/publish.yml).

    python scripts/publish.py --dry-run          # build + check + install the wheel in a fresh venv
    python scripts/publish.py --tag v0.1.0       # the release itself (CI)

Release steps:

1. The tag (``--tag`` or ``GITHUB_REF_NAME`` on a tag push) must equal ``v`` + the version in
   pyproject.toml.
2. ``uv build`` writes the sdist and wheel to ``dist/``; both are checked (names, metadata,
   ``py.typed``).
3. The wheel is installed into a fresh virtual environment and ``scripts/smoke.py`` runs against it.
4. ``--dry-run`` stops here and lists what would be uploaded. No network writes.
5. If PyPI already has this version, the upload is skipped (re-runs only do what is missing).
6. ``uv publish`` uploads with ``PYPI_API_TOKEN`` (passed as ``UV_PUBLISH_TOKEN`` in the
   environment, never on the command line) or, without a token, PyPI trusted publishing (OIDC).
7. Waits until ``https://pypi.org/pypi/<name>/<version>/json`` answers, then installs that version
   from PyPI into a clean virtual environment and runs the smoke program again.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
POLL_TIMEOUT_SECONDS = 900
POLL_INTERVAL_SECONDS = 10


def log(message: str) -> None:
    print(message, flush=True)


def run(args: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    log("+ " + " ".join(args))
    subprocess.run(args, cwd=cwd, env=env, check=True)


def project() -> tuple[str, str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[project]", 1)[1]
    name = re.search(r'^name = "([^"]+)"', section, re.MULTILINE)
    version = re.search(r'^version = "([^"]+)"', section, re.MULTILINE)
    if not name or not version:
        raise SystemExit("pyproject.toml: [project] name/version not found")
    return name.group(1), version.group(1)


def release_tag(arg: str | None) -> str | None:
    if arg:
        return arg
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        return os.environ.get("GITHUB_REF_NAME")
    return None


def build(name: str, version: str) -> list[Path]:
    shutil.rmtree(DIST, ignore_errors=True)
    # Release artifacts are built with the pinned interpreter, whatever UV_PYTHON says.
    run(["uv", "build", "--python", pinned_python(), "--out-dir", str(DIST)])
    stem = name.replace("-", "_")
    wheel = DIST / f"{stem}-{version}-py3-none-any.whl"
    sdist = DIST / f"{stem}-{version}.tar.gz"
    found = sorted(p for p in DIST.iterdir() if p.suffix in (".whl", ".gz"))
    if sorted([wheel, sdist]) != found:
        raise SystemExit(f"unexpected build output: {[p.name for p in found]}")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata = archive.read(f"{stem}-{version}.dist-info/METADATA").decode("utf-8")
    for required in (
        "desktopaccountingapi/py.typed",
        "desktopaccountingapi/__init__.py",
        "desktopaccountingapi/types/__init__.py",
    ):
        if required not in names:
            raise SystemExit(f"wheel is missing {required}")
    for line in (f"Name: {name}", f"Version: {version}", "License-Expression: MIT", "Requires-Python: >=3.9"):
        if line not in metadata.splitlines():
            raise SystemExit(f"wheel METADATA lacks {line!r}")
    return [sdist, wheel]


def pinned_python() -> str:
    return (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def smoke(install: list[str], version: str) -> None:
    """Installs ``install`` into a fresh virtual environment and runs scripts/smoke.py there."""
    with tempfile.TemporaryDirectory(prefix="daapi-smoke-") as tmp:
        work = Path(tmp)
        venv = work / "venv"
        # UV_PYTHON (CI matrix) wins; otherwise the interpreter pinned in .python-version.
        python = os.environ.get("UV_PYTHON") or pinned_python()
        run(["uv", "venv", "--quiet", "--python", python, str(venv)], cwd=work)
        run(["uv", "pip", "install", "--quiet", "--python", str(venv_python(venv)), *install], cwd=work)
        shutil.copy(ROOT / "scripts" / "smoke.py", work / "smoke.py")
        run([str(venv_python(venv)), "smoke.py", version], cwd=work)


def published(name: str, version: str) -> bool:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return bool(response.status == 200)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def upload(files: list[Path]) -> None:
    env = dict(os.environ)
    token = env.pop("PYPI_API_TOKEN", "")
    args = ["uv", "publish"]
    if token:
        env["UV_PUBLISH_TOKEN"] = token
        log("Uploading with the PyPI API token from the environment.")
    else:
        args += ["--trusted-publishing", "always"]
        log("PYPI_API_TOKEN is not set; uploading with PyPI trusted publishing (OIDC).")
    run([*args, *[str(f) for f in files]], env=env)


def wait_until_installable(name: str, version: str) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while not published(name, version):
        if time.monotonic() > deadline:
            raise SystemExit(f"{name} {version} did not appear on PyPI within {POLL_TIMEOUT_SECONDS} s")
        log(f"Waiting for {name} {version} on PyPI...")
        time.sleep(POLL_INTERVAL_SECONDS)
    log(f"{name} {version} is on PyPI: https://pypi.org/project/{name}/{version}/")
    while True:
        try:
            smoke(["--refresh", "--index-url", "https://pypi.org/simple", f"{name}=={version}"], version)
            return
        except subprocess.CalledProcessError:
            if time.monotonic() > deadline:
                raise SystemExit(
                    f"{name}=={version} could not be installed from PyPI within {POLL_TIMEOUT_SECONDS} s"
                ) from None
            log("The index does not serve the new version yet; retrying...")
            time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="build and check only; no network writes")
    parser.add_argument("--tag", help="release tag, default GITHUB_REF_NAME on tag pushes")
    args = parser.parse_args()

    name, version = project()
    tag = release_tag(args.tag)
    if tag is not None and tag != f"v{version}":
        raise SystemExit(f"tag {tag} does not match the package version {version} (expected v{version})")
    if tag is None and not args.dry_run:
        raise SystemExit("a release needs --tag v<version> (or a tag push); use --dry-run to only build")

    files = build(name, version)
    smoke([str(files[1])], version)
    if args.dry_run:
        log("Dry run: would upload")
        for f in files:
            log(f"  {f.relative_to(ROOT)} ({f.stat().st_size} bytes)")
        log(f"to https://pypi.org/project/{name}/ as version {version}.")
        return

    if published(name, version):
        log(f"{name} {version} is already on PyPI; skipping the upload.")
    else:
        upload(files)
    wait_until_installable(name, version)
    log(f"Released {name} {version}.")


if __name__ == "__main__":
    main()
