# Profile activity

The card shows commits, lines added, lines removed and active days during the last
12 months. Set `stats.period` in `profile.json` to `all-time` for all available
history, or `last-year` for a rolling 12-month window. The second card breaks down
commits by weekday and time of day, plus primary languages of active repositories.

## Counting rules

- Includes public repositories owned by the profile account. With
  `stats.includePrivate: true`, also includes repositories accessible to the token
  as owner, collaborator or organization member, including private repositories.
- Reads each repository's default branch. Work on other branches, deleted
  repositories and repositories the token cannot access is not included.
- Counts commits attributed by GitHub to the configured user. Merge commits are
  excluded; identical commit hashes are counted once across repositories/forks.
- Lines added and removed are GitHub's commit diff totals for **all tracked text
  files**, including code, documentation, lockfiles and generated files. They are
  cumulative changes, not the current codebase size or a measure of original code
  written. Rewriting a line contributes both an addition and a deletion.
- Active days, weekdays and time-of-day buckets use `stats.timezone` (initially
  `Europe/Rome`), including daylight-saving changes. The date window uses commit
  dates. Morning is 06–12, afternoon 12–18, evening 18–24 and night 00–06.
- Languages count each non-fork repository with your commits in the selected
  period once, using its current primary language reported by GitHub. Percentages
  are shares of those repositories, not shares of your code or coding time. Small
  categories are grouped when more than five languages are present.
- Only totals appear in the generated cards and logs. No private repository
  names, source, commit messages, hashes or per-repository statistics are saved.
- API failures abort the refresh before any card is written. Token permissions
  determine coverage; the script cannot discover repositories hidden from it.

GitHub documents the underlying [commit history and line-count fields](https://docs.github.com/en/graphql/reference/commits).

## Automatic refresh

The workflow runs daily at 04:17 UTC, on relevant changes to `master`, and manually
from the Actions tab. It becomes scheduled after this workflow reaches the default
branch. GitHub may delay scheduled runs or disable schedules in inactive public
repositories; the card displays its actual last refresh date.

For private activity, add a repository Actions secret named `ACCESS_TOKEN`:

1. Create a personal access token for the profile account with **Contents: read**
   access to the repositories to count. A fine-grained token is limited to one
   resource owner (user or organization). Select the owner that contains the
   private work you want to include; public personal repositories are also read.
2. Complete organization approval/SSO authorization where required. For private
   repositories across multiple owners, use an organization-approved credential
   covering those owners; one fine-grained token cannot span them.
3. In this repository, open **Settings → Secrets and variables → Actions → New
   repository secret**, name it `ACCESS_TOKEN`, and paste the token there.
4. Run **Actions → Update profile stats → Run workflow** after merging.

The built-in `GITHUB_TOKEN` commits only the generated cards; the personal token
is used only for reading statistics. A missing private token fails the refresh
instead of silently replacing private totals with public-only numbers.

See GitHub's [token setup documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
and [workflow token limits](https://docs.github.com/en/actions/concepts/security/github_token).

For public-only activity, set `stats.includePrivate` to `false`; the workflow can
then read statistics using its built-in token without a custom secret.

## Local use

Requires Python 3.12 or later; uses only the standard library.

```sh
python -m unittest discover -s tests -v
ACCESS_TOKEN="$(gh auth token)" python update_profile.py
```

Keep tokens in the environment or GitHub secrets, never in tracked files.

## Inspiration and optional telemetry

The coding-pattern sections are inspired by
[anmol098's profile](https://github.com/anmol098/anmol098), with original SVGs
matching this profile's ASCII card. Hours spent coding, editors and AI coding
usage need separate telemetry such as WakaTime; they cannot be inferred from
GitHub commits and are not fabricated here.
