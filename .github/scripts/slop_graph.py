"""Render a GitHub-style contribution graph of lines deleted per day (last year) to slop-graph.svg.

Sums `deletions` of every commit authored by the token owner on each repo's default branch.
Needs GH_TOKEN with `repo` scope (+ org access) to see private repos.
"""
import datetime as dt
import json
import os
import urllib.request
from collections import Counter

TOKEN = os.environ["GH_TOKEN"]
OUT = "slop-graph.svg"
# ponytail: all-or-nothing per commit (skips vendored/generated dumps); filter paths per file if totals look off.
MAX_COMMIT_DELETIONS = 50_000


def gql(query, **variables):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}"},
    )
    body = json.load(urllib.request.urlopen(req))
    if "errors" in body:
        raise SystemExit(body["errors"])
    return body["data"]


def paginate(query, path, **variables):
    cursor = None
    while True:
        conn = gql(query, cursor=cursor, **variables)
        for key in path:
            conn = conn[key]
            if conn is None:
                return
        yield from conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return
        cursor = conn["pageInfo"]["endCursor"]


def author_and_repos(since):
    # contributionsCollection hides org-private repos as "restricted", so also walk every repo
    # the token can reach (owned, collaborator, org member) that was pushed to since `since`.
    viewer = gql(
        """{ viewer { id contributionsCollection { commitContributionsByRepository(maxRepositories: 100) {
          repository { nameWithOwner } } } } }"""
    )["viewer"]
    repos = {r["repository"]["nameWithOwner"] for r in viewer["contributionsCollection"]["commitContributionsByRepository"]}
    for r in paginate(
        """query($cursor: String) { viewer { repositories(first: 100, after: $cursor,
          affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
          ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
          orderBy: {field: PUSHED_AT, direction: DESC}) {
            pageInfo { hasNextPage endCursor } nodes { nameWithOwner pushedAt } } } }""",
        ("viewer", "repositories"),
    ):
        if not r["pushedAt"] or r["pushedAt"] < since:
            break
        repos.add(r["nameWithOwner"])
    return viewer["id"], sorted(repos)


def commits(repo, author_id, since):
    owner, name = repo.split("/")
    return paginate(
        """query($owner: String!, $name: String!, $author: ID!, $since: GitTimestamp!, $cursor: String) {
          repository(owner: $owner, name: $name) { defaultBranchRef { target { ... on Commit {
            history(first: 100, after: $cursor, since: $since, author: {id: $author}) {
              pageInfo { hasNextPage endCursor } nodes { oid committedDate deletions } } } } } } }""",
        ("repository", "defaultBranchRef", "target", "history"),
        owner=owner, name=name, author=author_id, since=since,
    )


def level(n, cuts):
    return 0 if n == 0 else 1 + sum(n > c for c in cuts)


def render(days, start, today):
    nonzero = sorted(v for v in days.values() if v)
    cuts = [nonzero[len(nonzero) * q // 4] for q in (1, 2, 3)] if nonzero else [0, 0, 0]

    cell, gap, left, top = 11, 3, 32, 28
    step = cell + gap
    parts, last_month = [], None
    for i in range((today - start).days + 1):
        d = start + dt.timedelta(i)
        col, row = i // 7, i % 7
        if row == 0 and d.month != last_month and col < 52:
            parts.append(f'<text x="{left + col * step}" y="{top - 8}">{d:%b}</text>')
            last_month = d.month
        n = days[d.isoformat()]
        parts.append(
            f'<rect class="l{level(n, cuts)}" x="{left + col * step}" y="{top + row * step}" '
            f'width="{cell}" height="{cell}" rx="2"><title>{n:,} lines removed on {d:%b %-d, %Y}</title></rect>'
        )
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        parts.append(f'<text x="0" y="{top + row * step + 9}">{label}</text>')

    width, legend_y = left + 53 * step + 8, top + 7 * step + 12
    parts.append(f'<text x="{left}" y="{legend_y + 9}">{sum(days.values()):,} lines of slop removed</text>')
    lx = width - 5 * step - 70
    parts.append(f'<text x="{lx}" y="{legend_y + 9}">Less</text>')
    for k in range(5):
        parts.append(f'<rect class="l{k}" x="{lx + 30 + k * step}" y="{legend_y}" width="{cell}" height="{cell}" rx="2"/>')
    parts.append(f'<text x="{lx + 34 + 5 * step}" y="{legend_y + 9}">More</text>')

    style = """
text{font:10px -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;fill:#57606a}
.l0{fill:#ebedf0}.l1{fill:#ffc1c0}.l2{fill:#ff8182}.l3{fill:#e5534b}.l4{fill:#a40e26}
@media (prefers-color-scheme:dark){text{fill:#8b949e}
.l0{fill:#161b22}.l1{fill:#5d0f12}.l2{fill:#8e1519}.l3{fill:#da3633}.l4{fill:#ff7b72}}"""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{legend_y + cell + 2}" '
        f'viewBox="0 0 {width} {legend_y + cell + 2}"><style>{style}</style>{"".join(parts)}</svg>\n'
    )


if __name__ == "__main__":
    today = dt.datetime.now(dt.timezone.utc).date()
    # 53 week columns ending on today's week, weeks start Sunday like GitHub.
    start = today - dt.timedelta(days=today.isoweekday() % 7 + 52 * 7)
    since = f"{start}T00:00:00Z"
    author_id, repos = author_and_repos(since)
    days, seen = Counter(), set()
    for repo in repos:
        for c in commits(repo, author_id, since):
            # Forks share commits; huge single commits are vendored/generated dumps, not code.
            if c["oid"] in seen or c["deletions"] > MAX_COMMIT_DELETIONS:
                continue
            seen.add(c["oid"])
            days[c["committedDate"][:10]] += c["deletions"]
    with open(OUT, "w") as f:
        f.write(render(days, start, today))
    print(f"{len(repos)} repos, {sum(days.values()):,} lines deleted -> {OUT}")
