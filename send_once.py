"""Single send process. Imported by no background thread."""
import json
import os
import sys
import time
from pathlib import Path


def desktop_ready():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u.OpenInputDesktop.restype = wintypes.HANDLE
    u.SwitchDesktop.argtypes = [wintypes.HANDLE]
    u.CloseDesktop.argtypes = [wintypes.HANDLE]
    desktop = u.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        return False
    try:
        if not u.SwitchDesktop(desktop):
            return False
    finally:
        u.CloseDesktop(desktop)
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found = []
    cbtype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        cls = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, title, 256)
        u.GetClassNameW(hwnd, cls, 256)
        if title.value in ('微信', 'WeChat', 'Weixin') and cls.value.startswith('Qt') and u.IsWindowVisible(hwnd) and not u.IsIconic(hwnd):
            found.append(hwnd)
        return True
    u.EnumWindows(cbtype(cb), 0)
    return len(found) == 1


def execute(data):
    # Test modes are CLI-local and never taken from cloud task fields.
    mode = data.get('test_mode')
    if mode:
        if mode == 'timeout':
            time.sleep(60)
        if mode == 'crash':
            os._exit(3)
        return {'status': {'success': 'done', 'deferred': 'pending', 'ambiguous': 'uncertain'}[mode]}
    row = data['row']
    if row['expires_at'] <= time.time():
        return {'status': 'expired', 'error': 'expired before send'}
    if not desktop_ready():
        return {'status': 'pending', 'error': 'desktop locked or WeChat window unavailable; no UI action taken'}
    import yaml
    from wechat_client import make_client, com_init
    cfg = yaml.safe_load(Path(__file__).with_name('config.yaml').read_text(encoding='utf-8'))
    if cfg['wechat'].get('engine', 'rpa') != 'rpa' or row['grp'] not in cfg['wechat']['groups']:
        return {'status': 'uncertain', 'error': 'configuration changed; inspect task'}
    com_init()
    client = make_client(log=print)
    # From this point any exception may follow an actual send. Never auto-retry.
    try:
        client.send_to_group(row['grp'], row['text'], retries=1)
        return {'status': 'done', 'error': 'UI send returned; recipient delivery not verified'}
    except Exception as e:
        return {'status': 'uncertain', 'error': str(e)}


if __name__ == '__main__':
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    try:
        outcome = execute(data)
    except Exception as e:
        outcome = {'status': 'uncertain', 'error': str(e)}
    dest = Path(data['result'])
    temp = dest.with_suffix('.tmp')
    temp.write_text(json.dumps(outcome), encoding='utf-8')
    temp.replace(dest)
