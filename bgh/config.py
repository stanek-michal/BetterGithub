import fnmatch
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
DB_PATH = ROOT / "bgh.db"


@dataclass
class Config:
    port: int = 7777
    sync_interval_seconds: int = 120
    repos: list[str] = field(default_factory=list)
    issue_prefetch_days: int = 14
    hidden_authors: list[str] = field(default_factory=list)
    hidden_comment_patterns: list[str] = field(default_factory=list)
    list_hidden_labels: list[str] = field(default_factory=list)
    summarizer: dict = field(default_factory=dict)
    views: list[dict] = field(default_factory=list)


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"No {CONFIG_PATH.name} found. Copy config.example.toml to {CONFIG_PATH} and set your repos.")
    raw = tomllib.loads(CONFIG_PATH.read_text())
    known = Config.__dataclass_fields__.keys()
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    if not cfg.repos or "owner/repo" in cfg.repos:
        raise SystemExit(f'Set `repos` in {CONFIG_PATH} to the repos you want to sync, e.g. repos = ["python/cpython"].')
    return cfg


def gh_token() -> str:
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def author_key(login: str | None, typename: str | None) -> str:
    """Normalized author name used for hide matching: bots get a [bot] suffix."""
    if not login:
        return "ghost"
    if typename == "Bot" and not login.endswith("[bot]"):
        return f"{login}[bot]"
    return login


def _glob(p: str) -> str:
    # Only * and ? are wildcards; brackets are literal so "*[bot]" works as expected.
    return p.lower().replace("[", "\0").replace("]", "[]]").replace("\0", "[[]")


def matches_any(name: str, patterns) -> bool:
    name = name.lower()
    return any(fnmatch.fnmatchcase(name, _glob(p)) for p in patterns)
