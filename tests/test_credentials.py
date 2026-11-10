import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from credential_store import dpapi

from app import Application
from remote import (REFRESH_ENDPOINT, UPLOAD_ENDPOINT, UPSTREAM, RemoteClient, RemoteError,
                    credential_config, credentials, save_credentials)


TOKENS = {'accessToken': 'old-access-test-only', 'refreshToken': 'old-refresh-test-only'}
ROTATED = {'accessToken': 'new-access-test-only', 'refreshToken': 'new-refresh-test-only'}


class RefreshTransport:
    def __init__(self):
        self.calls = []
        self.refresh_status = 200
        self.refresh_data = ROTATED.copy()
        self.reject_rotated = False

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path
        self.calls.append((method, url, headers.copy()))
        if path == REFRESH_ENDPOINT:
            if self.refresh_status != 200:
                return self.refresh_status, b'{"error":"invalid_token"}'
            return 200, json.dumps({'code': 0, 'data': self.refresh_data}).encode()
        if self.reject_rotated or headers['Authorization'] == 'Bearer '+TOKENS['accessToken']:
            return 401, b'{"error":"invalid_token"}'
        responses = {
            '/education/semester/getCurrent': {'id': 'semester', 'organizationId': 'test-school'},
            '/authorization/user/getBasicInfo': {'id': 'test-user-identifier', 'realName': 'Test User'},
            '/running/app/getRunningLimit': {'hasRule': False},
            '/running/app/getTotalRunning': {'totalMileage': '1.00'},
            '/education/course/today': [],
        }
        return 200, json.dumps({'code': 0, 'data': responses[path]}).encode()


class CredentialTests(unittest.TestCase):
    def test_token_pair_does_not_require_cookie_or_send_refresh_header(self):
        self.assertEqual(credentials(TOKENS), {'Authorization': 'Bearer '+TOKENS['accessToken']})
        self.assertEqual(credential_config(TOKENS)['refreshToken'], TOKENS['refreshToken'])
        nested = {'headers': {'Authorization': 'Bearer '+TOKENS['accessToken']},
                  'refreshToken': TOKENS['refreshToken']}
        self.assertEqual(credential_config(nested), credential_config(TOKENS))

    def test_invalid_tokens_and_conflicting_authorization_are_rejected(self):
        for key in ('accessToken', 'refreshToken'):
            for value in ('', 'bad\r\nheader', 'bad token', 123, 'x'*8193):
                with self.subTest(key=key, value_type=type(value).__name__):
                    with self.assertRaises(ValueError):
                        credential_config({**TOKENS, key: value})
        with self.assertRaises(ValueError):
            credential_config({**TOKENS, 'Authorization': 'Bearer different-token'})

    def test_auth_failure_refreshes_read_once_and_persists_rotated_tokens(self):
        transport = RefreshTransport()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            client = RemoteClient(TOKENS, transport=transport)
            client.bind_credentials_file(path)
            self.assertEqual(client.connect()['id'], 'semester')
            self.assertEqual(json.loads(path.read_text('utf-8')), credential_config(ROTATED))
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            self.assertEqual([urlsplit(call[1]).path for call in transport.calls],
                             ['/education/semester/getCurrent', REFRESH_ENDPOINT, '/education/semester/getCurrent'])
            refresh = transport.calls[1]
            self.assertEqual(refresh[0], 'GET')
            self.assertEqual(urlsplit(refresh[1]).netloc, urlsplit(UPSTREAM).netloc)
            self.assertEqual(parse_qs(urlsplit(refresh[1]).query), {'refreshToken': [TOKENS['refreshToken']]})
            self.assertNotIn('refreshToken', refresh[2])
            self.assertNotIn('Authorization', refresh[2])
            self.assertNotIn('Organization', refresh[2])
            self.assertNotIn('Content-Type', refresh[2])
            checkpoints = list((Path(directory)/'credential-checkpoints').glob('*.dpapi'))
            self.assertEqual(len(checkpoints), 2)
            recovered = [json.loads(dpapi(item.read_bytes(), decrypt=True)) for item in checkpoints]
            self.assertIn(credential_config(TOKENS), recovered)
            self.assertIn(credential_config(ROTATED), recovered)
            for secret in (*TOKENS.values(), *ROTATED.values()):
                self.assertNotIn(secret, json.dumps(client.calls))
            self.assertEqual(client._message(ROTATED['refreshToken']), '[凭据已隐藏]')

    def test_write_auth_failure_never_refreshes_or_resends(self):
        transport = RefreshTransport()
        client = RemoteClient(TOKENS, transport=transport)
        with self.assertRaises(RemoteError) as error:
            client.request(UPLOAD_ENDPOINT, {}, write=True)
        self.assertEqual(error.exception.code, 'UPSTREAM_AUTH_REJECTED')
        self.assertEqual(len(transport.calls), 1)

    def test_failed_refresh_or_second_auth_failure_stops_without_looping(self):
        for refresh_status, reject_rotated, count in ((401, False, 2), (200, True, 3)):
            with self.subTest(refresh_status=refresh_status):
                transport = RefreshTransport()
                transport.refresh_status = refresh_status
                transport.reject_rotated = reject_rotated
                client = RemoteClient(TOKENS, transport=transport)
                with self.assertRaises(RemoteError) as error:
                    client.connect()
                self.assertEqual(error.exception.code, 'UPSTREAM_AUTH_REJECTED')
                self.assertEqual(len(transport.calls), count)

    def test_malformed_refresh_does_not_replace_credentials(self):
        transport = RefreshTransport()
        transport.refresh_data = {'accessToken': ROTATED['accessToken']}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            client = RemoteClient(TOKENS, transport=transport)
            client.bind_credentials_file(path)
            with self.assertRaises(RemoteError) as error:
                client.connect()
            self.assertEqual(error.exception.code, 'UPSTREAM_BAD_RESPONSE')
            self.assertEqual(json.loads(path.read_text('utf-8')), credential_config(TOKENS))

    def test_atomic_save_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            with patch('remote.os.replace', side_effect=OSError('test failure')):
                with self.assertRaises(OSError):
                    save_credentials(path, ROTATED)
            self.assertEqual(json.loads(path.read_text('utf-8')), credential_config(TOKENS))
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_saved_file_login_binds_refresh_persistence(self):
        transport = RefreshTransport()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            app = Application(directory, remote_factory=lambda auth: RemoteClient(auth, transport=transport))
            try:
                result, cookie = app.dispatch('POST', '/api/auth/remote-file', {}, '', {})
                self.assertTrue(result['authenticated'])
                self.assertIn('HttpOnly', cookie)
                self.assertEqual(json.loads(path.read_text('utf-8')), credential_config(ROTATED))
                token = cookie.split(';')[0].split('=', 1)[1]
                home = app.home(app.auth(token), app.remote_client(token))
                for secret in (*TOKENS.values(), *ROTATED.values()):
                    self.assertNotIn(secret, json.dumps(home))
            finally:
                app.db.close()

    def test_refresh_omits_bearer_preserves_optional_cookie_and_has_no_body(self):
        transport = RefreshTransport()
        seen = []
        def capture(method, url, headers, body):
            if urlsplit(url).path == REFRESH_ENDPOINT:
                seen.append((headers, body))
                self.assertNotIn('Authorization', headers)
                self.assertEqual(headers['Cookie'], 'JSESSIONID=test-cookie')
                self.assertIsNone(body)
            return transport(method, url, headers, body)
        RemoteClient({**TOKENS, 'Cookie': 'JSESSIONID=test-cookie'}, transport=capture).connect()
        self.assertEqual(len(seen), 1)

    def test_refresh_preserves_expiry_metadata_without_sending_it_as_headers(self):
        transport = RefreshTransport()
        transport.refresh_data.update(tokenType='bearer', accessTokenExpires=1900000000000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            client = RemoteClient(TOKENS, transport=transport)
            client.bind_credentials_file(path)
            client.connect()
            config = json.loads(path.read_text('utf-8'))
            self.assertEqual(config['accessTokenExpires'], 1900000000000)
            self.assertEqual(config['tokenType'], 'bearer')
            self.assertNotIn('tokenType', transport.calls[-1][2])
            self.assertNotIn('accessTokenExpires', transport.calls[-1][2])

    def test_refresh_failure_is_not_retried_on_later_reads(self):
        transport = RefreshTransport()
        transport.refresh_status = 401
        client = RemoteClient(TOKENS, transport=transport)
        for _ in range(3):
            with self.assertRaises(RemoteError):
                client.connect()
        self.assertEqual(sum(urlsplit(call[1]).path == REFRESH_ENDPOINT for call in transport.calls), 1)

    def test_dpapi_backup_failure_prevents_remote_rotation(self):
        transport = RefreshTransport()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            client = RemoteClient(TOKENS, transport=transport)
            client.bind_credentials_file(path)
            with patch('remote.checkpoint', side_effect=OSError('test failure')):
                with self.assertRaises(RemoteError) as error:
                    client.connect()
            self.assertEqual(error.exception.code, 'LOCAL_CREDENTIAL_BACKUP_FAILED')
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(json.loads(path.read_text('utf-8')), credential_config(TOKENS))
            event_text = (path.parent/'credential-refresh-events.jsonl').read_text('utf-8')
            self.assertEqual(json.loads(event_text)['code'], 'LOCAL_CREDENTIAL_BACKUP_FAILED')
            for secret in TOKENS.values():
                self.assertNotIn(secret, event_text)

    def test_save_failure_keeps_old_file_and_recoverable_new_checkpoint(self):
        transport = RefreshTransport()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            client = RemoteClient(TOKENS, transport=transport)
            client.bind_credentials_file(path)
            with patch('remote.save_credentials', side_effect=OSError('test failure')):
                with self.assertRaises(RemoteError) as error:
                    client.connect()
            self.assertEqual(error.exception.code, 'LOCAL_CREDENTIAL_SAVE_FAILED')
            self.assertEqual(json.loads(path.read_text('utf-8')), credential_config(TOKENS))
            after = next((path.parent/'credential-checkpoints').glob('*-after.dpapi'))
            self.assertEqual(json.loads(dpapi(after.read_bytes(), decrypt=True)), credential_config(ROTATED))

    def test_two_sessions_share_file_rotation_without_reusing_old_refresh(self):
        transport = RefreshTransport()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            clients = [RemoteClient(TOKENS, transport=transport) for _ in range(2)]
            for client in clients:
                client.bind_credentials_file(path)
                client.connect()
            self.assertEqual(sum(urlsplit(call[1]).path == REFRESH_ENDPOINT for call in transport.calls), 1)
            self.assertEqual(clients[1]._refresh_token, ROTATED['refreshToken'])

    def test_concurrent_requests_on_one_session_refresh_only_once(self):
        transport = RefreshTransport()
        barrier = threading.Barrier(2)
        def simultaneous(method, url, headers, body):
            if headers.get('Authorization') == 'Bearer '+TOKENS['accessToken']:
                barrier.wait(timeout=3)
            return transport(method, url, headers, body)
        client = RemoteClient(TOKENS, transport=simultaneous)
        results, errors = [], []
        def worker():
            try: results.append(client.connect())
            except Exception as error: errors.append(error)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(urlsplit(call[1]).path == REFRESH_ENDPOINT for call in transport.calls), 1)

    def test_changed_file_with_different_identity_cannot_take_over_session(self):
        transport = RefreshTransport()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, ROTATED)
            client = RemoteClient(TOKENS, transport=transport)
            client.bind_credentials_file(path)
            client.owner = 'remote:another-account'
            with self.assertRaises(RemoteError) as error:
                client.connect()
            self.assertEqual(error.exception.code, 'UPSTREAM_IDENTITY_CHANGED')
            self.assertEqual(client._headers['Authorization'], 'Bearer '+TOKENS['accessToken'])
            self.assertFalse(any(urlsplit(call[1]).path == REFRESH_ENDPOINT for call in transport.calls))

    def test_external_update_during_refresh_is_not_overwritten(self):
        transport = RefreshTransport()
        external = {'accessToken': 'external-access', 'refreshToken': 'external-refresh'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'credentials.private.json'
            save_credentials(path, TOKENS)
            def concurrent_update(method, url, headers, body):
                result = transport(method, url, headers, body)
                if urlsplit(url).path == REFRESH_ENDPOINT:
                    save_credentials(path, external)
                return result
            client = RemoteClient(TOKENS, transport=concurrent_update)
            client.bind_credentials_file(path)
            with self.assertRaises(RemoteError) as error:
                client.connect()
            self.assertEqual(error.exception.code, 'LOCAL_CREDENTIAL_CHANGED')
            self.assertEqual(json.loads(path.read_text('utf-8')), credential_config(external))

    def test_refresh_requires_exact_http_200_and_nonboolean_zero(self):
        for status, code in ((201, 0), (200, False)):
            def response(method, url, headers, body):
                if urlsplit(url).path == REFRESH_ENDPOINT:
                    return status, json.dumps({'code': code, 'data': ROTATED}).encode()
                return 401, b'{"error":"invalid_token"}'
            with self.subTest(status=status, code=code):
                with self.assertRaises(RemoteError) as error:
                    RemoteClient(TOKENS, transport=response).connect()
                self.assertEqual(error.exception.code, 'UPSTREAM_REJECTED')


if __name__ == '__main__':
    unittest.main()
