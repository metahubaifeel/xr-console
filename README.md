# XR Console · 扫码分拣控制台

量子1号 Pro 的社区实验控制台：三路相机预览、机器人状态、英文动作指令、HID 扫码输入和单轮分拣编排。不是机器人官方产品。

## 启动

需要 Python 3.10+，机器人局域网访问权限和自己的 WALLX API Key。

```bash
pip install -r requirements.txt
cp api.env.example api.env
# 编辑 api.env，填写自己的 WALLX_API_KEY
export PORTAINER_USER=admin
export PORTAINER_PASS='填写自己的管理密码'
python3 控制台.py --ip <机器人IP> --host 127.0.0.1 --port 8900
```

访问 http://localhost:8900 。先点击“演练自动流程”验证编排，演练中的 OUTC 是模拟事件。

机器人感知容器中需部署 robot_api.py，使用 Python、Pillow、v4l2-ctl，监听 8898。相机预览通过独立后台线程连续读取现有 MJPEG 格式，不逐帧重启相机。设备编号默认 8/0/6，其他设备需要自行适配。

## 自动扫码流程

抓枪 → 对准并等待本轮新码 → 按 scan_rules.json 的精确映射放置。每阶段时限可以在网页调整。未知码不进入分拣；抓取与放置按时限切步，没有实现视觉成功判断。扫码守护默认读取本机 Linux /dev/input 设备，需要相应输入设备权限。

默认抓枪指令假设黑色物体位于机器人画面左侧，请根据现场摆位调整 scan_flow.py。流程使用全车身权限，必须由现场人员监督。停止推理不等于硬件急停，也不保证停止后持物姿态；停止并回零会移动双臂。

## 已验证与限制

- 已验证：网页加载、扫码模拟编排、取消逻辑、连续采集。单机现场采集约 10 fps，不代表所有设备性能。
- 尚未验证：自动抓枪、工具对准、实物扫码、按码放置完整闭环。
- 抓住扫码枪不代表会使用扫码枪；工具朝向与视觉完成判定仍需开发。
- 本发布包不包含内部交接资料、真实凭证、厂家 URDF 或第三方 Three.js 文件。3D 页面需要自行准备有权使用的资产。

## 文件

控制台.py 为网页与机器人接口；robot_api.py 为机器人侧预览服务；scan_flow.py 为单轮编排；扫码枪.py 为 HID 监听；scan_rules.json 为条码映射。API Key 留在 api.env，不应分发。

项目原创代码采用 MIT License。机器人固件、模型服务和第三方资产的授权不包含在本项目许可证中。
