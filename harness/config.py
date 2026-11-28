import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def settings():
    env = {}
    path = ROOT.parent / 'harness.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    env.update(os.environ)
    config = json.loads((ROOT / 'config.json').read_text())
    config['model'] = env.get('OPENAI_VISION_MODEL', 'gpt-6.1-terra')
    config['openai_key'] = env.get('OPENAI_API_KEY', '')
    config['console_url'] = env.get('XR_CONSOLE_URL', 'http://127.0.0.1:8900').rstrip('/')
    config['control_url'] = env.get('XR_CONTROL_URL', '').rstrip('/')
    return config

def prompt(name, **values):
    text = (ROOT / 'prompts' / name).read_text()
    return text.format(**values) if values else text


def step_prompts(step, **values):
    data = json.loads((ROOT / 'prompts' / f'step_{step}.json').read_text())
    if step == 2:
        arm = values.get('scanner_arm')
        if arm not in ('left','right'):
            raise ValueError('Step2 requires the scanner holding hand')
        values.update(opening_direction='left' if arm=='right' else 'right',
                      opening_direction_zh='左' if arm=='right' else '右')
    return {key: data[key].format(**values) if data[key] is not None else None
            for key in ('execution_prompt', 'verification_prompt')}


def step2_update_prompt(next_action, scanner_arm):
    import re
    if scanner_arm not in ('left','right'):
        raise ValueError('Step2 requires a known reference hand')
    text=next_action.strip()
    for word in ('条码扫描器','条码扫码枪','扫码枪','扫描枪','扫描器'):
        text=text.replace(word,'黑色T字形物体')
    text=re.sub(r'\b(?:barcode\s+scanner|barcode\s+reader|scanner)\b','black T-shaped object',text,flags=re.I)
    return prompt('step_2_update.txt',next_action=text,scanner_arm=scanner_arm,
                  opening_direction='left' if scanner_arm=='right' else 'right').strip(),text


def step7_release_prompts():
    data=json.loads((ROOT/'prompts'/'step_7.json').read_text())
    return {'execution_prompt':data['release_execution_prompt'],
            'verification_prompt':data['release_verification_prompt']}


def step3_left_test_prompts():
    data=json.loads((ROOT/'prompts'/'step_3_left_test.json').read_text())
    return {key:data[key] for key in ('execution_prompt','verification_prompt')}
