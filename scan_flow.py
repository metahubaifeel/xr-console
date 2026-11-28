"""Single-round scanner orchestration; hardware callbacks are injected."""
import threading
import time


class ScanFlow:
    def __init__(self, run, stop, scan, rules, *, harness_ready=None, place=None, grasp_check=None):
        """run: right-locked harness; stop: stop-only, never home.

        harness_ready must verify the client route AND active lock mode.
        place, when supplied, owns returning the scanner and switching arms
        before dispatching the bottle instruction. It must honor cancel.
        """
        self.run, self.stop, self.scan, self.rules = run, stop, scan, rules
        self.harness_ready, self.place = harness_ready, place
        self.grasp_check = grasp_check
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
            if not dry and (not self.harness_ready or not self.harness_ready()):
                raise ValueError("真机流程尚未确认右臂锁定及 harness 通道；请先接入锁臂回调")
            rules = dict(self.rules().get("map", {}))
            for instruction in rules.values():
                self._validate_instruction(instruction)
            durations = [float(options.get(k, v)) for k, v in
                         (("grasp_seconds", 60), ("scan_seconds", 40), ("place_seconds", 30))]
            if not all(1 <= v <= 120 for v in durations):
                raise ValueError("各阶段时限必须为 1–120 秒")
            self.cancel.clear()
            self.data = {"stage": "starting", "dry_run": dry, "scan_only": bool(options.get("scan_only", False)), "code": None, "events": []}
            self.worker = threading.Thread(target=self._work, args=(dry, durations, rules), daemon=True)
            self.worker.start()

    def abort(self):
        self.cancel.set()

    def _command(self, dry, instruction):
        self._validate_instruction(instruction)
        if self.cancel.is_set():
            raise InterruptedError()
        self.event(self.data["stage"], instruction)
        if not dry:
            self.run(instruction)

    @staticmethod
    def _validate_instruction(instruction):
        # Conservative application policy based on this team's hardware trials;
        # not a claim about the model's universal maximum instruction length.
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("动作指令不能为空")

    def _wait(self, seconds):
        if self.cancel.wait(seconds):
            raise InterruptedError()

    def _work(self, dry, durations, rules):
        try:
            self.event("grasp", "抓枪：按时限切步，尚无视觉完成判定")
            self._command(dry, "grasp the black handle with the fingers aligned along it")
            self._wait(0.05 if dry else durations[0])
            if not dry and (self.grasp_check is None or self.grasp_check() is not True):
                self.event("grasp_unverified", "尚未确认扫码枪已抓稳；停止本轮，不下发对准或转腕")
                return
            # Establish baseline before issuing alignment: fast scans must not be lost.
            baseline = time.time()
            self.event("scan", "对准并等待本轮新码")
            self._command(dry, "point the scanner at the bottle")
            deadline = time.monotonic() + durations[1]
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
            if self.data.get("scan_only"):
                self.event("scan_received", "模拟读码完成" if dry else "收到本轮新码；两步测试结束，只停止推理")
                return
            if not dry and self.place is None:
                self.event("awaiting_handoff", "收到 " + code + "；等待放回枪及切换锁臂，不自动搬瓶")
                return
            self.event("place", "收到 " + code + "，执行对应放置指令")
            if dry:
                self._command(True, rules[code])
            else:
                if self.cancel.is_set():
                    raise InterruptedError()
                self.place(rules[code], self.cancel)
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
