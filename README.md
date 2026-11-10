# Fxxklejiansport

自动完成乐健体育的成绩生成和上传。

## 当前功能

- 添加、删除及恢复账号，支持账号密码登录和双 Token 导入。
- 使用账号时检查凭据并读取累计、今日、本周里程。
- 账号目标默认跟随学期目标，可独立修改和恢复默认。
- 直接生成轨迹；种子 `-1` 使用操作系统安全随机源，非负种子可复现。
- 每个账号可快捷生成并上传；账号池底部支持计划预览、自动逐账号处理、停止及中断后继续。
- 自动圈数按可用额度除以参考圈长向上取整，每批每个符合条件的账号最多提交一条。
- 服务器接收回执与有效里程分开显示，结果不确定的上传不会自动重发。

## 启动

需要 Windows、Python 3.10+；账号凭据使用当前 Windows 用户的 DPAPI 加密。

```powershell
python -m pip install -r requirements.txt
python launch.py
```

也可双击 `start.cmd`。服务监听 <http://127.0.0.1:18766/>；停止运行 `python stop.py`。

正常入口为 `server_v3.py`，页面位于 `web_v3/`。`app.py` 提供共享 HTTP 组件并保留旧版回归实现；当前流程没有计时时钟和本地运动记录列表。

## 私有路线与凭据

仓库的 `fixtures/` 是数学生成的示例，未包含真实校园区域、个人路线、账号、Token、密码或数据库。连接真实账号前，需在本机配置适用的参考路线：

- `.local/reference.json`：151 个首尾闭合的点，每点包含 `latitude` 和 `longitude`。
- `.local/region.json`：可选本地区域，结构参考公开示例；真实账号区域仍通过服务端读取。

私有配置优先于示例；`.local/` 已被 Git 忽略。请通过页面添加自己的账号，不要把任何真实凭据写入源码或提交到仓库。

## 验证

```powershell
python -m unittest discover -s tests -q
node --check web_v3/app.js
node verify_v3_web.cjs
```

前端回归需要 Node.js。自动化测试使用虚构账号和隔离模拟上游，不提交真实运动记录。

详细操作见 [README.txt](README.txt)，接口见 [API.txt](API.txt)，公开内容说明见 [PRIVACY.md](PRIVACY.md)。

## 许可证

沿用本仓库的 [GPL-3.0](LICENSE)。
