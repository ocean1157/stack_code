# 东方财富热度池量化信号系统

这是一个可审计的研究级 MVP：读取东方财富实时人气前 50，下载 Yahoo Finance 企业行动复权日线，构造只使用当时可见数据的技术/风险特征，用正则化逻辑回归预测下一交易日开盘到收盘方向，并在严格按时间划分的验证集上选择阈值，在未参与选择的测试集上报告结果。每行数据都记录来源字段。

## 运行

项目唯一总入口是根目录 `main.py`。直接运行不带参数时，仅启动只读 Web 服务：

```powershell
& "C:\Users\55055\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" main.py
```

浏览器访问 `http://127.0.0.1:8765/`。该默认动作不会采集数据、训练模型或写入数据库。

以下命令会显式执行数据采集/模型计算并写入 PostgreSQL：

```powershell
$env:PATH = "E:\postgresql\bin;$env:PATH"
& "C:\Users\55055\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" main.py run
```

交易时段增量刷新一次：

```powershell
& "C:\Users\55055\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" main.py refresh
```

`refresh` 仅在工作日 09:30–11:30、13:00–15:00 执行。信号动作变化、新入榜或移出榜时调用 Windows 消息弹窗，并将记录追加到 `data/notifications.log`。`--force` 仅用于人工立即核验。

启动本地 Web 报表：

```powershell
& "C:\Users\55055\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" main.py web --port 8765
```

PyCharm 可直接选择项目自带的 `Quant Web 8765` 运行配置。数据导入过程中出现的 `staging_*` 是临时中转表；代码只清理该临时表，不删除业务表。

每日抓取近三天的公开行业研报元数据、生成可审计分析并写入 PostgreSQL：

```powershell
& "C:\Users\55055\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" main.py research
```

研报中心保存公开标题、券商、行业、评级、作者、日期和原文链接；分析表保存情绪评分、观点、主题、催化剂、风险及摘要，不复制研报全文。

浏览器访问 `http://127.0.0.1:8765`。页面直接读取 PostgreSQL，不依赖 CSV 作为报表事实来源。

输出：

- `reports/latest_run.json`：样本外指标、成本假设、切分日期
- `reports/latest_signals.csv`：当前 BUY / HOLD / SELL 与置信度
- `reports/test_equity.csv`：严格样本外净值
- PostgreSQL：`universe_snapshots`、`bars_daily`、`model_runs`、`signals`

## 重要口径

- 信号在收盘后形成，假设下一交易日开盘执行；不会使用未来数据。
- 默认每次交易扣 15bp，包含佣金、滑点和卖出印花税的保守近似；实际成本应按账户校准。
- 热度榜只决定“今天研究谁”，不会伪装成历史上一直知道今天的前 50。
- SELL 表示模型对下一日表现偏弱，并不是自动下单。系统没有接入券商交易接口。
- 日线模型不是盘中毫秒级择时器。要实现盘中实时信号，还需要合规、稳定的逐笔/分钟数据和纸面交易验证。

## 研究依据

实现强调时间序列切分、交易成本、波动风险与样本外检验。任何单次回测都可能过拟合，因此每次运行都保存模型选择和测试结果，后续应以滚动纸面交易的稳定性作为上线门槛。
