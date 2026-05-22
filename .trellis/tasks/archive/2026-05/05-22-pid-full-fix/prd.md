# PID 全面修复与算法优化

## Goal

修复所有已识别的 bug 并将 PID 算法升级为最优实现，覆盖 C 库算法层、协议层、Python 后端和 Web 前端四个维度。

## Requirements

### C 库算法层（高优先级）

- **R1** dt 归一化：积分项改为 `integral += error * (dt_ms / 1000.f)`，微分项改为 `d_out = kd * d_error / (dt_ms / 1000.f)`，使 ki/kd 的物理单位与时间无关
- **R2** Derivative Kick 修复：D 项改为对 feedback 求导而非对 error 求导，避免 target 阶跃时产生尖峰；需新增 `last_feedback` 字段
- **R3** 积分抗饱和改进：改用 Conditional Integration 策略（仅在输出未饱和时才累积积分），比现有 clamping 策略更稳定

### 协议层

- **R4** `board_example.c` 中 GET_CFG 字符比较改为 `strcmp(result.command, "GET_CFG") == 0`
- **R5** `board_example.c` 补充 GET_STAT 命令的回复逻辑（发送 `{STAT}` 帧或占位回复）

### Python 后端

- **R6** 串口扫描并行化：`scan_ports` 改用 `ThreadPoolExecutor` 并行执行，减少多端口等待时间
- **R7** `ingest_line` 中对同一样本的两次 `json_safe_copy` 合并为一次

### Web 前端

- **R8** Canvas 高 DPI 支持：用 `devicePixelRatio` 缩放 canvas 分辨率，消除高分屏模糊
- **R9** 图表 X 轴改为 `ms` 时间戳，反映真实采样间隔
- **R10** 添加暂停/继续波形按钮，暂停时停止追加新样本到图表
- **R11** `applyCfgToForm` 补充 `target` 字段同步

## Acceptance Criteria

- [ ] C 测试 `test_pid_ai.c` 全部通过（需更新测试以匹配新算法）
- [ ] Python 测试 `test_pid_ai_dashboard.py` 全部通过
- [ ] `pid_ai_serial.py` 和 `pid_ai_dashboard.py` 语法检查通过
- [ ] 高分屏下 Canvas 波形清晰
- [ ] 暂停按钮可正常暂停/继续波形
- [ ] 图表 X 轴显示毫秒时间
- [ ] target 字段在 GET_CFG 后同步到表单

## Definition of Done

- C 测试编译并通过
- Python 单元测试通过
- 语法检查无错误
- 协议文档 `docs/pid_ai_serial_protocol.md` 更新（如有字段变更）
- `include/pid_ai.h` 注释更新（新增 last_feedback 字段）

## Technical Approach

### C 算法变更（破坏性）

新增 `last_feedback` 字段到 `PIDAI_Handle`，用于 D 项计算。
`PIDAI_Reset` 需要同步保留/清零该字段。
`PIDAI_Update` 核心计算改为：

```c
/* D 项对 feedback 求导，避免 target 阶跃尖峰 */
pid->d_error = pid->feedback - pid->last_feedback;
pid->d_out   = -pid->kd * pid->d_error / (dt_ms / 1000.f);

/* 积分归一化到物理时间 */
next_integral = pid->integral + pid->error * (dt_ms / 1000.f);

/* Conditional Integration 抗饱和 */
if (candidate_sat == PIDAI_SAT_NONE || ...) {
    pid->integral = next_integral;
}
```

### 并行扫描

```python
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=8) as ex:
    futures = [ex.submit(scan_port, p, b, ...) for p in ports for b in baud_rates]
    results = sorted([f.result() for f in futures], key=lambda r: r.score, reverse=True)
```

## Decision (ADR-lite)

**Context**: D 项求导方式和 dt 归一化是破坏性变更，会改变现有 kp/ki/kd 的物理含义。

**Decision**: 采用物理时间归一化 + feedback 求导，这是工业 PID 标准实现。

**Consequences**: 现有板端配置的 ki/kd 数值需要按 `dt_s` 重新标定（例如原来 ki=0.5 在 10ms 周期下等价于新 ki=50）。在文档中说明迁移方法。

## Out of Scope

- SSE 推送替换轮询（可后续优化）
- 数据记录到文件功能
- 二进制协议
- 多 PID 实例管理

## Technical Notes

- 核心文件：`src/pid_ai.c`, `include/pid_ai.h`, `src/pid_ai_protocol.c`, `examples/pid_ai_board_example.c`
- 测试文件：`tests/test_pid_ai.c`
- Python 文件：`.codex/skills/pid-ai-serial/scripts/pid_ai_serial.py`, `pid_ai_dashboard.py`
- Python 测试：`.codex/skills/pid-ai-serial/tests/test_pid_ai_dashboard.py`
- `PIDAI_Handle` 新增 `last_feedback` 字段后，`{PID}` 遥测帧字段顺序不变（last_feedback 是内部字段，不上传）
