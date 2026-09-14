"""Windows user-bound DPAPI storage. No plaintext credential files."""
import ctypes
import os
import sqlite3
from ctypes import wintypes
from . import storage as store


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def transform(raw, encrypt):
    if os.name != 'nt':
        raise RuntimeError('本机加密保存需要 Windows；其他系统请通过 WIND_KEY 环境变量配置')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError('Windows 凭据加密操作失败，请使用相同 Windows 用户运行服务')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def key_path():
    return store.DB.parent / 'wind-key.dpapi'


def config_connection():
    path = store.DB.parent / 'wind-config.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS credentials (name TEXT PRIMARY KEY, encrypted BLOB NOT NULL)')
    return db


def save_wind_key(key):
    encrypted = transform(key.encode('utf-8'), True)
    db = config_connection()
    try:
        with db:
            db.execute('INSERT OR REPLACE INTO credentials VALUES (?,?)', ('wind', encrypted))
    finally:
        db.close()
    key_path().unlink(missing_ok=True)


def read_wind_key():
    if os.environ.get('WIND_KEY'):
        return os.environ['WIND_KEY']
    db = config_connection()
    try:
        row = db.execute('SELECT encrypted FROM credentials WHERE name=?', ('wind',)).fetchone()
    finally:
        db.close()
    if row:
        return transform(row[0], False).decode('utf-8')
    # Migrate the encrypted file from the first persistence version, if present.
    path = key_path()
    if path.exists():
        key = transform(path.read_bytes(), False).decode('utf-8')
        save_wind_key(key)
        return key
    return ''


def clear_wind_key():
    db = config_connection()
    try:
        with db:
            db.execute('DELETE FROM credentials WHERE name=?', ('wind',))
    finally:
        db.close()
    key_path().unlink(missing_ok=True)
