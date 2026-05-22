# Add PID AI Serial Dashboard

## Goal

让 `pid-ai-serial` skill 不只会识别和读取板端串口，还能一键打开本地上位机，在浏览器里查看 PID 实时波形、状态与参数设置入口。

## Requirements

| 编号 | 需求 |
|---|---|
| R1 | 提供本地 Web 上位机入口，启动后监听 `127.0.0.1`，可自动选择空闲 HTTP 端口。 |
| R2 | 支持自动识别 PID AI 板端串口，复用现有串口扫描和协议解析逻辑。 |
| R3 | UI 第一屏就是可操作的调参工作台，不做 landing page。 |
| R4 | Canvas 实时显示 `target`、`feedback`、`error`、`p_out`、`i_out`、`d_out`、`out_limited`、`actuator` 等曲线，并允许勾选显示。 |
| R5 | 明确显示 `sat`、`anti_windup`、`mode`、`enable`、`sensor_ok`、`fault` 等安全状态。 |
| R6 | 参数设置只能由用户点击触发，发送前展示精确 `{CMD}` 字符串。 |
| R7 | 命令发送后必须进入历史记录，只有收到 `{ACK}` 才能视为确认；收到 `{ERR}` 或超时要保留错误信息。 |
| R8 | 上位机无真实板端时也能打开，串口扫描失败时 UI 不崩溃并显示原因。 |

## Acceptance Criteria

| 编号 | 验收条件 |
|---|---|
| A1 | `python .\.codex\skills\pid-ai-serial\scripts\pid_ai_dashboard.py --open --auto` 可作为 skill 调用入口。 |
| A2 | `/api/ports`、`/api/status`、`/api/samples`、`/api/connect`、`/api/disconnect`、`/api/command` 可返回结构化 JSON。 |
| A3 | 遥测缓冲区有上限，长时间运行不会无限增长。 |
| A4 | ACK/ERR 可以更新对应命令历史，UI 不在 ACK 前把参数标为已生效。 |
| A5 | 无板端环境下，本地 Web 页面仍可访问，自动连接失败时显示状态。 |
| A6 | 新增 Python 行为测试覆盖缓冲、解析、命令历史和基础状态快照。 |

## Technical Notes

| 主题 | 决策 |
|---|---|
| 技术栈 | Python 标准库 HTTP 服务 + 原生 HTML/CSS/JS/Canvas，避免引入 npm/构建工具。 |
| 串口依赖 | 继续复用 `.codex/skills/pid-ai-serial/scripts/pid_ai_serial.py` 的 `get_ports`、`find_best_port`、`open_serial`、`parse_frame`、`normalize_command`。 |
| 状态模型 | Python 侧维护连接状态、最新 `{PID}`、最新 `{CFG}`、有界样本缓冲和命令交易历史。 |
| 安全策略 | 启动、扫描、读取为只读；所有 `{CMD}` 只来自用户点击或显式 API 请求。 |
| 后续扩展 | 未来可把实验记录、AI 诊断、WebSocket 推流拆到 `host/`，当前先保持 skill 内部可复用。 |
