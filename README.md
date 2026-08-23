# 香港身份证预约名额监控（5分钟）

当前版本按“日期区间”监控。

你的建议配置：

- `TARGET_START = 2026-09-25`
- `TARGET_END = 2026-10-08`

只有这个区间（含首尾）出现：

- 已满 → 少量
- 已满 → 充足
- 不开放 → 少量/充足

才会 Bark 推送。其他日期完全忽略。

## GitHub 配置

### Secret

仓库：

`Settings → Secrets and variables → Actions → Secrets`

创建：

- `BARK_KEY` = Bark App 里的设备 Key

### Variables

创建：

- `TARGET_START` = `2026-09-25`
- `TARGET_END` = `2026-10-08`

可选：

- `OFFICES`：不填 = 六个办事处全部监控
- `BOOKING_URL`：不填 = 点击通知打开官方配额预览页

## 首次运行

`Actions → HKID quota monitor → Run workflow`

首次运行只建立基线，日志应出现：

`BASELINE_CREATED`

之后约每 5 分钟自动检查。

## 通知逻辑

只对“新出现”的目标区间名额提醒，不会因为某一天持续显示少量而每 5 分钟重复通知。

## 文件

- `monitor.py`：主程序
- `.github/workflows/monitor.yml`：5分钟 GitHub Actions
- `.data/state.json`：上一次官方状态
- `.data/history.jsonl`：发现过的目标放号历史
