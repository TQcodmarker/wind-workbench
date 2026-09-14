"""Read-only Wind MCP client: initialize, paginated tool discovery and tool calls."""
import json
import httpx
from .wind_connection import ENDPOINT


class WindError(RuntimeError):
    pass


class WindMCP:
    def __init__(self, key, client=None, recorder=None):
        if not key:
            raise WindError('请先在 Wind 数据源页面保存 Key')
        self.client = client
        self.owns_client = client is None
        self.headers = {'Authorization':'Bearer '+key, 'Accept':'application/json, text/event-stream'}
        self.sequence = 0
        self.tools = []
        self.version = None
        from .lineage import Recorder
        self.recorder = recorder or (Recorder('MCP 查询') if client is None else None)
        self._key = key
        self.last_request_id = None

    async def __aenter__(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False)
        try:
            result = await self.request('initialize', {'protocolVersion':'2024-11-05', 'capabilities':{},
                'clientInfo':{'name':'wind-workbench','version':'2.0.0'}})
            self.version = result.get('protocolVersion')
            if not self.version:
                raise WindError('Wind MCP 未返回协议版本')
            self.headers['MCP-Protocol-Version'] = self.version
            await self.request('notifications/initialized', notification=True)
            return self
        except Exception:
            if self.owns_client:
                await self.client.aclose()
            raise

    async def __aexit__(self, *args):
        if self.owns_client:
            await self.client.aclose()

    async def request(self, method, params=None, notification=False):
        self.response = None
        self.http_status = None
        error = None
        self.last_request_id = None
        try:
            return await self._request(method,params,notification)
        except WindError as exc:
            error=str(exc)
            raise
        except httpx.HTTPError as exc:
            error='Wind HTTP 传输失败，已保留收到的响应'
            raise WindError(error) from exc
        except Exception:
            error='请求处理异常，已保留收到的响应'
            raise
        finally:
            if self.recorder and self.last_request_id:
                response=json.loads(self.clean(json.dumps(self.response,ensure_ascii=False))) if self.response is not None else None
                self.recorder.finish(self.last_request_id,response,error,self.http_status)

    def clean(self, text):
        return text.replace(self._key,'[REDACTED]')

    def capture(self, text):
        if self.recorder and self.last_request_id:
            self.recorder.part(self.last_request_id,self.clean(text))

    async def _request(self, method, params=None, notification=False):
        self.sequence += 1
        rid = self.sequence
        body = {'jsonrpc':'2.0','method':method}
        if not notification:
            body['id'] = rid
        if params is not None:
            body['params'] = params
        if self.recorder:
            self.last_request_id=self.recorder.begin(method,json.loads(self.clean(json.dumps(body,ensure_ascii=False))))
        try:
            async with self.client.stream('POST', ENDPOINT, headers=self.headers, json=body) as response:
                self.http_status=response.status_code
                if response.status_code in [401,403]:
                    self.capture((await response.aread()).decode('utf-8',errors='replace'))
                    raise WindError(f'Wind HTTP {response.status_code}：请检查 Key 和债券服务权限')
                if response.status_code not in [200,202,204]:
                    self.capture((await response.aread()).decode('utf-8',errors='replace'))
                    raise WindError(f'Wind HTTP {response.status_code}：{method} 请求失败')
                if response.headers.get('mcp-session-id'):
                    self.headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
                if notification:
                    self.capture((await response.aread()).decode('utf-8',errors='replace'))
                    return {}
                if 'text/event-stream' in response.headers.get('content-type',''):
                    parts = []
                    async for line in response.aiter_lines():
                        self.capture(line+'\n')
                        if line.startswith('data:'):
                            parts.append(line[5:].lstrip())
                        elif not line and parts:
                            data = json.loads('\n'.join(parts)); parts = []
                            if data.get('id') == rid:
                                self.response=data
                                return self.result(data, method)
                    if parts:
                        data = json.loads('\n'.join(parts))
                        if data.get('id') == rid:
                            self.response=data
                            return self.result(data, method)
                    raise WindError('Wind 未返回对应请求结果')
                parts=[]
                async for line in response.aiter_lines():
                    self.capture(line+'\n')
                    parts.append(line)
                data = json.loads('\n'.join(parts))
                self.response=data
                if data.get('id') != rid:
                    raise WindError('Wind 请求编号不匹配')
                return self.result(data, method)
        except httpx.TimeoutException as exc:
            raise WindError(f'Wind {method} 请求超时；未发布部分数据') from exc
        except httpx.ConnectError as exc:
            reason = '本机出网受限（WinError 10013）' if '10013' in str(exc) else '网络连接失败'
            raise WindError(f'{reason}，请在正常本机环境启动服务') from exc
        except (ValueError,TypeError,AttributeError) as exc:
            raise WindError('Wind MCP 响应格式异常') from exc

    @staticmethod
    def result(data, method):
        if data.get('error'):
            raise WindError(f'Wind MCP {method} 被拒绝，错误码 {data["error"].get("code", "未知")}')
        result = data.get('result')
        if not isinstance(result,dict):
            raise WindError('Wind MCP 结果格式异常')
        return result

    async def list_tools(self):
        result = []; cursor = None; seen = set()
        for _ in range(100):
            page = await self.request('tools/list', {'cursor':cursor} if cursor else {})
            if not isinstance(page.get('tools'),list):
                raise WindError('Wind 未返回工具列表')
            result.extend(page['tools'])
            cursor = page.get('nextCursor')
            if not cursor:
                self.tools = result
                return result
            if cursor in seen:
                raise WindError('Wind 工具分页游标重复')
            seen.add(cursor)
        raise WindError('Wind 工具列表分页未完成')

    async def call(self, name, arguments):
        if name not in {tool.get('name') for tool in self.tools}:
            raise WindError('工具未在当前 Wind 服务中发现，禁止猜测调用')
        result = await self.request('tools/call', {'name':name, 'arguments':arguments})
        if result.get('isError'):
            if self.recorder and self.last_request_id:
                self.recorder.finish(self.last_request_id,json.loads(self.clean(json.dumps(self.response,ensure_ascii=False))),
                    f'Wind 工具 {name} 返回业务错误',self.http_status)
            raise WindError(f'Wind 工具 {name} 返回业务错误，未写入行情')
        return result
