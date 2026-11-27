"""Single-round scanner orchestration; hardware callbacks are injected."""
import threading
import time


class ScanFlow:
    def __init__(self, run, stop, scan, rules):
        self.run, self.stop, self.scan, self.rules = run, stop, scan, rules
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.worker = None
        self.data = {"stage": "idle", "events": []}

    def state(self):
        with self.lock:
            return {**self.data, "events": list(self.data["events"])}

    def event(self, stage, message):
        with self.lock:
            self.data["stage"] = stage
            self.data["events"].append({"ts": time.time(), "message": message})
            self.data["events"] = self.data["events"][-60:]

    def active(self):
        return bool(self.worker and self.worker.is_alive())

    def start(self, options):
        with self.lock:
            if self.active():
                raise ValueError("已有流程运行")
            dry = options.get("dry_run", True)
            if not isinstance(dry, bool):
                raise ValueError("dry_run 必须为布尔值")
            durations = [float(options.get(k, v)) for k, v in
                         (("grasp_seconds", 60), ("scan_seconds", 40), ("place_seconds", 30))]
            if not all(1 <= v <= 120 for v in durations):
                raise ValueError("各阶段时限必须为 1–120 秒")
            self.cancel.clear()
            self.data = {"stage": "starting", "dry_run": dry, "code": None, "events": []}
            self.worker = threading.Thread(target=self._work, args=(dry, durations), daemon=True)
            self.worker.start()

    def abort(self):
        self.cancel.set()

    def _command(self, dry, instruction):
        if self.cancel.is_set():
            raise InterruptedError()
        self.event(self.data["stage"], instruction)
        if not dry:
            self.run(instruction)

    def _wait(self, seconds):
        if self.cancel.wait(seconds):
            raise InterruptedError()

    def _work(self, dry, durations):
        try:
            self.event("grasp", "抓枪：按时限切步，尚无视觉完成判定")
            self._command(dry, "pick up the standing black object on the left and hold it")
            self._wait(0.05 if dry else durations[0])
            # Establish baseline before issuing alignment: fast scans must not be lost.
            baseline = time.time()
            self.event("scan", "对准并等待本轮新码")
            self._command(dry, "hold the black object with its front facing the label on the bottle")
            deadline = time.monotonic() + durations[1]
            rules = self.rules().get("map", {})
            while True:
                self._wait(0.05 if dry else 0.2)
                sample = {"ts": time.time(), "code": "OUTC"} if dry else self.scan()
                if sample and float(sample.get("ts", 0)) >= baseline:
                    code = str(sample.get("code", "")).strip().upper()
                    if code in rules:
                        break
                if time.monotonic() >= deadline:
                    raise TimeoutError("没有收到本轮已配置的条码，流程终止")
            with self.lock:
                self.data["code"] = code
            self.event("place", "收到 " + code + "，执行对应放置指令")
            self._command(dry, rules[code])
            self._wait(0.05 if dry else durations[2])
            self.event("finished_unverified", "演练完成" if dry else "指令流程结束；实物放置结果待确认")
        except InterruptedError:
            self.event("cancelled", "流程已取消")
        except Exception as exc:
            self.event("error", str(exc))
        finally:
            if not dry:
                try:
                    self.stop()
                except Exception as exc:
                    self.event("error", "停止会话失败：" + str(exc))
