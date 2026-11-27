# 上下文交接：BRIGHT 2026 · 超级棒棒糖队 · 机器人控制

> 给接手/协作的 agent。读完这一份就能继续，不用再问人。
> 最后更新：2026-11-27 22:40（比赛第一天深夜）

---

## 0. 一句话现状

官方云端 VLA（WALL-OSS）已经能用网页控制台完整驱动量子1号 Pro，**单步动作已跑通并成功抓取过物体**；
剩下三件事：**① 抓扫码枪的姿态 ② 多步流程的自动编排 ③ 场馆 WiFi 不稳**。

---

## 1. 硬件与网络

| 项 | 值 |
|---|---|
| 机器人 | 量子1号 Pro，`quanta_x1_pro/gripper-g`（G 夹爪，API 量程 [0, 1.526]） |
| 主机名 | `xr-msi-ex001-655` |
| SN | `XREX21ZD2605060655` |
| 工位 | 27 号 |
| CPU/内存 | 21 核 / 15.8GB |
| OS | Ubuntu，Docker 28.4.0 |

### 机器人的四条网络（都在场馆局域网）

| 接口 | IP | 说明 |
|---|---|---|
| `enp171s0` | **10.230.20.13**/22 | 有线（背部 RJ45），网关 10.230.23.254 |
| `wlo1` | 10.230.20.74/22 | 机器人自己的 WiFi |
| `wwan0` | 10.32.211.35/29 | 4G 模块（Fibocom FM160） |
| `tailscale0` | 100.100.35.189 | Tailscale |

**笔记本**（用户那台 Lenovo 83D5）：`enx00e04c6801e8` 是有线（USB 2.5G 网卡），`wlp4s0` 是 WiFi。

### ⚠️ 网络是当前最大风险

场馆 WiFi 有 30+ 队共用，实测 **ping 延迟 3~907ms、丢包 0~33%、HTTP 随机重置**。
症状：Portainer 握手 0/10 成功、SSH `Connection closed`、8898 无响应，但 **TCP 端口都是开的**（说明机器人活着，是链路问题）。

**对策**：全程插网线（有线走交换机，比 WiFi 稳得多）；机器人有 4G 备份。

### 账号

| 通道 | 凭证 |
|---|---|
| SSH | `ssh xr@10.230.20.13`，密码 **`123`**（官方 FAQ） |
| Portainer `:9000` | `admin` / `xsquarerobot`（官方文档） |
| 容器 | `ex001_client-robot_client-1`（**client 后面是下划线**，容易打错）、`ex001_client-robot_perception_ops-1` |
| API Key | `/home/amd/下载/具亮计划/比赛执行/api.env`（**敏感，勿外传**） |

---

## 2. 官方模型的真实事实（从服务端握手 metadata 拿到，非猜测）

```
model        = wall-x
model_version= 2610-dualhead-v2
训练配置     = TR-055-qwen35-4b-base-flashmask-ar001-tr051-60k-xpred-dualhead-r3-lr3e-5-cblr9e-5
```

| 项 | 值 |
|---|---|
| **语言主干** | **Qwen3.5-4B**（base）← 硬证据 |
| 视觉编码器 | **未公开**。Qwen-VL 系通常配 SigLIP 系 ViT，但**属推测，别写进材料** |
| 动作头 | dualhead（左右臂双头） |
| **动作空间** | **`Rel EEF`（相对末端位姿）** ← 极重要 |
| action_dim | 14（左右各 7：xyz + 6D 旋转 + 夹爪） |
| action_horizon | 32 帧 |
| row_dt | 0.0337s |
| 推理硬件 | **RTX 5090**，中位 65.3ms（服务端自报"慢于预期"） |
| 并发 | 上限 16 台机器人 |
| System prompt 声明 | `front view + left wrist view + right wrist view`，`Frequency: 20HZ`，`Rel EEF` |

**因为动作是「相对」的**，所以：每次换指令必须重开会话；误差会累积；`use_history` 有意义。

### API 行为

| 层 | 行为 |
|---|---|
| 单次请求 | **完全无状态**：图 + 一句指令 → 32 帧相对动作 |
| 会话（`start_ex001.sh`） | 持续循环：抓图→推理→执行→再抓图，**直到被停** |
| 换指令 | **必须停掉重开**（约 3~5 秒） |
| `use_agent: true` | 服务端内部自己多轮循环，一句长指令自己拆步骤 |
| 调用次数 | **无限制**（`max_inflight_per_robot: 2`） |
| 8 分钟 | **是比赛计时规则，不是 API 限制** |

---

## 3. 相机真相（官方标定 `sensor_list.yaml`）

| `/dev/videoN` | 是什么 | 模型看不看 |
|---|---|---|
| 8 | 头部前方 RGBD（USB3.0 5M） | ✅ 模型的 `front` |
| 0 / 6 | **BEV 鱼眼**（机身左右，鸟瞰 1600×1200，装在机身上不是肘部） | ❌ 只给导航/自检/人看 |
| 2 / 4 | 腕部（夹爪视角） | ✅ 模型的 `left_wrist`/`right_wrist`，**但外部取不到**（被感知栈独占 + 走 iceoryx 不暴露话题） |

---

## 4. 已建成的工具链

### 4.1 网页控制台（主力）

```bash
cd /home/amd/下载/具亮计划/比赛执行 && python3 控制台.py
# → http://localhost:8900
```

功能：三路相机、自由指令、原子动作按钮、车身权限 4 档、`use_agent` 开关、扫码枪面板、停止+归零、`/twin` 3D 孪生。

### 4.2 机器人端数据服务（性能关键）

`robot_api.py` 跑在感知容器内（容器是 **host 网络**，等于机器人本体端口 **:8898**）：
后台常驻抓帧 + **PIL 就地缩图**（760px / q72，330KB → 32KB）。

**这是必须的**：不缩图，一张图走 WiFi 要 3~11 秒；缩完 0.2 秒。
控制台只做 1ms 代理。

### 4.3 容器内脚本

| 脚本 | 作用 |
|---|---|
| `/tmp/d3.sh "<instruction>"` | 停旧会话 + 用新指令起会话（读 `/tmp/body.json` 和 `/tmp/infer_params.json`） |
| `/tmp/stop.sh` | 停会话 + 调 `zero_arm` 正规归零 |

### 4.4 其他

- `拉文件.py` —— 从容器拉任意文件（正确拆 Docker 多路复用流）
- `扫码枪.py` —— HID 键盘型扫码枪抓取，`--daemon` 写 `/tmp/last_scan.json`
- `urdf/ex001_robot.urdf` —— 官方 URDF，79 links / 78 joints / 23 可动
- GitHub 私有仓库：https://github.com/metahubaifeel/xr-console

---

## 5. 指令（prompt）设计原则与现状评估

### 5.1 必须遵守的原则

1. **只认英文，一句一个动作。** 别把三步塞进一句。
2. **不要说「scan / read / 扫码」。** 模型不懂扫码。要说**看见什么、往哪动**：
   - ❌ `scan the barcode`
   - ✅ `hold the bottle in front of the scanner`
3. **不要说「left hand / right hand」。** 训练数据里通常不指定左右，模型可能理解错。要让它自己选。
4. **颜色/位置词必须和画面里真实可见的一致。** 模型是靠眼睛认的。
5. **越短越好。** 修饰词多了会稀释重点。
6. **动作是相对的**，所以"放到哪"要描述得能让模型从当前画面推理出来。

### 5.2 现有指令逐条评估

| 现用指令 | 评价 |
|---|---|
| `pick up the product from the box` | ⚠️ `product` 太泛，场景里多个类似物体时定位不稳。改成描述性特征：`pick up the white round product` |
| `place the product into an empty hole of the tray` | ✅ 好 |
| `push the product down into the hole` | ✅ 好 |
| `pick up the barcode scanner by its handle with the right hand` | ⚠️ 两个问题：`with the right hand` 应删；`by its handle` 保留（有用） |
| `hold the bottle in front of the barcode scanner` | ✅ 好 |
| `put the bottle into the red square` | ✅ 好（方框在棕色纸板上，四色对比都够） |

### 5.3 建议改成的版本

```
抓枪      pick up the barcode scanner by its handle
扫码位    hold the bottle in front of the barcode scanner
入箱      put the bottle into the red square
抓产品    pick up the white round product
装坑      place the product into the empty hole of the tray
压到位    push the product down into the hole
```

---

## 6. 当前要解决的三件事

### ① 抓扫码枪的姿态（最紧急）

- 枪是 Honeywell 手持式，**把手朝上**摆在白色支架上（用户刚改的朝向，还没验证效果）
- 夹爪必须**从"虎口"方向**接近：闭合方向 ⊥ 把手轴线，两指面贴把手两侧
- 用户实测建议：把手露出枪身 **≥8cm**，留夹爪接近空间；枪最好**半悬空**（只靠把手支撑）
- G 夹爪最大张开 1.526 rad，把手 30~40mm 能夹
- **先单独验证抓枪一步**，成功后再串后续

### ② 全自动编排

用户明确要求：**不能靠人点按钮**。两种路线：

- **A（推荐先做）**：编排脚本，一条命令顺序跑完 3 个会话（抓枪→对准→入箱）。人只在开始启动一次。
- **B**：`use_agent: true` + 一句长指令，服务端自己拆。省事但不可控、可能一直跑不停。

**关键判断**：如果瓶子上只有一个码、目标框固定，**机器人根本不需要知道扫到了什么**，纯 A 就够。
多瓶多码时，才需要扫码结果参与（扫码 → 软件映射 → 发对应指令）。

### ③ 网络

见 §1。**这条优先级高于一切技术问题。**

---

## 7. 踩过的坑（别再踩）

1. **机器人网口是透传到场馆局域网的**，不是点对点。插网线 ≠ 直连机器人。
2. **`pkill -f <名字>` 会杀掉执行它的 shell**（命令行自身含该字符串）。用 PID 文件或 `[.]` 正则规避。
3. **归零要用 `x2robot_client.zero_arm`**，别依赖 `--home-on-stop`（`kill -9` 会打断归零，臂停在半路）：
   ```bash
   source /opt/xr/bot/x2robot_client/share/x2robot_client/scripts/session_env.sh
   python3 -m x2robot_client.zero_arm --timeout 90
   ```
4. **控制台不能"请求来了才抓帧"**：多相机并发 + 重试风暴会打爆 HTTP 超时。必须后台常驻抓帧。
5. **Docker exec 的输出要按 8 字节多路复用头拆包**，否则图片 base64 会被污染。
6. **URDF 的 joint 不是拓扑排序的**，建树要迭代多轮。
7. **URDF 是 Z-up，three.js 是 Y-up**，根节点要 `rotation.x = -π/2`。
8. **`re.sub` 的替换串会解释 `\n`**，把 Python 原始字符串塞进文件时会破坏 JS。用 lambda。
9. **容器里 uid 是 `xr`（1000）**，`whoami` 在容器内就是 xr。

---

## 8. 文件位置

```
/home/amd/下载/具亮计划/
├── 比赛执行/
│   ├── 控制台.py            ★ 网页控制台（含 /twin 3D 孪生）
│   ├── robot_api.py         ★ 机器人端数据服务（:8898）
│   ├── 扫码枪.py            扫码枪抓取
│   ├── 拉文件.py            从容器拉文件
│   ├── scan_rules.json      码 → 指令映射
│   ├── urdf/ex001_robot.urdf
│   ├── web/three.min.js
│   ├── api.env              ★ API Key（敏感）
│   ├── 交接_给下一个agent.md   项目级交接
│   └── 道具核对表.md
└── 提交包/、轻量包/
```

**GitHub**：https://github.com/metahubaifeel/xr-console（私有，账号 metahubaifeel）

---

## 9. 用户沟通要点

- 用户懂硬件、会拆机量电压，但**底层概念要用类比讲**
- 他反复问同一件事时要**直接给结论 + 一句话理由**，不要堆选项
- 他明确说过「**别写 md 直接跟我说**」—— 除非要求落到文件
- 他会用拼音打字，读不懂就结合上下文推断，别反问太多
- 他讨厌信息落错地方
- **他会纠正我的错误判断**（比如"人不该操作机器人"、"全自动才对"），要接受并修正，别辩解

---

## 10. 【2026-11-27 深夜更新】抓取突破 + 关键机制

### 10.1 已经成功抓起扫码枪（实测配方）

```
① 清场 —— 桌上只留扫码枪，其他物体全部拿走
② 指令 —— pick up the standing black object on the right
③ 档位 —— 全权限（car_pose + head_pose + lift 全开）
④ 模式 —— 裸 API（use_agent 不勾）
⑤ 实测 —— 约 60 秒抓住；抓住后右夹爪稳定在 0.338 rad，悬空保持 20 秒以上不滑落
```

**对照组**：清场前用同一句指令，机器人两次都去抓了怡宝瓶。**拿掉瓶子后第一次就锁定了扫码枪。**

### 10.2 指令措辞三原则（已实测验证）

| 原则 | 反例 | 正例 | 原因 |
|---|---|---|---|
| **不用类别名词** | `barcode scanner` | `black object` | "scanner" 训练样本稀少，嵌入不准；颜色/形状/姿态是高频基础属性 |
| **绝对不用 `barcode` 这个词** | `pick up the barcode scanner` | `pick up the standing black object` | 画面里有带条码的瓶子，这个词会把注意力拽到瓶子上（**实测踩过这个坑**） |
| **用「颜色 + 姿态 + 位置」组合** | `pick up the scanner` | `pick up the standing black object on the right` | `standing` 区分躺平的方框；`on the right` 缩小搜索范围 |

### 10.3 ⚠️ 最关键的机制问题：单句指令会导致「反复抓放」

`start_ex001.sh` 是**无状态循环**：每 0.3 秒重新看画面、重新推理**同一句指令**。

```
T1  画面：桌上有立着的黑色物体  → 去抓     ✅
T2  已在手里 → 画面上"桌上有立着的黑色物体"不成立
    → 目标消失 → 模型松手
T3  画面：桌上又有了 → 又去抓   ✅
    无限循环
```

**单句指令只能表达"一个瞬间的目标"，无法表达"抓住并保持"。**

**这是必须做多步编排的根本原因**，不是调参能解决的。

### 10.4 目标流程与待做

```
① 机器人拿起扫码枪
② 把枪口对准瓶子上的条码（自动感应模式下不用扣扳机）
③ 扫码枪读到码 → 电脑通过 HID 收到（扫码枪.py 已实现）
④ 码 → 颜色映射 → 变成一句自然语言
⑤ 机器人把瓶子放进对应颜色的框
```

**码 → 颜色映射（已配置在 `scan_rules.json`，实测 OUTC 正确解析）**：

```
OUTA  → red square
OUTB  → silver square
OUTC  → white square
ERR99 / ERR → black square
```

**待做**：编排器。方案 A（推荐）：脚本按固定时长自动切指令；方案 B：`use_agent: true` 一句长指令让它自己串。

### 10.5 网络问题（当前最大风险，优先于一切技术问题）

| 现象 | 实测 |
|---|---|
| 场馆 WiFi 拥堵 | ping 延迟 3~907ms 波动，丢包 0~33%，HTTP 随机重置（Portainer 握手 0/10、SSH `Connection closed`） |
| **WiFi 客户端隔离** | 同一 WiFi 下两台笔记本**互相访问被拒**（`连接被拒绝`），但都能访问有线网里的机器人 |

**对策**：
1. 全程插网线（有线走交换机，比 WiFi 稳得多）
2. 协作者不要连主控笔记本的控制台，**直接连机器人**：
   - 相机图 `http://10.230.20.13:8898/snap?d=8`
   - WebUI `http://10.230.20.13`
   - Portainer `http://10.230.20.13:9000`（admin / xsquarerobot）
3. 机器人有 4G 模块（`wwan0` 10.32.211.35）可作备份通道

### 10.6 左右方向的坑（血的教训）

**相机视角的左右，和从桌子对面拍的照片，左右是相反的。**

判断机器人视角的左右，**只信相机图**（`/snap?d=8`），不要用手拍的照片。连续两次因为拿手机照片当依据判错方向。

### 10.7 测试纪律

- **测试期间人绝对不能碰桌上的东西**。VLA 整条链路是「看见什么 → 怎么动」，中途任何碰触都会让它的判断错乱。
- 一次只改一个变量：要么改指令，要么改摆位，不要同时改。

---

## 11. 【重大发现】官方开源了 XR Harness —— 可能就是我们要的编排层

### 11.1 仓库

```
https://github.com/X-Square-Robot/xr-harness
TypeScript · ★48 · 2026-11-23 开源 · 开发中（开发者预览版）
中文 README：https://github.com/X-Square-Robot/xr-harness/blob/main/README.zh.md
```

**这是自变量官方的「机器人 Agent Harness」。**

### 11.2 三层架构（正好补齐我们缺的编排层）

| 层 | 组件 | 职责 |
|---|---|---|
| **System 2** | **XR Brain** | 结合现场画面理解用户目标、**规划任务步骤及各步约束**、根据执行反馈**调整计划** |
| **System 1** | **XR Cerebellum** | 调用 WALL-OSS-1.0 动作模型和原生工具，组织每一步执行。**Watcher 检查当前步骤是否完成**，**Monitor 报告场景变化和异常** |
| **System 0** | **RAP Runtime** | 通过 RAP（Robot Agent Protocol）连接机器人控制器与传感器，提供能力发现、操作执行、观测读取 |

**这直接解决了 §10.3 的「单句指令导致反复抓放」问题** —— `Watcher` 就是判断"这一步完成了没有"的组件。

### 11.3 官方 demo 验证过的能力（跟我们的任务高度重合）

1. **移开遮挡 → 找到纸上的图示 → 按提示把苹果放进篮子**
   → 从新发现的视觉信息推断操作对象与目的地，补全模糊指令里没说的细节
2. **一句"收拾桌子" → 水果归篮 + 杂物整理 + 餐盘堆叠**
   → 把笼统要求转成完整任务，自己排先后顺序
3. **根据执行反馈改路线**：发现远侧交接不可行 → 改近侧绕行，同时保留原有约束

### 11.4 快速开始（如果要部署）

```bash
# 依赖：Python 3.12+ / Node.js 24.9+ / pnpm / 原生编译工具
./xr-harness verify
./xr-harness configure --config var/site.json
./xr-harness install --config var/site.json
./xr-harness doctor --config var/site.json
# 工作区 http://127.0.0.1:5006（Brain 端口 5008）
```

### 11.5 ⚠️ 但**现在还不能直接用在量子1号 Pro 上**

```
rap-runtime/rap/adapters/  只有两个：
  ├── common
  └── robodojo        ← RoboDojo 仿真环境
```

**没有 ex001 / 量子1号 Pro 的 RAP 适配器。** 真机接入需要自己写 adapter，工作量不小。

README 的 News 里有：`[2026-11-23] XR Harness 已支持 RoboDojo 仿真，并可通过 MaaS 公共 API 接入 WALL X`
→ 说明 **WALL X 的接入已经打通**，缺的只是机器人侧的 RAP 适配。

---

## 12. 【重大发现】机器人上已经跑着官方 agent 服务

`docker ps` 里有一组之前没注意的容器：

```
x2robot-agent-service     acr-sz.../app/agent:ubuntu24.04-main.v26.11.13.02
agent-cloud_agent-1       acr-sz.../robot:ubuntu24.04-v26.11.01.07
agent-redis               redis:7.2.15-alpine3.21
```

`x2robot-agent-service` 的环境变量暴露了它的作用：

```
CHAT_URL       = https://infer.x2robot.com/infer/agent-llm/v1/chat/completions
X2ROBOT_ACTION_DISPATCHER = 1
X2ROBOT_INTERP_BRIDGE     = off
XR_IMAGE_TYPE             = runtime
```

**它自带一个 LLM 端点（Brain）+ 动作分发器（Cerebellum）。**

### 12.1 结论：`use_agent: true` 大概率就是调这个服务

也就是说 —— **官方已经把 Brain 层部署到机器人上了，不需要我们自己写编排器。**

### 12.2 因此当前的推荐做法

**优先试 `use_agent: true`，而不是自己造多步编排。**

之前试过一次（"grab the blue cube in the middle and place into the red blanket"），跑了 3 分半，
但**指令里的目标物桌面上根本不存在** → 等于没测到。

正确的测法：

```
指令：pick up the standing black object on the right,
      hold it, then put the bottle into the white square
档位：全权限
模式：use_agent = true
```

观察点：
- 它是否会自己分步（先拿枪 → 再对瓶子 → 再放框）
- Watcher 是否会在"抓住"后停止重复抓取（这正是裸 API 做不到的）
- 失败后是否会换策略

**如果这个跑通了，就不需要 §10.4 里的"编排器"了。**

---

## 13. 安全提醒

| 位置 | 问题 |
|---|---|
| 队伍微信群 `lingbot vla 2026` | 22:02 有人贴了**明文 OpenAI API key**（`sk-proj-...`）。已泄露，应立刻吊销 |
| 机器人 `x2robot-agent-service` 容器环境变量 | 有阿里云 AK/SK 明文（`ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET`）、`X2ROBOT_LOCATION_TOKEN`、`API_KEY`。属出厂配置，**不得外传、不得写进文档/仓库** |
| `api.env` | WALLX API Key，已在 `.gitignore` 排除 |

---

## 14. 队伍微信群里的其他线索（`lingbot vla 2026` = 46282951898@chatroom）

| 时间 | 内容 |
|---|---|
| 11-26 02:25 | 刘琦发 `bright2026-guide.pdf`（本地作战手册，含原子动作库附录） |
| 11-27 05:29 | 吴非桐发现 `xr-harness`，问"sys0/sys1 能不能直接用" → 刘琦："不知道 api 里 agent 是不是就用的这个" → "大概率是了" |
| 11-27 16:21 | 吴非桐发 `条形码.pdf`（条码测试素材），路径 `~/文档/xwechat_files/wxid_l40t7sdnas0j22_0d18/msg/file/2026-11/条形码.pdf` |
| 11-27 21:38 | 刘琦：想改 client 输出的动作；想给输入图像做标注（针对扫码枪识别不好的情况） |
| 11-27 21:39 | 唯一为为：可以提供场外算力 |

---

## 15. 【已实现】锁臂中间层 —— 强制只让一只手臂动

### 15.1 问题

API 每次请求**同时返回双臂的 32 帧动作**：

```json
{"actions": {
  "left_ee":  {"position":[32,3], "rotation_rpy":[32,3], "gripper":[32]},
  "right_ee": {"position":[32,3], "rotation_rpy":[32,3], "gripper":[32]}
}}
```

**client 原样执行双臂。** 用户明确要求「不要两个手一起动，各自分工」（左臂管枪、右臂管瓶子），
但 client 的 27 个 CLI 参数里**没有任何"只用一只臂"的开关**：

```
--instruction --embodiment --infer-params --return-body --control-context
--use-history --no-history --camera-resize-* --infer-type --model-address ...
```

`--control-context` 只能调延迟/滤波/夹爪增益（`eff_delay_ms`/`lpf_T_ms`/`grip_gain`/`z_offset_mm`/`rot_offset_deg`），**不能禁用某条臂**。

### 15.2 解法：client 与云端之间插一层（官方允许改 harness）

```
机器人 client ──ws──> 锁臂.py(:8001) ──wss──> 官方 VLA
                          ↑
              VLA 回来的 32 帧动作里，
              把被锁那条臂的目标位姿全部替换成
              observation.state 里传上来的「当前实际位姿」
              → 32 帧原地不动 = 物理锁死
              另一条臂原样放行
```

**文件**：`比赛执行/锁臂.py`

```bash
python3 锁臂.py --lock right    # 锁右臂，左臂干活
python3 锁臂.py --lock left     # 锁左臂，右臂干活
python3 锁臂.py --lock none     # 纯转发（对照组）
python3 锁臂.py --verbose       # 打印每帧锁定日志
```

**client 侧启动脚本**：`/tmp/d3h.sh`（容器内），把 `--model-address` 指到 `ws://<笔记本IP>:8001`

```bash
bash /tmp/d3h.sh "grasp the black handle of the scanner"
```

注意：走中间层时 **`--infer-params` 里不要带 api_key**（key 由中间层连云端时使用），
只需 `{"api_schema":"public_v2","normalization":"provided","algorithm":"sync"}`。

### 15.3 实测结果（2026-11-27 深夜）

```
右臂  J1 +0.0010  J2 +0.0025  J4 +0.0196   ← 全程 100 秒变化 < 0.001 rad，完全静止
左臂  J1 -0.17  J2 +1.67  J4 +0.72          ← 正常执行，伸到扫码枪上方并抓住
```

**锁臂 100% 生效。** 而且左臂在锁臂状态下**成功抓住了扫码枪把手**（夹爪稳定 0.190 rad）。

⚠️ **夹爪读数陷阱**：`0.190 rad` 看着像"张开"，其实是**夹住细把手后的宽度**。
    对比：全开 ≈ 0.43~0.58；空夹闭合 ≈ 0.000；夹住把手 ≈ 0.19~0.34。
    **不要用夹爪值判断"抓没抓住"，要看画面。**

---

## 16. 指令长度的定量结论（实测）

| 长度 | 结果 |
|---|---|
| 5~15 词 | ✅ 最好，立刻响应 |
| 13 词（`grasp the black handle of the scanner and point it at the bottle`） | ✅ 左臂伸手抓枪 |
| 16 词（`open the gripper, grasp the black handle of the scanner, do not touch the scanning head`） | ✅ 抓枪成功 |
| **161 词 / 954 字符**（详细的物体描述+动作步骤+负向约束） | ❌ **模型完全不动作** —— 52 秒内关节值变化 = 0，无报错、会话正常启动 |

**结论：VLA 的训练分布里 instruction 是单句（几十 token）。超过 ~60 词基本失效，超过 100 词直接输出全零。**

**要多个动作 → 发多条指令，不要写成一段。**

### 16.1 有效指令措辞（都实测过）

```
grasp the black handle of the scanner                              ✅ 抓到
grasp the black handle of the scanner and point it at the bottle    ✅ 抓到并转向
pick up the standing black object on the right                      ✅ 抓到（清场后）
open the gripper, grasp the black handle of the scanner, do not touch the scanning head  ✅ 抓到
```

### 16.2 无效 / 有害措辞

| ❌ | 原因 |
|---|---|
| `barcode scanner` | "scanner" 训练样本稀少；**画面里有带条码的瓶子，`barcode` 会把注意力拽过去**（实测两次都去抓了瓶子） |
| `with the right hand` / `with the left hand` | 训练数据不标左右，加了反而干扰 |
| 超过 60 词的任何描述 | 稀释注意力，最坏情况完全不动 |

---

## 17. 扫码枪的物理方案（用户提出的，已验证几何成立）

### 17.1 关键几何洞察

**条码竖直贴 + 枪口竖直 = 天然平行，少一个自由度。**

- 条码做成**竖直纸条**贴上去 → 那一刻它就是一个竖直平面
- 扫码枪正常握持时，**扫描线是横向的**，枪口平面是竖直的
- 横向扫描线 ⟂ 竖直条纹 ✅
- 枪口平面 ∥ 条码平面 ✅

**所以不需要做 3D 对准，只要枪口大致朝向条码即可。** 扫描枪的扫描线很宽，容差比直觉大。

### 17.2 用户定义的目标流程（左臂管枪、右臂管瓶）

```
1. 右手不动；左臂平行于枪身抓住扫码枪
   → 平行于地平面逆时针转腕关节
   → 对准瓶子（若条码朝左，右臂可微调位置）
2. 扫码
3. 扫码结果进控制台
4. 控制台把结果映射成指令，通过 harness 让 API 知道
5. 若 OUTA：左臂把枪放回原位 → 左臂归位 → 右臂抓瓶 → 放红色框 → 右臂归位 → 完成
6. 若 OUTB：同流程 → 银色框。OUTC → 白色框。ERR99 → 黑色框
```

**每步之后都要归位，两条臂绝不一起动。**

### 17.3 已配好的映射（`scan_rules.json`，实测 OUTC 解析正确）

```
OUTA  → "put the bottle into the red square"
OUTB  → "put the bottle into the silver square"
OUTC  → "put the bottle into the white square"
ERR99 / ERR → "put the bottle into the black square"
```

实测扫码枪输出为大写 `OUTA` / `OUTC`。

---

## 18. 停止 / 归零：必须拆成两个按钮（已修）

**踩过的坑**：原来「停止」= 杀会话 + `zero_arm` 归零。
**但归零只管手臂位置，不管夹爪开合** → 夹爪保持最后的闭合指令 → **手里的东西被带回零位**。

实测：左臂夹着扫码枪归零，枪被一起带走了。

**修法**：控制台拆成两个：

| 按钮 | 行为 |
|---|---|
| **■ 停止（臂不动）** | 只杀会话，手臂停在原地，夹爪保持开合 → 人可以先把东西取下来 |
| **⌂ 停止 + 归零** | 杀会话 + `zero_arm` 归零 → **夹爪里的东西会跟着回零位** |

**正确流程**：点「停止（臂不动）」→ 手动取下物体 → 再点「停止 + 归零」。

容器内对应脚本：`/tmp/stop.sh`（只停）与 `/tmp/home.sh`（停+归零）。

---

## 19. 比赛规则要点（8 分钟怎么用）

```
计时起点   机械臂开始动作
时限       8 分钟 / 每个 Case
超时       只把该 Case 未完成的得分项计零，不影响其他 5 个 Case
最终分     6 个 Case 得分之和 × K
```

### 人能在 8 分钟里做什么

| ✅ 允许 | ❌ 禁止 |
|---|---|
| 在控制台点按钮、发指令、启动推理 | 触碰机器人 |
| 开测前摆道具、切档位、开程序 | 触碰道具（Case 5 的裁判干扰除外） |
| 盯画面判断、决定下一步发什么 | 用手扶机器人、帮它对准、把手伸进视野 |

**录像里一眼能看出人有没有干预。** 开测前把所有能做的准备做完，机械臂一动就进入计时。
