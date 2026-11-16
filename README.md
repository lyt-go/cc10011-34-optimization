# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 / 推送批次 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 订阅推送的合并口径

同一接收人同时订阅日报、告警摘要、洞察快报时，多频率排期会被合并成批次，不再各自发一条：

- **合并维度**：接收人 + 推送渠道 + 推送频率 + 原定推送时间。同一频率同一排期上的多个
  订阅内容合成一个批次、一条消息、一条 `subscription_push` 记录。
- **数据集**：日报、告警摘要、洞察快报分别来自 `realtimemetric` / `alarmhistory` /
  `userinsight`；洞察部分与用户洞察页共用同一份快照（`GET /api/userinsight/snapshot`），
  报告发布或归档时快照统一失效重建，推送记录里的 `洞察快照版本` 可反查依据。
- **静默**：按原定推送时间和实际投递时刻共同判定，命中后进入“静默暂存”；静默结束时
  同一接收人同一渠道的暂存批次合并成一次投递，主批次之外的其他批次记为“已合并投递”并
  通过 `合并主批次` 指回主投递，每个批次仍各有一条可追溯结果。补推时静默已结束的批次
  同样走暂存再释放，不会在静默结束瞬间集中轰炸。
- **优先级权衡**：批次含重要告警时，重要告警优先于静默与日报，突破静默立即投递，同批
  日报内容随批送达；裁决结果写在 `优先级裁决` / `静默判定` 字段。非重要告警时静默优先。
- **下次推送时间**：始终沿原排期锚点前进（如每日 09:00 在 09:07 投递后仍是次日 09:00），
  不随实际投递时间漂移；暂停时按“原计划与实际暂停时间较晚者”顺延一个完整周期，启用时
  不重置已顺延的排期。排期变化记入订阅配置的 `排期变更记录`。
- **幂等与状态**：批次号由业务键确定性生成，“查重 + 落库 + 推进状态 + 推进排期”在同一
  临界区内完成，同一业务批次并发到达只落一条完整记录；状态只能沿
  `待合并 → 已合并 → 静默暂存 → 投递中 → 已投递/已合并投递/投递失败` 向前推进，重复推进
  同一阶段不重复写轨迹。

相关接口：`POST /api/subscription/push`（合并推送，可用 `at` 补推指定时刻）、
`GET /api/subscription/push-records`、`GET /api/subscription/push-records/{批次号}`、
`GET /api/subscription/timeline`（时间线 + 失败概览）、`GET /api/subscription/datasets`。

领域：数据工程
子方向：数据分析与数据应用
