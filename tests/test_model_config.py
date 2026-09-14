import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from backend import storage as store, model_config as model
from backend.api import app


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = store.DB
        store.DB = Path(self.temp.name) / 'model.sqlite3'
        store.initialize(seed=False)
        self.client = TestClient(app)
        self.client.post('/api/model/clear')
        self.body = dict(baseUrl='https://model.example/v1', model='test-model', apiKey='TEST_SECRET', requireKey=True)

    def tearDown(self):
        self.client.post('/api/model/clear')
        store.DB = self.original
        self.temp.cleanup()

    def test_settings_persist_without_secret_and_edit_preserves_key(self):
        result = self.client.post('/api/model', json=self.body)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('TEST_SECRET', result.text)
        with store.connection() as db:
            settings = db.execute("SELECT value FROM metadata WHERE key='model_config'").fetchone()['value']
            self.assertNotIn('TEST_SECRET', settings)
        result = self.client.post('/api/model', json=dict(self.body, apiKey='', model='new-model'))
        self.assertTrue(result.json()['hasKey'])
        model.secret = ''  # Simulate a server restart.
        result = self.client.get('/api/model').json()
        self.assertEqual(result['model'], 'new-model')
        self.assertFalse(result['readyToTest'])

    def test_key_never_reused_for_changed_url(self):
        self.client.post('/api/model', json=self.body)
        response = self.client.post('/api/model', json=dict(self.body, baseUrl='https://other.example/v1', apiKey=''))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/api/model').json()['baseUrl'], self.body['baseUrl'])

    def test_url_and_key_validation(self):
        for url in ['http://remote.example/v1', 'https://user:password@model.example/v1', 'https://model.example/v1?key=secret', 'file:///tmp/model', 'https://model.example/v1/chat/completions']:
            self.assertEqual(self.client.post('/api/model', json=dict(self.body, baseUrl=url)).status_code, 400)
        self.assertEqual(self.client.post('/api/model', json=dict(self.body, apiKey='Bearer bad')).status_code, 400)
        local = self.client.post('/api/model', json=dict(self.body, baseUrl='http://localhost:11434/v1', requireKey=False, apiKey=''))
        self.assertTrue(local.json()['readyToTest'])

    def probe(self, response=None, error=None):
        fake = AsyncMock()
        fake.post = AsyncMock(return_value=response, side_effect=error)
        context = AsyncMock()
        context.__aenter__.return_value = fake
        with patch('backend.model_config.httpx.AsyncClient', return_value=context):
            result = self.client.post('/api/model/test')
        return result, fake

    def test_real_request_contract_and_redacted_success(self):
        self.client.post('/api/model', json=self.body)
        response = httpx.Response(200, json={'choices':[{'message':{'content':'OK'}}]})
        result, fake = self.probe(response)
        self.assertEqual(result.json()['testResult']['status'], 'passed')
        self.assertNotIn('TEST_SECRET', result.text)
        self.assertEqual(fake.post.call_args.args[0], 'https://model.example/v1/chat/completions')
        self.assertEqual(fake.post.call_args.kwargs['json']['model'], 'test-model')
        self.assertEqual(fake.post.call_args.kwargs['json']['messages'], [{'role':'user','content':'Reply with OK only.'}])
        self.assertEqual(fake.post.call_args.kwargs['headers']['Authorization'], 'Bearer TEST_SECRET')
        self.client.post('/api/model', json=dict(self.body, apiKey='', model='another-model'))
        self.assertIsNone(self.client.get('/api/model').json()['testResult'])

    def test_errors_are_safe_and_clear_removes_settings(self):
        self.client.post('/api/model', json=self.body)
        result, _ = self.probe(httpx.Response(401, json={'error':'TEST_SECRET echoed by upstream'}))
        self.assertEqual(result.json()['testResult']['status'], 'failed')
        self.assertNotIn('TEST_SECRET', result.text)
        result, _ = self.probe(error=httpx.ReadTimeout('TEST_SECRET'))
        self.assertIn('超时', result.json()['testResult']['message'])
        self.client.post('/api/model/clear')
        self.assertFalse(self.client.get('/api/model').json()['configured'])
        self.assertEqual(self.client.post('/api/model/test').status_code, 400)


if __name__ == '__main__':
    unittest.main()
