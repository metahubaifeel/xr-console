"""Exercise installed hook methods with synthetic arrays only. No ROS/client init."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import numpy as np

base=Path('/opt/xr/bot/x2robot_client/lib/python3.12/site-packages/x2robot_client')
source=(base/'ex001_client.py').read_text()
cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='EX001Client')
methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('_decode_arm_actions','_execute_control_loop')]
assert len(methods)==2 and source.count('# XR_HARNESS_HOLD_V1')==2
package=types.ModuleType('xr_hold_verification');package.__path__=[];sys.modules[package.__name__]=package
spec=importlib.util.spec_from_file_location(package.__name__+'.xr_hold_runtime',base/'xr_hold_runtime.py')
rt=importlib.util.module_from_spec(spec);sys.modules[spec.name]=rt;spec.loader.exec_module(rt)
node=ast.ClassDef(name='SyntheticOwner',bases=[],keywords=[],body=methods,decorator_list=[])
module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
ns={'__package__':package.__name__,'np':np};exec(compile(ast.fix_missing_locations(module),'<installed-hook-test>','exec'),ns)
owner=ns['SyntheticOwner']();owner.control_mode='end_pose'
feedback={'state':{'follow1_joints':np.arange(7,dtype=float)/10,'follow2_joints':np.arange(7,dtype=float)/20,
                   'follow1_pos':np.array([.1,.2,.3,.1,.2,.3,.04]),'follow2_pos':np.array([.3,.2,.1,.3,.2,.1,.06])}}
owner._collect_state_snapshot=lambda:feedback
owner._decode_split_end_pose_actions=lambda outputs,side:outputs[side]
owner._decode_ndarray=lambda x:x
captured=[];owner._execute_cpp_dispatch_loop=lambda *args,**kw:captured.append(args)
with tempfile.TemporaryDirectory(prefix='xr-hold-synthetic-') as temp:
 path=Path(temp)/'hold_state.json';rt.configure(path,'synthetic-test',['left'])
 os.environ['XR_HARNESS_HOLD_FILE']=str(path);os.environ['XR_HARNESS_SESSION_ID']='synthetic-test'
 left=np.arange(21,dtype=float).reshape(3,7);right=left+100
 a,b=owner._decode_arm_actions({'follow_left':left,'follow_right':right})
 np.testing.assert_array_equal(a,np.tile(feedback['state']['follow1_pos'],(3,1)));assert b is right
 final_left=np.ones((6,8));final_right=np.ones((6,8))*2
 owner._execute_control_loop(6,final_left,final_right,None,None,None)
 dispatched=captured[0]
 assert dispatched[2] is final_right
 np.testing.assert_array_equal(dispatched[1],np.tile(dispatched[1][0],(6,1)))
 assert np.isclose(np.linalg.norm(dispatched[1][0,3:7]),1)
 d=rt.read(path);assert d['state']=='latched' and d['last_layout']=='dispatch'
 assert d['overlaid_chunks']==1 and rt.process_live(d)
 try:rt.configure(path,'synthetic-test',['right'])
 except RuntimeError:pass
 else:raise AssertionError('active-client policy mutation accepted')
 os.environ.pop('XR_HARNESS_HOLD_FILE');os.environ.pop('XR_HARNESS_SESSION_ID')
 a,b=owner._decode_arm_actions({'follow_left':left,'follow_right':right});assert a is left and b is right
print(json.dumps({'passed':True,'installed_hooks_exercised':2,'other_arm_unchanged':True,'opt_in_only':True,'robot_commands_sent':0}))
