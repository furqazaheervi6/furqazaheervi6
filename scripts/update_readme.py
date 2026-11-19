#!/usr/bin/env python3
"""Regenerate the dynamic blocks of README.md.

Standard library only. No third-party hosted widgets.

Blocks (delimited by HTML comment markers in README.md):
  NOW       from now.json
  FEATURED  from config/projects.json (allowlist), labelled with the four-state rule
  ACTIVITY  recent commits by the profile owner in allowlisted repos (GitHub REST API, read-only)
  WRITING   latest posts from an RSS feed; renders nothing until posts exist

Usage:
  python3 scripts/update_readme.py           # rewrite README.md in place
  python3 scripts/update_readme.py --check   # exit 1 if README.md is out of date (no write)
  python3 scripts/update_readme.py --offline # skip network (ACTIVITY and WRITING keep their current content)

Environment: GITHUB_TOKEN (optional locally, provided by Actions in CI) is used for read-only API calls.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
NOW_JSON = ROOT / "now.json"
PROJECTS_JSON = ROOT / "config" / "projects.json"

API = "https://api.github.com"
USER_AGENT = "profile-readme-updater"
MAX_BYTES = 1_000_000

# Four-state rule. "Proposed" is deliberately not renderable.
STATES = ("Shipped", "Built-unpublished", "Designed")


class ConfigError(Exception):
    """Invalid configuration. Fail loudly rather than publish something wrong."""


# ----------------------------------------------------------------------------- text helpers

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()|~])")


def md_escape(text: str) -> str:
    """Escape text coming from outside (commit messages, post titles) so it cannot inject markup."""
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("@", "@​")  # no mentions
    text = re.sub(r"#(?=\d)", "", text)  # no issue/PR cross-links
    return _MD_SPECIAL.sub(r"\\\1", text)


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def fmt_date(d: dt.datetime) -> str:
    return d.strftime("%b %d, %Y").replace(" 0", " ")


# ----------------------------------------------------------------------------- config

def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"missing file: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} is not valid JSON: {e}") from e


def validate_projects(cfg: dict) -> list[dict]:
    """Return enabled projects after validation. Raises ConfigError on anything unsafe."""
    projects = cfg.get("projects")
    if not isinstance(projects, list):
        raise ConfigError("projects.json: 'projects' must be a list")
    enabled = []
    seen = set()
    for p in projects:
        repo = p.get("repo", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ConfigError(f"bad repo value: {repo!r}")
        if repo in seen:
            raise ConfigError(f"duplicate repo: {repo}")
        seen.add(repo)
        if not p.get("enabled", False):
            continue
        state = p.get("state")
        if state not in STATES:
            raise ConfigError(
                f"{repo}: state must be one of {STATES} (got {state!r}); 'Proposed' items never go on a page"
            )
        for key in ("name", "summary"):
            if not str(p.get(key, "")).strip():
                raise ConfigError(f"{repo}: '{key}' is required")
        enabled.append(p)
    return enabled


# ----------------------------------------------------------------------------- network

def http_get(url: str, accept: str = "application/json", token: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token and url.startswith(API):
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (https only, checked by callers)
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ConfigError(f"response too large: {url}")
    return data


def api_json(path: str, token: str | None):
    return json.loads(http_get(API + path, token=token))


def verify_repo(project: dict, token: str | None) -> dict:
    """The repo must exist, be public, and not be a fork."""
    info = api_json(f"/repos/{project['repo']}", token)
    if info.get("private"):
        raise ConfigError(f"{project['repo']} is private; refusing to render")
    if info.get("fork"):
        raise ConfigError(f"{project['repo']} is a fork; forks are excluded")
    return info


def fetch_commits(project: dict, info: dict, login: str, since: dt.datetime, token: str | None) -> list[dict]:
    branches = project.get("branches") or [info["default_branch"]]
    seen: set[str] = set()
    out: list[dict] = []
    for br in branches:
        q = urllib.parse.urlencode(
            {
                "sha": br,
                "author": login,
                "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "per_page": 30,
            }
        )
        for c in api_json(f"/repos/{project['repo']}/commits?{q}", token):
            sha = c["sha"]
            if sha in seen:
                continue
            seen.add(sha)
            when = dt.datetime.fromisoformat(c["commit"]["author"]["date"].replace("Z", "+00:00"))
            out.append(
                {
                    "when": when,
                    "repo": project["repo"],
                    "sha": sha,
                    "message": c["commit"]["message"].split("\n", 1)[0],
                    "url": c["html_url"],
                }
            )
    return out


# ----------------------------------------------------------------------------- renderers

def render_now(now: dict) -> str:
    items = now.get("items") or []
    if not items:
        return ""
    lines = ["### Now", ""]
    for it in items:
        role = str(it.get("role", "")).strip()
        org = str(it.get("org", "")).strip()
        if not role or not org:
            raise ConfigError("now.json: every item needs 'role' and 'org'")
        detail = str(it.get("detail", "")).strip()
        line = f"- **{md_escape(role)}** at {md_escape(org)}"
        if detail:
            line += f". {md_escape(detail)}"
        lines.append(line)
    updated = str(now.get("updated", "")).strip()
    if updated:
        lines += ["", f"<sub>Updated {md_escape(updated)}</sub>"]
    return "\n".join(lines)


def render_featured(projects: list[dict]) -> str:
    if not projects:
        return ""
    lines = ["### Featured projects", ""]
    for p in projects:
        lines.append(
            f"- **[{md_escape(p['name'])}](https://github.com/{p['repo']})** "
            f"({p['state']}). {md_escape(p['summary'])}"
        )
    return "\n".join(lines)


def render_activity(commits: list[dict], limit: int) -> str:
    commits = sorted(commits, key=lambda c: (c["when"], c["sha"]), reverse=True)[:limit]
    if not commits:
        return ""
    lines = ["### Recent activity", ""]
    for c in commits:
        repo_name = c["repo"].split("/", 1)[1]
        lines.append(
            f"- {fmt_date(c['when'])}. [{md_escape(repo_name)}](https://github.com/{c['repo']}): "
            f"{md_escape(truncate(c['message'], 90))} "
            f"([`{c['sha'][:7]}`]({c['url']}))"
        )
    return "\n".join(lines)


def parse_feed(xml_bytes: bytes, link_prefix: str, limit: int) -> list[dict]:
    head = xml_bytes[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ConfigError("feed contains a DOCTYPE/ENTITY declaration; refusing to parse")
    root = ET.fromstring(xml_bytes)
    posts = []
    for item in root.iterfind("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not link.startswith(link_prefix):
            continue
        try:
            when = email.utils.parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            when = None
        posts.append({"title": title, "link": link, "when": when})
    posts.sort(key=lambda p: p["when"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    return posts[:limit]


def render_writing(posts: list[dict]) -> str:
    if not posts:
        return ""
    lines = ["### Latest writing", ""]
    for p in posts:
        when = f" ({fmt_date(p['when'])})" if p["when"] else ""
        lines.append(f"- [{md_escape(truncate(p['title'], 100))}]({p['link']}){when}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- README splice

def block_re(name: str) -> re.Pattern:
    return re.compile(
        rf"(<!-- {name}:START -->)(.*?)(<!-- {name}:END -->)", re.DOTALL
    )


def get_block(text: str, name: str) -> str:
    m = block_re(name).search(text)
    if not m:
        raise ConfigError(f"README.md is missing markers for {name}")
    return m.group(2).strip("\n")


def set_block(text: str, name: str, content: str) -> str:
    if not block_re(name).search(text):
        raise ConfigError(f"README.md is missing markers for {name}")
    body = f"\n{content}\n" if content else "\n"
    return block_re(name).sub(lambda m: m.group(1) + body + m.group(3), text, count=1)


# ----------------------------------------------------------------------------- main

def build(readme_text: str, now: dict, cfg: dict, offline: bool, token: str | None,
          today: dt.datetime | None = None) -> str:
    settings = cfg.get("settings", {})
    projects = validate_projects(cfg)
    text = readme_text
    text = set_block(text, "NOW", render_now(now))
    text = set_block(text, "FEATURED", render_featured(projects))

    if offline:
        return text

    today = today or dt.datetime.now(dt.timezone.utc)
    since = today - dt.timedelta(days=int(settings.get("activity_window_days", 90)))
    login = settings.get("login", "")
    commits: list[dict] = []
    for p in projects:
        if not p.get("show_activity", False):
            continue
        info = verify_repo(p, token)  # any failure here fails the run loudly
        per_repo = sorted(fetch_commits(p, info, login, since, token),
                          key=lambda c: (c["when"], c["sha"]), reverse=True)
        commits += per_repo[: int(settings.get("activity_per_repo", 3))]
    text = set_block(text, "ACTIVITY", render_activity(commits, int(settings.get("activity_limit", 5))))

    # Writing: a missing feed (404) means "no posts yet". A transient error keeps the existing block.
    feed_url = settings.get("feed_url", "")
    prefix = settings.get("feed_link_prefix", "")
    if feed_url:
        if not feed_url.startswith("https://") or not prefix.startswith("https://"):
            raise ConfigError("feed_url and feed_link_prefix must be https URLs")
        try:
            posts = parse_feed(
                http_get(feed_url, accept="application/rss+xml, application/xml"),
                prefix,
                int(settings.get("writing_limit", 5)),
            )
            text = set_block(text, "WRITING", render_writing(posts))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                text = set_block(text, "WRITING", "")
            else:
                print(f"::warning::feed returned HTTP {e.code}; keeping existing Latest writing block")
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
            print(f"::warning::feed unavailable ({type(e).__name__}); keeping existing Latest writing block")
    return text


def main(argv: list[str]) -> int:
    check = "--check" in argv
    offline = "--offline" in argv
    token = os.environ.get("GITHUB_TOKEN") or None
    try:
        now = load_json(NOW_JSON)
        cfg = load_json(PROJECTS_JSON)
        old = README.read_text(encoding="utf-8")
        new = build(old, now, cfg, offline, token)
    except (ConfigError, urllib.error.URLError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if new == old:
        print("README.md already up to date")
        return 0
    if check:
        print("README.md is out of date")
        return 1
    README.write_text(new, encoding="utf-8")
    print("README.md updated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
