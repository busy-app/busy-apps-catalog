"""Run the pinned BUSY CLI in an isolated CI build job."""

import json
import os
import posixpath
import re
import subprocess
import tarfile
from pathlib import Path

import yaml

from tools.check_registry import CheckError

VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][A-Za-z0-9.-]+)?$"
)
NODE_IMAGE = (
    "node@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205"
)
MAX_MAIN_JS = 256_000


IMPORT_PROBE = (
    "import fs from 'node:fs'; import vm from 'node:vm'; "
    "console.log(JSON.stringify(new vm.SourceTextModule("
    "fs.readFileSync(0, 'utf8')).dependencySpecifiers))"
)


def import_specifiers(name: str, source: bytes) -> list[str]:
    """Static import targets of one module; SourceTextModule parses without evaluating."""
    result = subprocess.run(
        [
            "node",
            "--experimental-vm-modules",
            "--input-type=module",
            "-e",
            IMPORT_PROBE,
        ],
        input=source,
        capture_output=True,
        check=False,
        timeout=15,
        env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
    )
    if result.returncode:
        raise CheckError(
            "script_imports",
            f"{name}: invalid module syntax: {result.stderr.decode(errors='replace')[-1500:]}",
        )
    return json.loads(result.stdout)


def check_import(name: str, specifier: str, prefix: str, modules: dict) -> None:
    """An import must be relative and point at another packaged module."""
    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), specifier))
    if (
        not specifier.startswith(("./", "../"))
        or not target.startswith(prefix)
        or target not in modules
    ):
        raise CheckError(
            "script_imports", f"{name}: invalid or missing import {specifier}"
        )


def validate_main_script(package: Path, app_id: str) -> None:
    """
    Parse packaged JavaScript modules and verify their static import targets.

    SourceTextModule parses source without evaluating it or loading dependencies.
    """
    prefix = f"{app_id}/scripts/"
    with tarfile.open(package, "r:gz") as archive:
        try:
            member = archive.getmember(prefix + "main.js")
        except KeyError:
            raise CheckError("script_main") from None
        if not member.isfile() or member.size > MAX_MAIN_JS:
            raise CheckError("script_main")
        modules = {
            item.name: item
            for item in archive
            if item.isfile()
            and item.name.startswith(prefix)
            and item.name.endswith(".js")
        }
        for name, module in modules.items():
            if module.size > MAX_MAIN_JS:
                raise CheckError("javascript_size", name)
            file = archive.extractfile(module)
            if file is None:
                raise CheckError("script_imports", f"{name}: could not read module")
            for specifier in import_specifiers(name, file.read(MAX_MAIN_JS + 1)):
                check_import(name, specifier, prefix, modules)


def run_cli_isolated(app: Path, environment: dict[str, str]) -> None:
    """
    Run the installed BUSY CLI in a restricted container with no network.

    Only the source checkout is mounted; host credentials and job tokens are absent.
    """
    source = app.resolve()
    if "," in str(source):
        raise CheckError("build", "Source path cannot be mounted safely")
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={source},dst=/work",
            "--workdir",
            "/work",
            "--env",
            "HOME=/tmp",
            NODE_IMAGE,
            "/work/node_modules/.bin/busy-cli",
            "build",
        ],
        env=environment,
        check=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
    )


def locked_cli_version(app: Path) -> str:
    """
    Read the exact installed BUSY CLI version from npm's lock file.

    An absent or ambiguous lock entry is rejected before installation.
    """
    lock = json.loads((app / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock.get("packages")
    cli = (
        packages.get("node_modules/@busy-app/cli")
        if isinstance(packages, dict)
        else None
    )
    version = cli.get("version") if isinstance(cli, dict) else None
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise CheckError("build_lockfile", "package-lock.json must pin @busy-app/cli")
    return version


def locked_pnpm_versions(app: Path) -> tuple[str, str]:
    """
    Read the pinned pnpm and BUSY CLI versions from package metadata and lock.

    Reject missing, ambiguous or unpinned versions before running a build.
    """
    package = json.loads((app / "package.json").read_text(encoding="utf-8"))
    manager = package.get("packageManager")
    if (
        not isinstance(manager, str)
        or not manager.startswith("pnpm@")
        or not VERSION.fullmatch(manager[5:])
    ):
        raise CheckError("build_lockfile", "package.json must pin the pnpm version")
    lock = yaml.safe_load((app / "pnpm-lock.yaml").read_text(encoding="utf-8"))
    importer = lock.get("importers", {}).get(".", {}) if isinstance(lock, dict) else {}
    versions = []
    for section in ("dependencies", "devDependencies"):
        dependency = importer.get(section, {}).get("@busy-app/cli")
        if isinstance(dependency, dict) and isinstance(dependency.get("version"), str):
            versions.append(dependency["version"].split("(", 1)[0])
    if len(versions) != 1 or not VERSION.fullmatch(versions[0]):
        raise CheckError(
            "build_lockfile", "pnpm-lock.yaml must pin one @busy-app/cli version"
        )
    return manager[5:], versions[0]


def run_npm_build(app: Path, home: Path) -> str:
    """
    Install locked dependencies without lifecycle scripts and run BUSY CLI.

    Only explicitly selected environment variables reach the build process.
    This must run in a separate unprivileged job without secrets or tokens.
    """
    version = locked_cli_version(app)
    allowed = (
        "PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {"HOME": str(home), "CI": "true", "npm_config_ignore_scripts": "true"}
    )
    subprocess.run(
        ["npm", "ci", "--ignore-scripts"],
        cwd=app,
        env=environment,
        check=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
    )
    executable = app / "node_modules" / ".bin" / "busy-cli"
    if not executable.is_file():
        raise CheckError("build", "Installed @busy-app/cli has no busy-cli executable")
    run_cli_isolated(app, environment)
    return version


PNPM_ENV_ALLOWED = (
    "PATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
)


def build_environment(home: Path) -> dict[str, str]:
    """The only variables the build sees: proxies, a private HOME, no scripts, no secrets."""
    environment = {
        key: os.environ[key] for key in PNPM_ENV_ALLOWED if key in os.environ
    }
    environment.update(
        {"HOME": str(home), "CI": "true", "npm_config_ignore_scripts": "true"}
    )
    return environment


def run_pnpm_build(app: Path, home: Path) -> str:
    """
    Install the frozen pnpm lock without lifecycle scripts or pnpm hooks.

    The caller supplies a separate unprivileged environment without secrets.
    """
    pnpm_version, cli_version = locked_pnpm_versions(app)
    environment = build_environment(home)
    pnpm = ["npx", "--yes", f"--package=pnpm@{pnpm_version}", "pnpm"]
    actual = subprocess.run(
        [*pnpm, "--version"],
        cwd=app,
        env=environment,
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual != pnpm_version:
        raise CheckError(
            "build_lockfile", f"Expected pnpm {pnpm_version}, got {actual}"
        )
    install = ["install", "--frozen-lockfile", "--ignore-scripts", "--ignore-pnpmfile"]
    subprocess.run(
        [*pnpm, *install],
        cwd=app,
        env=environment,
        check=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
    )
    if not (app / "node_modules" / ".bin" / "busy-cli").is_file():
        raise CheckError("build", "Installed @busy-app/cli has no busy-cli executable")
    run_cli_isolated(app, environment)
    return cli_version


def run_build(app: Path, home: Path) -> str:
    """
    Choose a single supported lock file and run its corresponding builder.

    Conflicting lock files fail rather than selecting a package manager implicitly.
    """
    npm = (app / "package-lock.json").is_file()
    pnpm = (app / "pnpm-lock.yaml").is_file()
    if npm == pnpm:
        raise CheckError("build_lockfile", "Provide exactly one supported lock file")
    return run_npm_build(app, home) if npm else run_pnpm_build(app, home)
