English | [简体中文](README.zh-CN.md)

# sealed-inbox

一个自托管（self-hosted）接收端，用于接收通过**普通电子邮件**送达的、
端到端加密的个人记录。发送端可以是任何带浏览器的设备；接收端可运行在
Linux 主机、Termux 手机或容器里。

```
┌──────────┐   标记行 + 缩进 JSON     ┌──────────┐   RFC822 over    ┌──────────┐
│  发送端   │ ──────────────────────▶ │ 邮件服务器 │ ────────────────▶ │  接收端   │
│ (任意浏览器)│   RSA-OAEP + AES-GCM  │ (Gmail…) │   IMAP / IDLE    │  (本项目) │
└──────────┘                          └──────────┘                  └─────┬─────┘
                                                                         │
                                                                  CSV、图表、
                                                                  Seafile
```

线上格式即 `secure-relay-fast-v4.html`（以及任何符合规范的发送端）所产生的
生产环境 v4 格式。邮件服务器自始至终只能看到不透明的信封；接收端负责解密
校验、追加写入 CSV，并（可选）重新生成图表、上传到 Seafile。

## 仓库内容

| 文件 | 用途 |
|---|---|
| `src/envelope.py` | v4 信封解析，RSA-OAEP + AES-256-GCM 解密 |
| `src/sender.py` | 参考发送端 + 初始化 CLI（`generate`、`new-kid`）—— 可构造合法信封 |
| `src/pipeline.py` | 单次接收管线：拉取、解密、追加 CSV、重绘图表、归档 |
| `src/watcher.py` | IMAP IDLE 常驻监听（事件驱动，秒级唤醒） |
| `src/charts.py` | Matplotlib 滚动窗口图表渲染 |
| `src/seafile_upload.py` | Seafile Web API 上传（覆盖模式） |
| `src/config.py` | YAML 配置加载，密钥不进源码 |
| `src/dashboard.py` | 零依赖本地网页面板：最新读数、趋势图、服务状态、登录审计 |
| `tests/test_t1_v4_compat.py` | T1：发送端 ↔ 本接收端 ↔ 生产接收端 三方结果一致 |
| `tests/test_t2_real_email.py` | T2：multipart / Formspree 风格 / HTML 转义 JSON / 中文正文 |
| `tests/test_t3_dedupe.py` | T3：同一封邮件重复处理 → CSV 只有一行 |
| `tests/test_t4_watcher_idle.py` | T4：手工实现 IMAP IDLE，不调用 `mail.idle()` |
| `tests/test_t5_no_pii.py` | T5：仓库内零个人/生产环境字符串 |
| `tests/test_t6_dashboard.py` | T6：面板认证流、会话 cookie、图表路径沙箱 |
| `docs/PROTOCOL.md` | 完整线上格式规范（v1）—— 足以照此实现一个发送端 |
| `config.example.yaml` | 配置模板 |

## 快速开始（Debian / Ubuntu / proot-distro）

环境要求：Python 3.10+（实测 3.13），`cryptography`、`PyYAML`、
`matplotlib`（仅图表需要）。

```bash
sudo apt install python3 python3-pip python3-cryptography python3-matplotlib python3-yaml
git clone https://github.com/<you>/sealed-inbox.git
cd sealed-inbox
cp config.example.yaml config.yaml
$EDITOR config.yaml                       # 填 imap.username；见下一节

# 一次性初始化：接收端密钥对 + 发送端的 kid secret。
python3 -m src.sender generate keys       # → keys/record_decrypt_private.pem（绝不外传）
                                          # → keys/record_encrypt_public.pem （给发送端）
python3 -m src.sender new-kid phone-form  # → kid_secrets.json；secret 只打印一次

# 把应用专用密码放进一个文件（不是你的账号密码）。
echo 'abcd efgh ijkl mnop' > ~/.config/secure-record/imap-app-password

# 面板访问口令（独立的 secret，同样 git-ignored）。
echo 'my-dashboard-secret' > ~/.config/secure-record/dashboard-access-key

# 验证安装（全部离线运行）。
python3 -m tests.test_t1_v4_compat
python3 -m tests.test_t2_real_email
python3 -m tests.test_t3_dedupe
python3 -m tests.test_t4_watcher_idle
python3 -m tests.test_t5_no_pii
python3 -m tests.test_t6_dashboard

# 单次运行管线，或作为常驻 IMAP IDLE 监听。
python3 -m src.pipeline
python3 -m src.watcher &

# 可选：本地网页面板（读数、趋势图、状态）。
python3 -m src.dashboard
```

Termux 上：`pkg install python python-cryptography python-matplotlib python-yaml`。

## 连接你的发送端

参考前端是已发布的 `secure-relay-fast` 网页（任何浏览器可用，无需安装）。
四个字段把它接到本接收端：

| 前端表单字段 | 填入值 |
|---|---|
| 公钥 PEM | `keys/record_encrypt_public.pem` 的完整内容 |
| 密钥 ID (kid) | 例如 `phone-form` —— 你在 `new-kid` 时起的名字 |
| 授权密钥 | `new-kid` 打印的 secret（可从 `kid_secrets.json` 重看） |
| 收件邮箱 | 与 `config.yaml` 里 `imap.username` 相同的地址 |

同时确认表单的**主题前缀**与 `config.yaml` 的 `imap.subject_prefix`
一致（前端默认 `[OpenClaw Secure Record]`，与仓库自带的示例配置一致）。
发一条测试记录，然后：

```bash
python3 -m src.pipeline      # 拉取 + 解密 + 追加
cat data/records.csv         # 你的记录应该是最后一行
```

默认表单带一套血糖字段（glucose_value）和一套记账字段（amount），
两者写入同一份 CSV —— 确切列定义见 `docs/PROTOCOL.md`。

## 正式运行

**Linux 服务器** —— 最小 systemd 单元：

```ini
# /etc/systemd/system/sealed-inbox-watcher.service
[Unit]
Description=sealed-inbox IMAP IDLE watcher
After=network-online.target

[Service]
WorkingDirectory=/opt/sealed-inbox
ExecStart=/usr/bin/python3 -m src.watcher
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Termux** —— 保持手机唤醒、开机自启：

```bash
termux-wake-lock
python3 -m src.watcher      # 加进 ~/.bashrc 或 Termux:Boot 脚本
```

watcher 断线后按指数退避重连，并每 25 分钟重新进入一次 IMAP IDLE
（Gmail 会掐断更长的 IDLE）；一旦有匹配主题的新邮件到达，就以子进程
方式运行接收管线。

## 查看数据（网页面板）

`python3 -m src.dashboard` 会在配置的端口（默认 `0.0.0.0:8086`）上
提供一个零依赖、手机友好的页面：最新读数（数值按阈值着色）、趋势图
PNG、watcher 状态、日志尾部和登录审计。认证使用
`dashboard.access_key_file` 里的口令：POST 登录表单验证通过后种下
一个 7 天有效的 HMAC 会话 cookie。**口令永远不接受出现在 URL 里**
（否则会留在浏览器历史和隧道日志中）。图表文件**只**从 `charts_dir`
提供 —— 路径穿越由 T6 测试把关。

手机上怎么访问：

* **局域网** —— 同一网络下直接 `http://<设备IP>:8086`。
* **Tailscale** —— 完全私有，把应用指向设备的 tailnet IP 即可。
* **cloudflared** —— `cloudflared tunnel --url http://localhost:8086`
  可得到一条公网 HTTPS 地址；务必配合面板访问口令使用。命名隧道
  可以拿到稳定域名。

如实说明的边界（个人/家庭部署可以接受，但要知道）：面板是 HTTP
明文（TLS 由你的隧道终结或没有）、口令尝试无防爆破限速、服务单一
用途 —— 口令请设长一些，泄露就换。

## 配置

整个项目由 `config.yaml` 驱动。每个键的说明见 `config.example.yaml`。
值得注意的字段：

* `imap.username` + `imap.app_password_file` —— 用邮件服务商的
  *应用专用密码*，不是账号密码。该文件内容只在启动时读取，不会写入
  磁盘日志或源码。
* `crypto.private_key_path` —— RSA-2048 PEM，由
  `python3 -m src.sender generate keys` 生成。
* `crypto.kid_secrets_path` —— `kid → {secret, enabled}` 的 JSON 文件。
  接收端对它是只读的。kid secret 由发送端用 HMAC 绑定记录；接收端
  **不校验** HMAC，与生产 v2 行为一致。
* `storage.archive.backend` —— `"local"`（仅本地写入）或 `"seafile"`。
* `storage.state_path` / `idle_state_path` —— 分别是逐封邮件去重
  （IMAP SEARCH 序号，与生产同一方案）和 watcher 状态的 JSON 文件。
* `charts.windows` —— 滚动窗口列表（`24h`、`48h`、`7d`、`30d`）。
* `dashboard.*` —— 监听地址/端口、`access_key_file`（git-ignored）、
  读数着色阈值（`low`/`high`）、watcher 的 `pgrep` 模式，以及页面
  可选展示的日志文件。

私钥与各类 token 文件均被 git-ignore。

## 线上格式

见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。简版：

```
OPENCLAW_SECURE_RECORD_V1
{
  "v": 1,
  "kid": "<kid 字符串>",
  "ts": <毫秒时间戳>,
  "nonce": "<base64url 16 字节>",
  "alg": "RSA-OAEP-SHA256+AES-256-GCM",
  "ek":   "<base64url RSA-OAEP 加密的 AES-256 密钥>",
  "iv":   "<base64url 12 字节 AES-GCM nonce>",
  "ct":   "<base64url 密文 ‖ 16 字节 GCM tag>",
  "mac":  "<base64url HMAC-SHA256(kid_secret, [1,kid,ts,nonce,ek,iv,ct].join('|'))>"
}
```

接收端同时接受别名标记 `HERMES_SECURE_RECORD_V1`。

## 安全属性

* 邮件服务器自始至终只看到不透明信封；主题行和邮件头没有任何明文泄露。
* AES-GCM 对记录做认证；篡改会被以 `AES-GCM authentication failed` 拒绝。
* kid secret 由发送端计算并写入信封；**接收端不校验它，也不按 kid
  过滤**（与生产 v2 一致）。实际推论：任何持有 RSA 公钥的人都能以
  任意 kid 标签提交记录 —— 请把公钥本身当作提交凭证，泄露就换钥。
* 私钥从不离开接收端主机。发送端只见公钥。

## 测试

```
$ for t in tests/test_t*.py; do python3 -m "tests.${t##*/}" || break; done
T1 PASS
T2 PASS
T3 PASS
T4 PASS
T5 PASS
T6 PASS
```

整套测试完全离线运行：

* T1 生成新密钥对，构造一个 v4 信封，断言本仓库接收端与参考生产接收端
  解出的内部记录完全一致。参考接收端以只读方式从 `$PROD_RECEIVER_PATH`
  加载（默认不设 —— oracle 部分干净跳过，例如在 CI 里；绝不修改该文件）。
* T2 构造一封真实的 `multipart/alternative` 邮件，正文 JSON 经 HTML
  转义（Formspree 的实际情况），以此检验接收端的邮件解析。
* T3 对 mock IMAP 服务器连续三轮运行管线，确认同一 UID 重跑时 CSV 不增长。
* T4 检查 watcher 源码确认没有调用 `mail.idle()`，再用假 IMAP 驱动
  一轮真实的 IDLE 收发。
* T5 对工作区做通用事故模式扫描（真实邮箱、隧道 URL、聊天群号、PEM
  密钥块、Termux 绝对路径、游离 UUID）；若你提供 git-ignored 的
  `tests/pii_patterns.local`（每行 `label|regex`），还会扫描你自己的
  具体敏感值。任何命中都拒绝通过。
* T6 在临时端口上启动真实面板服务，实测认证流（POST 错误/正确口令）、
  断言 URL 查询串携带口令按设计被拒绝、会话 cookie、`/api/status`，
  并断言图表请求无法逃出 `charts_dir`（路径穿越 → 404）。

## 背景

本项目最初围绕某个具体网页表单构建；线上协议本身不绑定任何单一
发送端。任何能产出符合规范 v4 信封的发送端（见 `docs/PROTOCOL.md`）
都可以使用。

## 许可证

MIT —— 见 `LICENSE`。
