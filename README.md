# 基于 LangGraph 的约束旅行规划系统 🌍✈️

一个采用“双层规划 + 真实工具执行 + 失败反馈”架构的旅行规划项目。系统不会让 LLM 直接编写整份行程，而是先收集可追溯的真实数据，再由 OR-Tools 在硬约束下生成时间轴。

## ✨ 功能特点

- 🧠 **双层规划**: Task Planner 决定需要收集什么数据，Itinerary Planner 解决真正的时间与路线安排
- 🛠️ **真实工具执行**: Executor 默认调用高德官方 REST API；兼容环境中可显式启用高德 MCP
- 💰 **可追溯价格**: 项目自有 Price MCP Server 提供门票、酒店、餐饮搜索和来源正文提取工具
- 💡 **有依据的建议**: Advice Generator 只读取结构化事实，Grounder 拒绝无引用或凭空增加金额的建议
- 🧮 **约束优化**: OR-Tools CP-SAT 处理开放时段、时间冲突、交通、用餐、必去和禁止条件
- 🔁 **校验与重规划**: Validator 独立检查结果；失败时补查证据、调整行程或明确返回不可行
- 🔎 **证据可追溯**: POI、路线、天气和价格都保存来源、获取时间与可信状态，未知价格不会伪装成 0 元
- 💾 **服务端持久化**: 原始请求、最终计划、编辑修订和父版本关系保存到 SQLite，不依赖浏览器缓存
- 📈 **真实工作流进度**: 前端展示 LangGraph 已完成的节点阶段，不再使用定时器模拟百分比
- ✏️ **编辑后重规划**: 删除景点或补充要求会创建新修订，并重新执行工具、约束求解和 Validator
- 🎨 **现代化前端**: Vue3 + TypeScript + Vite,响应式设计,流畅的用户体验
- 📱 **完整功能**: 包含住宿、交通、餐饮和景点游览时间推荐

## 🏗️ 技术栈

### 后端
- **工作流**: LangGraph
- **约束求解**: Google OR-Tools CP-SAT
- **API**: FastAPI
- **数据工具**: amap-mcp-server + 高德 Web 服务 REST API
- **价格工具**: FastMCP + Tavily Search/Extract（价格始终携带来源与可信状态）
- **LLM分工**: 只负责兴趣语义评分，不决定时间、路线或事实数据

### 前端
- **框架**: Vue 3 + TypeScript
- **构建工具**: Vite
- **UI组件库**: Ant Design Vue
- **地图服务**: 高德地图 JavaScript API
- **HTTP客户端**: Axios

## 📁 项目结构

```
my-trip-planner/
├── backend/                    # 后端服务
│   ├── app/
│   │   ├── graph/             # LangGraph状态、节点与图构建
│   │   ├── planning/          # 任务规划、执行、优化、校验和重规划
│   │   ├── api/               # FastAPI路由
│   │   │   ├── main.py
│   │   │   └── routes/
│   │   │       ├── trip.py
│   │   │       └── map.py
│   │   ├── services/          # 高德结构化服务与确定性解析器
│   │   ├── models/            # API模型与规划领域模型
│   │   └── config.py          # 配置管理
│   ├── requirements.txt
│   ├── .env.example
│   └── .gitignore
├── frontend/                   # 前端应用
│   ├── src/
│   │   ├── components/        # Vue组件
│   │   ├── services/          # API服务
│   │   ├── types/             # TypeScript类型
│   │   └── views/             # 页面视图
│   ├── package.json
│   └── vite.config.ts
└── README.md
```

## 🚀 快速开始

### 前提条件

- Python 3.10+
- Node.js 16+
- 高德地图 API 密钥（Web 服务 Key、Web 端 JS API Key 及配套 securityJsCode）
- LLM API密钥 (OpenAI/DeepSeek等)

### 后端安装

1. 进入后端目录
```bash
cd backend
```

2. 创建虚拟环境
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

3. 安装依赖
```bash
pip install -r requirements.txt
```

4. 配置环境变量
```bash
cp .env.example .env
# 编辑.env文件,填入你的API密钥
```

5. 启动后端服务
```bash
uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8000
```

### 前端安装

1. 进入前端目录
```bash
cd frontend
```

2. 安装依赖
```bash
npm install
```

3. 配置环境变量
```bash
# 创建 .env 文件，填入高德 Web API Key、JS API Key 和 securityJsCode
cp .env.example .env
```

4. 启动开发服务器
```bash
npm run dev
```

5. 打开浏览器访问 `http://localhost:5173`

## 📝 使用指南

1. 在首页填写旅行信息:
   - 目的地城市
   - 旅行日期和天数
   - 交通方式偏好
   - 住宿偏好
   - 旅行风格标签

2. 点击"生成旅行计划"按钮

3. 系统将:
   - 把用户输入转换为显式规划约束
   - 动态生成并并行执行 POI、天气、酒店和路线查询任务
   - 检查开放时间与路线证据是否足够，不足时定向补查
   - 使用 CP-SAT 生成包含交通和用餐的每日时间轴
   - 独立校验所有硬约束，失败时重规划或返回明确限制
   - 按真实节点更新任务进度，并把通过校验的结果保存到后端

4. 查看结果:
   - 每日详细行程
   - 景点信息与地图标记
   - 交通路线规划
   - 天气预报
   - 餐饮推荐
   - 使用带 `plan_id` 的结果地址刷新或再次打开计划
   - 调整要求后生成带父版本关系的新修订

### 计划任务与持久化 API

```text
POST /api/trip/jobs                    提交规划任务
GET  /api/trip/jobs/{job_id}           查询真实 LangGraph 阶段
GET  /api/trip/plans/{plan_id}         读取后端保存的计划
POST /api/trip/plans/{plan_id}/replan  基于新约束创建修订并重算
```

本地开发默认数据库为 `backend/data/trip_planner.db`。数据库文件已被
`.gitignore` 忽略，不会把用户行程提交到 Git。

## 🔧 核心实现

### LangGraph 主流程

```text
START → parse_request → task_planner
      → execute_task（Send 并行）→ aggregate_evidence → check_evidence
      → itinerary_planner（兴趣评分 + CP-SAT）→ validate_plan
      → calculate_budget → generate_advice → ground_advice
      → format_response → END

证据不足：check_evidence → task_planner
校验失败：validate_plan → replanner → 补证据 / 重新排程 / 明确失败
```

### LLM 与算法的边界

- LLM：理解偏好、对候选景点给分，并在白名单事实范围内组织旅行建议；失败时自动降级为规则建议。
- Python/OR-Tools：检查证据、计算交通与时间冲突、安排活动、控制预算惩罚并验证硬约束。
- 外部数据：默认使用高德官方 REST；`AMAP_MCP_ENABLED=true` 时启用 MCP 优先、REST 回退。Price MCP 负责实时价格取证；未经核实的字段保持 `unknown`。
- 价格安全：只有 `.gov.cn` 或 `PRICE_TRUSTED_DOMAINS` 白名单来源的单一明确票价可标记为 `verified`；酒店、餐饮和其他来源只作为估算范围。

### 价格 MCP 配置

在 `backend/.env` 增加：

```env
TAVILY_API_KEY=你的_Tavily_API_Key  # 可选；推荐配置
# 可选：人工确认的景区官方域名，多个域名用英文逗号分隔
PRICE_TRUSTED_DOMAINS=official-scenic.example.cn
```

未配置 `TAVILY_API_KEY` 时，Price MCP 会使用 Tavily 的 keyless 搜索/提取模式；如果限额或检索失败，行程规划仍可执行，但相关价格保持未知，预算明确标记为不完整。不要为了显示预算而填入模型猜测值。

### 测试

```bash
cd backend
python -m pytest -q
```

## 📄 API文档

启动后端服务后,访问 `http://localhost:8000/docs` 查看完整的API文档。

主要端点:
- `POST /api/trip/plan` - 生成旅行计划
- `GET /api/map/poi` - 搜索POI
- `GET /api/map/weather` - 查询天气
- `POST /api/map/route` - 规划路线

## 🤝 贡献指南

欢迎提交Pull Request或Issue!

## 📜 开源协议

CC BY-NC-SA 4.0

## 🙏 致谢

- [HelloAgents](https://github.com/datawhalechina/Hello-Agents) - 智能体教程
- [HelloAgents框架](https://github.com/jjyaoao/HelloAgents) - 智能体框架
- [高德地图开放平台](https://lbs.amap.com/) - 地图服务
- [amap-mcp-server](https://github.com/sugarforever/amap-mcp-server) - 高德地图MCP服务器

---

**LangGraph 约束旅行规划系统** - 用真实证据生成可执行行程 🌈
