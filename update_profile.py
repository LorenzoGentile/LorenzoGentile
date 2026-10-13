"""Refresh the profile cards using GitHub activity. Python stdlib only."""

import calendar
from collections import Counter
import html
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HISTORY_QUERY = """
query History($owner: String!, $name: String!, $author: ID!,
              $since: GitTimestamp, $until: GitTimestamp!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, after: $cursor, author: {id: $author}, since: $since, until: $until) {
        pageInfo { hasNextPage endCursor }
        nodes { oid additions deletions committedDate parents { totalCount } author { user { id } } }
      }
    } } }
  }
}
"""


def period_start(period, now):
    if period == "all-time":
        return None
    if period != "last-year":
        raise ValueError("stats.period must be last-year or all-time.")
    year = now.year - 1
    day = min(now.day, calendar.monthrange(year, now.month)[1])
    return now.replace(year=year, day=day).isoformat()


class GitHub:
    def __init__(self, token):
        if not token:
            raise RuntimeError("A GitHub token is required. Set ACCESS_TOKEN for private statistics.")
        self.token = token

    def request(self, path, payload=None):
        request = Request(
            "https://api.github.com/" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-profile-stats",
            },
        )
        # Never log response bodies or request URLs: they may contain private metadata.
        try:
            with urlopen(request, timeout=60) as response:
                if "partial-results" in response.headers.get("X-GitHub-SSO", ""):
                    raise RuntimeError("GitHub returned partial SSO access. Authorize the token before refreshing.")
                result = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"GitHub request failed (HTTP {error.code}). Check token access and rate limits.") from None
        except (URLError, TimeoutError):
            raise RuntimeError("GitHub request failed due to a network error.") from None
        if isinstance(result, dict) and result.get("errors"):
            raise RuntimeError("GitHub GraphQL returned errors; statistics were not refreshed.")
        return result

    def repositories(self, path):
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            batch = self.request(f"{path}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise RuntimeError("Unexpected GitHub repository response.")
            yield from batch
            if len(batch) < 100:
                break
            page += 1


def fetch_stats(config, api, now=None):
    now = now or datetime.now(timezone.utc)
    since = period_start(config["stats"]["period"], now)
    until = now.isoformat()
    local_timezone = ZoneInfo(config["stats"].get("timezone", "Europe/Rome"))
    username = quote(config["username"], safe="")
    user = api.request(f"users/{username}")
    repositories = list(api.repositories(f"users/{username}/repos?type=owner"))
    include_private = config["stats"]["includePrivate"]
    if include_private:
        viewer = api.request("user")
        if viewer["node_id"] != user["node_id"]:
            raise RuntimeError("Use a personal token belonging to the configured profile account.")
        repositories.extend(api.repositories("user/repos?affiliation=owner,collaborator,organization_member"))
        if not any(repo["private"] for repo in repositories):
            raise RuntimeError("Private statistics are enabled, but no private repositories are accessible. Check ACCESS_TOKEN permissions.")
    repositories = {repo["id"]: repo for repo in repositories}.values()

    seen, days = set(), set()
    weekdays = dict.fromkeys(calendar.day_name, 0)
    times = dict.fromkeys(("Morning", "Afternoon", "Evening", "Night"), 0)
    languages = Counter()
    commits = additions = deletions = 0
    for repo in repositories:
        if repo["private"] and not include_private:
            continue
        cursor = None
        contributed = False
        while True:
            result = api.request("graphql", {
                "query": HISTORY_QUERY,
                "variables": {
                    "owner": repo["owner"]["login"], "name": repo["name"],
                    "author": user["node_id"], "since": since, "until": until, "cursor": cursor,
                },
            })
            repository = result.get("data", {}).get("repository")
            if repository is None:
                raise RuntimeError("A repository became inaccessible; statistics were not refreshed.")
            if repository.get("defaultBranchRef", False) is None:
                break  # Empty repository.
            history = repository.get("defaultBranchRef", {}).get("target", {}).get("history")
            if not history:
                raise RuntimeError("GitHub returned incomplete commit history.")
            for commit in history["nodes"]:
                author = (commit.get("author") or {}).get("user") or {}
                # Ignore merges and deduplicate hashes shared by forks/mirrors.
                if author.get("id") != user["node_id"] or commit["parents"]["totalCount"] > 1:
                    continue
                contributed = True
                if commit["oid"] in seen:
                    continue
                if not all(type(commit.get(field)) is int and commit[field] >= 0 for field in ("additions", "deletions")):
                    raise RuntimeError("GitHub returned incomplete line counts.")
                seen.add(commit["oid"])
                commits += 1
                additions += commit["additions"]
                deletions += commit["deletions"]
                local_date = datetime.fromisoformat(commit["committedDate"].replace("Z", "+00:00")).astimezone(local_timezone)
                days.add(local_date.date().isoformat())
                weekdays[calendar.day_name[local_date.weekday()]] += 1
                times[("Night", "Morning", "Afternoon", "Evening")[local_date.hour // 6]] += 1
            page_info = history["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError("GitHub returned incomplete pagination.")
            cursor = next_cursor
        if contributed and not repo.get("fork", False):
            owner = quote(repo["owner"]["login"], safe="")
            name = quote(repo["name"], safe="")
            detected = api.request(f"repos/{owner}/{name}/languages")
            if not isinstance(detected, dict) or not all(type(size) is int and size >= 0 for size in detected.values()):
                raise RuntimeError("GitHub returned incomplete language statistics.")
            languages.update({language: size for language, size in detected.items() if size > 0})

    # Only aggregate data leaves this function. No private names, hashes or messages.
    return {
        "commits": commits, "additions": additions, "deletions": deletions,
        "active_days": len(days), "since": since, "until": until,
        "created_at": user["created_at"],
        "scope": "Public + private" if include_private else "Public",
        "weekdays": weekdays, "times": times,
        "languages": dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        "timezone": str(local_timezone),
    }


PALETTES = {
    "dark": {"bg": "#0d1117", "border": "#30363d", "text": "#c9d1d9", "muted": "#8b949e", "blue": "#58a6ff", "orange": "#ffa657", "green": "#7ee787", "red": "#ff7b72"},
    "light": {"bg": "#ffffff", "border": "#d0d7de", "text": "#24292f", "muted": "#57606a", "blue": "#0969da", "orange": "#9a4800", "green": "#1a7f37", "red": "#cf222e"},
}


def language_summary(languages):
    """Fit the leading detected languages on the compact profile card."""
    names = list(languages)
    if not names:
        return "No language data"
    selected = names[:3]
    while len(", ".join(selected)) > 32 and len(selected) > 1:
        selected.pop()
    summary = ", ".join(selected)
    return summary if len(summary) <= 32 else summary[:31] + "…"


def render(mode, config, stats, portrait):
    palette = PALETTES[mode]
    escape = lambda value: html.escape(str(value), quote=True)

    def text(x, y, value, color=None, size=14, weight=500, attributes=""):
        return f'<text x="{x}" y="{y}" fill="{color or palette["text"]}" font-size="{size}" font-weight="{weight}"{attributes}>{escape(value)}</text>'

    def row(y, label, value, color=None):
        attributes = f' text-anchor="end" paint-order="stroke" stroke="{palette["bg"]}" stroke-width="8" stroke-linejoin="round"'
        return (
            text(400, y, label, palette["orange"])
            + f'<path d="M{410 + len(label) * 8.5} {y - 4}H840" stroke="{palette["border"]}" stroke-width="2" stroke-dasharray="2 6"/>'
            + text(840, y, value, color, attributes=attributes)
        )

    def section(y, label):
        return text(400, y, f"— {label}", palette["blue"]) + f'<path d="M{430 + len(label) * 8.5} {y - 4}H840" stroke="{palette["border"]}"/>'

    period = "All time" if config["stats"]["period"] == "all-time" else "Last 12 months"
    # Preserve the original card's ASCII portrait and column positions.
    art = "\n".join(
        f'<text x="24" y="{76 + index * 8.25:g}" xml:space="preserve" textLength="{len(line) * 4.2:g}" lengthAdjust="spacingAndGlyphs">{escape(line)}</text>'
        for index, line in enumerate(portrait.splitlines()) if line
    )
    projects = "\n".join(text(400, 270 + index * 26, project) for index, project in enumerate(config["projects"]))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="880" height="548" viewBox="0 0 880 548" role="img" aria-labelledby="title desc">
<title id="title">{escape(config["name"])} — GitHub profile</title>
<desc id="desc">ASCII style portrait. Detected repository languages: {escape(", ".join(stats["languages"]) or "No language data")}. {period}; {stats["scope"]} accessible repositories, default branches, non-merge commits authored by {escape(config["username"])}. {stats["commits"]} commits, {stats["additions"]} lines added, {stats["deletions"]} lines removed, {stats["active_days"]} active days. Line counts include all tracked text files.</desc>
<rect x="1" y="1" width="878" height="546" rx="12" fill="{palette["bg"]}" stroke="{palette["border"]}"/>
<g font-family="'DejaVu Sans Mono', 'SFMono-Regular', Consolas, monospace">
{text(28, 34, "●", palette["orange"], 11)}{text(48, 34, "●", palette["green"], 11)}{text(68, 34, "●", palette["blue"], 11)}
{text(101, 34, "~/" + config["username"], palette["muted"], 12)}
<g fill="{palette["muted"]}" font-size="7" font-weight="600">{art}</g>
{text(400, 83, config["username"].lower() + "@github", palette["blue"], 16, 600)}
<path d="M400 101H840" stroke="{palette["border"]}"/>
{row(135, "Name:", config["name"])}
{row(164, "GitHub since:", stats["created_at"][:10])}
{row(193, "Top languages:", language_summary(stats["languages"]))}
{section(240, "Public projects")}
{projects}
{section(362, "Activity · " + period)}
{row(393, "Commits:", f'{stats["commits"]:,}')}
{row(422, "Lines added:", f'+{stats["additions"]:,}', palette["green"])}
{row(451, "Lines removed:", f'−{stats["deletions"]:,}', palette["red"])}
{row(480, "Active days:", f'{stats["active_days"]:,}')}
{text(28, 483, stats["scope"] + " · accessible repos", palette["muted"], 10)}
{text(28, 501, "Default branches · non-merge commits", palette["muted"], 10)}
{text(28, 519, "Updated " + stats["until"][:10], palette["muted"], 10)}
{text(400, 519, "Lines include code, docs and generated files", palette["muted"], 10)}
</g></svg>
'''


def render_activity(mode, config, stats):
    """Render aggregate coding patterns without disclosing any repository metadata."""
    p = PALETTES[mode]
    period = "All time" if config["stats"]["period"] == "all-time" else "Last 12 months"
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="880" height="610" viewBox="0 0 880 610" role="img" aria-labelledby="title desc">',
        '<title id="title">Coding rhythm</title>',
        f'<desc id="desc">{html.escape(period)}. Commits by weekday and time of day in {html.escape(stats["timezone"])}. Language shares use current code bytes reported by GitHub across non-fork repositories with authored commits, not personal authorship or coding time.</desc>',
        f'<rect x="1" y="1" width="878" height="608" rx="12" fill="{p["bg"]}" stroke="{p["border"]}"/>',
        '<g font-family="\'DejaVu Sans Mono\', \'SFMono-Regular\', Consolas, monospace">',
    ]

    def text(x, y, value, color=None, size=12, anchor="start"):
        out.append(f'<text x="{x}" y="{y}" fill="{color or p["text"]}" font-size="{size}" text-anchor="{anchor}">{html.escape(str(value))}</text>')

    def bar(x, y, width, fraction, color):
        out.append(f'<rect x="{x}" y="{y}" width="{width}" height="7" rx="3.5" fill="{p["border"]}"/>')
        if fraction:
            out.append(f'<rect x="{x}" y="{y}" width="{width * fraction:.2f}" height="7" rx="3.5" fill="{color}"/>')

    text(28, 36, "Coding rhythm", p["blue"], 18)
    text(28, 60, f'{period} · {stats["scope"]} · {stats["timezone"]}', p["muted"], 11)
    text(28, 102, "By day of the week", p["orange"], 14)
    text(413, 102, "Scale: 0–30%", p["muted"], 10, "end")
    text(468, 102, "By time of day", p["orange"], 14)
    for index, (day, count) in enumerate(stats["weekdays"].items()):
        y = 133 + index * 27
        fraction = count / stats["commits"] if stats["commits"] else 0
        text(28, y, day[:3])
        bar(75, y - 8, 196, min(fraction / 0.30, 1), p["blue"])
        text(413, y, f"{count:,} · {fraction:.1%}", p["muted"], 11, "end")
    ranges = ("06–12", "12–18", "18–24", "00–06")
    for index, ((label, count), hours) in enumerate(zip(stats["times"].items(), ranges)):
        y = 133 + index * 46
        fraction = count / stats["commits"] if stats["commits"] else 0
        text(468, y, f"{label} {hours}", size=11)
        text(840, y, f"{count:,} · {fraction:.1%}", p["muted"], 11, "end")
        bar(468, y + 9, 372, fraction, p["green"])
    if stats["commits"]:
        busiest = max(stats["weekdays"], key=stats["weekdays"].get)
        text(468, 315, f"Most commits on {busiest}", p["muted"], 11)
    else:
        text(468, 315, "No commits in this period", p["muted"], 11)
    out.append(f'<path d="M28 339H852" stroke="{p["border"]}"/>')
    text(28, 371, "Languages across active repositories", p["orange"], 14)
    text(28, 393, "Current code size (bytes) · GitHub language detection", p["muted"], 11)
    languages = list(stats["languages"].items())
    if len(languages) > 5:
        languages = languages[:4] + [("Other languages", sum(count for _, count in languages[4:]))]
    total = sum(stats["languages"].values())
    for index, (language, count) in enumerate(languages):
        y = 426 + index * 29
        fraction = count / total
        text(28, y, language, size=11)
        bar(220, y - 8, 425, fraction, p["blue"])
        text(840, y, f'{fraction:.1%}', p["muted"], 11, "end")
    if not languages:
        text(28, 426, "No repository language data for this period", p["muted"], 12)
    text(28, 583, "Default branches · non-merge commits · unique commit hashes", p["muted"], 10)
    text(840, 583, "Updated " + stats["until"][:10], p["muted"], 10, "end")
    out.append("</g></svg>\n")
    return "\n".join(out)


def main():
    config = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
    token = os.environ.get("ACCESS_TOKEN")
    if not config["stats"]["includePrivate"]:
        token = token or os.environ.get("GITHUB_TOKEN")
    stats = fetch_stats(config, GitHub(token))
    portrait = (ROOT / "assets/portrait.txt").read_text(encoding="utf-8")
    cards = {}
    for mode in PALETTES:
        cards[f"profile-{mode}"] = render(mode, config, stats, portrait)
        cards[f"activity-{mode}"] = render_activity(mode, config, stats)
    # Fetch and render everything before replacing either card.
    for name, svg in cards.items():
        target = ROOT / f"assets/{name}.svg"
        temporary = target.with_suffix(".svg.tmp")
        temporary.write_text(svg, encoding="utf-8")
        temporary.replace(target)
    print(f'Updated profile: {stats["commits"]} commits, +{stats["additions"]} / -{stats["deletions"]} lines, {stats["active_days"]} active days.')


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as error:
        print(f"Profile refresh failed: {error}", file=sys.stderr)
        sys.exit(1)
