"""Operator-controlled native zero-force teaching. No torque-disable commands."""
import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.sync.client import connect

ROBOT = '10.230.20.13'
LOCK = threading.Lock()
SESSION = None


def call(service, args=None):
    request_id = uuid.uuid4().hex
    with connect(f'ws://{ROBOT}:9090', proxy=None, open_timeout=4, close_timeout=1) as ws:
        ws.send(json.dumps({'op': 'call_service', 'id': request_id,
                            'service': service, 'args': args or {}}))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            msg = json.loads(ws.recv(timeout=max(0.1, deadline-time.monotonic())))
            if msg.get('id') != request_id:
                continue
            if msg.get('result') is False:
                raise RuntimeError(str(msg.get('values', msg)))
            return msg.get('values', {})
    raise TimeoutError('机器人服务未确认，请先检查当前状态，勿反复点击')


def task():
    return call('/application/request/get_current_task_info').get('task_info', {})


def recording(action=3):
    return call('/bag_recorder/sdk_recording_control',
                {'action': action, 'session_id': SESSION or '', 'profile_id': '',
                 'bag_name': '', 'data_items': []})


def drag_controller_status():
    controllers = call('/controller_manager/list_controllers').get('controller', [])
    matches = [c for c in controllers
               if c.get('type') == 'x2robot_controllers/ZeroForceDragController']
    active = [c for c in matches if c.get('state') == 'active']
    return {'loaded': bool(matches), 'active': bool(active),
            'controllers': [{'name': c.get('name'), 'state': c.get('state')} for c in matches]}


def status():
    info = task()
    controller = drag_controller_status()
    try:
        rec = recording()
    except Exception as exc:
        rec = {'error': str(exc)}
    ready = (info.get('type') == 23 and
             info.get('zero_force_drag_info', {}).get('state') == 2 and controller['active'])
    warning = '' if ready else ('EX001 零力拖动控制器未加载；任务就绪不代表机械臂可拖动'
                                if not controller['loaded'] else '拖动执行控制器尚未激活')
    return {'execution_ready': ready, 'execution_warning': warning,
            'drag_controller': controller,
            'task_type': info.get('type'), 'task_status': info.get('status'),
            'task_error': info.get('error_code'),
            'drag': info.get('zero_force_drag_info', {}), 'recording': rec}


def drag(enter):
    current = task()
    if enter and not drag_controller_status()['loaded']:
        raise RuntimeError('EX001 未加载零力拖动控制器；需配套的 EX001 厂家配置，不能只切换模式')
    if enter and current.get('type') != 0:
        raise RuntimeError('机器人不是空闲任务；先结束当前任务')
    if not enter and current.get('type') != 23:
        raise RuntimeError('当前不是零力拖动任务，不结束其他任务')
    result = call('/application/request/task_operation', {
        'operation_type': 1 if enter else 2, 'task_type': 23,
        'task_operation_request': {'need_homing': False},
        'is_from_remote': True, 'request_source': 1})
    if not result.get('res'):
        raise RuntimeError('官方任务接口拒绝：' + json.dumps(result, ensure_ascii=False))
    # Acceptance alone is not readiness. Surface the measured business state.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        info = task()
        d = info.get('zero_force_drag_info', {})
        if d.get('error_code', 0):
            raise RuntimeError('零力拖动错误：' + json.dumps(d))
        if enter and info.get('type') == 23 and d.get('state') == 2 and drag_controller_status()['active']:
            return {'message': '官方反馈：可拖动', 'task': info}
        if not enter and info.get('type') != 23:
            return {'message': '已退出零力拖动；没有请求归零', 'task': info}
        time.sleep(0.2)
    raise TimeoutError('任务已受理但未确认目标状态；查看实时状态，不要按未确认状态操作')


def perform(action):
    """Shared entry point used by the main console; no second server required."""
    global SESSION
    if not LOCK.acquire(blocking=False):
        raise RuntimeError('已有示教操作等待确认')
    try:
        if action == 'enter':
            return drag(True)
        if action == 'exit':
            if SESSION and recording().get('state') in (1, 2, 3):
                raise RuntimeError('先结束录制，等待文件完成')
            return drag(False)
        if action == 'record-start':
            info = task()
            if info.get('type') != 23 or info.get('zero_force_drag_info', {}).get('state') != 2:
                raise RuntimeError('先进入拖动，等待可拖动')
            if not drag_controller_status()['active']:
                raise RuntimeError('拖动控制器未激活，不能把此状态当作拖动示教')
            if recording().get('state') in (1, 2, 3):
                raise RuntimeError('已有录制任务')
            SESSION = uuid.uuid4().hex
            result = recording(1)
        elif action == 'record-stop':
            if not SESSION:
                raise RuntimeError('本控制台没有启动录制')
            result = recording(2)
        else:
            raise ValueError('未知示教操作')
        if not result.get('success'):
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result
    finally:
        LOCK.release()


PAGE = '''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>原生拖动示教</title><style>
body{background:#101925;color:#e2eaf4;font:16px/1.6 system-ui;margin:0;padding:24px}
main{max-width:1050px;margin:auto}section{background:#192638;padding:22px;border:1px solid #35485e;border-radius:12px;margin:16px 0}
button{padding:13px 18px;margin:7px;border:1px solid #64829c;border-radius:8px;background:#28445c;color:white;font:inherit;cursor:pointer}
button:disabled{opacity:.5}button.exit{background:#783442}label{display:block;margin:14px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}img{width:100%;max-height:450px;object-fit:contain}
#notice{color:#8fe1c0;min-height:26px}</style><main>
<h1>原生零力拖动 · 示教采集</h1>
<p>通过官方任务接口进入 Hold/Drag，不执行断力矩，也不自动归零。此面板不调用 VLA。</p>
<section><h2>1 · 进入拖动</h2>
<p>零力拖动可能涉及双臂，不等于单臂锁定。现场需清场、空爪，双臂获得支撑，能随时操作实体急停。手臂当前已停止后再进入。</p>
<label><input type="checkbox" id="ready"> 我已在机器人旁，确认双臂空载、获得支撑，准备好接管。</label>
<button id="enter" onclick="act('enter')">进入零力拖动</button>
<button class="exit" onclick="act('exit')">退出零力拖动（不归零）</button>
<p>只有状态显示“可拖动”才尝试轻缓引导；若仍僵硬、突然下坠或报错，停止尝试并联系现场 FAE。</p></section>
<section><h2>2 · 录制示范</h2>
<p>先开始录制，再示范抓枪与转向，最后停止录制。录制状态达到 READY 且 ready=true 才算文件完成。录制不等于模型训练，也不自动上传。</p>
<button onclick="act('record-start')">开始录制</button>
<button onclick="act('record-stop')">结束录制</button>
<p>accepted_items 是实际录制项：需核对是否包含画面、实际状态和动作数据，不能只凭生成了文件就当作训练集。</p></section>
<p id="notice" role="status"></p><section><h2>实时反馈</h2><pre id="state">读取中…</pre></section>
<p><a style="color:#93c8ff" href="http://localhost:8900/" target="_blank">打开相机控制台</a></p>
<script>
let busy=false;
async function poll(){try{const r=await fetch('/state');const d=await r.json();
document.getElementById('state').textContent=JSON.stringify(d,null,2);
const s=d.drag?.state;document.title=s===2?'可拖动 · 原生示教':'原生拖动示教';
document.getElementById('enter').disabled=busy||!document.getElementById('ready').checked||d.task_type!==0;
}catch(e){document.getElementById('state').textContent='状态读取失败：'+e;}setTimeout(poll,1500);}
document.getElementById('enter').disabled=true;
async function act(action){if(busy)return;if(action==='enter'&&!document.getElementById('ready').checked)return;
busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
document.getElementById('notice').textContent='等待机器人确认…';
try{const r=await fetch('/'+action,{method:'POST'});const d=await r.json();document.getElementById('notice').textContent=JSON.stringify(d);}
catch(e){document.getElementById('notice').textContent=String(e);}
finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}}poll();
</script></main></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, data, html=False):
        body = (data if html else json.dumps(data, ensure_ascii=False)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == '/':
                self.send(200, PAGE, True)
            elif self.path == '/state':
                self.send(200, status())
            else:
                self.send(404, {'error': 'not found'})
        except Exception as exc:
            self.send(502, {'error': str(exc)})

    def do_POST(self):
        global SESSION
        # This physical-control panel is local-only; reject cross-origin requests.
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            return self.send(403, {'error': 'cross-origin request rejected'})
        if not LOCK.acquire(blocking=False):
            return self.send(409, {'error': '已有操作等待确认'})
        try:
            if self.path in ('/enter', '/exit'):
                if self.path == '/exit' and SESSION:
                    active_recording = recording()
                    if active_recording.get('state') in (1, 2, 3):
                        raise RuntimeError('请先结束录制，等 ready=true 后再退出拖动')
                result = drag(self.path == '/enter')
            elif self.path == '/record-start':
                info = task()
                if info.get('type') != 23 or info.get('zero_force_drag_info', {}).get('state') != 2:
                    raise RuntimeError('先确认零力拖动可用，再录制示范')
                previous = recording()
                if previous.get('state') in (1, 2, 3):
                    raise RuntimeError('已有录制任务，不能覆盖')
                SESSION = uuid.uuid4().hex
                result = recording(1)
                if not result.get('success'):
                    raise RuntimeError(json.dumps(result, ensure_ascii=False))
            elif self.path == '/record-stop':
                if not SESSION:
                    raise RuntimeError('本面板没有启动录制，不停止其他人的录制')
                result = recording(2)
            else:
                return self.send(404, {'error': 'not found'})
            self.send(200, result)
        except Exception as exc:
            self.send(400, {'error': str(exc)})
        finally:
            LOCK.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ip', default=ROBOT)
    parser.add_argument('--port', type=int, default=8901)
    args = parser.parse_args()
    ROBOT = args.ip
    print(f'原生示教面板：http://localhost:{args.port}（启动不切换模式）', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
