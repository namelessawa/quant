# WorldQuant BRAIN Alpha 自动回测与筛选工具

批量提交 Alpha expression 到 WorldQuant BRAIN、轮询回测、提取指标、按阈值与年度稳定性自动筛选，并把结果持久化到 SQLite 与 CSV。支持中断恢复、重复检测、限速重试。

---

## 目录

- [安装](#安装)
- [配置账号](#配置账号)
- [快速开始](#快速开始)
- [命令行参数](#命令行参数)
- [配置文件](#配置文件)
- [断点恢复](#断点恢复)
- [数据库结构](#数据库结构)
- [CSV 输出字段](#csv-输出字段)
- [筛选规则](#筛选规则)
- [排行榜](#排行榜)
- [架构](#架构)
- [WorldQuant 请求流程](#worldquant-请求流程)
- [测试](#测试)
- [已知限制](#已知限制)
- [BRAIN API 验证状态](#brain-api-验证状态)
- [下一步：接入 Alpha 自动生成](#下一步接入-alpha-自动生成)
- [安全与合规](#安全与合规)

---

## 安装

```bash
pip install -r requirements.txt
```

依赖只有三个运行时包：`requests`、`PyYAML`、`python-dotenv`（测试用 `pytest`）。Python 3.10+（代码使用了 `X | None` 类型标注），已在 3.11 上验证。

## 配置账号

凭据不进源码、不进 `config.yaml`、不进日志、不进数据库。支持三种方式，任选其一。

### 方式一：JSON 凭据文件（推荐）

仓库里已经有 `credentials.json`（已被 gitignore），直接填进去即可：

```json
{
  "email": "you@example.com",
  "password": "your-password"
}
```

字段说明：

- `email` —— BRAIN 登录邮箱；也接受 `username` 作为别名（两者同时存在时 `email` 优先）
- `password` —— BRAIN 登录密码，**原样使用不去空格**（密码里的空格可能是有意义的）
- 键名大小写不敏感，多余的键（比如模板里的 `_help`）会被忽略

填完后 Linux / macOS 上建议收紧权限：

```bash
chmod 600 credentials.json
```

也可以放在别处，用 `--credentials` 指定：

```bash
python scripts/run_backtest.py --credentials ~/secrets/brain.json --input data/alphas.csv
```

### 方式二：环境变量

Windows PowerShell：

```powershell
$env:WQBRAIN_USERNAME="you@example.com"
$env:WQBRAIN_PASSWORD="your-password"
```

Windows CMD：

```cmd
set WQBRAIN_USERNAME=you@example.com
set WQBRAIN_PASSWORD=your-password
```

Linux / macOS：

```bash
export WQBRAIN_USERNAME="you@example.com"
export WQBRAIN_PASSWORD="your-password"
```

### 方式三：`.env` 文件

复制 `.env.example` 后填写。`.env` 已在 `.gitignore` 中，真实环境变量优先级高于 `.env`。

### 优先级

多个来源同时存在时，从高到低：

```
--credentials PATH
  > WQBRAIN_CREDENTIALS_FILE
  > WQBRAIN_USERNAME / WQBRAIN_PASSWORD（含 .env）
  > 项目根目录自动发现的 credentials.json
```

显式指定的文件（`--credentials` 或 `WQBRAIN_CREDENTIALS_FILE`）必须可用，出问题直接报错退出；自动发现的 `credentials.json` 属于便利机制，文件不存在或仍是模板占位值时会静默跳过，继续往下找。

### 安全约束

- `credentials.json` 和 `.env` 都在 `.gitignore` 里（`credentials.example.json` 保留跟踪）；
- 仍是模板占位值（`you@example.com` / `your-password` / `changeme` 等）时**拒绝加载**，不会把占位符当密码发给服务端；
- POSIX 下检测到 group/other 可读会告警并提示 `chmod 600`；Windows 下 `st_mode` 表达不了 NTFS ACL，因此改为提示这是明文存储、需自行确认只有当前用户可读；
- 密码不进日志、不进异常信息、不进数据库；JSON 解析失败时只报行列号，不回显文件内容。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `WQBRAIN_USERNAME` | 三选一 | BRAIN 登录邮箱 |
| `WQBRAIN_PASSWORD` | 三选一 | BRAIN 登录密码 |
| `WQBRAIN_CREDENTIALS_FILE` | 三选一 | 指向 JSON 凭据文件；相对路径按项目根解析 |
| `WQBRAIN_BASE_URL` | 否 | API 根地址，默认 `https://api.worldquantbrain.com` |
| `WQBRAIN_DB_PATH` | 否 | SQLite 路径覆盖 |
| `WQBRAIN_LOG_LEVEL` | 否 | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `WQBRAIN_YEARLY_PATH` | 否 | 年度数据 endpoint 模板覆盖，默认已实测可用（见 [BRAIN API 验证状态](#brain-api-验证状态)） |

## 快速开始

### 单个 Alpha

```bash
python scripts/run_backtest.py --expression "rank(ts_delta(close, 5))"
```

### 批量 Alpha

```bash
python scripts/run_backtest.py --input data/alphas.csv --config config.yaml
```

输入文件支持三种格式：

**CSV**（`name` 可省略，省略时按表达式哈希自动生成 ID）：

```csv
name,expression
alpha_001,"rank(ts_delta(close, 5))"
alpha_002,"rank(ts_mean(volume, 20))"
```

CSV 还可以带 per-row 的 settings 覆盖列（`region`、`universe`、`delay`、`decay`、`neutralization`、`truncation`、`pasteurization`、`nan_handling`、`unit_handling` 等），让同一个文件里的不同 Alpha 跑在不同区域/延迟上。

**TXT**（一行一个表达式，整行 `#` 或 `//` 是注释；行尾注释**不会**被剥离，以免误伤表达式）：

```txt
rank(ts_delta(close, 5))
rank(ts_mean(volume, 20))
-rank(ts_std_dev(returns, 20))
```

**JSON**：字符串数组，或 `{"name": ..., "expression": ..., "settings": {...}}` 对象数组。

### 生成候选（不提交）

生成与提交是**分离**的两步，避免一次性把上千个 Alpha 打到平台上：

```bash
# 先看看会生成什么
python scripts/generate_alphas.py --dry-run --operators ts_delta --fields close --windows 5 10 20

# 写成文件，人工筛过再提交
python scripts/generate_alphas.py --output data/candidates.csv --max-count 50
python scripts/run_backtest.py --input data/candidates.csv --limit 20
```

### 按 grade 搜索（命中即停）

`run_backtest.py` 会把候选跑完；`search_alpha.py` 则是**找到就停**，用来在配额内定向搜索某个评级的因子：

```bash
# 每次最多 3 个并发模拟，命中 AVERAGE 立即停止
python scripts/search_alpha.py --target-grade AVERAGE --concurrency 3 --max-attempts 30

# 逐轮调参：decay / neutralization / truncation 对换手和 fitness 影响很大
python scripts/search_alpha.py --target-grade AVERAGE \
  --input data/candidates_round8.csv \
  --setting decay=12 --setting neutralization=NONE --setting truncation=0.01

# 先看本地库里有没有现成的命中，完全不发请求
python scripts/search_alpha.py --target-grade AVERAGE --include-existing
```

| 参数 | 说明 |
| --- | --- |
| `--target-grade` | 目标评级，默认 `AVERAGE` |
| `--max-attempts N` | 安全上限，跑满 N 个候选仍未命中就停（默认 60） |
| `--concurrency N` | 并发模拟数，**强制夹在 1~3** |
| `--input FILE` | 用文件里的候选；不给则用内置候选池 |
| `--setting K=V` | 覆盖单个回测参数，可重复；数字/布尔会自动转型 |
| `--include-existing` | 先查本地库，已有命中就直接返回，不发任何请求 |

退出码：`0` 命中，`1` 跑满未命中，`2` 用法/配置错误。

**命中是两个条件同时成立**，缺一个都不算：

1. `grade` 达到 `--target-grade`（目标是下限，不是精确匹配 —— 搜 GOOD 时跑出 EXCELLENT 同样算命中）；
2. `GET /alphas/{id}/check` 返回的 **8 项提交检查全部 PASS**。

评级只看 fitness，完全不管能不能提交，所以第 2 条必须单独验。每个跑完的模拟都会走一次 check；评级够但检查没过的因子会在日志里写明**卡在哪一项**，然后被放弃、继续搜下一个候选。

`--include-existing` 同样受这条闸门约束：只有库里**已经记录了 8/8 结论**的因子才会被当作命中返回。在 check 接入之前跑出来的老数据没有这个结论，会打印一行说明并继续搜索，而不是把一个没验过的因子报成命中。

BRAIN 的评级词表（实测）：`INFERIOR` / `AVERAGE` / `GOOD` / `EXCELLENT` / `SPECTACULAR`。后两档都是**实跑时才第一次出现**的 —— 账号历史里从未产生过，所以词表必须当作开放的，代码遇到未知取值原样保存而不是拒绝（`EXCELLENT` 刚出现时 `rank()` 曾把它当成比 INFERIOR 还差，已修）。

**评级由 fitness 单独决定**，不是由 `is.checks` 决定。100+ 个本地样本 + 9 个账号样本按 fitness 排序可以完美分开各档（**0 个单调性反例**），按 sharpe 排序则有 51 个反例：

```
fitness ≤ 0.83      → INFERIOR
fitness 1.01~1.50   → AVERAGE
fitness 1.52~1.77   → GOOD
fitness 2.07~2.30   → EXCELLENT
fitness ≥ 2.69      → SPECTACULAR
```

边界只是被夹逼（没有样本正好落在中间）：INFERIOR/AVERAGE 在 (0.83, 1.01]，AVERAGE/GOOD 在 (1.50, 1.52]，GOOD/EXCELLENT 在 (1.77, 2.07]，EXCELLENT/SPECTACULAR 在 (2.30, 2.69]。

#### 八项提交检查（真正的验收闸门）

`is.checks` 管的是**能否提交给 WorldQuant**，与评级是两回事 —— 实测有因子挂着 3 条 FAIL 的 check 仍被评为 AVERAGE。证据与推导过程记在 `worldquant/api.py` 的 `AlphaGrade` 文档里。

完整名单（`api.SUBMISSION_CHECKS`，顺序即实测顺序）：

| 检查 | 阈值 |
| --- | --- |
| `LOW_SHARPE` | sharpe ≥ 1.25 |
| `LOW_FITNESS` | fitness ≥ 1.0 |
| `LOW_TURNOVER` | turnover ≥ 0.01 |
| `HIGH_TURNOVER` | turnover ≤ 0.7 |
| `CONCENTRATED_WEIGHT` | 无固定阈值 |
| `LOW_SUB_UNIVERSE_SHARPE` | ≈ 0.43 × sharpe |
| `SELF_CORRELATION` | 与账号内已有因子的相关性 ≤ 0.7 |
| `MATCHES_COMPETITION` | 不撞比赛因子 |

两个实测到的契约细节，决定了**必须单独调 `GET /alphas/{id}/check`**，不能只读 `GET /alphas/{id}`：

1. **`SELF_CORRELATION` 在 alpha payload 里永远是 `PENDING`**，只有 check 端点会把它算出来。所以一个因子可能评级齐全、看着完全可用，却从来没跟自己账号里的其他因子比对过相关性。
2. **check 端点是异步的**：计算期间返回 `200` + 空 `text/html` 响应体 + `Retry-After`，必须轮询到 JSON 出现为止。`client.check_submission()` 封装了这件事，最多 3 次、按 `Retry-After` 退避；拿不到结论时返回空结果而不是抛异常 —— 一次已经跑完并花了配额的模拟，不能因为读不到检查结论就被丢掉。

`PENDING` 一律按**没过**处理：没解析出来的检查不是「可以提交」的证据。同理，`all_checks_passed()` 按**名字**逐项核对，只数个数是不够的 —— 少一项 `SELF_CORRELATION` 却多一项未知检查，数量照样凑满 8，但那正是这条闸门要挡住的情况。

check 端点还会带回一个 `selfCorrelated` recordset，列出撞上了哪些因子、相关性多少，存在 `AlphaResult.self_correlation` 里。实测最好的那个因子相关性 0.6996，离 0.7 的上限只差 0.0004。

而 fitness 本身可以精确算出来（已在 7 个跨四档的真实因子上验证）：

```
fitness = sharpe × sqrt(|returns| / max(turnover, 0.125))
```

`max(turnover, 0.125)` 这个地板是关键：换手一旦低于 12.5%，再压低对 fitness 毫无帮助，剩下的杠杆只有 sharpe 和 returns。这也是为什么单纯加 decay 压换手是无效路线 —— 它把 sharpe 一起磨掉了。

#### 十一轮实跑的结论

搜到的最好成绩来自 `analyst4` 的**盈利收益率**（分析师一致预期 / 市值）。三轮迭代的过程很能说明问题：

| 路线 | fitness | 形态 | 结论 |
| --- | --- | --- | --- |
| 裸 `rank(预期 / cap)` | 1.09~1.66（AVERAGE/GOOD） | 换手 <1%、回撤 53%~89%、**纯多头** | 评级拿到了，但是退化解，checks 挂 2~3 条，不可提交 |
| 加 `winsorize` + `group_zscore` + `trade_when` 择时 | ~1.00 | 换手 5.4%、回撤 **7.8%**、多空均衡 | 形态健康，但 fitness 差一口气 |
| **同上但换成分析师一致预期净利润字段** | **2.07~2.30（EXCELLENT）** | 换手 4.3%、回撤 **3.6%~4.0%**、多空均衡 | 评级达标，但**相关性超标，不可提交**（见下） |

关键转折：**决定 returns 的是字段，不是参数**。同一个技术栈、同一套 settings，把 `anl4_ebit_value`（汇总 EBIT）换成 `anl4_fs_detail_estimates_advanced_af_nd_netprofit_mean`（分析师一致预期净利润）后，returns 从 7.4% 升到 11.1%、sharpe 从 1.41 升到 2.20 —— 而前一轮我在 `truncation`、`decay`、`winsorize`、`group_rank`、中性化层级上做消融，fitness 只在 1.08~1.13 之间动，怎么调都跨不过去。

Winning 配方（`Vk6YvwW8`，fitness 2.30 / sharpe 2.35 / 回撤 3.99%）：

```
ey = ts_backfill(anl4_ebit_value, 40) / cap;
np = ts_backfill(anl4_fs_detail_estimates_advanced_af_nd_netprofit_mean, 40) / cap;
group_zscore(winsorize(ts_mean(ey + np - ts_mean(ey + np, 120), 20), std=3.0), subindustry)
```

settings：`decay=12 / truncation=0.01 / neutralization=NONE / nanHandling=ON`，USA / TOP3000 / delay 1。

#### 复查结果：10 个最好的因子里只有 1 个真的可提交

上面这个配方曾被记成「checks 全过」，**那是错的** —— 当时的结论读的是 `GET /alphas/{id}` 里的 `checks`，而那里 `SELF_CORRELATION` 恒为 `PENDING`，被当成了没问题。2026-09-07 用 `GET /alphas/{id}/check` 把库里 10 个 GOOD 及以上的因子全部复查（`scripts/recheck_submissions.py --min-grade GOOD`，只读，不花模拟配额），结果：

| alpha | 评级 | fitness | 结论 | 卡在哪 |
| --- | --- | --- | --- | --- |
| `2rOn70lb` | SPECTACULAR | 2.69 | ✅ **8/8 PASS** | 相关性 0.6996，离上限只差 0.0004 |
| `npKzLemd` | EXCELLENT | 2.07 | ❌ 7/8 | `SELF_CORRELATION=1.0` |
| `YP5Wj6Rl` | EXCELLENT | 2.07 | ❌ 7/8 | `SELF_CORRELATION=1.0` |
| `Vk6YvwW8` | EXCELLENT | 2.30 | ❌ 7/8 | `SELF_CORRELATION=0.863` |
| `O0r5pPgq` | GOOD | 1.81 | ❌ 7/8 | `SELF_CORRELATION=0.8497` |
| `kqVL1PLd` | GOOD | 1.71 | ❌ 7/8 | `SELF_CORRELATION=0.8425` |
| `om6zPKjn` | GOOD | 1.66 | ❌ 5/8 | `LOW_SHARPE=0.91`、`LOW_SUB_UNIVERSE_SHARPE=0.22`、相关性 PENDING |
| `mLgzK5zW` | GOOD | 1.63 | ❌ 4/8 | 同上再加 `LOW_TURNOVER=0.0089` |
| `58QqvJkz` | GOOD | 1.56 | ❌ 7/8 | `SELF_CORRELATION=0.7358` |
| `Vk6Y8xXM` | GOOD | 1.52 | ❌ 7/8 | `SELF_CORRELATION=0.7206` |

三条结论：

1. **`npKzLemd` 和 `YP5Wj6Rl` 相关性正好 1.0** —— 它们是同一个表达式在不同 `truncation` 下跑出来的两份，正是「按 expression+settings 去重不够、必须按 expression 去重」那条规则的实证。评级都是 EXCELLENT，但作为两个因子毫无意义。
2. **`om6zPKjn` / `mLgzK5zW` 印证了退化解的判断**：纯多头账面（`shortCount=0`）不仅形态可疑，`LOW_SHARPE` 和 `LOW_SUB_UNIVERSE_SHARPE` 也直接挂掉 —— 子宇宙里 sharpe 只有 0.22，说明信号根本不在选股上。
3. **接下来搜索的真正瓶颈是相关性，不是 fitness。** 账号里已经有一个用「分析师盈利收益率 + 一致预期净利润」配方做到 SPECTACULAR 的因子，任何沿用同一主题的新因子都会跟它撞上 0.7 的上限。要找下一个可提交的因子，得换到**不同的数据主题**上去，而不是在同一配方上继续调参。

复查还暴露一个契约细节：`SELF_CORRELATION` 有时连 check 端点也返回 `PENDING`（上表 `om6zPKjn`、`mLgzK5zW` 就是）。这种情况按**没过**处理 —— 解析不出来不等于通过。

结论已经写回数据库并重建了台账（`submittable` / `checks_passed` / `self_correlation` / `checks_failed` 四列）。以后想刷新随时可以重跑：

```bash
python scripts/recheck_submissions.py --min-grade GOOD    # 只查还没结论的
python scripts/recheck_submissions.py --all               # 全部重查（相关性会随账号增长而漂移）
python scripts/recheck_submissions.py --alpha-id 2rOn70lb # 指定某一个
```

**退化解的具体形态（重要陷阱）**：那两个 GOOD 的 `shortCount = 0`，即**纯多头**。原因是 `neutralization=NONE` 配上恒为正的 `rank()` —— 没有中性化约束，一个全正信号就变成只做多的组合。41% 收益、53% 回撤都来自方向性集中，而不是横截面选股能力。因为 grade 只看 fitness，BRAIN 照样给它 GOOD。

工具现在会主动告警：`AlphaResult.is_one_sided_book` 检测到单边账面时，runner 和 `search_alpha.py` 的命中报告都会打 WARNING，`report_hit` 还会直接列出 `long/short` 数量。**看到高评级先看这一列。**

**第二道防护是过拟合告警**：`grade` 和 `is.checks` 都只看 IS 全期，所以两者都发现不了「留出年份崩掉」这件事。工具用 `AlphaResult.overfit_ratio`（= test 年 sharpe ÷ IS sharpe）来补这个洞，低于 **0.5** 就打 WARNING：

```
[WARNING] Alpha kqVL1PLd degrades out of sample: test-year sharpe 0.46 vs
          in-sample 1.80 (ratio 0.26). The GOOD grade covers the full IS
          window, so treat it as possibly overfit.
```

`metrics_line()` 和命中报告会在有 test period 时追加 `TestSharpe=`，台账里则有完整的 `train_sharpe` / `test_sharpe` 等 7 列可以横向比较。IS sharpe 非正时比值返回 `None`（对负基线取比值没有意义，不能当成「严重过拟合」）。

对照来看，中途那个形态健康但评级不够的版本（`RRVaGReo`）：sharpe 1.41、回撤 8.09%、换手 4.15%、多空 1443/1389 均衡，只拿到 AVERAGE，唯一短板是 returns 7.43%：`1.41 × sqrt(0.0743/0.125) = 1.09`。换字段把 returns 提上去之后，同样的形态直接变成 EXCELLENT。

已验证有效的细节：

- **字段选择压倒参数调优**：同一结构下把汇总 EBIT 换成分析师一致预期净利润，fitness 从 1.09 跳到 2.07；而在 `truncation`/`decay`/`winsorize`/`group_rank` 上做消融，fitness 只在 1.08~1.13 之间动。
- `neutralization=NONE` + `group_zscore` 是安全组合：`group_zscore` 组内零均值，天然市场中性（多空 1400/1400 量级）。**换成恒正的 `rank()` 就会退化成纯多头**（`shortCount=0`），见上文陷阱。
- `nanHandling=ON`：用了 `trade_when` 就必须开，它会产生 NaN。
- 目前最好的配置（SPECTACULAR，fitness 2.69）：`decay=8 / truncation=0.02 / neutralization=NONE / nanHandling=ON / testPeriod=P1Y`。

**一个尚未定论的点**：`group_zscore` 用 `industry` 还是 `subindustry`。round 9 有一组同 settings 的对照显示 subindustry 更好（fitness 0.82 → 1.00），但 round 13 的最高分（SPECTACULAR）用的是 `industry` —— 而它同时改了 `decay` 和 `truncation`，所以两者混淆了，不能据此下结论。两种分组都出过高分，干净的单变量对照还没做。

不值得重复的死路：

- **纯价量信号**（`pv1`，207 万个 alpha 在用，最拥挤）：57 连 INFERIOR，fitness 上限 0.83。快信号 sharpe 高（`-ts_rank(close,5)` 达 1.92）但换手 94.7%；一平滑，换手和 sharpe 同比例塌掉。
- **`hump` 压换手**：四个阈值（0.05/0.1/0.2/0.3）给出**完全相同**的结果，信号被冻死，sharpe 从 +1.92 翻成 -0.48。
- **`model16` 复合评分**：换手极低（2.6%）但 |sharpe| ≤ 0.89，且回撤 40%。
- **`fundamental2` 的 `fn_*_fair_val_*` 配 126 天 `ts_rank`**：换手达标但 sharpe 在 -0.58~+0.58 之间，方向还依赖具体字段（`l1` 有效、`l3` 无效），不可推广。
- **期权波动率 / 社媒情绪 / 系统性风险照搬「偏离 120 日均值」结构（round 14，10 个候选全 INFERIOR）**：`option8`、`option9`、`socialmedia12`、`model51` 四个数据集都试了，sharpe 最好只有 0.79。根因不是字段选错，而是**这些数据按日变动**：换手落在 26%~40%，远高于 fitness 公式的 0.125 地板，而 returns 只有 0~4%。分析师盈利预期是季度更新的，所以同一套结构在它身上换手只有 4.8%。**换主题不能只换字段，结构也得跟着换** —— round 15 把 `decay` 提到 16、round 16 改成两腿混合之后，同一批期权字段就做出了 8/8 PASS 的因子，见下一节。
  - 关键副产品：`implied_volatility_mean_skew_30` 实测 sharpe **-0.92**，说明符号反了 —— 取正号才是可交易方向（round 15 翻正后得 +0.96，round 16 配 60 日平滑后留出年 sharpe 1.61）。
  - `r14_fwd90`（`forward_price_90 / close`）IS sharpe 0.01、留出年 sharpe **1.62**，两者完全背离，不能采信。
  - 覆盖率 0.95 上下的字段照样出退化账面：`-implied_volatility_mean_30` 回撤 **86.9%**、`-implied_volatility_mean_skew_30` 回撤 61.5%，且 `CONCENTRATED_WEIGHT` 直接 FAIL。
  - `fundamental6`（886 个字段）**覆盖率只有 0.5**，`r15_assetgrowth` 实测 sharpe 0.26；`pv13` 的 `rel_ret_cust` 覆盖率 0.49，`r15_custsig` sharpe -0.69。低覆盖字段先别碰。
  - `pv13_com_page_rank` 出**严重不均衡账面**（774 长 / 2030 短，换手 0.6%）：图中心性字段近似静态，`group_zscore` 之后大量取值并列，不是一个可用的横截面信号。

#### 换主题成功：期权隐含波动率 × 关系图谱（round 15-17）

复查发现 9/10 的高评级因子卡在 `SELF_CORRELATION` 之后，round 14-18 换到两个**与分析师预期完全不同源**的数据集：

- `option8`（波动率，覆盖率 0.97，2087 个 alpha 在用）—— IV 期限结构、IV 偏度
- `pv13`（关系图谱，覆盖率 0.82~0.99，1745 个 alpha 在用）—— 竞争对手/伙伴/客户收益溢出

结构上是三步：**(1)** `decay` 从 8 提到 **16** 把换手压到 0.125 地板以下（round 14 的致命伤）；**(2)** 每条腿**先各自 `group_zscore` 标准化再相加**，否则量纲不同的一条腿会主导整个组合；**(3)** 混合**跨数据集**的两条腿，而不是同一数据集的两个字段。

```
t = implied_volatility_mean_30 / implied_volatility_mean_360;
c = ts_mean(rel_ret_comp, 10);
a = group_zscore(winsorize(-ts_mean(t, 20), std=3.0), industry);
b = group_zscore(winsorize(c, std=3.0), industry);
group_zscore(winsorize(a + b, std=3.0), industry)
```

`decay=16 / truncation=0.02 / neutralization=NONE / nanHandling=ON / testPeriod=P1Y`，USA / TOP3000 / delay 1。

这个因子（`d5OlJNwv`）是闸门启用后第一个真正可提交的：评级 AVERAGE、**8/8 PASS**、sharpe 1.37、fitness 1.04、换手 10.75%、returns 7.24%、回撤 **4.93%**、多空 1541/1369、train 1.57 → test 0.60、年度 4 年全正（最差年 0.84）。

**一个必须澄清的点**：这些因子的 `selfCorrelated` recordset 是**空的**（`SELF_CORRELATION` 返回 `PASS` 且 `value=None`）。我一度把这当成「换主题成功绕开了相关性上限」的证据，**这个推论是错的**：全量测下来，41 个 8/8 PASS 的因子里 **40 个的 recordset 都是空的**，包括与冠军共享 4 条腿里 3 条的那些。唯一有数值的是分析师族的 `2rOn70lb`（0.6996）。也就是说 BRAIN 这个检查到底在跟什么比，**目前无法确定**（「只比已提交的」「按主题分组」「recordset 只列超过某显示阈值的」都与数据相容，没有一个被证实）。换主题在 fitness 上的收益是真的，但别把空 recordset 当成它的功劳。

#### round 18-24 的收尾结论

| 轮次 | 做了什么 | 结果 |
| --- | --- | --- |
| 18 | 三条腿 + 期限对搜索 | fitness 1.18 → **1.34**，10 个候选里 8 个 8/8 PASS |
| 19 | 更短期限对（10/90、10/60、10/120） | 期限对**单调**：30/360(1.15) < 20/180(1.30) < 10/90(1.34)；11 个候选**全部 8/8** |
| 20 | universe × delay 对照扫描 | **两个轴都封死**：delay 0 无权限；TOP3000(1.42) >> TOP1000(0.85) > TOPSP500(0.64) ≈ TOP200(0.65)，小宇宙留出年转负 |
| 21 | 权重与期限微调 | **命中 GOOD**：`Vk627oWY`，期限对 10/60，fitness **1.53**，10 个候选全 8/8 |
| 22 | 换数据族，11 条新腿单腿探针 | 全 INFERIOR，最强 `model51` 系统性风险期限结构 0.91 |
| 23 | 把新腿接进现役配方 | **全是稀释**：`systerm` 换掉 `rel_ret_comp` → 1.53 掉到 1.08；作第四条腿 → 1.20 |
| 24 | **量价作为一条腿**接入 | **第二个 GOOD**：`78ZaNvA8`，fitness **1.53**，8/8 PASS |

最终两个 GOOD 是**等价可互换**的，fitness 都是 1.53：

| | `Vk627oWY`（r21_t1060） | `78ZaNvA8`（r24_term_skew_rev） |
| --- | --- | --- |
| 三条腿 | IV 期限结构 ×2 + `rel_ret_comp` + IV 偏度 | IV 期限结构 ×2 + IV 偏度 + **pv 短期反转** |
| sharpe / returns | **1.84** / 8.6% | 1.75 / **9.5%** |
| 回撤 | **7.0%** | 9.6% |
| train → test | 1.90 → **1.58**（83%） | 1.82 → 1.48（81%） |

**量价（`pv1`）的正确用法**：只能当腿，不能当主角。`-ts_mean(returns,5)` 单腿 sharpe 仅 0.59、换手 21.4%；120 日动量 **−0.19**；量能趋势 −0.54（0/4 年为正）；日内振幅回撤 **85.3%**；`ts_rank(close,250)` 单腿 −0.15，接进混合更是把 fitness 从 1.53 拖到 0.46。但作为**已标准化的一条腿**替换掉 `rel_ret_comp`，能拿到完全相同的 fitness —— 所以旧结论「纯价量是死路」没被推翻，只是补了一句「当腿可以」。

**第四条腿有害，已验证四次**：`pcr_oi_180`（1.30→1.21、1.38→1.25）、`systerm`（1.53→1.20）。三条腿是甜点位。

**算子可用性要先查**：`ts_max` / `ts_min` **不在账号可用的 66 个算子里**（`GET /operators`），用了会直接 `FAILED: Attempted to use inaccessible or unknown operator`，round 24 因此废掉 3 个候选。内置候选池 `POOL_OPERATORS` 里原本正含这两个，已换成 `ts_zscore` / `ts_arg_max` 并加测试钉住。52 周高点接近度改用 `ts_rank(close, 250)`（虽然实测这条腿没用）。

一个已经修掉的操作坑：round 17 里两个「只把 decay 从 16 改成 12/24」的候选被当时的表达式级去重跳过了。去重规则后来放宽到 `scope_hash`（表达式 + region/universe/delay），但**只改构造参数仍然不算新因子**，所以要调 decay/truncation 还是得同时改表达式。

### 查看结果

```
data/results.csv    # 全部
data/passed.csv     # 通过筛选
data/failed.csv     # 未通过（含 FAILED / TIMEOUT / REQUEST_ERROR，不会被静默丢弃）
logs/worldquant.log
data/worldquant.db  # SQLite，权威数据源
```

运行结束时控制台会直接打印排行榜：

```
Top 3 Alpha
Rank  Alpha ID           Expression                                  Sharpe  Fitness  Turnover  Returns  Drawdown   Margin  Year Stability
--------------------------------------------------------------------------------------------------------------------------------------------------
   1  alpha_001          rank(ts_delta(close, 5))                      1.41     1.08     42.3%     7.2%      4.3%   0.0006  4/5+ s=0.66 w=-0.40
```

只想重新导出 CSV 和排行榜、完全不碰网络：

```bash
python scripts/run_backtest.py --export-only --top 20
```

## 命令行参数

```
python scripts/run_backtest.py --input data/alphas.csv --config config.yaml
```

| 参数 | 说明 |
| --- | --- |
| `--input`, `-i` | Alpha 列表文件（`.csv` / `.txt` / `.json`） |
| `--expression`, `-e` | 直接跑单个表达式，可重复；与 `--input` 可同时使用（会自动去重） |
| `--config`, `-c` | YAML / JSON 配置文件 |
| `--credentials` | JSON 凭据文件路径；默认自动发现项目根的 `credentials.json` |
| `--force` | 忽略已完成的去重结果，强制重跑 |
| `--limit N` | 最多跑 N 个 |
| `--poll-interval S` | 轮询间隔秒数（服务端给了 `Retry-After` 时优先用它） |
| `--poll-jitter S` | 每次轮询叠加的随机抖动上限 |
| `--max-wait S` | 单个 simulation 的最长等待时间，超时写入 `TIMEOUT` |
| `--concurrency N` | 并发数，**强制夹在 1~3**，超出会被降下来并告警 |
| `--min-request-interval S` | 全局任意两个 HTTP 请求之间的最小间隔 |
| `--no-yearly` | 跳过年度数据补充请求 |
| `--resume-only` | 只把上次中断的在途 simulation 跑完，不需要输入文件 |
| `--export-only` | 只从数据库重新导出 CSV 和排行榜，不访问网络 |
| `--rebuild-ledger` | 配合 `--export-only`：从数据库整份重写 xlsx 台账 |
| `--refresh-yearly` | 给库里**没有年度数据**的因子重新拉一次 recordset，不提交任何模拟；按评级从高到低排序，`--limit N` 控制花多少请求 |
| `--top N` | 排行榜条数，默认 20 |
| `--db` / `--data-dir` / `--log-file` / `--log-level` | 覆盖对应路径与级别 |

退出码：`0` 正常；`1` 运行时失败（认证失效、被中断）；`2` 用法/配置错误。

## 配置文件

`config.yaml` 里每一项都带注释，删掉某段即用内置默认值。优先级从低到高：

```
内置默认值 < config.yaml < 环境变量 < 命令行参数
```

回测 settings 使用 BRAIN 实际要求的 **camelCase** 字段名；`unit_handling` / `nan_handling` / `instrument_type` 这类 snake_case 写法也会被自动转换，配置文件因此可以写得更好读：

```yaml
settings:
  instrumentType: EQUITY
  region: USA
  universe: TOP3000
  delay: 1
  decay: 0
  neutralization: INDUSTRY
  truncation: 0.08
  pasteurization: "ON"     # 必须加引号：裸写 ON/OFF 会被 YAML 1.1 解析成布尔值
  unitHandling: "VERIFY"
  nanHandling: "OFF"       # 同理
  language: "FASTEXPR"     # 注意是 FASTEXPR，不是 FAST_EXPRESSION
  visualization: false     # 这个确实是布尔值
  testPeriod: "P1Y"        # 强制：留出最后 1 年作为 test period
```

> 任务书里给的字段名（`unit_handling`、`nan_handling`、缺少 `instrumentType` / `language` / `visualization`）与真实接口不一致，这里以真实接口为准，差异在 `worldquant/api.py` 的注释中有说明。未知的 settings 键会原样透传，所以 BRAIN 以后新增字段不需要改代码。

## 断点恢复

这是核心能力，不依赖输入文件也能恢复。

假设中断时数据库里是：

```
alpha1  COMPLETED
alpha2  RUNNING   (有 remote_simulation_id)
alpha3  PENDING   (行已建，POST 还没返回)
```

重新启动后（`python scripts/run_backtest.py --input data/alphas.csv`）：

- **alpha1** —— `sha256(表达式 + settings)` 命中已完成结果，直接跳过，不重复提交；
- **alpha2** —— 复用原有 simulation 行，拿 `remote_simulation_id` 继续轮询，**不会**重复 POST；
- **alpha3** —— 复用那条孤立的 PENDING 行重新提交（不会再插一行，避免残留永远无法收敛的孤儿记录）。

只想收尾在途任务：

```bash
python scripts/run_backtest.py --resume-only
```

按 `Ctrl+C` 中断时会打印提示，已提交的 simulation 都在库里，`--resume-only` 即可接上。

各状态的恢复策略：

| 状态 | 是否自动恢复 | 原因 |
| --- | --- | --- |
| `PENDING` / `SUBMITTED` / `RUNNING` | 是 | 远端还在跑 |
| `TIMEOUT` | 是 | 是**我们**不等了，不是服务端停了；重新轮询很便宜，重新提交会白烧配额并在平台上产生重复 Alpha |
| `REQUEST_ERROR` | 是 | 只是本地网络断了，远端可能已经完成 |
| `FAILED` | 否 | 服务端已拒绝该表达式，再轮询没意义；下次跑同一表达式会重新提交 |
| `AUTH_ERROR` | 否 | 要修的是凭据，不是轮询 |
| `COMPLETED` / `SKIPPED` | 否 | 无事可做 |

**已超时过的行只给一次短探测**：它已经烧掉过一整轮 `max_wait`，所以恢复时只给 `max(30s, 2 × poll_interval)`，而不是再来一遍完整预算。这条是实跑逼出来的 —— 一个陈旧 TIMEOUT 按完整 900s 预算重轮，把整轮新候选**阻塞了 15 分钟**才开始跑。中途被打断的 `SUBMITTED` / `RUNNING` 行从没拿到过预算，仍用完整的 `max_wait`。

短预算不会丢掉结果：如果服务端其实已经跑完，第一次探测就会拿到 alpha id 并正常入库。

## 数据库结构

SQLite（WAL 模式，读写可并行）。三张表：

```sql
alphas(
    id              INTEGER PRIMARY KEY,
    dedup_key       TEXT UNIQUE,      -- sha256(归一化表达式 + 归一化 settings)
    name            TEXT,
    expression      TEXT,             -- 原始表达式，未被归一化改写
    expression_hash TEXT,             -- sha256(归一化表达式)
    settings_json   TEXT,
    created_at      TEXT
)

simulations(
    id                   INTEGER PRIMARY KEY,
    alpha_id             INTEGER REFERENCES alphas(id),
    remote_simulation_id TEXT,        -- BRAIN 的 simulation id
    remote_alpha_id      TEXT,        -- 回测完成后 BRAIN 的 alpha id
    status               TEXT,        -- PENDING/SUBMITTED/RUNNING/COMPLETED/
                                      -- FAILED/TIMEOUT/AUTH_ERROR/REQUEST_ERROR/SKIPPED
    submitted_at         TEXT,
    completed_at         TEXT,
    error                TEXT
)

results(
    id                INTEGER PRIMARY KEY,
    simulation_id     INTEGER UNIQUE REFERENCES simulations(id),
    sharpe, fitness, turnover, returns, drawdown, margin, pnl, book_size  REAL,
    long_count, short_count   INTEGER,
    grade             TEXT,           -- BRAIN 官方评级 INFERIOR/AVERAGE/GOOD/EXCELLENT
    stage             TEXT,           -- 通常 IS
    train_json        TEXT,           -- testPeriod 切出的训练期指标
    test_json         TEXT,           -- 留出的那一年（样本外）指标
    yearly_stats_json TEXT,           -- {"2019": {"sharpe": ..., "turnover": ...}}
    checks_json       TEXT,           -- 八项提交检查的归一化结果（check 端点优先）
    submittable       INTEGER,        -- 1=8/8 PASS，0=有 FAIL/PENDING，NULL=从未检查
    self_correlation  REAL,           -- 与账号内已有因子的最高相关性
    raw_json          TEXT,           -- 服务端原始响应（已抹掉认证类字段）
    passed            INTEGER,
    reasons           TEXT,
    created_at        TEXT
)
```

`checks_json` 存的是 **check 端点解析出来的那一份**，不是 `GET /alphas/{id}` 里的副本 —— 后者 `SELF_CORRELATION` 永远是 `PENDING`。读取时按 `submittable` 是否为 NULL 决定要不要把它当成提交结论：NULL 表示这个因子从没检查过，那份 checks 只能当诊断信息，不能当成「通过了」。

`submittable` 三态是刻意的：`NULL`（没检查过）和 `0`（检查了但没过）必须分得开，否则台账里一个空单元格会被读成「实测不可提交」。

后加的列（`grade` / `stage` / `train_json` / `test_json` / `submittable` / `self_correlation`）由 `_migrate()` 增量补齐：`CREATE TABLE IF NOT EXISTS` 不会修改已存在的表，所以老库打开时会自动 `ALTER TABLE ADD COLUMN`，而不是让引用这些列的查询直接报错。迁移是幂等的。

`raw_json` 保存原始响应，接口字段以后变了可以直接重新解析，不必重跑回测。写库前会递归抹掉 `password` / `token` / `authorization` / `cookie` / `secret` / `session` / `inquiry` 这些键，认证信息不会进数据库（有测试守着）。

去重键为什么分三段哈希：

- `expression_hash` = `sha256(归一化表达式)` —— 只看表达式；
- `scope_hash` = `sha256(归一化表达式 + "\x00" + region/universe/delay)` —— **搜索的跳过规则用它**；
- `dedup_key` = `sha256(归一化表达式 + "\x00" + 规范化 settings JSON)` —— 数据库行的唯一键。同一表达式换个 region/delay 就是另一个回测，不能被跳过。

表达式归一化只做无意义空白的折叠（`rank( close , 5 )` → `rank(close,5)`），保留字符串字面量内容和大小写；**归一化结果只用于算哈希，提交给 BRAIN 的永远是原始表达式**。

### 三层去重

| 层 | 键 | 用在哪 | 语义 |
| --- | --- | --- | --- |
| 宽 | `dedup_key` = 表达式 + **全部** settings | `run_backtest.py` | 同一表达式换任何参数都是另一个回测，不该跳过；只有重跑同一批次时才跳过 |
| **中** | `scope_hash` = 表达式 + `region`/`universe`/`delay` | **`search_alpha.py`** | **禁止两个一模一样的 alpha**：换信息集算新因子，换组合构造参数不算 |
| 严 | `expression_hash` = 只看表达式 | 数据库列 / 查询原语 | 「这个表达式跑过没有」，与参数无关 |

中间那层是搜索的跳过规则，两个方向都被实跑逼过：

- **只看表达式太严**。同一个信号在 TOP1000 和 TOP3000 上、或 delay 0 和 delay 1 下，预测的是**不同的标的集合、不同的信息时点**，本来就是不同的 alpha。round 19 之后想做宇宙/delay 对照扫描，被这条规则整个挡死（round 17 里两个「只改 decay」的候选就是这么被跳过的）。经用户确认后放宽到 `region`/`universe`/`delay`。
- **看全部 settings 太松**。同一个表达式在 `truncation=0.01` 和 `0.08` 下各跑一次，结果几乎完全相同（fitness 都是 2.07），白花一次配额，还会在提交时撞上 `SELF_CORRELATION`。

所以 scope 只包含**决定「预测什么、在哪些标的上、用多新的信息」的三项**；`decay`、`truncation`、`neutralization`、`nanHandling`、`testPeriod` 属于组合构造，只改这些不构成新 alpha。

**一个直接后果**：固定表达式已经不能再扫构造参数了 —— 想试 `truncation=0.05`，必须同时改表达式。这是刻意的：既然实测过构造参数对结果影响很小，就不该为它花配额。

搜索脚本开跑前用 `simulated_scope_hashes()` 一次性取回全部已跑过的 scope 哈希，把候选池过滤一遍并打印跳过了几个。该哈希**不落库**，而是从已存的 `expression` + `settings_json` 现算，所以不需要迁移，将来改 `SCOPE_SETTINGS` 也会自动作用于全部历史。

要故意重跑，用 `run_backtest.py --force`（它走最宽那层）。

## CSV 输出字段

`results.csv` / `passed.csv` / `failed.csv` 三个文件字段一致：

```
alpha_id, expression, dedup_key, status, grade, stage,
simulation_id, remote_alpha_id,
sharpe, fitness, turnover, returns, drawdown, margin, pnl, book_size,
long_count, short_count,
positive_years, negative_years, total_years, positive_year_ratio,
yearly_sharpe_std, worst_year_sharpe,
passed, submittable, checks_passed, checks_failed, self_correlation,
reasons, created_at, completed_at, error,
settings_json, yearly_stats_json
```

- `passed.csv` = `passed` 为真的行；
- `failed.csv` = 其余所有已到终态的行，**包含 FAILED / TIMEOUT / REQUEST_ERROR**，失败不会被静默吞掉；
- `grade` 是 BRAIN 自己的评级（`INFERIOR` / `AVERAGE` / `GOOD`），与本项目可配置的 `passed` 是两套独立判据；
- **`passed` 和 `submittable` 刻意放在一起，因为它们回答的是两个问题**：`passed` 是「达没达到我设的指标线」，`submittable` 是「BRAIN 让不让提交」。所以 `passed.csv` 里完全可能出现一个各项阈值都过了、却 `submittable=NO` 的因子 —— 这四列就是为了让人一眼看出这件事，`checks_failed` 直接写明卡在哪一项。
- `submittable` 三态：`YES` = 8/8 PASS，`NO` = 有 FAIL/PENDING，**空 = 从没检查过**。空值和 `NO` 必须分得开，否则一个没验过的因子会被读成「实测不可提交」。
- `turnover` / `returns` / `drawdown` 一律是**小数**（`0.423` 表示 42.3%），只有日志和排行榜里才渲染成百分比，不存在 `70` 和 `0.70` 混用。

## 实验台账（xlsx）

`data/experiments.xlsx` 是**只追加**的实验记录，**每次模拟一行**，无论成功还是失败。目的是让一轮搜索可以被完整复盘：用了哪个数据集、哪些字段、什么参数、结果如何，不用去翻 SQLite 或平台 UI。

52 列，分六组：

| 组 | 列 |
| --- | --- |
| 标识 | `run_at`, `alpha_id`, `status`, `grade`, `expression`, `simulation_id`, `remote_alpha_id`, `alpha_url` |
| **数据来源** | `datasets`（数据集 id，逗号分隔）, `fields`（表达式里用到的数据字段）, `field_count` |
| **参数** | `region`, `universe`, `delay`, `decay`, `neutralization`, `truncation`, `pasteurization`, `nan_handling`, `unit_handling`, `instrument_type`, `language` |
| **结果（IS 全期）** | `sharpe`, `fitness`, `turnover`, `returns`, `drawdown`, `margin`, `pnl`, `book_size`, `long_count`, `short_count`, `passed`, `reasons`, 年度稳定性 6 列, `error` |
| **训练/测试期** | `train_sharpe`, `train_fitness`, `test_sharpe`, `test_fitness`, `test_returns`, `test_turnover`, `test_drawdown` |
| **提交检查** | `checks_failed`, `submittable`, `checks_passed`, `self_correlation` |

**训练/测试期**那一组存在的原因：本项目**强制留出 1 年 test period**（`settings.testPeriod = "P1Y"`），BRAIN 会把 IS 窗口切成 train/test 并分别返回指标。`grade` 只看 IS 全期，所以**IS 与 test 的落差就是过拟合的直接证据** —— 实测有因子 IS sharpe 1.80（评级 GOOD）、train 2.05，而留出的那一年只有 0.46。只盯 grade 会完全看不到这件事。

未启用 test period 的历史行，这 7 列留空。

几个设计要点：

- **字段抽取**是纯本地解析：跳过函数名（后面紧跟 `(`）、字符串字面量（`'industry'`）、命名参数（`RETTYPE=1`），以及多语句表达式里的局部变量（`ey = ...; rank(ey)` 中的 `ey`），只留真正的数据字段。局部变量靠括号深度识别 —— 只有深度 0 且后跟裸 `=` 的标识符才算变量定义，所以 `winsorize(x, std=3.0)` 里的 `std` 不会被误当成变量名。
- **数据集归属**要查 `/data-fields/{id}`，所以由 `FieldCatalog` 负责，结果缓存在 `data/field_catalog.json`。每个字段只查一次，之后走缓存；查不到就留空，**绝不因此让模拟失败**（归属失败最多丢一列，不会赔上整行）。
- **提交检查那四列是验收依据**，不是装饰：`submittable` 写 `YES`/`NO`，`checks_passed` 写 `8/8`，`checks_failed` 直接写明**为什么不能提交**，例如 `LOW_FITNESS=0.83(limit 1.0):FAIL, HIGH_TURNOVER=0.9466(limit 0.7):FAIL`。没跑过 check 的历史行这四列**全部留空**，而不是写 `NO` —— 空格表示「没验过」，`NO` 表示「验了没过」，两者混在一起就没法复盘了。
- 注意这四列**不决定 grade**（grade 由 fitness 决定），所以会出现「评级 GOOD 但 checks 全挂」的因子：评级好看，却无法提交。
- **失败的模拟也记账**。一次 400、一次超时都是实验信息，记下来才不会重复踩。
- 台账写入失败只告警不抛异常：丢掉一行记录，不能毁掉一次已经成功的回测。

从数据库重建整份台账（比如台账功能上线前跑过的历史模拟）：

```bash
python scripts/run_backtest.py --export-only --rebuild-ledger
```

重建是**覆盖**而非追加 —— 正常运行时每次模拟已经各自追加了一行，若这里也追加就会把整段历史复制一遍。重建走本地缓存解析数据集，不联网；缓存里没有的字段，`datasets` 列会是空的，等下次实跑时自动补齐。

## 筛选规则

两组规则独立评估，任何一条不满足即 `passed = False`，所有不满足项都会列进 `reasons`：

**聚合指标**（`null` 表示关闭该条）：

```yaml
filters:
  min_sharpe: 1.25
  min_fitness: 1.0
  max_turnover: 0.70      # 小数，0.70 == 70%
  min_returns: null
  max_drawdown: null
  min_margin: null
```

**年度稳定性** —— 总 Sharpe 尚可但逐年表现极不稳定的 Alpha 就是靠这组规则挡掉的：

```yaml
filters:
  min_positive_year_ratio: 0.6    # 盈利年份占比下限
  max_negative_sharpe_years: 1    # 允许的最大亏损年份数
  min_worst_year_sharpe: null     # 最差年份 Sharpe 下限
  max_yearly_sharpe_std: null     # 年度 Sharpe 标准差上限
  require_yearly_data: false      # true 时，拿不到年度数据即判不通过
```

统计量 `positive_years` / `negative_years` / `positive_year_ratio` / `yearly_sharpe_std` / `worst_year_sharpe` 都会写进 CSV。缺失 Sharpe 的年份会被**排除**在所有统计之外，而不是按 0 计入，避免残缺数据把占比拉低。

失败信息长这样：

```
FAILED:
  Sharpe 0.82 < 1.25
  Fitness 0.33 < 1.00
  Turnover 134.2% > 70.0%
  Positive years 1/5 = 20.0% < 60.0%
  Negative-sharpe years 4 > 1
```

两个实现细节：

- 未跑完的 Alpha 一律不通过（`status != COMPLETED` 直接判负），指标再好也没用；
- 阈值比较带 `1e-9` 相对容差。turnover 恰好等于上限时，浮点噪声（`0.7000000000000001`）不会导致误判，也不会打印出 `70.0% > 70.0%` 这种自相矛盾的信息。

## 排行榜

默认排序：`Fitness DESC` → `Sharpe DESC` → `Turnover ASC` → `Drawdown ASC`。缺失指标一律排最后（不会因为有 `None` 就浮到榜首）。

字段：`Rank / Alpha ID / Grade / Subm / Expression / Sharpe / Fitness / Turnover / Returns / Drawdown / Margin / Year Stability`。

`Grade` 是 BRAIN 的**官方评级**（直接取自 `GET /alphas/{id}` 的 `grade` 字段，不是本地推算），放在靠前位置因为它才是「这个因子好不好」的答案，周围那些指标只是它的输入。未完成的模拟显示 `-`。

`Subm` 是**提交检查结论**（`n/8`，从没检查过显示 `-`）。这一列是必须加的：排序按 fitness，而 fitness 和 grade **都不反映能不能提交**，所以榜单顶部可能全是提交不了的因子。实测的真实榜单就是这样：

```
Rank  Alpha ID           Grade     Subm Expression                          Sharpe  Fitness  ...
   1  r13_industry       SPECTACUL  8/8 ey = ts_backfill(anl4_ebit_value...   2.28     2.69
   2  r11_combo          EXCELLENT  7/8 ey = ts_backfill(anl4_ebit_value...   2.35     2.30
   ...
   9  r13_win60          GOOD       7/8 ey = ts_backfill(anl4_ebit_value...   1.80     1.56
  10  r21_t1060          GOOD       8/8 t = implied_volatility_mean_10 / ...  1.84     1.53
```

前 12 名里只有 2 个是 `8/8`。**看排行榜先看 `Subm` 列，再看 fitness。**

`Year Stability` 列形如 `4/5+ s=0.66 w=-0.40`，即「5 年里 4 年为正、年度 Sharpe 标准差 0.66、最差年份 -0.40」。

排行榜统计的是**数据库里所有已完成的 Alpha**，不只是本次运行的，所以多次运行会累积成一张总榜。

## 架构

```
worldquant/
  api.py           所有 endpoint 与请求/响应结构的唯一出处，并标注每条契约的验证状态
  exceptions.py    异常层级，每个异常都带 url / status / 表达式上下文
  config.py        配置加载与优先级、阈值校验、并发夹取
  models.py        AlphaSpec / AlphaResult / YearlySummary / FilterOutcome
  hashing.py       表达式归一化 + 去重哈希
  logging_utils.py 控制台 + 文件双通道日志，响应头脱敏
  client.py        WorldQuantClient：唯一的 HTTP 出口
  storage.py       ResultStore：SQLite + CSV 导出
  filters.py       阈值筛选 + 年度稳定性筛选
  ranking.py       排序与排行榜渲染
  loader.py        txt / csv / json 输入加载
  generator.py     AlphaGenerator 抽象 + 组合式生成器（不提交）
  simulator.py     SimulationRunner：提交、轮询、恢复、批量编排

scripts/
  run_backtest.py    主入口
  generate_alphas.py 候选生成（与提交分离）

tests/               479 个用例，HTTP 全部 mock
data/                输入样例与导出结果
logs/                worldquant.log
credentials.json     你的 BRAIN 凭据（gitignored，仓库里是待填写的模板）
credentials.example.json  可提交的模板样例
```

依赖方向是单向的：`api`/`exceptions` 在最底层，`client` 只认 `api`，`simulator` 编排 `client` + `storage` + `filters`，`scripts` 在最外层。业务代码里没有任何裸拼的 URL 或请求体。

### 职责边界

- **`WorldQuantClient`** 只管 HTTP：超时、重试、429/5xx 退避、限速、认证失效、脱敏日志。所有请求都走 `_request()`。
- **`ResultStore`** 只管持久化，`RLock` + `check_same_thread=False` 保证并发写安全。
- **`SimulationRunner`** 只管生命周期：状态机、轮询节奏、恢复、批量并发、单个 Alpha 出错不拖垮整批。
- **解析**集中在 `api.py`，字段缺失一律返回 `None` 而不是抛异常。

## WorldQuant 请求流程

```
POST /authentication          HTTP Basic Auth，201 表示成功，响应体含 user
        ↓
POST /simulations             body: {"type":"REGULAR","regular":<expr>,"settings":{...}}
        ↓                     201 + Location 头 = simulation URL
GET  /simulations/{id}        进行中: {"progress": 0.5} + Retry-After
        ↓                     完成:   {"progress": 1.0, "alpha": "<alpha_id>"}
        ↓                     失败:   {"status":"FAIL"|"ERROR", "message": "..."}
GET  /alphas/{alpha_id}       is 块: sharpe/fitness/turnover/returns/drawdown/
        ↓                     margin/pnl/bookSize/longCount/shortCount/checks[]
GET  /alphas/{id}/recordsets/yearly-stats
                              JSON recordset（不是 CSV）:
                              {"schema":{"properties":[{"name","type"}...]},
                               "records":[[<按位置排列的值>], ...]}
                              列: year, pnl, bookSize, longCount, shortCount,
                                  turnover, sharpe, returns, drawdown, margin,
                                  fitness, stage(无 testPeriod 时 "IS"/"OS"，
                                                 有则 "TRAIN"/"TEST")
        ↓
GET  /alphas/{id}/check       异步: 计算中返回 200 + 空 text/html + Retry-After，
                              需轮询到 JSON 出现（最多 3 次）
                              is.checks[]: 8 项，SELF_CORRELATION 在此才有真值
                              is.selfCorrelated: recordset，列出撞上的因子及相关性
```

以上全部契约已于 2026-09-06 用真实账号跑通验证（登录 → 提交 → 轮询 → 取结果 → 年度数据 → 提交检查）。
recordset 是自描述的，解析时**按 schema 里的字段名映射，不写死列下标**，所以 BRAIN 调整列顺序或增列都不会串位。
`stage` 列存在时只保留 `IS` 行，与聚合指标口径一致。

轮询策略：优先用服务端的 `Retry-After`；没有就用 `poll_interval + random(0, poll_jitter)`。任何情况下都会夹在 `[2s, 60s]` 之间，所以**不可能出现 tight loop**，也不会一次睡过头导致 `max_wait` 失效。

限速与重试：

- 全局最小请求间隔（默认 1s），跨所有并发线程共享一个令牌闸；
- **但限速是「每进程」的**：搜索跑着的时候另开进程做临时探测（查字段、查数据集）不共享这个闸，实测这样会真的触发 429。要探测就等当前批次结束；
- 429 / 5xx / 连接错误 / 超时：指数退避 `min(backoff_cap, backoff_base ** attempt)` 加随机抖动，最多 `max_retries` 次，**不会无限重试**。429 退避已实测验证（阶梯 2.4 → 4.8 → 8.7 → 18.3 → 41.4s）；
- `Retry-After` 会被尊重（上限 `retry_after_cap`，默认 120s）；
- 401/403：只重新登录**一次**再重试；再失败就抛 `AuthError` 并中止整批，不会继续刷请求；
- 响应体带 `inquiry`（BRAIN 的 biometric/persona 交互验证）时抛 `CaptchaRequiredError` 并停止 —— **不绕过任何验证码或风控**，需要人工在浏览器里完成后重跑。

认证失效时整批会 halt，剩余 Alpha 标记为 `SKIPPED` 而不是逐个撞墙。

## 测试

```bash
python -m pytest            # 765 passed, ~13s
python -m pytest -q tests/test_integration.py
```

**测试不会访问真实的 WorldQuant**：`requests.Session` 被 `FakeSession` 替换，runner 层用 `FakeClient`，时间用 `FakeClock` 虚拟化，所以 1800s 超时也是瞬间跑完。

覆盖范围：

| 文件 | 覆盖内容 |
| --- | --- |
| `test_api.py` | settings 归一化与 ON/OFF 陷阱、payload 形状、评级词表、年度 recordset 解析、**8 项提交检查解析与 `all_checks_passed` 的严格性** |
| `test_hashing.py` | 表达式归一化、哈希稳定性、字符串字面量与大小写保留 |
| `test_filters.py` | 阈值筛选、缺失指标、turnover 小数单位、浮点边界 |
| `test_yearly.py` | 年度统计量、缺年排除、年度规则、聚合好但年度差的场景 |
| `test_dedup.py` | 重复检测、settings 差异、`--force`、批内去重 |
| `test_client.py` | 429/5xx 重试与退避、`Retry-After`、认证失效重登、畸形 JSON、限速、脱敏、**check 端点的异步轮询与降级** |
| `test_storage.py` | 建表、幂等 upsert、raw_json 脱敏、CSV 拆分、孤儿行、**提交结论三态（NULL/0/1）的往返与迁移** |
| `test_experiment_log.py` | 字段抽取、数据集归属缓存、台账追加与按名迁移、**提交检查四列** |
| `test_timeout.py` | 轮询节奏、超时落库、远端 FAIL、单个失败不拖垮整批 |
| `test_resume.py` | 中断恢复三态场景、TIMEOUT 复poll、认证失败 halt |
| `test_search.py` | 候选池构造与排序、命中即停、评级下限语义、纯多头告警、过拟合比、**评级 + 8/8 PASS 的双重闸门** |
| `test_integration.py` | login→submit→poll→result→yearly→**check**→filter→export 全链路 |
| `test_cli.py` | 命令行参数、退出码、日志文件、密码不落日志 |
| `test_credentials.py` | JSON 凭据解析、四种来源的优先级、占位模板拒绝加载、文件权限告警、密码不外泄 |
| `test_loader.py` / `test_ranking.py` / `test_generator.py` / `test_config.py` | 输入解析、排序、生成器、配置优先级 |

其中 `test_integration.py` 只 mock 了 `requests.Session`，其余（真实 client、重试、轮询器、解析器、SQLite、筛选器、CSV 导出）全部走真实代码路径。

## 已知限制

1. **年度数据只取样本内行**，与聚合指标口径一致；样本外那一年被丢弃。哪一行算「样本内」取决于有没有 `testPeriod`：没有时标签是 `IS`/`OS`，有时是 `TRAIN`/`TEST`。**这里踩过一个静默坑** —— 只按 `IS` 过滤，在 `testPeriod=P1Y` 变成强制项之后会把**每一行都丢掉**，于是整整两轮实跑的年度统计全空，而端点返回的 recordset 完全正常、有 5 行数据。失败仍会自动降级为空 + 告警，年度规则随之跳过，主流程不受影响；告警现在会把响应体前 200 字打出来，否则只看到「application/json, 1152 bytes」根本判断不出是空记录还是标签不匹配。用 `--no-yearly` 可完全关闭这次请求。
2. **round 11 的 3 个 EXCELLENT 从未经过样本外检验**。它们跑在 `testPeriod` 成为强制项之前，响应里没有 `train`/`test` 块，评级完全建立在整个 IS 窗口上。参照 round 13 的实测（IS sharpe 1.80 / train 2.05 → test 0.46），这类分数很可能有明显过拟合成分。而「禁止两个一模一样的 alpha」这条规则又不允许原样重跑它们，所以要验证就得改表达式（换字段搭配或窗口），那已经是另一个因子了。
3. **并发上限硬编码为 3**。BRAIN 限流激进，任务书也要求保守并发，所以这个值不开放配置，超出会被夹取并告警。
4. **多 simulation 合并提交未实现**。BRAIN 支持一次 POST 提交一组表达式（`MultiAlpha`），本工具一次一个，便于逐个记录状态与失败原因。
5. **提交检查不参与 `passed` 判定**。8 项检查是 `search_alpha.py` 的命中闸门（评级够 + 8/8 PASS 才算找到），也会写进台账的四列，但**不影响** `run_backtest.py` 里可配置阈值算出来的 `passed`。两套判据刻意分开：`passed` 回答「达没达到我设的指标线」，`submittable` 回答「BRAIN 让不让提交」。
6. **`os` 块未采集**。`train` / `test` 已经解析入库（`testPeriod` 强制启用后每个 alpha 都有），但 alpha 提交后才会出现的独立 `os` 块没有接；本工具不做提交，所以暂时用不上。
7. **不做提交（submit）动作**。只回测和筛选，不会把 Alpha 提交给 WorldQuant，也不会调用 `/alphas/{id}/submit`。
8. **`.txt` 输入不剥离行尾注释**，这是刻意的：猜注释起点有可能悄悄改掉表达式。
9. **单机单进程假设**。SQLite 用了 WAL，可以一边跑一边 `--export-only` 查看，但多个进程同时写同一个库不在设计范围内；限速器也是**每进程**的，搜索运行时另开进程探测会真的触发 429。

## BRAIN API 验证状态

本项目原本没有可复用的代码，BRAIN 也没有长期稳定的公开 REST 规范。契约先交叉比对了三个独立公开实现（`rocky-d/wqb`、`pyworldquant`、`jdhruv1503/Brainiac`），再于 **2026-09-06 用真实账号端到端跑通验证**。逐条状态标注在 `worldquant/api.py` 顶部。

**已实测验证**：

- `POST /authentication`：HTTP Basic Auth、`201` 为成功、响应体含 `user`
- `POST /simulations`：`{"type":"REGULAR","regular":...,"settings":{...}}`、`201` + `Location` 头
- `GET /simulations/{id}`：`progress` / `alpha` / `status`(`FAIL`,`ERROR`) / `message`
- `GET /alphas/{id}`：`is` 块的 `sharpe`、`fitness`、`turnover`、`returns`、`drawdown`、`margin`、`pnl`、`bookSize`、`longCount`、`shortCount`、`startDate`、`checks[]`（其中 `SELF_CORRELATION` 恒为 `PENDING`）
- `GET /alphas/{id}/check`：**异步**，计算期间返回 `200` + 空 `text/html` + `Retry-After`，需轮询；返回 `is.checks[]`（8 项，`SELF_CORRELATION` 在此才解析出真值）与 `is.selfCorrelated` recordset（`alpha_id, name, instrument_type, region, universe, correlation, sharpe, turnover, drawdown, fitness, margin`）
- `GET /alphas/{id}/recordsets/yearly-stats`：JSON recordset（`schema.properties` + 位置数组 `records`），列为 `year, pnl, bookSize, longCount, shortCount, turnover, sharpe, returns, drawdown, margin, fitness, stage`。**`stage` 的取值随 `testPeriod` 变化**：无则 `IS`/`OS`，有则 `TRAIN`/`TEST`（2026-09-07 实测；只按 `IS` 过滤曾让两轮实跑的年度数据全空）
- `GET /alphas/{id}/recordsets`：列出可用 recordset（共 5 个）
- `Retry-After` 头用于轮询节奏
- `400` 错误体形状：`{"detail": ...}` 或 `{"settings": {"<字段>": ["<原因>"]}}`

**实测暴露并已修掉的两个契约错误**：

| 错误假设 | 真实情况 | 现象与修法 |
| --- | --- | --- |
| `pasteurization` / `nanHandling` 可以直接写 `ON` / `OFF` | 必须是字符串 `"ON"`/`"OFF"`。YAML 1.1 把裸写的 `ON`/`OFF` 解析成布尔值，序列化成 JSON `true`/`false` | `400 {'settings': {'pasteurization': ['"True" is not a valid choice.']}}`。`normalize_settings` 现在把布尔（以及 `yes`/`no`/`true`/`false` 字符串）强制转回 `"ON"`/`"OFF"`；`visualization` 是唯一真布尔的字段 |
| 年度数据返回 CSV | 返回自描述 JSON recordset，行是按位置排列的数组 | 首跑出现 `yearly stats could not be parsed` 告警并降级。解析器改为按 `schema` 字段名映射列，不写死下标 |

两个错误都被优雅降级机制兜住了，没有中断主流程 —— 这正是保留 `raw_json` 和「字段缺失返回 None」策略的价值。

**仍未验证**：

| 项 | 当前假设 | 如何确认与修正 |
| --- | --- | --- |
| `is.checks[]` 的 `result` 取值 | 假设为 `PASS` / `WARNING` / `FAIL` | 原样存进 `checks_json`，不影响主流程 |
| 登录响应 `inquiry` 分支 | 视为需要人工完成的交互验证并停止 | 本次登录未触发该分支；若平台改了字段名，在 `api.is_captcha_payload` 里补 |
| 429 之外是否还有其他限流状态码 | 只重试 `429/500/502/503/504` | 本次未触发限流；需要时扩 `client._RETRYABLE_STATUS` |
| 其余 4 个 recordset 的内容 | 未采集（`/recordsets` 列出了 `pnl`、`sharpe`、`turnover`、`daily-pnl` 等） | 需要日频 PnL 曲线时按 yearly-stats 同样的 recordset 解析方式接入 |

所有响应解析都做了字段存在性检查，不假设固定 JSON 结构；`raw_json` 完整保留，字段变化后可以离线重解析。

## 下一步：接入 Alpha 自动生成

`AlphaGenerator` 是预留的扩展点，只需要实现 `generate()`：

```python
from worldquant.generator import AlphaGenerator

class LLMAlphaGenerator(AlphaGenerator):
    def __init__(self, llm_client, prompt_template: str) -> None:
        self.llm = llm_client
        self.template = prompt_template

    def generate(self) -> list[str]:
        # 只返回表达式字符串，不要在这里提交
        return self.llm.complete(self.template)
```

它自动获得 `to_specs(settings)` 和 `write(path)`，可以直接产出 CSV 交给 `run_backtest.py --input`。

建议的接入方式（保持生成与提交分离）：

1. 新写一个 `scripts/generate_alphas_llm.py`，用 `LLMAlphaGenerator().write("data/candidates.csv")`；
2. 人工或用现有的 `filters` 先做一轮静态筛（比如表达式语法、字段是否存在）；
3. 再 `python scripts/run_backtest.py --input data/candidates.csv --limit 50` 小批量验证；
4. 想闭环的话，可以把排行榜里表现好的 Alpha 回喂给 LLM 作为 few-shot 示例 —— 数据已经在 `results.csv` / SQLite 里了。

现有 `CombinatorialGenerator` 可以直接当基线对照。

## 安全与合规

- 密码只从 gitignored 的 `credentials.json`、环境变量或 `.env` 读取，不进源码、不进 `config.yaml`、不进日志、不进数据库（有专门测试守着）。JSON 文件是**明文存储**，方便但有代价：务必确认它不会被提交，Linux/macOS 上 `chmod 600`；
- 仍是模板占位值的凭据文件会被拒绝加载，不会把 `your-password` 当密码发给服务端；
- 日志与异常里的响应头、响应体都经过脱敏和截断，`Authorization` / `Cookie` 一律显示 `<redacted>`；
- 不绕过验证码、biometric/persona 验证、风控或任何平台访问控制 —— 遇到就停下来交给人处理；
- 所有 HTTP 请求都强制带 timeout；
- 并发夹在 1~3，全局请求间隔下限，429/5xx 指数退避，认证失败只重登一次，杜绝高频轰炸；
- 只读回测接口，不会调用 `/alphas/{id}/submit` 之类的写操作。

请自行确认你的 BRAIN 账号服务条款允许自动化访问。
