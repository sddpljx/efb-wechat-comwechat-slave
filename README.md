# Windows + WSL 部署基于 EFB 转发的ComWeChat

> 代码在 ehForwarderBot/efb-wechat-comwechat-slave 的基础上修改，并使用了 tom-snow/docker-ComWechat，ljc545w/ComWeChatRobot 相关文件，在此一并感谢！
>
> 本教程分为三部分：①开启WSL并安装；②在WSL内安装从端；③实现Windows端对微信的Hook；

## 开启WSL

> 微软官方文档，[链接](https://learn.microsoft.com/zh-cn/windows/wsl/install)。请注意务必安装 WSL 2

**系统要求**：Windows 10 版本 2004+ 或 Windows 11

**安装步骤**：

1. 以管理员身份运行 PowerShell
2. 执行安装命令：
   ```powershell
   wsl --install
   ```
3. 重启计算机
4. 首次启动 Ubuntu，设置用户名和密码
5. 在 Windows 开始菜单中找到 WSL Settings，在网络选项卡中把网络模式改为 Mirrored

## WSL内安装从端

> 从端的安装分为两种情况，如果之前运行过EFB，可以把 .ehforwarderbot 文件夹整体迁移到 WSL 的用户目录下，例如 /home/yourusername/.ehforwarderbot，不建议直接使用 root 用户。若直接复制原有配置，可直接关注 3 和 7 部分。

> 可以使用 conda 或 uv 管理 Python 环境，依据个人喜好。

> 从端的安装可参考 [教程](https://514.live/2023/10/04/efbwechattg)，去除 docker 相关部分。

1. 更新系统包：

   ```bash
   sudo apt update && sudo apt upgrade
   ```
2. 安装依赖：

   ```bash
   sudo apt install libopus0 ffmpeg libmagic1 python3-pip git libssl-dev
   ```
3. **【重点关注】安装Python包：**

   ```bash
   pip3 install -U git+https://github.com/ehForwarderBot/efb-telegram-master.git
   pip3 install -U git+https://github.com/0honus0/python-comwechatrobot-http.git  
   pip3 install lottie cairosvg pyqrcode
   可能缺少相关依赖，请根据报错自行安装
   ```

   ---

   > **注意：从端请不要使用 ehForwarderBot/efb-wechat-comwechat-slave，我修改的代码未合并至官方分支，使用下面的仓库替代，该分支修正了 WSL 的路径问题：**

   ```bash
   pip3 install -U git+https://github.com/sddpljx/efb-wechat-comwechat-slave.git
   ```

   ---

4. 创建配置目录：

   ```bash
   mkdir -p ~/.ehforwarderbot/profiles/ComWeChat/blueset.telegram
   mkdir -p ~/.ehforwarderbot/profiles/ComWeChat/honus.comwechat
   ```
5. 配置EFB主配置文件 `~/.ehforwarderbot/profiles/ComWeChat/config.yaml`：

   ```yaml
   master_channel: blueset.telegram
   slave_channels:
   - honus.comwechat
   ```
6. 配置Telegram机器人 `~/.ehforwarderbot/profiles/ComWeChat/blueset.telegram/config.yaml`：

   ```yaml
   token: "你的Bot Token"
   admins:
   - 你的Telegram用户ID
   ```
7. **【重点关注】配置微信从端** `~/.ehforwarderbot/profiles/ComWeChat/honus.comwechat/config.yaml`：

   ```yaml
   dir: "/mnt/c/Users/yourusername/Documents/WeChat\ Files"
   ```

   > **关于路径**：WSL 会自动将 Windows 的盘符挂载到 `/mnt/` 目录下。例如，Windows 中的 `C:\Users\yourusername` 路径在 WSL2 中对应为 `/mnt/c/Users/yourusername`。请根据你的实际情况修改 `dir` 配置中的路径。注意，路径中的空格需要使用反斜杠 `\` 进行转义。

## 实现Windows端对微信的Hook

1. 安装 Windows 版微信，版本号[3.7.0.30](https://github.com/tom-snow/wechat-windows-versions/releases/download/v3.7.0.30/WeChatSetup-3.7.0.30.exe)
2. [下载 Hook 组件](https://github.com/ljc545w/ComWeChatRobot/releases/download/3.7.0.30-0.1.1-pre/3.7.0.30-0.1.1-pre.zip)，解压到无需管理员权限的英文路径，在存放路径中找到 com 文件夹，以管理员身份打开 PowerShell 或者 cmd，运行：

   ```cmd
   CWeChatRobot.exe /regserver
   ```

    由于不会有任何返回，若无法正常 Hook，请安装 Visual C++ 相关运行库。

3. [下载 WeChatHook.exe](https://github.com/tom-snow/docker-ComWechat/raw/refs/heads/main/WeChatHook.exe)，将其放在上一步解压 Hook 文件的 http 文件夹中，以管理员身份运行。成功后会看到和 docker 版左上角类似的“注入器”。
4. 扫码登录微信

   > 注：登录前建议修改微信版本号，修改后刷新一次二维码。修改方式为 curl -X POST 'http://127.0.0.1:18888/api/?type=35' -d '{"version": "3.9.12.55"}'
   
5. 在 WSL 中启动服务：

   ```bash
   ehforwarderbot -p ComWeChat
   ```
   启动后，日志会显示从端已经根据 dir 中填写的 WSL 路径，将 Hook 路径自动映射为Windows路径。此时测试相关功能是否正常。



