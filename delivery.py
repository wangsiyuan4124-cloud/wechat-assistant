"""Subprocess boundary: no ambiguous sends are retried automatically."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def deliver(row, cfg, base, *, test_mode=None):
    expiry = row.get('expires_at')
    if expiry is None or expiry <= time.time():
        return 'expired', 'expired or legacy task without expiry'
    if row['grp'] not in cfg['wechat']['groups']:
        return 'failed', 'target outside allowlist'
    runtime = Path(base) / 'runtime'
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as tmp:
        request = Path(tmp) / 'request.json'
        result = Path(tmp) / 'result.json'
        request.write_text(json.dumps({'row': row, 'result': str(result), 'test_mode': test_mode}), encoding='utf-8')
        timeout = max(0.1, float(cfg.get('delivery', {}).get('send_timeout_sec', 45)))
        with (runtime / 'sender.log').open('a', encoding='utf-8') as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('send_once.py')), str(request)],
                                    cwd=str(base), stdout=log, stderr=log,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return 'uncertain', 'sender terminated on timeout; inspect WeChat before retry'
        if code != 0 or not result.exists():
            return 'uncertain', 'sender interrupted without reliable outcome'
        try:
            outcome = json.loads(result.read_text(encoding='utf-8'))
            if outcome['status'] not in ('done', 'pending', 'expired', 'uncertain'):
                raise ValueError('invalid status')
            return outcome['status'], outcome.get('error', '')
        except (ValueError, KeyError):
            return 'uncertain', 'invalid sender outcome'
