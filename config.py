"""Loads config.toml and environment secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Repo:
    key: str            # short name the agent uses, e.g. "liwords"
    full_name: str      # "owner/repo" on GitHub
    description: str    # what lives in this repo; helps the agent route reports


@dataclass
class Config:
    discord_token: str
    anthropic_api_key: str
    github_token: str
    allowed_user_ids: set[int]
    pr_user_ids: set[int]
    repos: dict[str, Repo]
    model: str = "claude-haiku-5-5"
    default_labels: list[str] = field(default_factory=list)
    context_messages: int = 15
    max_agent_turns: int = 12
    session_ttl_hours: int = 24
    dry_run: bool = False
    extra_instructions: str = ""


def _ids(values) -> set[int]:
    return {int(v) for v in (values or [])}


def load_config(path: str | os.PathLike = "config.toml") -> Config:
    data = tomllib.loads(Path(path).read_text())

    repos: dict[str, Repo] = {}
    for key, r in (data.get("repos") or {}).items():
        full = r["github"]
        if full.count("/") != 1:
            raise ValueError(f"repos.{key}.github must look like 'owner/repo', got {full!r}")
        repos[key] = Repo(key=key, full_name=full, description=r.get("description", ""))
    if not repos:
        raise ValueError("config.toml needs at least one [repos.<name>] section")

    access = data.get("access", {})
    allowed = _ids(access.get("allowed_user_ids"))
    if not allowed:
        raise ValueError("access.allowed_user_ids is empty; add your Discord user ID")

    bot = data.get("bot", {})

    def env(name: str) -> str:
        val = os.environ.get(name, "").strip()
        if not val:
            raise ValueError(f"Missing environment variable {name} (put it in .env)")
        return val

    return Config(
        discord_token=env("DISCORD_TOKEN"),
        anthropic_api_key=env("ANTHROPIC_API_KEY"),
        github_token=env("GITHUB_TOKEN"),
        allowed_user_ids=allowed,
        pr_user_ids=_ids(access.get("pr_user_ids")),
        repos=repos,
        model=bot.get("model", "claude-haiku-5-5"),
        default_labels=list(bot.get("default_labels", [])),
        context_messages=int(bot.get("context_messages", 15)),
        max_agent_turns=int(bot.get("max_agent_turns", 12)),
        session_ttl_hours=int(bot.get("session_ttl_hours", 24)),
        dry_run=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
        or bool(bot.get("dry_run", False)),
        extra_instructions=bot.get("extra_instructions", ""),
    )
