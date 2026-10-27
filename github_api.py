"""Minimal async GitHub REST client: just what the triage agent needs."""

from __future__ import annotations

import httpx

API = "https://api.github.com"


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class GitHub:
    def __init__(self, token: str, dry_run: bool = False):
        self.dry_run = dry_run
        self._http = httpx.AsyncClient(
            base_url=API,
            timeout=20,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "woogles-issuebot",
            },
        )

    async def close(self):
        await self._http.aclose()

    async def _req(self, method: str, url: str, **kw) -> dict | list:
        r = await self._http.request(method, url, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {url} -> {r.status_code}: {_clip(r.text, 300)}")
        return r.json()

    async def search_issues(self, full_name: str, query: str, limit: int = 10) -> list[dict]:
        q = f"repo:{full_name} is:issue {query}"
        data = await self._req("GET", "/search/issues", params={"q": q, "per_page": limit})
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "url": i["html_url"],
                "comments": i.get("comments", 0),
                "snippet": _clip(i.get("body"), 200),
            }
            for i in data.get("items", [])
        ]

    async def get_issue(self, full_name: str, number: int) -> dict:
        issue = await self._req("GET", f"/repos/{full_name}/issues/{number}")
        comments = []
        if issue.get("comments"):
            raw = await self._req(
                "GET", f"/repos/{full_name}/issues/{number}/comments", params={"per_page": 100}
            )
            comments = [
                {"author": c["user"]["login"], "body": _clip(c["body"], 400)} for c in raw[-5:]
            ]
        return {
            "number": issue["number"],
            "title": issue["title"],
            "state": issue["state"],
            "url": issue["html_url"],
            "labels": [l["name"] for l in issue.get("labels", [])],
            "body": _clip(issue.get("body"), 2000),
            "last_comments": comments,
        }

    async def create_issue(self, full_name: str, title: str, body: str, labels: list[str]) -> dict:
        if self.dry_run:
            return {"number": 0, "url": f"https://github.com/{full_name}/issues/DRY-RUN"}
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        issue = await self._req("POST", f"/repos/{full_name}/issues", json=payload)
        return {"number": issue["number"], "url": issue["html_url"]}

    async def add_comment(self, full_name: str, number: int, body: str) -> dict:
        if self.dry_run:
            return {"url": f"https://github.com/{full_name}/issues/{number}#DRY-RUN"}
        c = await self._req(
            "POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": body}
        )
        return {"url": c["html_url"]}
