import unittest
from unittest.mock import patch
import httpx
from backend.wind_connection import probe_wind


class OpenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}\n\n'
        raise AssertionError('The probe should stop after initialize, not wait for EOF')


class WindConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('backend.wind_connection.httpx.AsyncClient', return_value=client):
            return await probe_wind('TEST_KEY')

    async def test_local_network_block_is_not_reported_as_bad_key(self):
        def denied(request):
            raise httpx.ConnectError('[WinError 10013] denied', request=request)
        self.assertIn('10013', await self.run_probe(denied))

    async def test_auth_http_status_is_preserved_without_body_or_key(self):
        for code in [401, 403]:
            text = await self.run_probe(lambda request: httpx.Response(code, text='TEST_KEY'))
            self.assertIn(str(code), text)
            self.assertNotIn('TEST_KEY', text)

    async def test_sse_success_does_not_wait_for_disconnect(self):
        text = await self.run_probe(lambda request: httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=OpenStream()))
        self.assertEqual(text, '握手成功')

    async def test_json_and_protocol_error(self):
        text = await self.run_probe(lambda request: httpx.Response(200, json={'id':1, 'result':{'protocolVersion':'2024-11-05'}}))
        self.assertEqual(text, '握手成功')
        text = await self.run_probe(lambda request: httpx.Response(200, json={'id':1,'error':{'message':'TEST_KEY'}}))
        self.assertIn('拒绝', text)
        self.assertNotIn('TEST_KEY', text)
