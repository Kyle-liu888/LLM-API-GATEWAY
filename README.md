# LLM API Gateway

将公司内部大模型网关封装为标准 OpenAI/Anthropic 兼容 API，支持双上游路由、API Key 管理、多租户隔离、自动更新。

## 功能

- **双上游路由**：根据模型名精确匹配分流 Copilot HDP 或 ZJ aigateway
- **协议兼容**：同时支持 OpenAI Chat Completions 和 Anthropic Messages
- **Tool Calling**：Anthropic tool_use/tool_result 与 OpenAI function calling 双向转换
- **推理支持**：自动将上游 reasoning_content 转为 Anthropic thinking 块
- **负载均衡**：Copilot 路径按账号在飞请求数自动分配最空闲账号
- **健康退化**：连续失败自动降级（healthy → degraded → down + 5 分钟冷却），成功即恢复
- **API Key 管理**：创建、吊销、轮换密钥
- **多租户隔离**：不同团队独立配额和模型权限
- **Token 用量**：按模型 + Windows 域账号 + 租户维度记录日/月/年 Token 消耗，支持模型筛选、排序、账号搜索
- **自动更新**：从网络共享目录检测新版本，确认后自动下载替换并重启
- **单实例保护**：Windows 命名互斥体防止重复启动
- **系统托盘**：右键菜单打开管理界面、检查更新或关闭服务
- **Web 管理界面**：Vue3 SPA，6 个子页面——账号管理、API Key、租户管理、管理员、Token 用量、状态概览

## 架构

```
客户端 (OpenAI / Anthropic 格式)
    │
    ▼
FastAPI 网关 (127.0.0.1:9899)
    │
    ├─ API Key 鉴权 + 租户配额检查
    │
    ├─ Anthropic Messages → OpenAI 格式自动转换
    │   ├─ tool_use/tool_result ↔ function calling
    │   └─ reasoning_content → thinking 块
    │
    ├─ model 含 GLM-V5 ──────→ ZJ aigateway (Bearer API Key)
    │
    └─ model 含 Qwen/MiniMax/Glm ──→ Copilot HDP (IAM Token + 账号池)
                                       ├─ 负载均衡 (最少在飞优先)
                                       ├─ 账号级重试 (同模型换账号)
                                       └─ 健康退化 (自动隔离/恢复)
```

SQL Server 存储账号池、API Key、租户、Token 用量等，所有用户共享。

## 项目结构

```
├── entry.py              # PyInstaller 入口
├── main.py               # CLI 入口
├── copilot_proxy.spec    # PyInstaller 打包配置（含自动版本号生成）
├── build.bat             # 一键打包（含前端构建）
├── pyproject.toml        # 项目依赖
├── .env                  # 运行配置（打包时内嵌进 exe）
│
├── backend/
│   ├── __init__.py       # __version__ = "1.9.0"（版本号唯一定义处）
│   ├── config.py         # .env 配置读取 + 常量
│   ├── db.py             # SQL Server 数据库层
│   ├── server.py         # FastAPI 路由 + 鉴权 + 分流
│   ├── proxy_core.py     # 上游请求（SSE 流式/非流式透传）
│   ├── admin_api.py      # 管理 API 端点
│   ├── tray.py           # 系统托盘图标 + 版本显示 + 检查更新菜单
│   └── updater.py        # 自动更新模块（检查/下载/替换/重启）
│
├── frontend/
│   ├── package.json      # Vue3 + Vite5 依赖
│   ├── vite.config.js    # Vite 配置（base + dev proxy）
│   ├── index.html        # Vite 入口 HTML
│   └── src/
│       ├── main.js       # Vue3 挂载
│       ├── style.css     # 全局样式
│       ├── api.js        # fetch 封装 + 工具函数
│       ├── App.vue       # 主组件（鉴权 + Tabs 路由）
│       └── components/
│           ├── Modal.vue           # 可复用弹窗
│           ├── StatsGrid.vue       # 状态统计卡片
│           ├── AccountsPanel.vue   # 账号管理
│           ├── KeysPanel.vue       # API Key 管理
│           ├── TenantsPanel.vue    # 租户管理
│           ├── AdminsPanel.vue     # 管理员管理
│           └── TokenUsagePanel.vue # Token 用量（模型筛选/排序/搜索）
│
└── driver/
    └── msodbcsql18_*.msi # ODBC 驱动（首次运行自动安装）
```

## 快速开始

开发文档入口：[docs/README.md](./docs/README.md)。

实际配置仅保存在本机 `.env`，可从 [.env.example](./.env.example) 复制后填写。
使用 `SEED_ACCOUNTS_JSON` 配置上游账号，格式为账号 ID 与显示名的二维 JSON 数组，默认不添加账号。
`DEFAULT_API_KEY` 非空时才创建默认 Key。真实账号、内网地址和密钥不得提交到仓库。
ODBC 安装包不随仓库上传，安装方式见 [driver/README.md](./driver/README.md)。

### 1. 配置 .env

```env
# 数据库
DB_SERVER=localhost,1433
DB_DATABASE=gateway_dev
DB_UID=gateway_user
DB_PWD="your_password"

# Copilot 上游
COPILOT_API_URL=https://copilot.example.invalid/v1/chat/completions
TOKEN_API_URL=https://iam.example.invalid/token
APP_ID=com.example.gateway
UPSTREAM_AUTH_TYPE=dynamic_token

# ZJ 上游
ZJ_API_URL=https://zj.example.invalid/v1/chat/completions
ZJ_API_KEY=sk-xxxxxxxx

# 服务
LOCAL_PORT=9899
UPSTREAM_TIMEOUT=300
TOKEN_CACHE_TTL=240

# 默认 API Key（首次启动种子数据，留空则不创建种子密钥）
DEFAULT_API_KEY=

# 自动更新（留空则禁用）
UPDATE_SHARE_PATH=
UPDATE_CHECK_INTERVAL_HOURS=24
```

### 2. 启动

```bash
# 后端
uv sync --extra build      # 创建 .venv 并安装运行和打包依赖
uv run python main.py      # 托盘模式
uv run python main.py --no-tray          # 无托盘模式
uv run python main.py --port 9000        # 指定端口
uv run python main.py --skip-update      # 跳过更新检查

# 前端开发（可选，用于调试）
cd frontend
npm install
npm run dev                # Vite dev server，自动代理到后端 9899
```

### 3. 首次配置

1. 启动后打开 `http://127.0.0.1:9899/admin/`
2. 如当前 Windows 用户不在管理员名单中，需先通过数据库添加：
   ```sql
   INSERT INTO admin_users (domain_account, display_name) VALUES ('域账号', '姓名');
   ```
3. 在管理界面中配置账号、创建租户、分发 API Key

## 客户端配置

| 配置项 | 值 |
|--------|-----|
| Base URL | `http://127.0.0.1:9899/v1` |
| API Key | 管理员在 Web UI 中创建的 Key |
| Model | `Qwen3.6-27B-VL` / `MiniMax-M2.7` / `Glm-5.1` / `GLM-V5` |

## API 端点

### 客户端接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI Chat Completions |
| `/v1/messages` | POST | Anthropic Messages |
| `/messages` | POST | Anthropic Messages（无前缀兼容） |
| `/v1/models` | GET | 可用模型列表 |
| `/health` | GET | 健康检查（无需鉴权） |

### 管理接口（仅 127.0.0.1，需管理员域账号）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/admin/auth/check` | GET | 检查当前用户是否管理员 |
| `/admin/status` | GET | 网关状态概览 |
| `/admin/accounts` | GET | 账号列表及健康状态 |
| `/admin/accounts/{id}/reset-health` | POST | 重置账号健康状态 |
| `/admin/accounts/reset-counts` | POST | 重置所有账号在飞计数 |
| `/admin/keys` | GET/POST | 查看/创建 API Key |
| `/admin/keys/{id}` | DELETE | 吊销 Key |
| `/admin/keys/{id}/rotate` | POST | 轮换 Key |
| `/admin/tenants` | GET/POST | 查看/创建租户 |
| `/admin/tenants/{id}` | PUT/DELETE | 更新租户配额和模型权限 / 删除租户 |
| `/admin/admins` | GET/POST/DELETE | 管理员名单 |
| `/admin/token-usage` | GET | Token 用量统计（支持 `?model=xxx` 筛选） |

## 打包

```bash
# 方式一：build.bat（自动构建前端 + 打包 exe）
build.bat

# 方式二：手动步骤
cd frontend && npm install && npm run build && cd ..
uv run --extra build pyinstaller copilot_proxy.spec --noconfirm --clean
```

产物：`dist/LLM-API-Gateway.exe`（单文件）

- `.env` 和 `driver/` 自动内嵌进 exe，无需额外文件
- 前端构建产物 `frontend/dist/` 内嵌进 exe，由 FastAPI StaticFiles 托管
- 版本号自动从 `backend/__init__.py` 的 `__version__` 读取并通过 `file_version_info.txt` 嵌入 exe 的 Windows FileVersion 资源
- 目标机器无需预装 ODBC 驱动（首次运行自动安装）

### 发版流程

1. 修改 `backend/__init__.py` 中的 `__version__`
2. 打包：`build.bat` 或手动前端构建 + pyinstaller
3. 将 `dist/LLM-API-Gateway.exe` 放到网络共享目录

## 自动更新

1. 在 `.env` 中配置 `UPDATE_SHARE_PATH=\\server\share`（留空则禁用）
2. 在共享目录放置新版 `LLM-API-Gateway.exe`（版本号从 exe 的 FileVersion 资源自动读取，无需额外文件）
3. 启动时立即检查 + 每 24 小时周期检查 + 托盘菜单"检查更新"手动触发
4. 检测到新版本 → 弹窗显示版本对比 → 确认下载 → 确认替换 → 自动重启
5. 已是最新版本时，手动检查会弹窗提示当前版本号
6. 更新流程：当前 exe → `.exe.old`，新 exe 拷贝到原位 → 停服务 → 释放互斥体 → 启动新进程 → 强制退出旧进程
7. 新版本启动成功后写入标记文件，下次启动时自动清理 `.exe.old`

## 权限管理流程

```
用户请求 → API Key 鉴权 → 查 api_keys 表验证 Key 有效
                         → 查关联租户状态和配额
                         → 返回 TenantContext(tenant_id, name, allowed_models, quota)
                         → 检查 allowed_models 是否包含请求模型
                         → 检查小时级配额是否超限
                         → 按 model 决定上游路由
```

- **管理员**：Windows 域账号 → admins 表校验，仅 127.0.0.1 可访问管理 API
- **租户**：独立配额 + 模型权限，API Key 绑定租户
- **路由**：由 model 名称决定，与租户无关（GLM-V5 → ZJ，其余 → Copilot）
