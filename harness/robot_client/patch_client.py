"""Apply two opt-in hooks to the inspected sync EX001 client; never start it."""
import ast
import hashlib
from pathlib import Path
import shutil

MARKER = '# XR_HARNESS_HOLD_V1'

def patch(source):
    source = remove_progress(source)
    if source.count(MARKER) == 2:
        return source
    if MARKER in source:
        raise ValueError('partial client patch: restore backup first')
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'EX001Client')
    decode = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_decode_arm_actions')
    control = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_execute_control_loop')
    lines = source.splitlines(keepends=True)
    returns = [n for n in ast.walk(decode) if isinstance(n, ast.Return)]
    if len(returns) != 1 or ast.unparse(returns[0].value) != '(arm1_actions, arm2_actions)':
        raise ValueError('unsupported decode shape; refusing heuristic patch')
    at_decode = returns[0].lineno - 1
    doc = control.body[0]
    if not isinstance(doc, ast.Expr) or not isinstance(doc.value, ast.Constant) or not isinstance(doc.value.value, str):
        raise ValueError('unsupported control method shape')
    hook = '''        # XR_HARNESS_HOLD_V1
        from .xr_hold_runtime import apply_client_hold
        arm1_actions, arm2_actions = apply_client_hold(
            self, arm1_actions, arm2_actions, layout="{layout}")
'''
    for pos, layout in sorted([(at_decode,'decoded'),(doc.end_lineno,'dispatch')],reverse=True):
        lines.insert(pos,hook.format(layout=layout))
    result = ''.join(lines)
    compile(result,'ex001_client.py','exec')
    return result


def remove_progress(source):
    if '# XR_HARNESS_PROGRESS_V1' not in source:
        return source
    tree=ast.parse(source)
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='EX001Client')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_execute_sync_cpp_dispatch')
    lines=source.splitlines(keepends=True)
    if lines[method.lineno-2].strip()!='# XR_HARNESS_PROGRESS_V1':
        raise ValueError('unexpected progress hook layout')
    del lines[method.lineno-2:method.end_lineno]
    result=''.join(lines).replace('        from .xr_hold_runtime import record_joint_frame\n        record_joint_frame(self, i)\n','')
    compile(result,'ex001_client.py','exec')
    return result


def install(module_dir, runtime):
    target = Path(module_dir) / 'ex001_client.py'
    old = target.read_text()
    new = patch(old)
    if old != new:
        digest = hashlib.sha256(old.encode()).hexdigest()[:12]
        backup = target.with_name('ex001_client.before-xr-hold-' + digest + '.py')
        if not backup.exists():shutil.copy2(target,backup)
    # Runtime first: no window where the hooks point at a nonexistent module.
    shutil.copy2(runtime,Path(module_dir)/'xr_hold_runtime.py')
    if old != new:
        temp=target.with_suffix('.xr-hold-tmp')
        temp.write_text(new);temp.chmod(target.stat().st_mode);temp.replace(target)
    return {'installed':True,'hooks':new.count(MARKER),'source_sha256':hashlib.sha256(new.encode()).hexdigest()}

if __name__=='__main__':
    import json,sys
    print(json.dumps(install(sys.argv[1],sys.argv[2])))
