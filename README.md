# A 股多股票智能分析系统

> 基于 [AKQuant](https://github.com/akfamily/akquant) + [AKShare](https://github.com/akfamily/akshare) 的 A 股量化分析系统，每日 5 个时段对监控池中的 **6 只蓝筹/白马/科技股** 自动进行**基本面 + 技术面 + 资金面**全维度分析，**合并产出**一份 Markdown 报告。

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
- 报告自动推送到 IMA 笔记（每轮每个 slot 一篇），可在 IMA 客户端搜索 `cn-stock-analyzer` 查看历史
- **6 只股票合并一份报告**：`reports/latest_{slot}.md` 用 `## 📊 {name}` 分隔
- **监控池**：中国神华 / 长江电力 / 北方华创 / 中国银行 / 中远海能 / 长电科技

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

#### 配置 IMA 笔记推送（必做，否则 step 失败）

报告通过 [腾讯 IMA 开放 API](https://ima.qq.com) 推送到你的 IMA 笔记，不写入 git 仓库。
在 **GitHub 仓库 → Settings → Secrets and variables → Actions** 新增两个 secret：

| Secret 名称 | 值 |
|---|---|
| `IMA_CLIENT_ID` | IMA 控制台拿到的 client id |
| `IMA_API_KEY`  | IMA 控制台拿到的 api key |

> 这两个值**只存在 GitHub secret**里，不入代码、也不入 commit。
> Action 通过 `secrets.IMA_CLIENT_ID` / `secrets.IMA_API_KEY` 注入到 `scripts/ima_upload.py` 的环境变量里。

推送逻辑：每个 `reports/latest_{slot}.md` 作为一篇新笔记，标题形如 `cn-stock-analyzer post_close 2026-08-22 15:10`。`--slot all` 跑 5 轮会推 5 篇。

如果只想本地测试，可设置环境变量后手动运行：

```bash
export IMA_CLIENT_ID=xxx
export IMA_API_KEY=yyy
python3 scripts/ima_upload.py reports/latest_post_close.md
```

### 性能数据（典型 run）

| 阶段 | 耗时 |
|---|---|
| Runner 启动 + checkout | ~10s |
| pip 装依赖（命中 cache）| ~3s |
| akshare/akquant 启动 | ~1s |
| 拉数据（cache 全命中 + 腾讯直连实时）| ~2s |
| 报告生成 | ~5s |
| 推送到 IMA 笔记 | ~3s |
| **总耗时** | **~24s** |

腾讯直连作为首选数据源（单 symbol 而非全市场 5000+ 股票），避开了 akshare `stock_zh_a_spot_em` 在 GH Action 上 ~90s 的全市场拉取。

### 依赖管理

- **Python 3.13** LTS（避免 3.14 太新生态未稳）
- **Dependabot** 自动监控依赖和 GitHub Actions 版本（配置：`.github/dependabot.yml`）
  - 每周一 04:00 (Asia/Shanghai) 检查 pip + github-actions 升级
  - 依赖按组分组（pandas+numpy / http / akshare），减少 PR 数量
  - Security alerts + auto security fixes 已开启，有 CVE 自动修

---

> ⚠️ 当前为简化实现，未做 A 股节假日判断。所有 cron 都会触发，由脚本内抓取失败 / 当日无数据时优雅降级。如需精准判断交易日，可用 [chinaholiday](https://pypi.org/project/chinaholiday/) 之类包做前置检查。

也可以在 Actions 页面手动 **Run workflow** 指定时段。

---

## 📁 目录结构

```
.
├── .github/workflows/analyze.yml   # GitHub Actions
├── src/
│   ├── analyzer.py                 # 主入口
│   ├── cache.py                    # SQLite 缓存层
│   ├── config.py                   # 配置（股票/时段）
│   ├── data_fetcher.py             # akshare 拉数据（带 cache 兜底）
│   ├── indicators.py               # akquant 技术指标
│   └── report_generator.py         # 报告生成
├── reports/                        # 历史报告 (Markdown + JSON)
├── data_cache.sqlite               # 运行时缓存 (gitignore, 通过 actions/cache 跨 run 复用)
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

## 🗃️ 数据缓存

为避免 akshare/东财接口被反爬封锁，4 类可缓存数据走 SQLite 缓存层 (`src/cache.py`)，跨 GitHub Actions run 通过 `actions/cache@v4` 持久化：

| 数据 | TTL | 说明 |
|---|---|---|
| 历史 K 线 | 6 小时 | 每天变动极少，1 天抓 1-2 次足够 |
| 板块指数（煤炭/上证） | 30 分钟 | 盘中变化快，但 5 个时段内可复用 |
| 个股基本信息 | 1 周 | 上市日期/总股本等基本不变 |
| 资金流 | 6 小时 | 收盘后基本不变 |

**实时行情不缓存**（每个时段必须拉取最新值）。  
**财务数据**由于 akshare 接口在容器里基本都连不上，目前全部 fallback 到 N/A 跳过；等找到稳定源再接 cache。

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
