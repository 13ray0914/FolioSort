from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import __version__

WORKSPACE_ENTRIES = (
    "assets",
    "lib",
    "profiles",
    "prompts",
    "schemas",
    "scripts",
    "windows",
    "check_environment.py",
    "config.example.json",
    "config.v4.defaults.json",
    "config.v4_1.defaults.json",
    "docker-compose.grobid.yml",
    "network_config.json",
    "requirements.txt",
    "requirements_v4.txt",
    "requirements_network.txt",
    "requirements_network_v4.txt",
    "requirements_specter2.txt",
    "run_pipeline.py",
)


def _template_root() -> Path:
    embedded = Path(__file__).resolve().parent / "_workspace"
    if embedded.is_dir():
        return embedded
    source_checkout = Path(__file__).resolve().parents[1]
    if (source_checkout / "scripts" / "review_app_server.py").is_file():
        return source_checkout
    raise RuntimeError("The installed FolioSort workspace template is missing; reinstall FolioSort.")


def _workspace(value: str | None) -> Path:
    raw = value or os.environ.get("REVIEW_ROOT") or os.getcwd()
    return Path(raw).expanduser().resolve()


def _require_workspace(path: Path) -> None:
    required = ("scripts/review_app_server.py", "run_pipeline.py", "config.json")
    missing = [name for name in required if not (path / name).is_file()]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Not a FolioSort workspace ({path}); missing: {joined}. Run 'foliosort init PATH'.")


def _runtime_environment(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["REVIEW_ROOT"] = str(path)
    env["REVIEW_PYTHON"] = sys.executable
    env["PYTHONUNBUFFERED"] = "1"
    return env


def command_init(args: argparse.Namespace) -> int:
    target = _workspace(args.directory)
    if target.exists() and any(target.iterdir()) and not args.force:
        raise SystemExit(f"Destination is not empty: {target}. Use --force to refresh application files.")
    target.mkdir(parents=True, exist_ok=True)
    template = _template_root()
    for name in WORKSPACE_ENTRIES:
        source = template / name
        if not source.exists():
            raise RuntimeError(f"The workspace template is incomplete; missing: {name}")
        destination = target / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
    config = target / "config.json"
    if not config.exists():
        shutil.copy2(target / "config.example.json", config)
    for directory in ("data", "db", "logs", "outputs", "backups"):
        (target / directory).mkdir(exist_ok=True)
    for script in (target / "scripts").glob("*.sh"):
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    print(f"FolioSort {__version__} workspace ready: {target}")
    print(f"Next: cd {target} && foliosort check")
    return 0


def command_check(args: argparse.Namespace) -> int:
    workspace = _workspace(args.workspace)
    _require_workspace(workspace)
    return subprocess.call(
        [sys.executable, str(workspace / "check_environment.py")],
        cwd=workspace,
        env=_runtime_environment(workspace),
    )


def command_serve(args: argparse.Namespace) -> int:
    workspace = _workspace(args.workspace)
    _require_workspace(workspace)
    command = [
        sys.executable,
        str(workspace / "scripts" / "review_app_server.py"),
        "--config",
        str(workspace / "config.json"),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    print(f"FolioSort {__version__}: http://{args.host}:{args.port}/")
    return subprocess.call(command, cwd=workspace, env=_runtime_environment(workspace))


def command_pipeline(args: argparse.Namespace) -> int:
    workspace = _workspace(args.workspace)
    _require_workspace(workspace)
    command = [sys.executable, "-u", str(workspace / "run_pipeline.py"), "--config", str(workspace / "config.json")]
    if args.from_step is not None:
        command.extend(("--from-step", str(args.from_step)))
    if args.to_step is not None:
        command.extend(("--to-step", str(args.to_step)))
    if args.ids:
        command.extend(("--ids", args.ids))
    if args.limit is not None:
        command.extend(("--limit", str(args.limit)))
    if args.force:
        command.append("--force")
    if args.local_references_only:
        command.append("--local-references-only")
    return subprocess.call(command, cwd=workspace, env=_runtime_environment(workspace))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foliosort", description="FolioSort workspace manager")
    parser.add_argument("--version", action="version", version=f"FolioSort {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create or refresh a runnable workspace")
    init_parser.add_argument("directory", nargs="?", default="foliosort-workspace")
    init_parser.add_argument("--force", action="store_true", help="refresh bundled application files in a non-empty workspace")
    init_parser.set_defaults(handler=command_init)

    check_parser = subparsers.add_parser("check", help="check required services and Python dependencies")
    check_parser.add_argument("--workspace", "-w")
    check_parser.set_defaults(handler=command_check)

    serve_parser = subparsers.add_parser("serve", aliases=["start"], help="serve the local dashboard in the foreground")
    serve_parser.add_argument("--workspace", "-w")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8766)
    serve_parser.set_defaults(handler=command_serve)

    pipeline_parser = subparsers.add_parser("pipeline", help="run the resumable core pipeline")
    pipeline_parser.add_argument("--workspace", "-w")
    pipeline_parser.add_argument("--from-step", type=int, choices=range(1, 12))
    pipeline_parser.add_argument("--to-step", type=int, choices=range(1, 12))
    pipeline_parser.add_argument("--ids")
    pipeline_parser.add_argument("--limit", type=int)
    pipeline_parser.add_argument("--force", action="store_true")
    pipeline_parser.add_argument("--local-references-only", action="store_true")
    pipeline_parser.set_defaults(handler=command_pipeline)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return int(args.handler(args))
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as exc:
        print(f"foliosort: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
