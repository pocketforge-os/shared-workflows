#!/usr/bin/env python3
"""Authorize a privileged run using original and current immutable identities."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API_ROOT = "https://api.github.com"
MAX_ATTEMPTS = 3


class AuthorizationError(RuntimeError):
    """A typed fail-closed authorization refusal."""


class Response:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers


def github_request(request):
    try:
        with urllib.request.urlopen(request, timeout=10) as raw:
            return Response(raw.status, raw.read(), raw.headers)
    except urllib.error.HTTPError as error:
        return Response(error.code, error.read(), error.headers)
    except (OSError, urllib.error.URLError) as error:
        raise AuthorizationError("github_api_transport_error") from error


def _retry_delay(response, attempt):
    if response.status == 429:
        try:
            return min(5, max(1, int(response.headers.get("Retry-After", "1"))))
        except (TypeError, ValueError):
            return 1
    return min(5, attempt)


def authorize(*, triggering_actor, actor_id, required_login, required_id,
              github_token, request=github_request, sleep=time.sleep):
    if not all((triggering_actor, actor_id, required_login, required_id, github_token)):
        raise AuthorizationError("missing_required_input")
    if not required_id.isascii() or not required_id.isdecimal():
        raise AuthorizationError("required_id_invalid")
    if actor_id != required_id:
        raise AuthorizationError("original_actor_id_mismatch")
    if triggering_actor.casefold() != required_login.casefold():
        raise AuthorizationError("triggering_actor_mismatch")

    url = f"{API_ROOT}/users/{urllib.parse.quote(required_login, safe='')}"
    api_response = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        api_request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pocketforge-require-owner-trigger",
            },
        )
        try:
            api_response = request(api_request)
        except AuthorizationError:
            raise
        except Exception as error:
            raise AuthorizationError("github_api_transport_error") from error
        retryable = api_response.status == 429 or 500 <= api_response.status <= 599
        if api_response.status == 200 or not retryable or attempt == MAX_ATTEMPTS:
            break
        sleep(_retry_delay(api_response, attempt))

    if api_response.status != 200:
        raise AuthorizationError(f"github_api_status_{api_response.status}")
    try:
        resolved = json.loads(api_response.body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        raise AuthorizationError("malformed_api_response") from error
    if not isinstance(resolved, dict):
        raise AuthorizationError("malformed_api_response")
    if not isinstance(resolved.get("login"), str):
        raise AuthorizationError("resolved_login_invalid")
    if type(resolved.get("id")) is not int:
        raise AuthorizationError("resolved_id_invalid")
    if resolved["login"].casefold() != required_login.casefold():
        raise AuthorizationError("resolved_login_mismatch")
    if str(resolved["id"]) != required_id:
        raise AuthorizationError("resolved_id_mismatch")


def main():
    try:
        authorize(
            triggering_actor=os.environ.get("GATE_TRIGGERING_ACTOR", ""),
            actor_id=os.environ.get("GATE_ACTOR_ID", ""),
            required_login=os.environ.get("GATE_REQUIRED_LOGIN", ""),
            required_id=os.environ.get("GATE_REQUIRED_ID", ""),
            github_token=os.environ.get("GATE_GITHUB_TOKEN", ""),
        )
    except AuthorizationError as error:
        print(f"owner_trigger_authorization=DENY reason={error}", file=sys.stderr)
        return 1
    print("owner_trigger_authorization=ALLOW")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
