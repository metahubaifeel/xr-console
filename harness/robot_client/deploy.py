"""Deploy opt-in sync-client hooks through existing SSH. No inference startup."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile


def deploy(host='10.230.20.13', user='xr', control='/tmp/xr-console-ssh.sock'):
    src=Path(__file__).resolve().parent
    ssh=['ssh','-S',control,'-o','BatchMode=yes','-o','ConnectTimeout=8',f'{user}@{host}']
    # Verify account access before uploading. Never stop an active inference automatically.
    guard="docker exec ex001_client-robot_client-1 bash -lc 'if pgrep -f \"start_ex001[.]py\" >/dev/null; then echo active_inference_refusing_deploy; exit 1; fi; mkdir -p /tmp/xr-harness; chmod 700 /tmp/xr-harness'"
    subprocess.run(ssh+[guard],check=True)
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as tar:
        for name in ['hold_runtime.py','bridge.py','patch_client.py','update_instruction.py','motor_control.py']:
            tar.add(src/name,arcname=name)
    subprocess.run(ssh+['docker exec -i ex001_client-robot_client-1 tar -xf - -C /tmp/xr-harness'],input=stream.getvalue(),check=True)
    command=('docker exec ex001_client-robot_client-1 python3 /tmp/xr-harness/patch_client.py '
             '/opt/xr/bot/x2robot_client/lib/python3.12/site-packages/x2robot_client '
             '/tmp/xr-harness/hold_runtime.py')
    result=subprocess.run(ssh+[command],capture_output=True,text=True,check=True)
    print(result.stdout.strip())
    result=subprocess.run(ssh+['docker exec ex001_client-robot_client-1 python3 /tmp/xr-harness/bridge.py capabilities'],capture_output=True,text=True,check=True)
    print(result.stdout.strip())

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--host',default='10.230.20.13');p.add_argument('--user',default='xr');p.add_argument('--control',default='/tmp/xr-console-ssh.sock')
    a=p.parse_args();deploy(a.host,a.user,a.control)
