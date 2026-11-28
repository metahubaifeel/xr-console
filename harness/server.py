import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from .config import ROOT
from .engine import Conflict


def make_server(engine, host='127.0.0.1', port=8910):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type='application/json; charset=utf-8'):
            raw = json.dumps(value, ensure_ascii=False).encode() if not isinstance(value, bytes) else value
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def trusted(self):
            host_header = self.headers.get('Host', '')
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            origin = self.headers.get('Origin')
            return host_header in allowed and (origin is None or origin in {'http://' + h for h in allowed})

        def do_GET(self):
            if not self.trusted():
                return self.send(403, {'error': 'local same-origin access required'})
            path = urlsplit(self.path).path
            if path == '/':
                return self.send(200, (ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if path == '/app.js':
                return self.send(200, (ROOT / 'app.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path.startswith('/api/camera/'):
                try:
                    return self.send(200, engine.robot.camera(path.rsplit('/',1)[1]), 'image/jpeg')
                except ValueError:
                    return self.send(404, {'error':'unknown camera'})
                except Exception:
                    return self.send(503, {'error':'camera unavailable'})
            if path == '/api/runtime':
                try:
                    return self.send(200,engine.robot.runtime_status())
                except Exception:
                    return self.send(503,{'error':'client status unavailable'})
            if path == '/api/motors':
                try:return self.send(200,engine.robot.motor('catalog'))
                except Exception as exc:return self.send(503,{'error':str(exc)[:300]})
            if path == '/api/motor/status':
                return self.send(200,engine.motor_jobs.state())
            if path == '/api/state':
                return self.send(200, engine.state())
            if path == '/api/hold':
                try:
                    return self.send(200, engine.robot.lock_state())
                except Exception:
                    return self.send(503, {'error': 'hold status unavailable'})
            if path == '/api/frames':
                return self.send(200, engine.frames())
            if path == '/api/config':
                return self.send(200, {'mode': engine.robot.mode, 'vision_model': engine.c['model'],
                    'vision_key_configured': bool(engine.c['openai_key']), 'verification_mode': 'after_execution_single_success',
                    'code_targets': engine.c['code_targets'], 'monitor_interval': engine.c['monitor_interval'],
                    'max_vision_calls': engine.c['max_vision_calls']})
            return self.send(404, {'error': 'not found'})

        def do_POST(self):
            if not self.trusted():
                return self.send(403, {'error': 'local same-origin access required'})
            try:
                self.connection.settimeout(5)
                n = int(self.headers.get('Content-Length', '0'))
                if n < 0 or n > 8192:
                    return self.send(413, {'error': 'body too large'})
                if self.headers.get_content_type() != 'application/json':
                    return self.send(415, {'error': 'application/json required'})
                data = json.loads(self.rfile.read(n) or b'{}')
                if not isinstance(data, dict):
                    raise ValueError('JSON object required')
                sid = data.get('session_id')
                path = urlsplit(self.path).path
                if path == '/api/motor/plan':
                    return self.send(200,engine.robot.motor('plan',data))
                if path == '/api/motor/move':
                    return self.send(202,engine.move_motor(data))
                if path == '/api/motor/cancel':
                    return self.send(200,engine.motor_jobs.cancel(data.get('command_id')))
                if path == '/api/reference':
                    return self.send(200, engine.record_reference())
                if path=='/api/test/step3-left':
                    return self.send(202,engine.start(step=3,single_step=True,test_profile='step3_left'))
                if path in ('/api/test/start','/api/vision/check'):
                    return self.send(202, engine.start(step=data.get('step',1),single_step=True,
                        target=data.get('target'),code=data.get('code'),check_only=path=='/api/vision/check'))
                if path == '/api/session/start':
                    return self.send(200, engine.start(data.get('auto_monitor', False)))
                if path in ('/api/session/complete', '/api/session/observe'):
                    if path == '/api/session/complete' and type(data.get('step')) is not int:
                        raise ValueError('step number required for completion event')
                    return self.send(202, engine.observe(sid, data.get('step')))
                if path == '/api/session/scan':
                    return self.send(200, engine.scan(sid, data.get('code')))
                if path == '/api/session/retry':
                    return self.send(200, engine.retry(sid))
                if path == '/api/session/stop':
                    return self.send(200, engine.stop(sid))
                return self.send(404, {'error': 'not found'})
            except Conflict as exc:
                self.send(409, {'error': str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                self.send(400, {'error': str(exc)[:300]})
            except Exception:
                self.send(502, {'error': 'backend operation failed; see session state (no upstream body exposed)'})
    return ThreadingHTTPServer((host, port), Handler)
