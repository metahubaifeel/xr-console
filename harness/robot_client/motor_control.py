"""EX001 direct single-motor positioning, using the installed SDK and application modes.

catalog/plan only read feedback. move is the only operation that enables hardware
and publishes targets. Angles use degrees externally and SDK radians internally.
"""
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid
if __package__:
    from .hold_runtime import atomic_write, process_live, read, DEFAULT_PATH
else:
    from hold_runtime import atomic_write, process_live, read, DEFAULT_PATH

ROOT=Path(DEFAULT_PATH).parent


def finite(value,name):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ValueError(name+' must be finite numeric degrees')
    return float(value)


def plan_move(motors, payload, grippers=None):
    name=payload.get('motor')
    if name not in motors:
        raise ValueError('unknown motor')
    degrees=finite(payload.get('degrees'),'degrees')
    relative=payload.get('relative',False)
    if type(relative) is not bool:raise ValueError('relative must be boolean')
    speed=finite(payload.get('speed_deg_s',5),'speed_deg_s')
    tolerance=finite(payload.get('tolerance_deg',.5),'tolerance_deg')
    if not .1<=speed<=15:raise ValueError('speed_deg_s must be between 0.1 and 15')
    if not .01<=tolerance<=2:raise ValueError('tolerance_deg must be between 0.01 and 2')
    current=motors[name]['position_rad']
    target=current+math.radians(degrees) if relative else math.radians(degrees)
    lo,hi=motors[name]['limits_rad']
    if not lo<=target<=hi:raise ValueError(f'target outside motor limits: {math.degrees(lo):.3f}..{math.degrees(hi):.3f} degrees')
    baseline={n:m['position_rad'] for n,m in motors.items()}
    for arm,lock in (grippers or {}).items():
        key=arm+'_arm_gripper'
        if key==name and abs(target-lock['target'])>1e-8:
            raise ValueError('scanner gripper is locked; wait for VLM drop confirmation or final placement release')
        if key in baseline:baseline[key]=lock['target']
    targets=dict(baseline);targets[name]=target
    # Quintic interpolation has a peak speed 1.875x its average.
    duration=max(.2,1.875*abs(math.degrees(target-current))/speed)
    if duration>30:raise ValueError('move exceeds 30 seconds; choose a smaller angle or higher speed')
    return {'motor':name,'relative':relative,'requested_deg':degrees,
            'start_deg':math.degrees(current),'target_deg':math.degrees(target),
            'delta_deg':math.degrees(target-current),'duration_s':duration,'speed_deg_s':speed,
            'tolerance_deg':tolerance,'baseline_rad':baseline,'targets_rad':targets}


def interpolation(plan, fraction):
    x=max(0.,min(1.,fraction));weight=x*x*x*(10+x*(-15+6*x))
    values=dict(plan['baseline_rad']);name=plan['motor']
    values[name]=math.radians(plan['start_deg'])+(plan['targets_rad'][name]-math.radians(plan['start_deg']))*weight
    return values


def active_inference():
    for p in Path('/proc').glob('[0-9]*'):
        try:
            if any(a.endswith(b'/start_ex001.py') for a in (p/'cmdline').read_bytes().split(b'\0')):return True
        except OSError:pass
    return False


def active_motor():
    p=ROOT/'motor_active.json'
    try:
        d=json.loads(p.read_text())
        return d if process_live(d) else None
    except (OSError,ValueError):return None


def cancel(command_id):
    if not isinstance(command_id,str) or not command_id:raise ValueError('command_id required')
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    atomic_write(ROOT/'motor_cancel.json',{'command_id':command_id,'requested_at':time.time()})
    return {'requested':True,'command_id':command_id}


def cancelled(command_id):
    try:return json.loads((ROOT/'motor_cancel.json').read_text()).get('command_id')==command_id
    except (OSError,ValueError):return False


class SDKMotors:
    def __init__(self):
        import rclpy
        from x2robot_client.robot_io import create_robot_io
        self.rclpy=rclpy;rclpy.init();self.node=rclpy.create_node('xr_motor_'+uuid.uuid4().hex[:8])
        self.io=create_robot_io('ex001',node=self.node)
        self.started=time.monotonic();self.feedback_sequence=None

    def spin(self,seconds=.02):self.rclpy.spin_once(self.node,timeout_sec=seconds)

    def feedback(self,timeout=3):
        deadline=time.monotonic()+timeout
        while True:
            result={}
            for side in ('left','right'):
                arm=getattr(self.io,side+'_arm');values=arm.get_state_joints_pos();limits=arm.get_state_limit()
                if values is not None:
                    for i,value in enumerate(values):
                        result[f'{side}_arm_joint{i+1}']={'position_rad':float(value),'limits_rad':[limits['joints_lower'][i],limits['joints_upper'][i]],'kind':'arm','side':side}
                grip=getattr(self.io,side+'_ee').get_state()
                if grip is not None:
                    result[side+'_arm_gripper']={'position_rad':float(grip),'limits_rad':list(self.io.profile.gripper),'kind':'gripper','side':side}
            if self.io.head is not None:
                values=self.io.head.get_state();limits=self.io.head.get_state_limit()
                if values is not None:
                    for i,axis in enumerate(('pitch','yaw')):
                        result['head_'+axis+'_joint']={'position_rad':float(values[i]),'limits_rad':limits[axis],'kind':'head'}
            sample=self.io._latest_sample('left_arm/joints')
            sequence=sample[0] if sample is not None else None
            if len(result)==16 and sequence is not None and sequence!=self.feedback_sequence and all(math.isfinite(v['position_rad']) for v in result.values()):
                self.feedback_sequence=sequence
                return result
            if time.monotonic()>deadline:raise RuntimeError('fresh motor feedback missing')
            self.spin()

    def call(self,kind,path,request,timeout=5):
        client=self.node.create_client(kind,path)
        try:
            if not client.wait_for_service(timeout_sec=2):raise RuntimeError('controller service unavailable: '+path)
            future=client.call_async(request);self.rclpy.spin_until_future_complete(self.node,future,timeout_sec=timeout)
            if not future.done() or future.result() is None:raise RuntimeError('controller service timed out')
            return future.result()
        finally:self.node.destroy_client(client)

    def enable_joint_control(self):
        from protocol.srv import HardwareEnabled,SwitchControllerMode
        from protocol.msg import ModeControllerEnabledStatus,EnumControllerMode
        status=[]
        sub=self.node.create_subscription(ModeControllerEnabledStatus,'/realtime_controller_manager/mode_controller_enabled_status',lambda m:status.append((int(m.mode),bool(m.ready))),10)
        try:
            req=HardwareEnabled.Request();req.enabled=True
            if not self.call(HardwareEnabled,'/realtime_controller_manager/hardware_enabled',req).res:raise RuntimeError('hardware enable rejected')
            status.clear();req=SwitchControllerMode.Request();req.mode=EnumControllerMode.CONTROLLER_MODE_JOINT_INFERENCE
            if self.call(SwitchControllerMode,'/realtime_controller_manager/switch_controller_mode',req).status!=1:raise RuntimeError('joint controller mode rejected')
            deadline=time.monotonic()+5
            while not any(mode==4 and ready for mode,ready in status):
                if time.monotonic()>deadline:raise RuntimeError('joint controller did not become ready')
                self.spin()
            self.io.left_arm.set_mode('joints_pos');self.io.right_arm.set_mode('joints_pos')
        finally:self.node.destroy_subscription(sub)

    def publish(self,targets):
        from x2robot_client.robot_io import CODE_REJECTED
        if not self.io.lock.write_allowed():raise RuntimeError('robot control lock is held by another process')
        for side in ('left','right'):
            values=[targets[f'{side}_arm_joint{i}'] for i in range(1,7)]
            if getattr(self.io,side+'_arm').send_cmd_joints_pos(values)==CODE_REJECTED:raise RuntimeError('joint command rejected')
            if getattr(self.io,side+'_ee').send_cmd(targets[side+'_arm_gripper'])==CODE_REJECTED:raise RuntimeError('gripper command rejected')
        if self.io.head.send_cmd([targets['head_pitch_joint'],targets['head_yaw_joint']])==CODE_REJECTED:raise RuntimeError('head command rejected')

    def close(self):
        self.io.destroy();self.node.destroy_node();self.rclpy.shutdown()


def command(operation,payload):
    if operation=='cancel':return cancel(payload['command_id'])
    if operation=='status':return {'active':active_motor()}
    sdk=SDKMotors()
    try:
        motors=sdk.feedback()
        if operation=='catalog':
            return {'motors':{n:{**m,'position_deg':math.degrees(m['position_rad']),'limits_deg':[math.degrees(x) for x in m['limits_rad']]} for n,m in motors.items()},
                    'gripper_angle':'motor joint angle, not geometric finger opening angle'}
        grippers=read(DEFAULT_PATH).get('grippers',{}) if Path(DEFAULT_PATH).exists() else {}
        plan=plan_move(motors,payload,grippers)
        if operation=='plan':return plan
        if operation!='move':raise ValueError('unknown motor operation')
        command_id=payload.get('command_id')
        if not isinstance(command_id,str) or not command_id:raise ValueError('command_id required')
        ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
        with open(ROOT/'motor.lock','a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('another motor command is active') from None
            if active_inference():raise RuntimeError('stop VLA inference before direct motor control')
            # Let SDK lock-heartbeat subscriptions receive the current owner.
            while time.monotonic()-sdk.started<1.0:sdk.spin()
            if not sdk.io.lock.write_allowed():raise RuntimeError('robot control lock is held elsewhere')
            record={'command_id':command_id,'client_pid':os.getpid(),'client_start_ticks':Path('/proc/self/stat').read_text().split(') ',1)[1].split()[19]}
            atomic_write(ROOT/'motor_active.json',record)
            try:
                if cancelled(command_id):return {'cancelled':True,'plan':plan}
                sdk.enable_joint_control()
                # Rebase relative rotations after mode switch; modes can change while planning.
                fresh=sdk.feedback();plan=plan_move(fresh,payload,grippers)
                started=time.monotonic();last=plan['baseline_rad'];done=False;last_feedback=started
                while True:
                    if cancelled(command_id):break
                    if active_inference():raise RuntimeError('VLA started during direct motor control')
                    if time.monotonic()-last_feedback>.1:
                        sdk.feedback(timeout=.3);last_feedback=time.monotonic()
                    fraction=(time.monotonic()-started)/plan['duration_s']
                    last=interpolation(plan,fraction);sdk.publish(last);sdk.spin(.02)
                    if fraction>=1:done=True;break
                deadline=time.monotonic()+3;measured=sdk.feedback()[plan['motor']]['position_rad']
                while done and abs(math.degrees(measured)-plan['target_deg'])>plan['tolerance_deg'] and time.monotonic()<deadline:
                    if cancelled(command_id):done=False;break
                    sdk.publish(last);sdk.spin(.02);measured=sdk.feedback()[plan['motor']]['position_rad']
                error=math.degrees(measured)-plan['target_deg']
                return {'command_id':command_id,'plan':plan,'cancelled':not done,'reached':done and abs(error)<=plan['tolerance_deg'],
                        'measured_deg':math.degrees(measured),'error_deg':error,'controller_mode':4}
            finally:
                (ROOT/'motor_active.json').unlink(missing_ok=True)
    finally:sdk.close()

if __name__=='__main__':
    try:print(json.dumps(command(sys.argv[1],json.loads(sys.argv[2]) if len(sys.argv)>2 else {}),allow_nan=False))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'message':str(exc)}))
