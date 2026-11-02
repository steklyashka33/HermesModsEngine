#!/usr/bin/env python3
"""
Create and configure GitHub repository 'HermesModsEngine' for user steklyashka33.
Uses GITHUB_TOKEN from environment or /root/.hermes/.env.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def get_token():
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()
    env_file = os.environ.get("HERMES_ENV_PATH", str(Path.home() / ".hermes" / ".env"))
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("GITHUB_TOKEN="):
                    return line.strip().split("=", 1)[1].strip("\"'")
    return None


def github_request(endpoint, token, method="GET", data=None):
    url = f"https://api.github.com{endpoint}" if endpoint.startswith("/") else endpoint
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "Hermes-Agent-Deployer")

    body_bytes = None
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body_bytes = json.dumps(data).encode("utf-8")

    try:
        with urllib.request.urlopen(req, data=body_bytes) as resp:
            headers = dict(resp.headers)
            res_body = resp.read().decode("utf-8")
            return {
                "status": resp.status,
                "headers": headers,
                "data": json.loads(res_body) if res_body else None,
            }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        try:
            err_json = json.loads(err_body)
        except Exception:
            err_json = {"raw": err_body}
        return {
            "status": e.code,
            "error": err_json,
            "headers": dict(e.headers),
        }


def main():
    parser = argparse.ArgumentParser(description="Create GitHub repository HermesModsEngine")
    parser.add_argument("--private", action="store_true", help="Create as private repository (default is public)")
    parser.add_argument("--repo-name", default="HermesModsEngine", help="Repository name (default: HermesModsEngine)")
    parser.add_argument("--dry-run", action="store_true", help="Validate credentials and show payload without creating repo")
    args = parser.parse_args()

    token = get_token()
    if not token:
        env_file = os.environ.get("HERMES_ENV_PATH", str(Path.home() / ".hermes" / ".env"))
        print(f"ERROR: GITHUB_TOKEN not found in environment or {env_file}", file=sys.stderr)
        sys.exit(1)

    # 1. Verify User and Scopes
    print("[1/3] Verifying GitHub token and user...")
    user_res = github_request("/user", token)
    if user_res.get("status") != 200:
        print(f"ERROR: Failed to authenticate with GitHub API: {user_res}", file=sys.stderr)
        sys.exit(1)

    login = user_res["data"].get("login")
    scopes = user_res["headers"].get("X-OAuth-Scopes", "")
    print(f"      Authenticated as: {login}")
    print(f"      OAuth Scopes: {scopes or '(none)'}")

    repo_payload = {
        "name": args.repo_name,
        "description": "Production-grade declarative modding, hot-patching & resilience engine for Hermes Agent",
        "private": args.private,
        "has_issues": True,
        "has_wiki": False,
    }

    topics_payload = {
        "names": [
            "hermes-agent",
            "modding-engine",
            "hot-patching",
            "resilience",
            "declarative",
            "ai-agent",
            "python",
            "extensibility",
        ]
    }

    if args.dry_run:
        print("[DRY RUN] Ready to create repository:")
        print("Endpoint: POST https://api.github.com/user/repos")
        print(json.dumps(repo_payload, indent=2))
        print("Topics endpoint: PUT https://api.github.com/repos/{login}/{repo_name}/topics")
        print(json.dumps(topics_payload, indent=2))
        return

    # 2. Check if repo already exists or create it
    print(f"[2/3] Creating repository {login}/{args.repo_name} (private={args.private})...")
    create_res = github_request("/user/repos", token, method="POST", data=repo_payload)

    if create_res.get("status") in (200, 201):
        print(f"      Repository created successfully: {create_res['data'].get('html_url')}")
    elif create_res.get("status") == 422:
        # Check if already exists
        err_msg = str(create_res.get("error", ""))
        print(f"      Notice: Repository may already exist (status 422: {err_msg})")
        # Verify if existing repo is accessible
        check_repo = github_request(f"/repos/{login}/{args.repo_name}", token)
        if check_repo.get("status") == 200:
            print(f"      Verified existing repository: {check_repo['data'].get('html_url')}")
        else:
            print(f"      Error checking existing repo: {check_repo}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"ERROR: Failed to create repository: {create_res}", file=sys.stderr)
        sys.exit(1)

    # 3. Apply Topics
    print(f"[3/3] Setting repository topics: {topics_payload['names']}...")
    topics_res = github_request(
        f"/repos/{login}/{args.repo_name}/topics",
        token,
        method="PUT",
        data=topics_payload,
    )
    if topics_res.get("status") == 200:
        print(f"      Topics updated successfully: {topics_res['data'].get('names')}")
    else:
        print(f"WARNING: Setting topics returned status {topics_res.get('status')}: {topics_res.get('error')}")

    print("\n[SUCCESS] Repository setup complete and ready for push!")
    print(f"Remote URL: https://github.com/{login}/{args.repo_name}.git")


if __name__ == "__main__":
    main()
