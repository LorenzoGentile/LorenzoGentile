import copy
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

import update_profile as profile


NOW = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)
CONFIG = {
    "username": "example", "name": "A & B", "languages": ["Python", "R"],
    "projects": ["<project>"], "stats": {"includePrivate": True, "period": "last-year"},
}
USER = {"node_id": "account-id", "created_at": "2018-02-26T10:00:00Z"}


def repository(id, private=False):
    return {"id": id, "name": f"repository-{id}", "private": private, "owner": {"login": "owner"}, "language": "Python" if id == 1 else "TypeScript"}


def commit(oid, additions=10, deletions=3, author="account-id", parents=1, day="2026-10-12"):
    return {
        "oid": oid, "additions": additions, "deletions": deletions,
        "author": {"user": {"id": author}}, "parents": {"totalCount": parents},
        "committedDate": day + "T15:00:00Z",
    }


def history(nodes, cursor=None):
    return {"data": {"repository": {"defaultBranchRef": {"target": {"history": {
        "nodes": nodes, "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
    }}}}}}


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.pages = {
            ("repository-1", None): history([commit("shared"), commit("merge", parents=2), commit("someone-else", author="another-id")], "next"),
            ("repository-1", "next"): history([commit("second", 20, 7)]),
            ("repository-2", None): history([commit("shared"), commit("private", 5, 1, day="2026-10-11")]),
            ("repository-3", None): {"data": {"repository": {"defaultBranchRef": None}}},
        }

    def repositories(self, path):
        if path.startswith("users/"):
            return [repository(1), repository(3)]
        return [repository(1), repository(2, private=True)]

    def request(self, path, payload=None):
        if path != "graphql":
            return USER
        variables = payload["variables"]
        self.calls.append(variables)
        return self.pages[(variables["name"], variables["cursor"])]


class StatisticsTests(unittest.TestCase):
    def test_counts_paginated_public_and_private_activity_once(self):
        api = FakeGitHub()
        stats = profile.fetch_stats(CONFIG, api, NOW)
        self.assertEqual((stats["commits"], stats["additions"], stats["deletions"], stats["active_days"]), (3, 35, 11, 2))
        self.assertEqual(len(api.calls), 4)
        self.assertTrue(all(call["since"] == "2025-10-13T10:00:00+00:00" for call in api.calls))
        self.assertNotIn("repository-", json.dumps(stats))
        self.assertNotIn("account-id", json.dumps(stats))
        self.assertNotIn("shared", json.dumps(stats))
        self.assertEqual(sum(stats["weekdays"].values()), stats["commits"])
        self.assertEqual(sum(stats["times"].values()), stats["commits"])
        self.assertEqual(stats["languages"], {"Python": 1, "TypeScript": 1})

    def test_public_only_does_not_read_private_repositories(self):
        config = copy.deepcopy(CONFIG)
        config["stats"] = {"includePrivate": False, "period": "all-time"}
        api = FakeGitHub()
        stats = profile.fetch_stats(config, api, NOW)
        self.assertEqual(stats["commits"], 2)
        self.assertEqual(stats["scope"], "Public")
        self.assertTrue(all(call["since"] is None for call in api.calls))
        self.assertNotIn("repository-2", [call["name"] for call in api.calls])

    def test_inaccessible_repository_and_missing_counts_abort(self):
        api = FakeGitHub()
        api.pages[("repository-2", None)] = {"data": {"repository": None}}
        with self.assertRaisesRegex(RuntimeError, "inaccessible"):
            profile.fetch_stats(CONFIG, api, NOW)
        api.pages[("repository-2", None)] = history([commit("private", additions=None)])
        with self.assertRaisesRegex(RuntimeError, "incomplete line counts"):
            profile.fetch_stats(CONFIG, api, NOW)

    def test_window_handles_leap_day(self):
        leap = datetime(2024, 2, 29, 12, tzinfo=timezone.utc)
        self.assertEqual(profile.period_start("last-year", leap), "2023-02-28T12:00:00+00:00")
        self.assertIsNone(profile.period_start("all-time", leap))
        with self.assertRaises(ValueError):
            profile.period_start("typo", leap)

    def test_rendered_cards_escape_text_and_show_matching_totals(self):
        stats = profile.fetch_stats(CONFIG, FakeGitHub(), NOW)
        for mode in profile.PALETTES:
            svg = profile.render(mode, CONFIG, stats, "<&>\n ascii")
            root = ET.fromstring(svg)
            text = " ".join(root.itertext())
            for expected in ("A & B", "<project>", "Commits:", "+35", "−11", "Public + private", "Last 12 months"):
                self.assertIn(expected, text)
            self.assertNotIn("Stars:", text)
            self.assertNotIn("repository-2", svg)
            activity = profile.render_activity(mode, CONFIG, stats)
            text = " ".join(ET.fromstring(activity).itertext())
            self.assertIn("Europe/Rome", text)
            self.assertIn("50.0%", text)
            self.assertNotIn("repository-2", activity)

    def test_coding_patterns_use_local_time_and_daylight_saving(self):
        api = FakeGitHub()
        late = commit("late")
        late["committedDate"] = "2026-10-11T22:30:00Z"  # Monday 00:30 in Rome.
        early_summer = commit("summer")
        early_summer["committedDate"] = "2026-07-01T04:30:00Z"  # 06:30, morning.
        early_winter = commit("winter")
        early_winter["committedDate"] = "2026-01-01T04:30:00Z"  # 05:30, night.
        api.pages[("repository-1", None)] = history([late, early_summer, early_winter])
        api.pages[("repository-2", None)] = history([])
        stats = profile.fetch_stats(CONFIG, api, NOW)
        self.assertEqual(stats["weekdays"]["Monday"], 1)
        self.assertEqual(stats["times"]["Night"], 2)
        self.assertEqual(stats["times"]["Morning"], 1)

    def test_empty_activity_is_rendered_without_division_by_zero(self):
        api = FakeGitHub()
        api.pages[("repository-1", None)] = history([])
        api.pages[("repository-2", None)] = history([])
        stats = profile.fetch_stats(CONFIG, api, NOW)
        self.assertEqual(stats["commits"], 0)
        for mode in profile.PALETTES:
            text = " ".join(ET.fromstring(profile.render_activity(mode, CONFIG, stats)).itertext())
            self.assertIn("No commits in this period", text)
            self.assertNotIn("Most commits", text)

    def test_api_failure_preserves_existing_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profile.json").write_text(json.dumps(CONFIG))
            (root / "assets").mkdir()
            for mode in profile.PALETTES:
                (root / f"assets/profile-{mode}.svg").write_text("existing card")
            with patch.object(profile, "ROOT", root), patch.object(profile, "fetch_stats", side_effect=RuntimeError("API failed")), patch.dict("os.environ", {"ACCESS_TOKEN": "test-token"}):
                with self.assertRaisesRegex(RuntimeError, "API failed"):
                    profile.main()
            for mode in profile.PALETTES:
                self.assertEqual((root / f"assets/profile-{mode}.svg").read_text(), "existing card")

    def test_private_mode_requires_access_token(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "workflow-token"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ACCESS_TOKEN"):
                profile.main()


class ApiTests(unittest.TestCase):
    def test_repository_pagination(self):
        api = profile.GitHub("test-token")
        with patch.object(api, "request", side_effect=[[repository(i) for i in range(100)], [repository(100)]]) as request:
            self.assertEqual(len(list(api.repositories("user/repos?affiliation=owner"))), 101)
            self.assertIn("page=2", request.call_args.args[0])

    def test_http_errors_do_not_leak_private_paths(self):
        error = HTTPError("https://api.github.com/repos/private-name", 403, "secret details", {}, None)
        with patch.object(profile, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                profile.GitHub("test-token").request("user")
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("private-name", str(caught.exception))
        self.assertNotIn("secret details", str(caught.exception))

    def test_graphql_errors_and_partial_sso_results_fail(self):
        for payload, headers in (({"errors": [{"message": "private metadata"}]}, {}), ([], {"X-GitHub-SSO": "partial-results; organizations=123"})):
            response = BytesIO(json.dumps(payload).encode())
            response.headers = headers
            with patch.object(profile, "urlopen", return_value=response):
                with self.assertRaises(RuntimeError) as caught:
                    profile.GitHub("test-token").request("graphql", {})
            self.assertNotIn("private metadata", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
