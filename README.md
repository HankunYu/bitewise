# bitewise

个人饮食记录工具:手机拍照/发文字 → Claude(Opus 5.5)估算卡路里和三大营养素 → 内网 Dashboard 查看汇总、趋势和建议。

## 工作方式

```
iPhone 快捷指令 ──POST──▶ FastAPI 服务(本机常驻)
                             ├─ 立即入库返回,后台队列调 Claude API 识别
                             ├─ 文字输入由 AI 判断意图:记饭 / 修正记录 / 记体重
                             ├─ 每晚定时生成当日总结建议
                             └─ 内网网页 Dashboard(今日 / 趋势 / 设置)
                                        │
                                     SQLite
```

- 每顿饭记录:描述、食物明细、卡路里、蛋白质/碳水/脂肪、一句 AI 点评
- 餐次按上传时间自动划分(早/午/晚/加餐),边界可在设置中调整
- 「我的目标」自由文本会注入每次 AI 调用,影响点评与建议口径
- 直接调用 Anthropic API,API key 放在项目根目录的 `.env`(见下方「运行」)

## 运行

```bash
uv sync
cp .env.example .env   # 填入 ANTHROPIC_API_KEY
.venv/bin/python -m bitewise --host 0.0.0.0 --port 8600
```

数据存在 `BITEWISE_DATA` 指向的目录(默认 `data/`,SQLite + 上传原图),已 gitignore。

### systemd 常驻(每个用户一个实例)

`systemd/bitewise@.service` 是模板单元,实例名即用户名:数据在 `data/<用户名>/`,端口读取 `~/.config/bitewise/<用户名>.env` 里的 `BITEWISE_PORT`。各实例的数据、设置、每晚建议互相独立。

单元里的仓库路径是占位符,由 `systemd/install.sh` 按仓库实际位置渲染后装到 `~/.config/systemd/user/`,仓库放在哪都行;移动仓库后重跑一次即可。

```bash
./systemd/install.sh                                      # 首次及移动仓库后执行
loginctl enable-linger "$USER"                            # 开机免登录自启

# 新增用户:选一个未占用的端口
mkdir -p ~/.config/bitewise
echo "BITEWISE_PORT=8601" > ~/.config/bitewise/alice.env
systemctl --user enable --now bitewise@alice
```

该用户的快捷指令和 Dashboard 改用对应端口即可。

## 手机端

见 [docs/shortcut.md](docs/shortcut.md):三个快捷指令——拍照记饭、文字记录/修正/体重(都打 `/api/upload`),以及吃之前问「能吃吗」(`/api/checks`)。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` | multipart:`image`(可选)+ `text`(可选) |
| GET | `/api/day/{date}` | 某日记录、汇总、建议、体重 |
| PATCH / DELETE | `/api/meals/{id}` | 编辑 / 删除记录 |
| POST | `/api/meals/{id}/retry` | 重试识别失败的记录 |
| GET | `/api/trends?days=30` | 每日汇总 + 体重序列 |
| GET / POST / DELETE | `/api/weights` | 体重记录 |
| POST | `/api/advice/{date}/generate` | (重新)生成当日建议 |
| GET / PUT | `/api/settings` | 目标、提示词、餐次边界等 |
| POST | `/api/checks` | 「能吃吗」咨询:multipart `image`,异步分析 |
| GET | `/api/checks/{id}` | 咨询结果(含快捷指令直接显示的 `summary`) |
| POST | `/api/checks/{id}/eat` | 把咨询记成一顿饭 |
