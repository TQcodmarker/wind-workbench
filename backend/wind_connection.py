"""Wind MCP initialization probe with redacted, actionable failures."""
import json
import httpx

ENDPOINT = 'https://mcp.wind.com.cn/vserver_bond_data/mcp/'


def handshake_status(data):
    if not isinstance(data, dict) or data.get('id') != 1:
        return None
    if data.get('error'):
        return 'MCP 握手被服务拒绝，请检查服务权限与协议支持'
    result = data.get('result')
    return '握手成功' if isinstance(result, dict) and result.get('protocolVersion') else 'MCP 返回格式不兼容，未完成握手'


async def probe_wind(key):
    if not key.isascii():
        return 'Key 包含非英文字符，请重新复制官方提供的 Key'
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream('POST', ENDPOINT,
                headers={'Authorization':'Bearer '+key, 'Accept':'application/json, text/event-stream'},
                json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{
                    'protocolVersion':'2024-11-05','capabilities':{},
                    'clientInfo':{'name':'wind-workbench-demo','version':'1.0.0'}}}) as response:
                if response.status_code == 401:
                    return 'Wind 返回 HTTP 401：Key 未被接受，请检查是否有效或已过期'
                if response.status_code == 403:
                    return 'Wind 返回 HTTP 403：访问被拒绝，请检查债券服务权限或访问限制'
                if response.status_code != 200:
                    return f'Wind 返回 HTTP {response.status_code}：服务请求未成功，请稍后重试'
                if 'text/event-stream' in response.headers.get('content-type', ''):
                    parts = []
                    async for line in response.aiter_lines():
                        if line.startswith('data:'):
                            parts.append(line[5:].lstrip())
                        elif not line and parts:
                            status = handshake_status(json.loads('\n'.join(parts)))
                            if status:
                                return status  # Do not wait for an SSE connection to close.
                            parts = []
                    if parts:
                        status = handshake_status(json.loads('\n'.join(parts)))
                        if status:
                            return status
                    return 'MCP 未返回初始化结果，未完成握手'
                data = json.loads(await response.aread())
                return handshake_status(data) or 'MCP 未返回对应的初始化结果'
    except httpx.ConnectError as error:
        if '10013' in str(error):
            return '本机禁止服务进程联网（WinError 10013），请在正常本机权限下启动后端'
        if 'CERTIFICATE_VERIFY_FAILED' in str(error):
            return 'TLS 证书验证失败，请检查本机证书或网络代理'
        return '尚未连接到 Wind，请检查本机网络、DNS 或防火墙；Key 尚未验证'
    except httpx.TimeoutException:
        return 'Wind 连接或握手响应超时，请稍后重试；尚不能确认 Key 是否有效'
    except (ValueError, TypeError):
        return 'Wind 响应无法按 MCP 格式解析，未完成握手'
    except Exception:
        return '本机连接测试发生异常，尚不能确认 Key 是否有效'
