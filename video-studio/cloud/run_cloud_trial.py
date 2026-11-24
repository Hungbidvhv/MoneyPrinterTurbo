"""One real end-to-end CI test; refuses to install/run rendering on a local PC.

Runs n8n and MoneyPrinterTurbo on a standard GitHub-hosted Linux runner.
Only synthetic public test content; no provider keys, paid LLMs or Pexels.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / '.cloud-trial'
RESULTS = ROOT / 'trial-results'
N8N_IMAGE = 'docker.n8n.io/n8nio/n8n:2.49.5'
MPT_IMAGE = 'ghcr.io/harry0703/moneyprinterturbo:latest'
BASE = 'http://127.0.0.1:8786'


def request(route, body=None, headers=None):
    default_headers = {'Content-Type': 'application/json', 'X-Studio-Request': '1'}
    req = urllib.request.Request(BASE + route, data=json.dumps(body).encode() if body is not None else None,
                                 headers=default_headers | (headers or {}))
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def run(command, name, timeout=300):
    with (WORK / f'{name}.log').open('wb') as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{name} failed; raw logs remain only on the temporary runner (not published).')


def wait_health(url, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise RuntimeError('Service did not become ready within the time limit.')


def workflow():
    return {
        'id': 'studioCloudTrial01', 'name': 'Video Studio - real integration test', 'active': False,
        'nodes': [
            {'id': 'start', 'name': 'Start', 'type': 'n8n-nodes-base.manualTrigger', 'typeVersion': 1, 'position': [0, 0], 'parameters': {}},
            {'id': 'step', 'name': 'Process one step', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.2,
             'position': [240, 0], 'parameters': {'method': 'POST', 'url': BASE + '/internal/tick', 'authentication': 'genericCredentialType',
                 'genericAuthType': 'httpHeaderAuth', 'sendBody': True, 'specifyBody': 'json', 'jsonBody': '{}', 'options': {'timeout': 240000}},
             'credentials': {'httpHeaderAuth': {'id': 'studioTrialKey01', 'name': 'Trial internal key'}}, 'retryOnFail': False},
            {'id': 'finished', 'name': 'Finished', 'type': 'n8n-nodes-base.if', 'typeVersion': 2.2, 'position': [480, 0],
             'parameters': {'conditions': {'options': {'caseSensitive': True, 'typeValidation': 'strict', 'version': 2},
                 'conditions': [{'id': 'terminal-state', 'leftValue': "={{ ['done','failed','attention'].includes($json.state) }}",
                                 'rightValue': '', 'operator': {'type': 'boolean', 'operation': 'true', 'singleValue': True}}], 'combinator': 'and'}}},
            {'id': 'wait', 'name': 'Wait ten seconds', 'type': 'n8n-nodes-base.wait', 'typeVersion': 1.1,
             'position': [480, 220], 'parameters': {'amount': 10, 'unit': 'seconds'}}
        ],
        'connections': {'Start': {'main': [[{'node': 'Process one step', 'type': 'main', 'index': 0}]]},
            'Process one step': {'main': [[{'node': 'Finished', 'type': 'main', 'index': 0}]]},
            'Finished': {'main': [[], [{'node': 'Wait ten seconds', 'type': 'main', 'index': 0}]]},
            'Wait ten seconds': {'main': [[{'node': 'Process one step', 'type': 'main', 'index': 0}]]}},
        'settings': {'executionOrder': 'v1', 'saveDataSuccessExecution': 'none', 'saveDataErrorExecution': 'none', 'executionTimeout': 900},
    }


def main():
    if os.getenv('GITHUB_ACTIONS') != 'true' or sys.platform != 'linux':
        raise SystemExit('This real-video test runs only on GitHub Actions Linux. Nothing was installed or rendered locally.')
    if WORK.exists():
        raise SystemExit('Trial workspace already exists; refusing to reuse ambiguous render state.')
    WORK.mkdir(mode=0o700)
    RESULTS.mkdir(exist_ok=True)
    imports = WORK / 'imports'; imports.mkdir(mode=0o700)
    n8n_data = WORK / 'n8n-data'; n8n_data.mkdir(mode=0o700)
    key = secrets.token_hex(24)
    renderer_key = secrets.token_hex(24)
    password = secrets.token_hex(24)
    env = os.environ | {'STUDIO_MODE': 'live', 'STUDIO_HOST': '127.0.0.1', 'STUDIO_PORT': '8786',
        'STUDIO_DATA': str(WORK / 'studio-data'), 'STUDIO_INTERNAL_KEY': key, 'STUDIO_PASSWORD': '',
        'MPT_BASE_URL': 'http://127.0.0.1:8088', 'MPT_API_KEY': renderer_key,
        'MPT_LOCAL_MATERIALS': json.dumps(['trial-1.png', 'trial-2.png', 'trial-3.png']),
        'LLM_API_KEY': '', 'LLM_MODEL': '', 'DAILY_VIDEO_LIMIT': '1', 'MIN_FREE_MB': '2048'}
    credential = [{'id': 'studioTrialKey01', 'name': 'Trial internal key', 'type': 'httpHeaderAuth', 'data': {'name': 'X-Studio-Key', 'value': key}}]
    (imports / 'credentials.json').write_text(json.dumps(credential))
    (imports / 'workflow.json').write_text(json.dumps(workflow()))
    config = WORK / 'config.toml'
    config.write_text('listen_host = "0.0.0.0"\nlisten_port = 8080\nlog_level = "WARNING"\n[app]\n'
                      f'api_key = "{renderer_key}"\nmax_concurrent_tasks = 1\nmax_queued_tasks = 1\n'
                      'enable_redis = false\nendpoint = ""\ntls_verify = true\n')
    run(['docker', 'pull', MPT_IMAGE], 'pull-mpt', timeout=600)
    run(['docker', 'pull', N8N_IMAGE], 'pull-n8n', timeout=600)
    # Persist the exact images used in this integration test, without leaking any credentials.
    images = {}
    for label, image in [('moneyprinter', MPT_IMAGE), ('n8n', N8N_IMAGE)]:
        images[label] = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]['Id']
    app = None
    renderer_started = False
    try:
        run(['docker', 'run', '-d', '--name', 'studio-trial-mpt', '--memory', '5g', '--cpus', '2',
             '-p', '127.0.0.1:8088:8080', '-v', f'{config}:/MoneyPrinterTurbo/config.toml:ro',
             '--log-opt', 'max-size=5m', '--log-opt', 'max-file=1', MPT_IMAGE, 'python3', 'main.py'], 'start-mpt')
        renderer_started = True
        wait_health('http://127.0.0.1:8088/openapi.json')
        run(['docker', 'cp', str(ROOT / 'cloud/make_trial_art.py'), 'studio-trial-mpt:/tmp/make_trial_art.py'], 'copy-art')
        run(['docker', 'exec', 'studio-trial-mpt', 'python3', '/tmp/make_trial_art.py'], 'make-art')
        app_log = (WORK / 'studio.log').open('wb')
        app = subprocess.Popen([sys.executable, '-B', str(ROOT / 'studio.py')], env=env, stdout=app_log, stderr=subprocess.STDOUT)
        wait_health(BASE + '/healthz', timeout=30)
        script = ('Tiết kiệm điện bắt đầu từ những việc nhỏ. '
                  'Tắt đèn và thiết bị khi không sử dụng. '
                  'Mở rèm để tận dụng ánh sáng ban ngày. '
                  'Với điều hòa, hãy chọn mức nhiệt phù hợp và đóng kín cửa. '
                  'Mỗi thói quen nhỏ đều góp phần giảm lãng phí.')
        job = request('/api/jobs', {'topic': 'Ba thói quen tiết kiệm điện - kiểm thử tích hợp', 'seconds': 30, 'review': False, 'script': script}, {'Idempotency-Key': 'real-integration-trial-01'})
        n8n = ['docker', 'run', '--rm', '--network', 'host', '--user', f'{os.getuid()}:{os.getgid()}',
               '--memory', '1500m', '--cpus', '1', '-e', 'HOME=/home/node', '-e', 'N8N_USER_FOLDER=/home/node',
               '-e', 'N8N_DIAGNOSTICS_ENABLED=false', '-e', 'N8N_LOG_LEVEL=warn',
               '-e', f'N8N_ENCRYPTION_KEY={password}', '-v', f'{imports}:/imports:ro',
               '-v', f'{n8n_data}:/home/node/.n8n', N8N_IMAGE]
        run(n8n + ['import:credentials', '--input=/imports/credentials.json'], 'import-credentials')
        run(n8n + ['import:workflow', '--input=/imports/workflow.json'], 'import-workflow')
        # n8n itself drives each step and waits for rendering; not a mocked scheduler.
        run(n8n + ['execute', '--id=studioCloudTrial01'], 'execute-n8n', timeout=960)
        final = next(j for j in request('/api/jobs') if j['id'] == job['id'])
        if final['state'] != 'done' or not final['has_video']:
            (RESULTS / 'result.json').write_text(json.dumps({'success': False, 'state': final['state'], 'error': final['error']}, ensure_ascii=False, indent=2))
            raise RuntimeError('Real integration test did not produce a completed video. No paid retry was made.')
        source = WORK / 'studio-data/videos' / f"{job['id']}.mp4"
        if source.stat().st_size > 25 * 1024**2:
            raise RuntimeError('Trial output exceeds the 25 MB artifact limit; refusing to publish it.')
        shutil.copyfile(source, RESULTS / 'video-test.mp4')
        # Verify actual decode and media streams inside the renderer, not just MP4 signature.
        run(['docker', 'cp', str(RESULTS / 'video-test.mp4'), 'studio-trial-mpt:/tmp/result.mp4'], 'copy-result-for-validation')
        probe = subprocess.check_output(['docker', 'exec', 'studio-trial-mpt', 'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', '/tmp/result.mp4'])
        metadata = json.loads(probe)
        if not any(s['codec_type'] == 'video' for s in metadata['streams']) or not any(s['codec_type'] == 'audio' for s in metadata['streams']):
            raise RuntimeError('Missing video or narration audio stream.')
        run(['docker', 'exec', 'studio-trial-mpt', 'ffmpeg', '-v', 'error', '-i', '/tmp/result.mp4', '-f', 'null', '-'], 'decode-result', timeout=120)
        (RESULTS / 'result.json').write_text(json.dumps({'success': True, 'orchestrator': 'n8n', 'renderer': 'MoneyPrinterTurbo',
            'paid_llm_calls': 0, 'pexels_calls': 0, 'bytes': source.stat().st_size,
            'duration_seconds': float(metadata['format']['duration']), 'images': images,
            'content': 'Original synthetic integration-test slides and Vietnamese narration; no user media.'}, indent=2))
        print('Real n8n → Video Studio → MoneyPrinterTurbo integration test passed.')
    except Exception:
        # This job only contains synthetic test content. Redact all ephemeral keys
        # before publishing bounded diagnostics to the GitHub job log.
        diagnostics = []
        for name in ('import-credentials', 'import-workflow', 'execute-n8n', 'studio'):
            log = WORK / f'{name}.log'
            if log.exists():
                diagnostics.append(f'--- {name}: ' + log.read_text(errors='replace')[-4000:])
        if renderer_started:
            result = subprocess.run(['docker', 'logs', '--tail', '70', 'studio-trial-mpt'], capture_output=True, text=True, timeout=15)
            diagnostics.append('--- renderer: ' + (result.stdout + result.stderr)[-6000:])
        text = '\n'.join(diagnostics)
        for secret in (key, renderer_key, password):
            text = text.replace(secret, '[REDACTED]')
        print(text)
        raise
    finally:
        if app is not None:
            app.terminate()
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill(); app.wait(timeout=5)
            app_log.close()
        if renderer_started:
            subprocess.run(['docker', 'rm', '-f', 'studio-trial-mpt'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
