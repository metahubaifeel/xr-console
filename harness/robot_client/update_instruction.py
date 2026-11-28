"""Use the native UPDATE_INSTRUCTION service; no INIT/STOP/START commands."""
import json
import sys
import time
import uuid


def update(payload):
    import rclpy
    from protocol.srv import InferenceControl
    from protocol.msg import InstructionUpdateResult
    from hold_runtime import read, DEFAULT_PATH, atomic_write
    from pathlib import Path
    session=payload.get('session_id')
    text=payload.get('instruction')
    if not isinstance(text,str) or not text.strip() or len(text)>6000:
        raise ValueError('invalid instruction')
    if not isinstance(session,str) or read(DEFAULT_PATH)['session_id']!=session:
        raise ValueError('instruction/session mismatch')
    request_id=uuid.uuid4().hex
    rclpy.init()
    node=rclpy.create_node('xr_harness_instruction_'+request_id[:8])
    received=[]
    def on_result(msg):
        if msg.instruction_request_id==request_id and msg.episode_id==session:
            received.append({'status':int(msg.status),'effective_timestamp_ns':int(msg.effective_timestamp_ns)})
    subscription=node.create_subscription(InstructionUpdateResult,'/x2robot_infer/instruction_update_result',on_result,10)
    client=node.create_client(InferenceControl,'/x2robot_infer/inference_control')
    try:
        if not client.wait_for_service(timeout_sec=2):
            raise RuntimeError('instruction update service unavailable')
        request=InferenceControl.Request()
        request.infer_cmd=InferenceControl.Request.UPDATE_INSTRUCTION
        request.model_info_json=json.dumps({'instruction':text,'instruction_request_id':request_id,
            'episode_id':session,'case_id':session+'-step'+str(payload.get('step',2)),'source':'xr_harness_vlm'})
        future=client.call_async(request)
        rclpy.spin_until_future_complete(node,future,timeout_sec=3)
        if not future.done() or future.result() is None or not future.result().success:
            raise RuntimeError('native client rejected or timed out on instruction update')
        deadline=time.monotonic()+2
        while not received and time.monotonic()<deadline:
            rclpy.spin_once(node,timeout_sec=.1)
        effective=bool(received and received[-1]['status']==InstructionUpdateResult.STATUS_EFFECTIVE)
        result={'accepted':True,'effective':effective,'request_id':request_id,
                'session_id':session,'instruction':text,'updated_at':time.time(),'result':received[-1] if received else None}
        if received and not effective:
            result.update(accepted=False,error='native_update_failed',message='new instruction did not become effective')
        atomic_write(Path(DEFAULT_PATH).with_name('instruction_update.json'),result)
        return result
    finally:
        node.destroy_node();rclpy.shutdown()

if __name__=='__main__':
    if sys.argv[1]=='--check':
        from protocol.srv import InferenceControl
        from protocol.msg import InstructionUpdateResult
        print(json.dumps({'native_update_command':int(InferenceControl.Request.UPDATE_INSTRUCTION),
                          'result_fields':list(InstructionUpdateResult.get_fields_and_field_types())}))
    else:
        try:print(json.dumps(update(json.loads(sys.argv[1])),ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'error':type(exc).__name__,'message':str(exc)}));sys.exit(1)
