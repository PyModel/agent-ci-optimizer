import io
import json
import unittest
from email.message import Message
from unittest.mock import Mock

from scripts import collect_gitlab as gl


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(json.dumps(data).encode())
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value


class GitLabTests(unittest.TestCase):
    def client(self, *responses):
        opener = Mock()
        opener.open.side_effect = list(responses)
        return gl.Client("https://gitlab.example/sub", "fixture-token", opener=opener)

    def test_reads_native_durations_retries_and_cross_project_child(self):
        client = self.client(
            Response({"id": 42, "project_id": 7, "status": "running"}),
            Response([{"id": 9, "duration": 3, "queued_duration": 8, "status": "success"},
                      {"id": 8, "status": "failed"}]),
            Response([{"id": 9, "status": "success"}]),
            Response([{"id": 3, "downstream_pipeline": {"project_id": 99, "id": 52}}]),
            Response({"id": 52, "project_id": 99, "status": "manual"}),
            Response([]), Response([]), Response([]),
        )
        result = gl.collect(client, "group/sub/repo", 42)
        self.assertTrue(result["complete"])
        root, child = result["pipelines"]
        self.assertEqual(root["jobs"][0]["queued_duration_s"], 8)
        self.assertEqual(root["jobs"][1]["queued_duration_s"], "unknown")
        self.assertEqual([j["attempt_role"] for j in root["jobs"]], ["current", "retried"])
        self.assertEqual(child["pipeline"]["status"], "manual")
        self.assertNotIn("fixture-token", json.dumps(result))
        req = client.opener.open.call_args_list[0].args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertIn("/sub/api/v4/projects/group%2Fsub%2Frepo/pipelines/42", req.full_url)

    def test_documented_bridge_without_project_id_uses_same_instance_route(self):
        child = {"id": 52, "web_url": "https://gitlab.example/sub/group/downstream/-/pipelines/52"}
        client = self.client(
            Response({"id": 42, "project_id": 7}), Response([]), Response([]),
            Response([{"id": 3, "downstream_pipeline": child}]),
            Response({"id": 52, "project_id": 99}), Response([]), Response([]), Response([]),
        )
        result = gl.collect(client, "7", 42)
        self.assertTrue(result["complete"])
        self.assertEqual(result["pipelines"][1]["project"], "group/downstream")
        for url in ["https://elsewhere.example/sub/group/downstream/-/pipelines/52",
                    "https://gitlab.example/other/group/downstream/-/pipelines/52",
                    "https://gitlab.example/sub/group/downstream/-/pipelines/53",
                    "https://gitlab.example/sub/group/../downstream/-/pipelines/52"]:
            self.assertIsNone(gl.downstream_project(client.base, {**child, "web_url": url}))

    def test_missing_access_and_child_cap_are_reported(self):
        client = self.client(
            Response({"id": 42, "project_id": 7}), Response([{"id": 1}]),
            gl.Unavailable("HTTP 403"),
            Response([{"id": 3, "downstream_pipeline": {"project_id": 99, "id": 52}}]),
        )
        result = gl.collect(client, "7", 42, max_pipelines=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["pipelines"][0]["jobs"][0]["attempt_role"], "unknown")
        self.assertEqual(len(result["gaps"]), 2)

    def test_nonatomic_attempts_fail_closed(self):
        client = self.client(
            Response({"id": 42, "project_id": 7}),
            Response([{"id": 9, "status": "running"}]),
            Response([{"id": 9, "status": "failed"}]), Response([]),
        )
        result = gl.collect(client, "7", 42)
        self.assertFalse(result["complete"])
        self.assertEqual(result["pipelines"][0]["jobs"][0]["attempt_role"], "unknown")

    def test_pagination_ignores_untrusted_response_url_and_validates_count(self):
        client = self.client(
            Response([{"id": 1}], {"Link": '<https://elsewhere.example/foo>; rel="next"'}),
            Response([{"id": 2}], {"X-Total": "2"}),
        )
        self.assertEqual(client.pages("/some", 2), [{"id": 1}, {"id": 2}])
        self.assertIn("page=2", client.opener.open.call_args.args[0].full_url)
        self.assertNotIn("elsewhere", client.opener.open.call_args.args[0].full_url)
        for headers, match in [({"X-Next-Page": "4"}, "pointer"), ({"X-Total": "2"}, "count")]:
            with self.subTest(headers=headers), self.assertRaisesRegex(gl.Unavailable, match):
                self.client(Response([{"id": 1}], headers)).pages("/some", 1)

    def test_duplicate_and_full_unconfirmed_pages_fail(self):
        with self.assertRaisesRegex(gl.Unavailable, "duplicate"):
            self.client(Response([{"id": 1}, {"id": 1}])).pages("/some", 1)
        with self.assertRaisesRegex(gl.Unavailable, "before completeness"):
            self.client(Response([{"id": i} for i in range(1, gl.PAGE_SIZE + 1)])).pages("/some", 1)

    def test_no_credential_urls_or_redirects(self):
        for url in ["http://gitlab.example", "https://user@gitlab.example",
                    "https://gitlab.example/../bad", "https://gitlab.example?foo=bar"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                gl.api_base(url)
        self.assertIsNone(gl.NoRedirect().redirect_request(None, None, 302, "", None, "https://elsewhere"))

    def test_bounded_payload_and_expired_budget(self):
        client = self.client()
        client.deadline = 0
        with self.assertRaisesRegex(gl.Unavailable, "budget"):
            client.get("/some")
        client.opener.open.assert_not_called()
        from unittest.mock import patch
        with patch.object(gl, "MAX_RESPONSE", 16), self.assertRaisesRegex(gl.Unavailable, "size"):
            self.client(Response({"value": "long text long text"})).get("/some")
