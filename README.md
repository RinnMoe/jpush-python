# jpush-python

一个只接收 JPush 设备侧推送的 Python 客户端，移植自
[thibauddavid/jpush-go](https://github.com/thibauddavid/jpush-go)。它实现的是
JCore TCP 协议，不是用于发送推送的 `api.jpush.cn` REST SDK。

当前实现包含：

- 经典 JHead + TLV 明文协议；
- Android JCore 4.7.3 与 iOS JCore 2.4.0 的 AES-256-CBC 协议；
- SIS UDP 服务发现、注册、登录、设置别名、心跳、推送确认；
- JSON 文件凭据存储；
- 离线 JCore 抓包解密和字段解析。

## 安装

```powershell
python -m pip install -e .
```

## 基本用法

```python
from jpush import Client, Config, FileStore, NewJCore473Codec

client = Client(
    Config(
        app_key="0123456789abcdef01234567",
        package_name="com.example.app",
        codec=NewJCore473Codec(),
        store=FileStore("creds.json"),
    )
)

credentials = client.register(timeout=30)
try:
    client.set_alias("my-alias", timeout=20)
    push = client.wait_for_push(timeout=3600)
    print(push.content)
finally:
    client.close()
```

客户端是同步 API；`register()` 成功后，接收循环和心跳在线程中运行。测试或本地模拟时可以通过
`Config.servers` 固定地址，并通过 `Config.dial` 注入 socket。

## 测试

```powershell
python -m pytest
python -m compileall -q src tests
```

测试使用本地 socket 模拟完整的注册→登录→别名→推送→ACK 链路，不需要真实 AppKey。真实 JPush
服务器、AppKey/包名绑定、SIS 网络可达性和生产推送投递仍需在目标环境单独验证。

## 与上游 Go 实现的兼容性

当前 Python 回归测试覆盖了上游 Go 测试中的帧、TLV、SIS、存储、抓包和客户端场景；另外用固定
seed 对 register、login、alias、heartbeat、push-ack 五种现代 JCore 帧做了逐字节比对。Go 上游
测试和 Python 测试均通过。Python 版本仍未替代真实服务器/设备环境验证，这部分需要目标 AppKey、
包名绑定和可达的 JPush 网络。

## 上游与许可证

协议字段和测试向量以 [jpush-go](https://github.com/thibauddavid/jpush-go) 为参考；本项目沿用 MIT
许可证。当前实现不会发送推送，也不会实现 JPush REST API。
