import io, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import CompressedImage

WANT = {
    '/camera_head_front/color/image_raw/compressed':    'head',
    '/camera_chest_front/color/image_raw/compressed':   'chest',
    '/camera_chassis_front/color/image_raw/compressed': 'bev',
}
FRAME = {}
STAMP = {}
LOCK = threading.Lock()


class G(Node):
    def __init__(self):
        super().__init__('cam_srv')
        q = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                       history=HistoryPolicy.KEEP_LAST)
        for t, name in WANT.items():
            self.create_subscription(CompressedImage, t,
                                     lambda m, n=name: self.on(n, m), q)

    def on(self, name, m):
        d = bytes(m.data)
        if d[:2] != b'\xff\xd8' or d[-2:] != b'\xff\xd9':
            return
        with LOCK:
            FRAME[name] = d
            STAMP[name] = time.time()


def spin():
    rclpy.init()
    n = G()
    while True:
        try:
            rclpy.spin_once(n, timeout_sec=0.2)
        except Exception:
            time.sleep(0.2)


ALIAS = {'8': 'head', 'head': 'head', '0': 'bev', '6': 'bev', 'bev': 'bev',
         'bev1': 'bev', 'bev2': 'bev', 'wa': 'chest', 'wb': 'chest',
         'left': 'chest', 'right': 'chest', 'chest': 'chest', 'C': 'chest'}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/health':
            with LOCK:
                body = json.dumps({'ok': True,
                                   'cams': {k: len(v) for k, v in FRAME.items()},
                                   'age': {k: round(time.time() - v, 1) for k, v in STAMP.items()}})
            return self.send(body.encode(), 'application/json')
        if p == '/snap':
            d = '8'
            if 'd=' in self.path:
                d = self.path.split('d=', 1)[1].split('&')[0]
            name = ALIAS.get(d)
            with LOCK:
                img = FRAME.get(name) if name else None
            if not img:
                return self.send(json.dumps({'error': 'no frame for ' + d}).encode(),
                                 'application/json', 404)
            return self.send(img, 'image/jpeg')
        self.send(b'{"error":"not found"}', 'application/json', 404)

    def send(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == '__main__':
    threading.Thread(target=spin, daemon=True).start()
    time.sleep(3)
    port = int(os.environ.get('CAM_PORT', '8895'))
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
