#!/usr/bin/env python3
"""List Infisical secret NAMES (never values) via the REST API.

Avoids parsing the CLI's box-drawing table, which wraps wide rows and makes
naive whitespace parsing silently drop secrets. Reads JSON instead, prints
only keys and counts, and never prints a secret value.

Auth (in priority order):
  1. INFISICAL_TOKEN            - an existing machine-identity access token
  2. INFISICAL_CLIENT_ID + INFISICAL_CLIENT_SECRET - universal-auth login

Usage:
  python list_secret_names.py                     # all visible projects/envs
  python list_secret_names.py --project <id>      # one project
  python list_secret_names.py --env dev           # one environment slug
  python list_secret_names.py --domain https://eu.infisical.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_DOMAIN = os.getenv("INFISICAL_API_URL", "https://app.infisical.com").rstrip("/")


def _request(url: str, token: str | None = None, data: dict | None = None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def login(domain: str) -> str:
    token = os.getenv("INFISICAL_TOKEN")
    if token:
        return token

    client_id = os.getenv("INFISICAL_CLIENT_ID")
    client_secret = os.getenv("INFISICAL_CLIENT_SECRET")
    if not (client_id and client_secret):
        sys.exit(
            "No credentials: set INFISICAL_TOKEN, or INFISICAL_CLIENT_ID and "
            "INFISICAL_CLIENT_SECRET for universal auth."
        )

    result = _request(
        f"{domain}/api/v1/auth/universal-auth/login",
        data={"clientId": client_id, "clientSecret": client_secret},
    )
    return result["accessToken"]


def list_projects(domain: str, token: str) -> list[dict]:
    result = _request(f"{domain}/api/v1/workspace", token)
    return result.get("workspaces", [])


def list_secret_names(domain: str, token: str, project_id: str, env: str) -> list[str]:
    query = urllib.parse.urlencode(
        {
            "workspaceId": project_id,
            "environment": env,
            "secretPath": "/",
            "recursive": "true",
        }
    )
    result = _request(f"{domain}/api/v3/secrets/raw?{query}", token)
    names = {s["secretKey"] for s in result.get("secrets", [])}
    for imported in result.get("imports", []):
        names.update(s["secretKey"] for s in imported.get("secrets", []))
    return sorted(names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="Project (workspace) ID. Default: all visible.")
    parser.add_argument("--env", help="Environment slug. Default: all in each project.")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN, help="Infisical instance URL.")
    args = parser.parse_args()

    domain = args.domain.rstrip("/")
    token = login(domain)

    projects = list_projects(domain, token)
    if args.project:
        projects = [p for p in projects if p.get("id") == args.project]
        if not projects:
            sys.exit(
                f"Project {args.project} is not visible to this identity. "
                f"Visible: {[p.get('id') for p in list_projects(domain, token)]}"
            )

    if not projects:
        sys.exit("No projects visible to this identity.")

    for project in projects:
        env_slugs = [e["slug"] for e in project.get("environments", [])]
        if args.env:
            env_slugs = [s for s in env_slugs if s == args.env]
        print(f"project: {project.get('name')} ({project.get('id')})")
        for slug in env_slugs:
            try:
                names = list_secret_names(domain, token, project["id"], slug)
            except Exception as exc:  # no access to this env, keep going
                print(f"  env {slug}: error ({exc})")
                continue
            print(f"  env {slug}: {len(names)} secret(s)")
            for name in names:
                print(f"    {name}")


if __name__ == "__main__":
    main()
