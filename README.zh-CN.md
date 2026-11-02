# 小米摄像头直连 Home Assistant

[English](README.md)

在现有 HA 容器内管理小米摄像头取流，由 HA 统一提供视频实体、账号授权、
镜头开关和画质选择。

```text
小米摄像头 → HA 管理的原生取流进程 → MPEG-TS → HA stream → 播放端
```

运行时不需要独立摄像头容器、独立 go2rtc、RTSP 服务或额外网页控制台。
小米原生库依赖 glibc，标准 HA 容器使用 musl，因此保留一个隔离的内部进程，
由 HA 管理启停，并在进程崩溃后的下一次请求或轮询时恢复。

## 当前范围

- 源码版本 **0.1.0**，开发验证环境为 Linux x86_64、HA Container 2026.10.4。
- 首次安装从已有可用的摄像头桥接部署迁移。本仓库**不包含、不下载小米原生二进制**。
- 原始视频只重新封装，不重新编码。H.265 摄像头需要播放端支持 H.265。
- `chuangmi.camera.061a01` 已通过 HA HLS 解码与快照验证；其他型号以原生库支持范围为基础，未全部进行硬件验证。
- 目前仅接入主通道，不含旧机型 go2rtc 兼容模式、双向语音、云台控制或历史回放。
- 选择摄像头不会自动打开镜头。

这是初始源码版本，尚不是一键安装的 HACS 包，也不会替代 Xiaomi Home 集成的全部设备控制能力。

## 从现有部署迁移

在已经运行 HA 和可用的
[ha-xiaomi-camera 桥接服务](https://github.com/willjayyyy/ha-xiaomi-camera)
的 Docker 主机上执行：

```sh
git clone https://github.com/bkcarlos/ha-xiaomi-camera-direct.git
cd ha-xiaomi-camera-direct
python3 scripts/install.py --ha homeassistant --source xiaomi-camera-bridge
```

将参数替换成自己的容器名称。脚本会备份 HA 配置和实体注册表，将本机运行库、
已授权账号迁移到 HA 的持久目录，并检查原生库能否加载；不会启动或停止其他服务，
也不会切换镜头。

重启 HA，在「设置 → 设备与服务 → 添加集成」选择 **Xiaomi Camera Direct**，
勾选需要接入的摄像头。已有授权直接沿用，需要重新登录时通过 HA 表单完成。
验证新实体之后，可以停用并移除旧摄像头容器，保留旧配置和数据用于回退。

后续更新源码不再需要旧摄像头容器：

```sh
python3 scripts/install.py --ha homeassistant
```

更新集成代码后重启 HA。

## 保存位置

- `/config/custom_components/xiaomi_camera_direct/`：集成代码。
- `/config/.xiaomi_camera/runtime/`：隔离的 glibc、Python 与取流运行库。
- `/config/.xiaomi_camera/data/`：账号授权、缓存、设置和私有诊断。

这些目录随 HA 的 `/config` 持久化，普通容器更新不会丢失。备份应包含这些目录，
其中也包含账号授权；不要将它们提交到 Git。

运行模块复用本机已有的 `ha-xiaomi-camera` 安装，第三方组件沿用各自许可证。
本仓库不发布原生二进制、个人部署记录、账号凭据或摄像头画面。
首次迁移的来源镜像信息保存在 `runtime/provenance.json`。

## 开发检查

```sh
python3 -m venv .venv
.venv/bin/pip install aiohttp pytest pytest-asyncio ruff
.venv/bin/pytest -q
.venv/bin/ruff check custom_components tests scripts
```

自动化测试覆盖进程恢复、停止清理、身份校验和解释器环境隔离。
摄像头兼容性仍需硬件测试。

[栖间](https://github.com/bkcarlos/qijian) 是独立的 HomeKit 客户端；若要在栖间使用
HA 摄像头，需先通过 HA HomeKit Bridge 加入 Apple 家庭。
