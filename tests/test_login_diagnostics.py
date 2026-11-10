import gzip
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from account_auth import LOGIN_HEADERS, LoginError, MAX_RESPONSE, password_login
from app import Server
from login_protocol import encrypt_bytes
from server_v3 import Application


class LoginDiagnosticsTests(unittest.TestCase):
    def reject(self,status,body):
        calls=[]
        def transport(*args): calls.append(1); return status,body
        with self.assertRaises(LoginError) as result:
            password_login('test-user','test-password',transport)
        self.assertEqual(len(calls),1)
        return result.exception

    def test_business_rejection_preserves_code_reason_and_phase(self):
        error=self.reject(200,json.dumps({'code':10013,'message':'账号或密码不正确'}).encode())
        self.assertEqual(error.code,'LOGIN_REJECTED')
        self.assertEqual(error.business_code,10013)
        self.assertEqual(error.phase,'business')
        self.assertIn('账号或密码不正确',error.message)
        self.assertEqual(len(error.public()['diagnosticId']),12)

    def test_successful_gzip_encrypted_response_and_capture_headers(self):
        tokens={'accessToken':'fake-access-token','refreshToken':'fake-refresh-token','tokenType':'bearer'}
        timestamp=1794300000001
        response={'code':0,'data':{'t':timestamp,'pyd':encrypt_bytes(json.dumps(tokens).encode(),timestamp)}}
        def transport(method,url,headers,body):
            self.assertEqual(headers['User-Agent'],'okhttp/4.8.1')
            self.assertEqual(headers['Content-Type'],'application/json')
            self.assertEqual(headers['Charset'],'UTF-8')
            self.assertEqual(headers['Accept-Encoding'],'gzip')
            self.assertNotIn('Authorization',headers);self.assertNotIn('Cookie',headers)
            return 200,gzip.compress(json.dumps(response).encode())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'diagnostics.jsonl'
            config=password_login('test-user','test-password',transport,diagnostic_file=path)
            self.assertEqual(config['refreshToken'],tokens['refreshToken'])
            report=json.loads(path.read_text('utf-8'))
            self.assertEqual(report['outcome'],'accepted')
            self.assertTrue(report['responseGzip']);self.assertTrue(report['tokenPairRecovered'])
            for secret in ('test-user','test-password',*tokens.values()):
                self.assertNotIn(secret,path.read_text('utf-8'))

    def test_rejection_echoes_are_redacted_in_ui_and_diagnostic_log(self):
        phone='19900001111';password='private-password';token='11111111-2222-3333-4444-555555555555'
        body=json.dumps({'code':1001,'message':f'{phone} {password} Bearer {token}'}).encode()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'diagnostics.jsonl'
            with self.assertRaises(LoginError) as result:
                password_login(phone,password,lambda *args:(200,gzip.compress(body)),diagnostic_file=path)
            text=path.read_text('utf-8')+json.dumps(result.exception.public())
            for secret in (phone,password,token):self.assertNotIn(secret,text)
            self.assertEqual(json.loads(path.read_text('utf-8'))['error']['businessCode'],1001)

    def test_rate_limit_is_distinct_from_bad_password(self):
        error=self.reject(429,b'limited')
        self.assertEqual(error.code,'LOGIN_RATE_LIMIT');self.assertEqual(error.phase,'http')
        self.assertEqual(error.http_status,429)

    def test_service_error_without_json_retains_http_status(self):
        error=self.reject(503,b'<html>unavailable</html>')
        self.assertEqual(error.code,'LOGIN_HTTP_ERROR');self.assertEqual(error.http_status,503)

    def test_missing_business_code_is_format_error(self):
        error=self.reject(200,b'{"error":"unexpected"}')
        self.assertEqual(error.code,'LOGIN_BAD_RESPONSE');self.assertEqual(error.phase,'format')

    def test_success_status_with_invalid_ciphertext_is_decode_error(self):
        error=self.reject(200,b'{"code":0,"data":{"t":1794300000001,"pyd":"bad"}}')
        self.assertEqual(error.phase,'decode');self.assertEqual(error.business_code,0)
        self.assertNotIn('密码错误',error.message)

    def test_redirect_is_not_forwarded_or_misreported_as_bad_password(self):
        error=self.reject(302,b'')
        self.assertEqual(error.code,'LOGIN_REDIRECT_BLOCKED');self.assertEqual(error.http_status,302)

    def test_decompressed_size_limit_and_corrupt_gzip(self):
        for raw in (gzip.compress(b'x'*(MAX_RESPONSE+1)),b'\x1f\x8bnot-gzip'):
            with self.subTest(size=len(raw)):
                error=self.reject(200,raw)
                self.assertEqual(error.code,'LOGIN_BAD_RESPONSE');self.assertEqual(error.phase,'format')

    def test_local_http_returns_actionable_login_error_without_saving_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            log=Path(directory)/'login-diagnostics.redacted.jsonl'
            def login(phone,password):
                return password_login(phone,password,lambda *args:(200,b'{"code":10013,"message":"wrong password"}'),diagnostic_file=log)
            app=Application(directory,login_factory=login)
            server=Server(0,app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            try:
                conn.request('POST','/api/accounts',json.dumps({'mode':'password','phone':'test-user','password':'test-password'}),{'Content-Type':'application/json'})
                response=conn.getresponse();result=json.loads(response.read())
                self.assertEqual(response.status,401)
                self.assertEqual(result['details']['businessCode'],10013)
                self.assertEqual(result['details']['phase'],'business')
                self.assertEqual(app.accounts(),[])
                self.assertEqual(list(Path(directory).glob('accounts/*/credentials.dpapi')),[])
                self.assertNotIn('test-password',log.read_text('utf-8'))
            finally:conn.close();server.shutdown();server.server_close();thread.join();app.db.close()


if __name__=='__main__':unittest.main()
