# XR Harness

真实机器人、OpenAI视觉API与WALL-X VLA后端。启动：

```bash
./start-harness.sh
```

访问 http://127.0.0.1:8910 。生产入口仅支持live模式。启动服务和打开页面只提供预览；点击执行按钮才启动机器人动作。

## 单步测试

选择Step后点击“执行所选步骤”，观察五路实时镜头。执行期间每隔2.5秒自动采集三路图像并调用VLM。未完成或证据不足时继续执行，成功时停止本次测试，不启动下一步。一次检测未返回时不叠加请求。也可点击“结束执行并检测”手动结束动作并检测一次。

- Step1：直接执行。
- Step2：按上一步held_objects.scanner确定扫描窗口方向：右手持枪向左，左手持枪向右；执行和检测共用该方向。独立测试缺少持物结果时先识别持物手臂。
- Step3/4/6：先用一次真实VLM识别持物手臂，再按该步锁定非操作臂并执行。持物侧无法确认时停止准备，不发送动作。
- Step4：完成信号为前端扫码消息；单步模式收到后结束，完整流程收到后进入Step5映射及Step6放置。没有Step4完成检测。
- Step5：输入码值后只做固定映射，无机器人动作或模型调用。
- Step6：在下拉框选择目标颜色。
- Step7：先记录初始位姿，或使用Step1执行前自动记录的参考。

“仅检测当前画面”只调用一次真实VLM，不执行机器人动作。五路预览和状态轮询均不调用模型。VLM默认gpt-6.1-terra，reasoning.effort=none，无工具、无自动重试。

单步Step3/4/6的持物预检JSON单独显示；本步完成检测保留OpenAI原始JSON、response_id、模型名和token用量。最近完成的检测结果保存在本地`.harness_state/last_result.json`，初始参考保存在`reference.json`；均被Git忽略。模型输出只是判断结果，执行器只按固定步骤使用它。

WALL-X执行经现有8900控制台启动官方client。后端核实带本会话ID的client进程已启动；停止时核实进程已退出。进程存在仅表示client已启动，执行是否成功由画面及检测结果确认。

## 完整流程

点击“执行完整流程”从Step1开始。每个视觉步骤执行期间每隔2.5秒自动检测；成功才停止本步并下发下一步。Step4由扫码消息切步，Step5固定映射：OUTA红、OUTB白、OUTC银、ERR99/未知码黑。

检测频率由config.json中的min_check_interval控制，默认2.5秒；不依赖动作帧数或相机帧数。执行超时只停止。

## 提示词和配置

`prompts/step_1.json`等文件包含execution_prompt和verification_prompt，可随时修改，下次调用直接读取。Step4检测提示词为null，Step5无动作文件。Step6的`{target}`由后端填写。

- `vision_system.txt`：统一输出JSON格式及状态定义。
- `vision_user.txt`：上下文模板。
- `holding_system.txt`：独立步骤的持物手臂预检。
- `retry.txt`：完整流程的人工重试指令。

`config.json`管理超时、调用上限、关节容差、相机映射及颜色映射。`harness.env`保存OpenAI Key和client地址，权限600、Git忽略；视觉Key仅由本地后端发给OpenAI。原`api.env`保存WALL-X Key，由机器人client使用。配置修改后重启。

## 前端接口

默认仅监听本机8910，要求同源访问。写操作JSON；会话操作携带session_id，complete还需step。原始密钥不返回前端。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/config` | 模型和模式，是否已配置Key |
| GET | `/api/state` | 步骤、阶段、原始VLM JSON、调用数、事件 |
| GET | `/api/camera/head` | 头部实时JPEG |
| GET | `/api/camera/left`、`right`、`bev1`、`bev2` | 双腕和两路机身JPEG |
| GET | `/api/frames` | 最近一次检测的观测快照 |
| GET | `/api/hold` | 机器人动作覆盖状态 |
| GET | `/api/runtime` | 真实VLA client进程状态 |
| POST | `/api/test/start` | `{"step":1}`，单步执行；Step6可附target，Step5附code |
| POST | `/api/vision/check` | `{"step":1}`，仅检测当前画面 |
| POST | `/api/reference` | 记录初始图像与关节参考，无动作 |
| POST | `/api/session/start` | 完整流程 |
| POST | `/api/session/complete` | `{"session_id":"...","step":1}`，结束动作并检测一次 |
| POST | `/api/session/scan` | `{"session_id":"...","code":"OUTB"}`，Step4扫码消息 |
| POST | `/api/session/retry` | 完整流程中人工确认后重试当前步 |
| POST | `/api/session/stop` | 停止动作并丢弃迟到的模型返回 |

单会话最多一个VLM请求在途，旧会话或错误步骤号拒绝执行。停止预检期间也会使迟到结果失效，防止取消后又下发动作。

## 单臂保持

VLM报告held_objects（scanner/bottle分别为left、right、unknown或null）。抓瓶时保持扫码枪臂，扫描时保持瓶子臂，放瓶时保持扫码枪臂，调整扫码枪及最终归位解除保持。

`robot_client/hold_runtime.py`在官方同步EX001 client解码后和最终下发前覆盖轨迹。锁定目标取自client首次处理模型响应时的实时SDK反馈，只采样一次；被锁定侧的整段轨迹及夹爪覆盖为固定目标，另一侧保留原输出。

- joints/joints_pos：按SDK实际维度固定6或7个关节目标。
- 默认end_pose：固定末端xyz、姿态及夹爪；不保证冗余关节角完全不变。

机器人client容器的`/tmp/xr-harness/hold_state.json`记录会话、锁定侧、关节与末端快照、锁定空间、最后覆盖时间和计数。armed表示已配置，latched表示已生成覆盖目标。没有持续发指令的独立保持循环，停止推理后的实际持物效果需现场验证。

部署命令：

```bash
python3 harness/robot_client/deploy.py --host 10.230.20.13
```

复用已认证SSH连接；先检查无运行中的推理进程，再备份并安装两个opt-in hook，不启动动作。普通旧控制台会话不自动加载harness策略。容器重建后需重新部署。当前只支持同步EX001 client，异步/RTC路径未接入。

8900控制台提供capabilities、arm-lock、client-status桥接；XR_CONTROL_URL留空即可。腕部左右映射应按现场标定配置wrist_left/wrist_right。多路图像与关节来自独立请求，当前不保证严格硬件同步。

## 检查

```bash
python3 -m pip install -r harness/requirements-test.txt
python3 -m unittest discover -s harness/tests -v
python3 harness/tests/smoke_http.py
```

逻辑测试使用隔离测试替身。HTTP检查访问真实服务和五路相机，不发动作或模型请求。真实VLM连通检查用前端“仅检测当前画面”，每点击一次产生一次API调用。

## Step2检测反馈更新

Step2每完成两次检测，若仍未成功，取最新next_action作为新VLA指令，并追加原持物手和开口方向约束。扫码枪/扫描器替换为黑色T字形物体；英文scanner替换为black T-shaped object。成功后直接结束或切步，空建议跳过。

提示词模板位于`prompts/step_2_update.txt`，使用SDK原生UPDATE_INSTRUCTION服务（cmd5）热更新，保留VLA会话、WebSocket和机器人控制资源。事件中记录request_id与effective状态；accepted表示已接收，effective表示收到首轮新指令推理成功的回执。更新失败会保留当前VLA执行并记录vla_update_error。前端展示最新发送的提示词。

## 抓枪夹爪保持

VLM识别持枪侧后，保存该次观测的left_arm_gripper/right_arm_gripper关节值，作为固定夹爪目标。SDK在每批轨迹解码后及最终下发前覆盖该侧夹爪列，保留手臂轨迹；控制频率不受VLM请求阻塞。目标不会随后续反馈漂移，也不会因换步骤、独立测试或停止而自动重采样/解除。

只有scanner_dropped=true才能在任务中解除；null、unknown、证据不足或held_objects.scanner=null都不会单独解除。单独测试及完整流程共用此策略。策略保存在机器人hold_state.json的grippers字段，前端显示抓枪夹爪锁定侧。

Step7分两阶段：先闭爪放回原位；VLM确认scanner_placed=true后解除夹爪保持，热更新为松爪并复位，再检查全步骤完成。两阶段执行/检测提示词都在step_7.json。仅检测画面不会设置或解除保持，不发送动作。

ROS服务内的client通过会话启动进程与初始指令指纹匹配绑定策略；不依赖启动脚本向常驻服务传播环境变量。SDK实际关节维度支持6/7，夹爪值直接使用SDK对应关节单位。策略仅约束新下发的动作批次，不宣称已排入底层控制器的历史动作能被追溯修改。

## Step3：锁右手、左手抓瓶

前端新增独立按钮“Step3：锁右手、左手抓瓶”，对应POST `/api/test/step3-left`。后端固定右臂当前位姿和右夹爪，用左手抓稳瓶子并抬离桌面；每2.5秒检测一次，成功后结束，不进入Step4。无需先运行Step1/2，也不伪造右手持枪的视觉结果。

独立提示词在`prompts/step_3_left_test.json`。这个入口只验证左手抓瓶，普通Step3的标签朝向逻辑保留。若左夹爪仍有此前的抓枪保持策略，拒绝启动并提示处理，避免为测试清掉持枪约束。

## 单电机角度控制函数

`harness/motors.py`提供真实控制函数，面向本地8910后端。支持16个角度电机：left_arm_joint1..6、right_arm_joint1..6、left_arm_gripper、right_arm_gripper、head_pitch_joint、head_yaw_joint。升降机构是直线位移，未纳入角度接口。

```python
from harness.motors import (
    get_motor_angles, plan_joint_degrees, set_joint_degrees,
    rotate_joint_degrees, set_gripper_degrees, motor_status, stop_motor,
)

# 只读：当前角度和真实SDK限位
angles = get_motor_angles()
# 只读：检查目标、预计时长和其余电机保持值
plan = plan_joint_degrees("left_arm_joint1", 15.0)

# 以下函数会提交真实动作，返回command_id。
job = set_joint_degrees("left_arm_joint1", 15.0, speed_deg_s=5)
# 相对实际开始执行时的反馈，再转-5度
# job = rotate_joint_degrees("left_arm_joint1", -5.0)
# 夹爪电机关节角度转到30度
# job = set_gripper_degrees("left", 30.0)

result = motor_status()
# 可中断后续轨迹点的下发：
# stop_motor(job["command_id"])
```

set_joint_degrees为绝对角度，rotate_joint_degrees为相对角度。内部用SDK弧度；支持角度小数，默认速度上限5°/s、到位容差0.5°。speed_deg_s可设0.1..15，tolerance_deg可设0.01..2；单次轨迹最长30秒，使用五次平滑插值控制速度。实际执行后返回measured_deg、error_deg和reached，不把“请求已发出”当作到位。

夹爪度数为**夹爪电机关节角度**，不是两个夹指的几何夹角；几何开口角或毫米开口需另行标定。SDK本机当前夹爪允许范围约-5.73°..263.56°，最终限位以get_motor_angles返回值为准。

实际动作通过application启用关节控制模式4，再用SDK发送关节目标，不调用VLA。只改变选中电机的命令，其他角度电机保持起始目标；已有抓枪夹爪目标继续保留，违反该锁定的开合指令会拒绝。执行期间禁止启动VLA流程，存在VLA进程时也拒绝单电机动作。读取和预览不启用硬件或切换控制模式。

动作结束后保持模式4及最后目标，不自动归零。stop_motor只中断后续命令，底层仍可能继续趋近最后目标，不等同硬件急停。实际定位精度取决于反馈、驱动和负载。

接口：GET `/api/motors`、POST `/api/motor/plan`、POST `/api/motor/move`（异步202）、GET `/api/motor/status`、POST `/api/motor/cancel`。move/plan请求示例：`{"motor":"left_arm_joint1","degrees":15,"relative":false,"speed_deg_s":5,"tolerance_deg":0.5}`。cancel携带command_id。
