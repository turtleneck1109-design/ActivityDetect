# AI 总结昨天工作

这个功能会把本地生成的 `work_log_YYYY-MM-DD.txt` 发给 SJTU 兼容 OpenAI 的接口，总结昨天的工作内容，并保存为：

```text
data/ai_summary_YYYY-MM-DD.txt
```

## 配置 API key

任选一种方式：

```powershell
$env:SJTU_API_KEY="你的 API key"
```

或新建：

```text
data\sjtu_api_key.txt
```

文件里只放 API key 本身。`data` 目录已被 `.gitignore` 忽略，不会进入仓库。

## 手动生成

```powershell
python work_tracker.py summary --day yesterday
```

或双击：

```text
generate_yesterday_summary.bat
```

重新生成已有总结：

```powershell
python work_tracker.py summary --day yesterday --force
```

## 自动生成

后台记录默认每天 `08:30` 自动总结前一天工作：

```powershell
python work_tracker.py run
```

调整自动总结时间：

```powershell
python work_tracker.py run --daily-summary-time 09:00
```

关闭自动总结：

```powershell
python work_tracker.py run --daily-summary-time off
```

补生成历史缺失 AI 总结：

```powershell
python work_tracker.py backfill-summaries
```
