import rclpy, os, time
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import CompressedImage
TOPS = [
  ('/camera_head_front/color/image_raw/compressed', '/tmp/hd_head.jpg'),
  ('/camera_chest_front/color/image_raw/compressed',   '/tmp/hd_left.jpg'),
  ('/camera_chassis_front/color/image_raw/compressed', '/tmp/hd_right.jpg'),
]
class G(Node):
    def __init__(self):
        super().__init__('hd_grab')
        q = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST)
        self.seen = {}
        for t, f in TOPS:
            self.create_subscription(CompressedImage, t, lambda m, ff=f: self.on(ff, m), q)
    def on(self, f, m):
        d = bytes(m.data)
        if d[:2] != b'\xff\xd8': return
        try:
            open(f, 'wb').write(d); self.seen[f] = len(d)
        except Exception: pass
rclpy.init(); n = G(); t0 = time.time()
while time.time() - t0 < 5: rclpy.spin_once(n, timeout_sec=0.3)
n.destroy_node(); rclpy.shutdown()
print(' '.join(f'{os.path.basename(k)}={v}' for k, v in n.seen.items()) or 'NONE')
