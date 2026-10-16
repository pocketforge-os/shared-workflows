#!/usr/bin/env python3
"""Adversarial controls for the owner-trigger authorization boundary."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


MODULE = Path(__file__).parents[1] / ".github/actions/require-owner-trigger/require-owner-trigger.py"
SPEC = importlib.util.spec_from_file_location("require_owner_trigger", MODULE)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


OWNER_LOGIN = "Burke9077"
OWNER_ID = "6282442"


class FakeRequester:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status=200, body=None, headers=None):
    if body is None:
        body = {"login": OWNER_LOGIN, "id": int(OWNER_ID)}
    if not isinstance(body, bytes):
        body = json.dumps(body).encode("utf-8")
    return SimpleNamespace(status=status, body=body, headers=headers or {})


class AuthorizationMatrixTests(unittest.TestCase):
    def authorize(self, triggering_actor, actor_id, requester, **kwargs):
        return gate.authorize(
            triggering_actor=triggering_actor,
            actor_id=actor_id,
            required_login=OWNER_LOGIN,
            required_id=OWNER_ID,
            github_token="public-read-token",
            request=requester,
            sleep=lambda _delay: None,
            **kwargs,
        )

    def test_owner_initial_and_rerun_pass_with_authenticated_lookup(self):
        for label in ("owner initial", "owner re-run"):
            with self.subTest(label=label):
                requester = FakeRequester(response())
                self.authorize(OWNER_LOGIN, OWNER_ID, requester)
                self.assertEqual(len(requester.requests), 1)
                request = requester.requests[0]
                self.assertEqual(request.full_url, "https://api.github.com/users/Burke9077")
                self.assertEqual(request.get_header("Authorization"), "Bearer public-read-token")
                self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")

    def test_app_initial_fails_without_an_api_call(self):
        requester = FakeRequester(response())
        with self.assertRaisesRegex(gate.AuthorizationError, "original_actor_id_mismatch"):
            self.authorize("pocketforge-dispatch[bot]", "194588806", requester)
        self.assertEqual(requester.requests, [])

    def test_app_rerun_of_owner_run_fails_without_an_api_call(self):
        requester = FakeRequester(response())
        with self.assertRaisesRegex(gate.AuthorizationError, "triggering_actor_mismatch"):
            self.authorize("pocketforge-dispatch[bot]", OWNER_ID, requester)
        self.assertEqual(requester.requests, [])

    def test_owner_rerun_of_app_run_fails_without_an_api_call(self):
        requester = FakeRequester(response())
        with self.assertRaisesRegex(gate.AuthorizationError, "original_actor_id_mismatch"):
            self.authorize(OWNER_LOGIN, "194588806", requester)
        self.assertEqual(requester.requests, [])

    def test_renamed_or_reassigned_login_fails_closed(self):
        requester = FakeRequester(response(body={"login": "BurkeRenamed", "id": int(OWNER_ID)}))
        with self.assertRaisesRegex(gate.AuthorizationError, "resolved_login_mismatch"):
            self.authorize(OWNER_LOGIN, OWNER_ID, requester)

    def test_malformed_api_responses_fail_closed(self):
        cases = (
            (b"not-json", "malformed_api_response"),
            ({"login": OWNER_LOGIN}, "resolved_id_invalid"),
            ({"login": OWNER_LOGIN, "id": OWNER_ID}, "resolved_id_invalid"),
            ({"login": 123, "id": int(OWNER_ID)}, "resolved_login_invalid"),
        )
        for body, reason in cases:
            with self.subTest(reason=reason):
                requester = FakeRequester(response(body=body))
                with self.assertRaisesRegex(gate.AuthorizationError, reason):
                    self.authorize(OWNER_LOGIN, OWNER_ID, requester)

    def test_retries_only_429_and_5xx_with_a_hard_bound(self):
        delays = []
        requester = FakeRequester(
            response(429, {"message": "slow down"}, {"Retry-After": "99"}),
            response(503, {"message": "unavailable"}),
            response(),
        )
        gate.authorize(
            triggering_actor=OWNER_LOGIN,
            actor_id=OWNER_ID,
            required_login=OWNER_LOGIN,
            required_id=OWNER_ID,
            github_token="token",
            request=requester,
            sleep=delays.append,
        )
        self.assertEqual(len(requester.requests), 3)
        self.assertEqual(delays, [5, 2])

        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                requester = FakeRequester(response(status, {"message": "no"}), response())
                with self.assertRaisesRegex(gate.AuthorizationError, f"github_api_status_{status}"):
                    self.authorize(OWNER_LOGIN, OWNER_ID, requester)
                self.assertEqual(len(requester.requests), 1)

    def test_exhausted_or_transport_failure_fails_closed(self):
        requester = FakeRequester(response(500), response(502), response(503))
        with self.assertRaisesRegex(gate.AuthorizationError, "github_api_status_503"):
            self.authorize(OWNER_LOGIN, OWNER_ID, requester)
        self.assertEqual(len(requester.requests), 3)

        requester = FakeRequester(OSError("offline"), response())
        with self.assertRaisesRegex(gate.AuthorizationError, "github_api_transport_error"):
            self.authorize(OWNER_LOGIN, OWNER_ID, requester)
        self.assertEqual(len(requester.requests), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
