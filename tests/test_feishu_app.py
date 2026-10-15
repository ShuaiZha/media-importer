"""Application-bot protocol tests with all HTTP requests mocked."""
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from media_importer.adapters import feishu
from media_importer.model import ConfigError


CREDENTIALS = {"app_id": "cli_test12345678", "app_secret": "only-test-app-secret",
               "receive_id_type": "email", "receive_id": "me@example.com"}
TOKEN = {"code": 0, "tenant_access_token": "t-only-test-token", "expire": 7200}
SUCCESS = {"code": 0, "data": {"message_id": "om_testmessage"}}


class ApplicationBotTests(unittest.TestCase):
    def setUp(self):
        feishu._TOKEN_CACHE.clear()
        self.opener = Mock()
        self.build = patch.object(feishu.urllib.request, "build_opener", return_value=self.opener)
        self.build.start()
        self.addCleanup(self.build.stop)
        self.guard = patch("socket.socket.connect", side_effect=AssertionError("No network in protocol tests"))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.addCleanup(feishu._TOKEN_CACHE.clear)

    def response(self, value):
        response = Mock()
        response.read.return_value = value if isinstance(value, bytes) else json.dumps(value).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def replies(self, *values):
        self.opener.open.side_effect = [v if isinstance(v, Exception) else self.response(v) for v in values]

    def request(self, index):
        return self.opener.open.call_args_list[index].args[0]

    def error(self, status, body):
        return urllib.error.HTTPError(feishu.MESSAGE_URL, status, "only-test-app-secret", {},
                                      io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode()))

    def test_validate_only_app_credentials_and_one_direct_recipient(self):
        self.assertEqual(CREDENTIALS, feishu.validate_credentials(CREDENTIALS))
        self.assertEqual(CREDENTIALS, feishu.validate_credentials({key: " " + value + " " for key, value in CREDENTIALS.items()}))
        for invalid in ({}, {"webhook": "https://example.com", "secret": "x"},
                        {**CREDENTIALS, "url": "https://example.com"},
                        {**CREDENTIALS, "app_id": "https://example.com"},
                        {**CREDENTIALS, "app_secret": "bad\nvalue"},
                        {**CREDENTIALS, "receive_id_type": "chat_id", "receive_id": "oc_test12345678"},
                        {**CREDENTIALS, "receive_id_type": "user_id"},
                        {**CREDENTIALS, "receive_id": ["me@example.com"]}):
            with self.subTest(invalid=list(invalid)), self.assertRaises(ConfigError):
                feishu.validate_credentials(invalid)
        self.opener.open.assert_not_called()

    def test_reject_email_lists_names_uri_and_bad_domains(self):
        for value in ("me@example.com,you@example.com", "me@example.com;you@example.com", "Name <me@example.com>",
                      "mailto:me@example.com", "me@example.com\nyou@example.com", "a@@example.com", "a@-example.com",
                      "a@example..com", "a@localhost", "a..b@example.com", ".a@example.com", "a.@example.com"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                feishu.validate_credentials({**CREDENTIALS, "receive_id": value})

    def test_open_id_must_be_current_app_user_id_format(self):
        valid = {**CREDENTIALS, "receive_id_type": "open_id", "receive_id": "ou_test12345678"}
        self.assertEqual(valid, feishu.validate_credentials(valid))
        for value in ("oc_test12345678", "ou_a", "ou_test12345678,ou_test23456789", "https://example.com"):
            with self.assertRaises(ConfigError):
                feishu.validate_credentials({**valid, "receive_id": value})

    def test_acquire_token_then_send_json_text_to_fixed_endpoint(self):
        self.replies(TOKEN, SUCCESS)
        feishu.send(CREDENTIALS, "测试标题", "导入：1/3", keyword="素材", event_id="installation:42")
        auth, message = self.request(0), self.request(1)
        self.assertEqual(feishu.TOKEN_URL, auth.full_url)
        self.assertEqual({"app_id": CREDENTIALS["app_id"], "app_secret": CREDENTIALS["app_secret"]}, json.loads(auth.data))
        self.assertIsNone(auth.get_header("Authorization"))
        self.assertEqual(feishu.MESSAGE_URL + "?receive_id_type=email", message.full_url)
        self.assertEqual("Bearer " + TOKEN["tenant_access_token"], message.get_header("Authorization"))
        payload = json.loads(message.data)
        self.assertEqual(CREDENTIALS["receive_id"], payload["receive_id"])
        self.assertEqual("text", payload["msg_type"])
        self.assertEqual({"text": "素材\n测试标题\n导入：1/3"}, json.loads(payload["content"]))
        self.assertLessEqual(len(payload["uuid"]), 50)
        self.assertNotIn(CREDENTIALS["app_secret"], message.data.decode())
        for call in self.opener.open.call_args_list:
            self.assertEqual(8, call.kwargs["timeout"])
            self.assertEqual("POST", call.args[0].method)

    def test_open_id_send_uses_only_open_id_query(self):
        self.replies(TOKEN, SUCCESS)
        feishu.send({**CREDENTIALS, "receive_id_type": "open_id", "receive_id": "ou_test12345678"}, "title", "body")
        self.assertEqual(feishu.MESSAGE_URL + "?receive_id_type=open_id", self.request(1).full_url)

    def test_token_cache_and_early_expiry(self):
        self.replies(TOKEN, SUCCESS, SUCCESS, TOKEN, SUCCESS)
        with patch.object(feishu.time, "monotonic", return_value=1000):
            feishu.send(CREDENTIALS, "title", "first")
        with patch.object(feishu.time, "monotonic", return_value=8139):
            feishu.send(CREDENTIALS, "title", "second")
        self.assertEqual(3, self.opener.open.call_count)
        with patch.object(feishu.time, "monotonic", return_value=8140):
            feishu.send(CREDENTIALS, "title", "third")
        self.assertEqual(5, self.opener.open.call_count)
        self.assertEqual(feishu.TOKEN_URL, self.request(3).full_url)

    def test_rotated_secret_and_other_app_never_share_tokens(self):
        self.replies(TOKEN, SUCCESS, {**TOKEN, "tenant_access_token": "t-rotated-token"}, SUCCESS,
                     {**TOKEN, "tenant_access_token": "t-other-app-token"}, SUCCESS)
        feishu.send(CREDENTIALS, "title", "first")
        feishu.send({**CREDENTIALS, "app_secret": "new-test-secret"}, "title", "second")
        feishu.send({**CREDENTIALS, "app_id": "cli_other12345678"}, "title", "third")
        self.assertEqual("Bearer t-rotated-token", self.request(3).get_header("Authorization"))
        self.assertEqual("Bearer t-other-app-token", self.request(5).get_header("Authorization"))
        self.assertEqual(3, len(feishu._TOKEN_CACHE))
        self.assertNotIn(CREDENTIALS["app_secret"], repr(feishu._TOKEN_CACHE))

    def test_expired_token_refreshes_once_and_reuses_message_uuid(self):
        self.replies(TOKEN, self.error(400, {"code": 99991663, "msg": "secret"}),
                     {**TOKEN, "tenant_access_token": "t-new-test-token"}, SUCCESS)
        feishu.send(CREDENTIALS, "title", "text", event_id="installation:42")
        self.assertEqual(4, self.opener.open.call_count)
        self.assertEqual("Bearer t-new-test-token", self.request(3).get_header("Authorization"))
        self.assertEqual(json.loads(self.request(1).data), json.loads(self.request(3).data))

    def test_http_401_refreshes_once(self):
        self.replies(TOKEN, self.error(401, b"not-json"), TOKEN, SUCCESS)
        feishu.send(CREDENTIALS, "title", "body")
        self.assertEqual(4, self.opener.open.call_count)

    def test_refresh_rejection_stops_after_second_send(self):
        self.replies(TOKEN, {"code": 99991663}, TOKEN, {"code": 99991663})
        with self.assertRaises(feishu.NotificationDeliveryError) as caught:
            feishu.send(CREDENTIALS, "title", "text")
        self.assertEqual("notification_authentication", caught.exception.code)
        self.assertEqual(4, self.opener.open.call_count)

    def test_token_credentials_error_is_not_retried(self):
        self.replies({"code": 10015, "msg": CREDENTIALS["app_secret"]})
        with self.assertRaises(feishu.NotificationDeliveryError) as caught:
            feishu.send(CREDENTIALS, "title", "text")
        self.assertEqual("notification_authentication", caught.exception.code)
        self.assertEqual(1, self.opener.open.call_count)
        self.assertFalse(feishu._TOKEN_CACHE)
        self.assertNotIn(CREDENTIALS["app_secret"], str(caught.exception))

    def test_event_uuid_is_stable_on_retries_and_distinct_for_events_and_recipients(self):
        self.replies(TOKEN, SUCCESS, SUCCESS, SUCCESS, SUCCESS)
        feishu.send(CREDENTIALS, "title", "first", event_id="installation:42")
        feishu.send(CREDENTIALS, "title", "updated", event_id="installation:42")
        feishu.send(CREDENTIALS, "title", "first", event_id="installation:43")
        feishu.send({**CREDENTIALS, "receive_id": "other@example.com"}, "title", "first", event_id="installation:42")
        ids = [json.loads(self.request(index).data)["uuid"] for index in range(1, 5)]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(3, len(set(ids)))

    def test_independent_test_clicks_use_new_uuid(self):
        self.replies(TOKEN, SUCCESS, SUCCESS)
        feishu.send(CREDENTIALS, "title", "body")
        feishu.send(CREDENTIALS, "title", "body")
        self.assertNotEqual(json.loads(self.request(1).data)["uuid"], json.loads(self.request(2).data)["uuid"])

    def test_business_errors_with_http_400_are_classified_without_response_text(self):
        cases = {99991672: "notification_permission", 230027: "notification_permission",
                 230013: "notification_recipient", 99992361: "notification_recipient",
                 230034: "notification_recipient", 230053: "notification_recipient",
                 230006: "notification_bot_unavailable", 99991400: "notification_rate_limited",
                 230020: "notification_rate_limited",
                 99991403: "notification_quota", 99991401: "notification_ip_restricted",
                 42: "notification_rejected"}
        for code, expected in cases.items():
            with self.subTest(code=code):
                feishu._TOKEN_CACHE.clear()
                self.opener.open.reset_mock()
                self.replies(TOKEN, self.error(400, {"code": code, "msg": CREDENTIALS["app_secret"]}))
                with self.assertRaises(feishu.NotificationDeliveryError) as caught:
                    feishu.send(CREDENTIALS, "title", "body")
                self.assertEqual(expected, caught.exception.code)
                self.assertNotIn(CREDENTIALS["app_secret"], str(caught.exception))
                self.assertEqual(2, self.opener.open.call_count)

    def test_http_limits_service_errors_and_unstructured_forbidden(self):
        for status, expected in ((429, "notification_rate_limited"), (503, "notification_network"),
                                 (403, "notification_permission"), (400, "notification_rejected")):
            with self.subTest(status=status):
                feishu._TOKEN_CACHE.clear()
                self.replies(self.error(status, b"secret-response"))
                with self.assertRaises(feishu.NotificationDeliveryError) as caught:
                    feishu.send(CREDENTIALS, "title", "body")
                self.assertEqual(expected, caught.exception.code)
                self.assertNotIn("secret", str(caught.exception))

    def test_redirect_is_never_followed(self):
        self.assertIsNone(feishu._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.test"))
        self.replies(self.error(302, {"code": 0, "tenant_access_token": "fake-token"}))
        with self.assertRaises(feishu.NotificationDeliveryError) as caught:
            feishu.send(CREDENTIALS, "title", "body")
        self.assertEqual("notification_rejected", caught.exception.code)
        self.assertEqual(1, self.opener.open.call_count)
        self.assertIsInstance(feishu.urllib.request.build_opener.call_args.args[0], feishu._NoRedirect)

    def test_malformed_or_oversized_responses_fail_closed(self):
        for value in (b"not json", b"x" * 65537, {"code": False}, {"StatusCode": 0}, [],
                      {"code": 0, "tenant_access_token": "token\r\nInjected:x", "expire": 7200},
                      {**TOKEN, "expire": False}, {**TOKEN, "expire": 0}, {**TOKEN, "expire": "7200"}):
            with self.subTest(value_type=type(value).__name__):
                response = self.response(value)
                self.opener.open.side_effect = [response]
                with self.assertRaises(feishu.NotificationDeliveryError) as caught:
                    feishu.send(CREDENTIALS, "title", "body")
                self.assertEqual("notification_response", caught.exception.code)
                response.read.assert_called_once_with(65537)
                self.assertFalse(feishu._TOKEN_CACHE)

    def test_success_requires_message_id_and_cannot_use_http_error(self):
        for value in ({"code": 0}, {"code": 0, "data": {}}, {"code": 0, "data": {"message_id": ""}},
                      self.error(400, SUCCESS)):
            with self.subTest(value_type=type(value).__name__):
                feishu._TOKEN_CACHE.clear()
                self.replies(TOKEN, value)
                with self.assertRaises(feishu.NotificationDeliveryError):
                    feishu.send(CREDENTIALS, "title", "body")

    def test_network_error_is_safe(self):
        self.replies(urllib.error.URLError("https://example.com/" + CREDENTIALS["app_secret"]))
        with self.assertRaises(feishu.NotificationDeliveryError) as caught:
            feishu.send(CREDENTIALS, "title", "body")
        self.assertEqual("notification_network", caught.exception.code)
        self.assertNotIn(CREDENTIALS["app_secret"], str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_shared_card_is_sent_then_patched_in_place(self):
        content = {'config': {'update_multi': True}, 'elements': []}
        self.replies(TOKEN, SUCCESS, {'code': 0, 'data': {}})
        message = feishu.send_card(CREDENTIALS, content, event_id='progress:batch')
        self.assertEqual('om_testmessage', message)
        feishu.update_card(CREDENTIALS, message, content)
        self.assertEqual('interactive', json.loads(self.request(1).data)['msg_type'])
        request = self.request(2)
        self.assertEqual('PATCH', request.method)
        self.assertEqual(feishu.MESSAGE_URL + '/om_testmessage', request.full_url)
        self.assertEqual({'content': json.dumps(content, ensure_ascii=False)}, json.loads(request.data))

    def test_patch_refreshes_token_once_without_creating_another_message(self):
        self.replies(TOKEN, {'code': 99991663}, TOKEN, {'code': 0})
        feishu.update_card(CREDENTIALS, 'om_testmessage', {'config': {'update_multi': True}})
        self.assertEqual(['POST', 'PATCH', 'POST', 'PATCH'], [call.args[0].method for call in self.opener.open.call_args_list])
        self.assertEqual(self.request(1).data, self.request(3).data)

    def test_obsolete_card_errors_are_distinct_from_permissions_and_network(self):
        for code in (230011, 230031):
            self.replies(TOKEN, {'code': code})
            with self.assertRaises(feishu.NotificationDeliveryError) as caught:
                feishu.update_card(CREDENTIALS, 'om_testmessage', {})
            self.assertEqual('notification_card_unavailable', caught.exception.code)
            feishu._TOKEN_CACHE.clear()

    def test_patch_cannot_redirect_to_an_arbitrary_message_endpoint(self):
        for message in ('https://example.invalid', '../token', 'om_x?receive_id=y', 'om_x/else', ''):
            with self.assertRaises(feishu.NotificationDeliveryError):
                feishu.update_card(CREDENTIALS, message, {})
        self.opener.open.assert_not_called()

    def test_token_cache_is_bounded(self):
        self.replies(*([TOKEN, SUCCESS] * 10))
        for index in range(10):
            feishu.send({**CREDENTIALS, "app_secret": "test-secret-" + str(index)}, "title", "body")
        self.assertEqual(8, len(feishu._TOKEN_CACHE))


if __name__ == "__main__":
    unittest.main()
