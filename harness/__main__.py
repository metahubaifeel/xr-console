import argparse
from .adapters import ConsoleRobot, OpenAIVision
from .config import settings
from .engine import Harness
from .server import make_server


def main():
    p = argparse.ArgumentParser(description='XR Visual Execution Monitor Harness')
    p.add_argument('--mode', choices=['live'], default='live')
    p.add_argument('--port', type=int, default=8910)
    args = p.parse_args()
    c = settings()
    if not c['openai_key']:
        raise SystemExit('OPENAI_API_KEY is required')
    robot = ConsoleRobot(c)
    vision = OpenAIVision(c)
    engine = Harness(c, robot, vision)
    server = make_server(engine, port=args.port)
    print(f'Harness {args.mode}: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.close()

if __name__ == '__main__':
    main()
