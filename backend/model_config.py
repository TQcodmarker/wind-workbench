"""Model configuration and an explicit, minimal connectivity probe."""
import json
import time
from threading import RLock
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from . import storage as store

router = APIRouter(prefix='/api/model', tags=['Model configuration'])
lock = RLock()
secret = ''
revision = 0
test_result = None
DEFAULTS = {'baseUrl': '', 'model': '', 'requireKey': True}
REQUEST_TIMEOUT_SECONDS = 30


def saved_settings():
    with store.connection() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='model_config'").fetchone()
    stored = json.loads(row['value']) if row else {}
    return {key: stored.get(key, value) for key, value in DEFAULTS.items()}


def public_config():
    settings = saved_settings()
    return dict(**settings, hasKey=bool(secret), configured=bool(settings['baseUrl'] and settings['model']),
                readyToTest=bool(settings['baseUrl'] and settings['model'] and (secret or not settings['requireKey'])),
                testResult=test_result, assistantEnabled=False)


class ModelInput(BaseModel):
    baseUrl: str = Field(max_length=2048)
    model: str = Field(max_length=200)
    apiKey: str = Field(default='', max_length=4096)
    requireKey: bool = True


@router.get('')
def get_config():
    with lock:
        return public_config()


@router.post('')
def save_config(body: ModelInput):
    global secret, revision, test_result
    base = body.baseUrl.strip().rstrip('/')
    try:
        parsed = urlsplit(base)
        parsed.port  # Reject malformed port specifications as well.
        valid = parsed.hostname and parsed.scheme in ['http', 'https'] and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in base):
        raise HTTPException(400, '请输入有效的 HTTP(S) Base URL，不要包含凭据、查询参数或片段')
    if parsed.scheme == 'http' and parsed.hostname not in ['localhost', '127.0.0.1', '::1']:
        raise HTTPException(400, '远程服务请使用 HTTPS；HTTP 仅用于本机模型服务')
    if parsed.path.endswith('/chat/completions'):
        raise HTTPException(400, '请填写 Base URL，不要包含 /chat/completions')
    model, key = body.model.strip(), body.apiKey.strip()
    if not model or any(c.isspace() for c in model):
        raise HTTPException(400, '请填写不含空格的模型 ID')
    if key and (key.lower().startswith('bearer ') or any(c.isspace() for c in key)):
        raise HTTPException(400, 'API Key 不应包含空格或 Bearer 前缀')
    with lock:
        old = saved_settings()
        # Never forward a previously saved credential to a changed provider URL.
        next_key = (key or secret) if old['baseUrl'] == base else key
        if body.requireKey and not next_key:
            raise HTTPException(400, '请填写 API Key；更换服务地址时需要重新填写')
        settings = dict(baseUrl=base, model=model, requireKey=body.requireKey)
        with store.connection() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('model_config',?)", (json.dumps(settings),))
        secret = next_key if body.requireKey else ''
        revision += 1
        test_result = None
        return public_config()


@router.post('/clear')
def clear_config():
    global secret, revision, test_result
    with lock:
        with store.connection() as db:
            db.execute("DELETE FROM metadata WHERE key='model_config'")
        secret = ''
        revision += 1
        test_result = None
        return public_config()


@router.post('/test')
async def test_connection():
    global test_result
    with lock:
        config = public_config()
        token, version = secret, revision
    if not config['readyToTest']:
        raise HTTPException(400, '请先保存完整配置；服务重启后需重新填写 API Key')
    start = time.monotonic()
    result = dict(status='failed', message='连接失败', testedAt=store.now(), latencyMs=None)
    try:
        headers = {'Authorization': 'Bearer ' + token} if config['requireKey'] else {}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
            response = await client.post(config['baseUrl'] + '/chat/completions', headers=headers,
                json={'model': config['model'], 'messages': [{'role': 'user', 'content': 'Reply with OK only.'}], 'stream': False})
        if response.status_code in [401, 403]:
            result['message'] = '鉴权失败，请检查 API Key 与模型访问权限'
        elif response.status_code == 429:
            result['message'] = '请求受到限流或额度不足，请检查服务商账户'
        elif response.status_code == 404:
            result['message'] = '接口或模型不存在，请检查 Base URL 与模型 ID'
        elif response.status_code != 200:
            result['message'] = f'服务返回 HTTP {response.status_code}，请检查服务地址与协议兼容性'
        else:
            data = response.json()
            content = data.get('choices', [{}])[0].get('message', {}).get('content')
            if isinstance(content, str) and content.strip():
                result.update(status='passed', message='连接成功，指定模型已返回有效回复')
            else:
                result['message'] = '服务可达，但未返回兼容的文本回复'
    except httpx.TimeoutException:
        result['message'] = '请求超时，请检查服务状态或稍后重试'
    except Exception:
        # No provider payloads, credentials, or raw network errors are echoed.
        result['message'] = '连接失败，请检查网络、服务地址与返回格式'
    result['latencyMs'] = round((time.monotonic() - start) * 1000)
    with lock:
        if revision != version:
            raise HTTPException(409, '配置已更改，本次测试结果已丢弃，请重新测试')
        test_result = result
        return public_config()
