#!/usr/bin/env python3
"""
wp_rest_client.py — Shared WordPress REST API helpers.

Used by sync_wp_categories.py, sync_wp_authors.py, validate_project_config.py,
and onboard_project.py so credential verification and category/user fetching
logic lives in exactly one place.

No CLI here — this module is imported, not run directly.
"""
from __future__ import annotations

import base64
import html
import json
import urllib.error
import urllib.request


class WpAuthError(RuntimeError):
    """Raised when WordPress REST auth fails (bad credentials / permissions)."""


class WpRequestError(RuntimeError):
    """Raised on network / HTTP errors talking to the WP REST API."""


def _auth_header(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def _get(url: str, user: str, password: str, *, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=_auth_header(user, password), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body
    except (urllib.error.URLError, TimeoutError) as e:
        raise WpRequestError(f"request failed: {e}") from e


def verify_auth(base_url: str, user: str, password: str) -> dict:
    """GET /wp-json/wp/v2/users/me — confirms credentials work.

    Returns the user record dict on success (id, name, roles, slug).
    Raises WpAuthError on 401/403, WpRequestError on other failures.
    """
    api = f"{base_url.rstrip('/')}/wp-json/wp/v2/users/me?context=edit"
    code, body = _get(api, user, password)
    if code in (401, 403):
        raise WpAuthError(f"HTTP {code} — invalid credentials or insufficient permissions: {body[:300]}")
    if code != 200:
        raise WpRequestError(f"HTTP {code} verifying auth: {body[:300]}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise WpRequestError(f"invalid JSON from users/me: {body[:200]}") from e
    if not isinstance(data, dict) or "id" not in data:
        raise WpRequestError(f"unexpected users/me response: {body[:200]}")
    return data


def fetch_categories(base_url: str, user: str, password: str) -> list[dict]:
    """Fetch all categories (paginated, 100/page) from the WP REST API.

    Returns a list of {id, name, slug, count} sorted by id ascending.
    """
    api = f"{base_url.rstrip('/')}/wp-json/wp/v2/categories"
    out: list[dict] = []
    page = 1
    while True:
        url = f"{api}?per_page=100&page={page}&hide_empty=false&_fields=id,name,slug,count"
        code, body = _get(url, user, password)
        if code == 400 and page > 1:
            break
        if code in (401, 403):
            raise WpAuthError(f"HTTP {code} fetching categories: {body[:300]}")
        if code != 200:
            raise WpRequestError(f"HTTP {code} fetching categories (page {page}): {body[:300]}")
        try:
            batch = json.loads(body)
        except json.JSONDecodeError as e:
            raise WpRequestError(f"invalid JSON on page {page}: {body[:200]}") from e
        if not isinstance(batch, list) or not batch:
            break
        for c in batch:
            out.append(
                {
                    "id": int(c.get("id")),
                    "name": html.unescape(str(c.get("name") or "")).strip(),
                    "slug": str(c.get("slug") or "").strip(),
                    "count": int(c.get("count") or 0),
                }
            )
        if len(batch) < 100:
            break
        page += 1
    out.sort(key=lambda c: c["id"])
    return out


def fetch_users(base_url: str, user: str, password: str, *, roles: str = "author,editor,administrator") -> list[dict]:
    """Fetch WP users via REST, filtered to publishing-capable roles.

    WordPress's public /wp/v2/users endpoint does not support a `roles`
    filter param for non-admins reliably across all setups, so we fetch all
    users (paginated) and filter client-side when role info is present;
    otherwise return the full list (roles require `context=edit` + elevated
    permissions, which the app-password user should have if it's an admin).

    Returns a list of {id, name, slug, roles}.
    """
    api = f"{base_url.rstrip('/')}/wp-json/wp/v2/users"
    out: list[dict] = []
    page = 1
    while True:
        url = f"{api}?per_page=100&page={page}&context=edit&_fields=id,name,slug,roles"
        code, body = _get(url, user, password)
        if code == 400 and page > 1:
            break
        if code in (401, 403):
            raise WpAuthError(f"HTTP {code} fetching users: {body[:300]}")
        if code != 200:
            raise WpRequestError(f"HTTP {code} fetching users (page {page}): {body[:300]}")
        try:
            batch = json.loads(body)
        except json.JSONDecodeError as e:
            raise WpRequestError(f"invalid JSON on page {page}: {body[:200]}") from e
        if not isinstance(batch, list) or not batch:
            break
        for u in batch:
            user_roles = u.get("roles") or []
            out.append(
                {
                    "id": int(u.get("id")),
                    "name": html.unescape(str(u.get("name") or "")).strip(),
                    "slug": str(u.get("slug") or "").strip(),
                    "roles": user_roles if isinstance(user_roles, list) else [],
                }
            )
        if len(batch) < 100:
            break
        page += 1

    wanted = {r.strip() for r in roles.split(",") if r.strip()}
    if wanted and any(u["roles"] for u in out):
        filtered = [u for u in out if wanted.intersection(u["roles"])]
        if filtered:
            return filtered
    return out
