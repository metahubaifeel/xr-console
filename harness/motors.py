"""Public degree-based functions and asynchronous motor-job management."""
import copy
import json
import threading
import time
import uuid
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor


class MotorJobs:
    def __init__(self,robot):
        self.robot=robot;self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='motor-control')
        self.job={'status':'idle','command_id':None}
    def busy(self):
        with self.lock:return self.job['status'] in ('queued','running')
    def state(self):
        with self.lock:return copy.deepcopy(self.job)
    def submit(self,payload):
        with self.lock:
            if self.busy():raise ValueError('another motor command is active')
            command_id=uuid.uuid4().hex
            self.job={'status':'queued','command_id':command_id,'request':dict(payload),'created_at':time.time(),'cancel_requested':False}
            self.future=self.pool.submit(self._run,{**payload,'command_id':command_id})
            return self.state()
    def _run(self,payload):
        with self.lock:
            if self.job['cancel_requested']:
                self.job['status']='cancelled';return
            self.job['status']='running'
        try:
            result=self.robot.motor('move',payload)
            with self.lock:self.job.update(status='cancelled' if result.get('cancelled') else 'completed' if result.get('reached') else 'not_reached',result=result,finished_at=time.time())
        except Exception as exc:
            with self.lock:self.job.update(status='failed',error=str(exc)[:500],finished_at=time.time())
    def cancel(self,command_id):
        with self.lock:
            if command_id!=self.job['command_id']:raise ValueError('motor command_id mismatch')
            if not self.busy():return self.state()
            self.job['cancel_requested']=True
        self.robot.motor('cancel',{'command_id':command_id})
        return self.state()
    def close(self):
        if self.busy():
            try:self.cancel(self.state()['command_id'])
            except Exception:pass
        self.pool.shutdown(wait=True,cancel_futures=True)


def _request(path,payload=None,base_url='http://127.0.0.1:8910'):
    data=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(base_url+path,data=data,headers={'Content-Type':'application/json'})
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req,timeout=15) as r:return json.load(r)
    except urllib.error.HTTPError as exc:
        try:message=json.load(exc).get('error',str(exc))
        except (ValueError,AttributeError):message=str(exc)
        raise RuntimeError(f'{exc.code}: {message}') from None


def get_motor_angles(*,base_url='http://127.0.0.1:8910'):
    return _request('/api/motors',base_url=base_url)


def plan_joint_degrees(motor,degrees,*,relative=False,speed_deg_s=5,tolerance_deg=.5,base_url='http://127.0.0.1:8910'):
    return _request('/api/motor/plan',dict(motor=motor,degrees=degrees,relative=relative,speed_deg_s=speed_deg_s,tolerance_deg=tolerance_deg),base_url)


def set_joint_degrees(motor,degrees,*,relative=False,speed_deg_s=5,tolerance_deg=.5,base_url='http://127.0.0.1:8910'):
    """Submit a REAL motor move. Returns a command_id, not a completion claim."""
    return _request('/api/motor/move',dict(motor=motor,degrees=degrees,relative=relative,speed_deg_s=speed_deg_s,tolerance_deg=tolerance_deg),base_url)


def rotate_joint_degrees(motor,delta_degrees,**kwargs):
    return set_joint_degrees(motor,delta_degrees,relative=True,**kwargs)


def set_gripper_degrees(side,degrees,**kwargs):
    if side not in ('left','right'):raise ValueError('side must be left or right')
    return set_joint_degrees(side+'_arm_gripper',degrees,**kwargs)


def motor_status(*,base_url='http://127.0.0.1:8910'):
    return _request('/api/motor/status',base_url=base_url)


def stop_motor(command_id,*,base_url='http://127.0.0.1:8910'):
    return _request('/api/motor/cancel',{'command_id':command_id},base_url)
