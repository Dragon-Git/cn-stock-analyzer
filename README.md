# 中国神华 (601088) 智能分析系统

> 基于 [AKQuant](https://github.com/akfamily/akquant) + [AKShare](https://github.com/akfamily/akshare) 的 A 股量化分析系统，每日 5 个时段自动对中国神华（601088）进行**基本面 + 技术面 + 资金面**全维度分析并产出 Markdown 报告。

---

## ✨ 功能

- **5 时段自动分析**（A 股交易日，北京时间）
  | 时段 | 触发时间 | 报告侧重 |
  |---|---|---|
  | 盘前 | 08:00 | 隔夜外盘、昨日复盘、关键位预判 |
  | 竞价结束 | 09:35 | 集合竞价结果、开盘缺口、早盘策略 |
  | 午间 | 11:35 | 上午 K 线、量能、午后预判 |
  | 收盘后 | 15:10 | 全日 K 线、MACD/RSI/布林全天态 |
  | 盘后深度 | 15:35 | 资金流向、估值与基本面综合 |
- **技术面**（全部用 AKQuant 内置 102 个指标）：
  趋势（MA/EMA/MACD）、震荡（RSI/KDJ/布林/CCI/WR）、波动（ATR/ADX）、量能（OBV/MFI）、动量（ROC/MOM/TRIX/BIAS）
- **基本面**：滚动 PE/PB/PS、ROE、毛利率、净利率、营收/净利同比、资产负债率
- **资金面**：主力/超大单/大单/中单/小单净流入、北向资金（估算）、融资余额
- 报告自动 commit 到 `reports/`，可通过 README 入口跳转

---

## 🚀 快速开始

### 本地运行

```bash
# 安装依赖（注意 akshare 依赖较多系统库）
pip install -r requirements.txt

# 列出所有时段
python -m src.analyzer --list

# 跑单个时段
python -m src.analyzer --slot post_close

# 跑全部 5 个时段
python -m src.analyzer --slot all
```

### GitHub Actions 自动运行

workflow 已在 `.github/workflows/analyze.yml` 配置好 5 个 cron：

| 时段 | cron (UTC) | 北京时间 |
|---|---|---|
| 盘前 | `0 0 * * 1-5` | 08:00 |
| 竞价结束 | `35 1 * * 1-5` | 09:35 |
| 午间 | `35 3 * * 1-5` | 11:35 |
| 收盘后 | `10 7 * * 1-5` | 15:10 |
| 盘后深度 | `35 7 * * 1-5` | 15:35 |

> ⚠️ 当前为简化实现，未做 A 股节假日判断。所有 cron 都会触发，由脚本内抓取失败 / 当日无数据时优雅降级。如需精准判断交易日，可用 [chinaholiday](https://pypi.org/project/chinaholiday/) 之类包做前置检查。

也可以在 Actions 页面手动 **Run workflow** 指定时段。

---

## 📁 目录结构

```
.
├── .github/workflows/analyze.yml   # GitHub Actions
├── src/
│   ├── analyzer.py                 # 主入口
│   ├── config.py                   # 配置（股票/时段）
│   ├── data_fetcher.py             # akshare 拉数据
│   ├── indicators.py               # akquant 技术指标
│   └── report_generator.py         # 报告生成
├── reports/                        # 历史报告 (Markdown + JSON)
├── requirements.txt
└── README.md
```

---

## 📊 报告样例

报告保存到 `reports/YYYYMMDD_slot_HHMMSS.md`，并同步覆盖 `latest_<slot>.md`。报告结构：

1. **行情快照** — 价格、量能、估值快照
2. **技术面** — 5 大类指标 + 智能解读
3. **最近 K 线** — 10 日 K 线表
4. **基本面** — 财务摘要 + 同比
5. **资金面** — 主力/北向/融资
6. **综合研判** — 时段定制的简短结论

---

## 🔧 自定义

### 改股票

编辑 `src/config.py` 的 `SHENHUA` 配置（或新增多个 `StockConfig`），并在 `analyzer.py` 调整调用。

### 改时段

编辑 `src/config.py` 的 `TIME_SLOTS` 列表，并在 `.github/workflows/analyze.yml` 加对应 cron。

### 加新指标

所有指标在 `src/indicators.py` 中独立函数，添加新指标只需：

1. 调用 `akquant.XXX(_to_1d(close), n)` 等
2. 加入 `compute_all()` 的对应分组
3. 报告生成器自动展示（在 `_section_technical` 中）

---

## ⚠️ 免责声明

本项目仅供量化研究与学习使用。报告由算法自动生成，**不构成任何投资建议**。  
市场有风险，投资需谨慎。
