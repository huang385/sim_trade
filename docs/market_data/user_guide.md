# ymm_data_sdk 用户使用手册

说明文档版本：`0.1.3`，更新日期：2026/07/20

适用版本：`0.5.3`，更新日期：2026/07/20

**如需IT支持，可在群内联系相关人员**

说明：目前版本仍为测试版，可能会出现以下问题

1. 数据接口参数与实际不符合，或与文档不符。

2. 数据缺、漏、错。

3. 偶发性的无法使用，可能是管理员在进行维护。维护升级前，一般会在群中进行通知。

4. 速度较慢。对于公网TS模式而言，速度瓶颈可能在网络，没有很好的解决方案。对于内网LAN模式而言，可能是因为数据库同时使用人数较多导致的并发问题。

   **对于上述问题，请和管理员及时反映。**

## 文档目录

### 使用说明

- [环境要求](#1-环境要求)
- [安装与升级](#2-安装与升级)
- [初始化](#3-初始化)
- [整体数据说明](#4-整体数据说明)
- [大查询建议](#5-大查询建议)
- [常见问题](#6-常见问题)

### Python API 手册

- [跨品种通用 API](#跨品种通用-api)
- [A股](#a股)
- [可转债](#可转债-1)
- [金融、商品期货](#金融商品期货)
- [金融、商品期权](#金融商品期权)
- [指数、场内基金](#指数场内基金)

## 1. 基本要求

- Python `>=3.10`，推荐 Python 3.11 或 3.12。

- 不要在安装后将 NumPy 降级到 1.x 或将 pandas 升级到 3.x。

- **网络安全起见，需要向管理员申请Token，请微信或企业微信私聊管理员。**

  请提供你的IP地址：

  1. 右键“开始”，选择“终端”；或者桌面直接输入win键+R键盘，输入”cmd“，并回车。
     ![image-20260720164443570](C:\Users\HP\AppData\Roaming\Typora\typora-user-images\image-20260720164443570.png)
  2. 输入“ipconfig"，回车。
     ![image-20260720164504177](C:\Users\HP\AppData\Roaming\Typora\typora-user-images\image-20260720164504177.png)
  3. 查看”IPv4地址“
     ![image-20260720163753714](C:\Users\HP\AppData\Roaming\Typora\typora-user-images\image-20260720163753714.png)
  4. 如果IPv4地址的后半部分不是例如”11.XX“的字样，证明你无法使用LAN模式。对于这种情况，同样请联系管理员。


## 2. 安装与升级

首先将whl文件放置在常用的python的根目录环境下。推荐使用conda或uv虚拟环境，防止和原有环境产生冲突。

在常用的python环境下运行powershell命令：

```powershell
python -m pip uninstall -y ymm-data-sdk #如果安装了之前的版本！
python -m pip install .\ymm_data_sdk-x.x.x-py3-none-any.whl
```

其中`x.x.x`为对应版本号

## 3. 初始化

LAN 和 Tailscale 用户填写管理员分配的 Token 与访问模式；服务器本机只填写访问模式：

```python
import ymm_data_sdk

ymm_data_sdk.init(
    token=None,
    mode=None,
)
```

`mode` 支持以下三个值：

| `mode` | 使用场景 | SDK 服务地址 |
| --- | --- | --- |
| `"lan"` | 11 网段内的普通用户电脑 | `http://192.168.11.172:18080` |
| `"local"` | 仅在 SDK 服务器本机运行 | `http://127.0.0.1:18080` |
| `"TS"` | 通过 Tailscale 私有网络访问 | `http://100.96.143.111:18080` |

### 3.1 LAN 模式

```python
import ymm_data_sdk

ymm_data_sdk.init(
    token="YOUR_LAN_TOKEN",
    mode="lan",
)
```

LAN Token 绑定管理员登记的固定 `192.168.11.*` 源地址。换电脑或 IP 发生变化后，需要先联系管理员更新白名单。

### 3.2 TS模式

```python
import ymm_data_sdk

ymm_data_sdk.init(
    token="YOUR_TAILSCALE_TOKEN",
    mode="TS",
)
```

对于大部分用户而言，请忽略。

## 4. 整体数据说明

### 4.1 API 关于日期的格式支持

SDK 中以日期作为参数的 API 支持多种常见写法。下列写法表示同一个日期：

| 格式描述 | 格式示例 |
| --- | --- |
| 8 位数字 `YYYYMMDD` | `20150103` |
| 字符串 `"YYYY-MM-DD"` | `"2015-01-03"` |
| 字符串 `"YYYYMMDD"` | `"20150103"` |
| `datetime.datetime` 对象 | `datetime.datetime(2015, 1, 3)` |
| `datetime.date` 对象 | `datetime.date(2015, 1, 3)` |
| `pandas.Timestamp` 对象 | `pandas.Timestamp("2015-01-03")` |

示例：

```python
import datetime
import pandas as pd
import ymm_data_sdk

ymm_data_sdk.get_trading_dates(20150101, 20150131)
ymm_data_sdk.get_trading_dates("2015-01-01", "2015-01-31")
ymm_data_sdk.get_trading_dates(
    datetime.date(2015, 1, 1),
    pd.Timestamp("2015-01-31"),
)
```

- 8 位数字日期必须按照 `YYYYMMDD` 填写。
- 除具体 API 另有说明外，`start_date` 和 `end_date` 均包含在查询范围内。
- 对于按交易日查询的 API，传入 `datetime` 中的时分秒通常不会用于日内筛选；日内行情请使用对应 API 的 `time_slice` 等参数。
- 日期参数为 `None` 时，使用该 API 自身的默认日期范围，具体规则见对应方法说明。

### 4.2 每日数据更新时间

以下时间均为北京时间（`Asia/Shanghai`），适用于正常交易日的本地数据库增量更新。
“批次最早执行时间”是整批任务开始运行的最早时间，不表示所有数据会在该分钟瞬间完成。只有整批抓取、发布和校验成功后，新数据才会对 SDK 用户可见；批次失败时不会发布半批数据。

| 批次 | 最早执行时间 | 主要内容 | 本地可见时间 |
| --- | --- | --- | --- |
| A | `17:15` | 合约快照、日历、行业/状态、交易时段、当日行情 | A 批完整成功后，大约需要几十分钟 |
| B | `20:30` | 可转债资料、复权因子、期货参数/主力、期权主力月份/指标 | B 批完整成功后，大约需要十分钟 |
| C | `23:35` | 期权合约属性、可转债指标、拆分、期权 Greeks | C 批完整成功后，大约需要十分钟 |

#### A 批

批次最早执行时间：`17:15`。

<!-- BEGIN UPDATE BATCH A -->
| SDK 数据接口 | 主要数据 | 数据性质 | 数据源保守就绪时间 |
| --- | --- | --- | --- |
| `ymm_data_sdk.all_instruments` | 全品种合约基础信息 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.instruments` | 单个或多个合约详细信息 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_tick_size` | 合约最小变动价位 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_trading_dates` | 交易日历 | 日历 | 不依赖目标日期 |
| `ymm_data_sdk.get_industry_mapping` | 行业分类映射 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_industry_change` | 行业分类变更 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_instrument_industry` | 标的与行业归属关系 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.get_industry` | 可转债行业资料 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.is_st_stock` | 股票 ST 状态 | 按日期 | `07:31` |
| `ymm_data_sdk.is_suspended` | 股票停牌状态 | 按日期 | `09:15` |
| `ymm_data_sdk.convertible.is_suspended` | 可转债停牌状态 | 按日期 | `11:51` |
| `ymm_data_sdk.futures.get_continuous_contracts` | 期货连续合约资料 | 按日期 | `08:35` |
| `ymm_data_sdk.get_trading_periods` | 交易时段和夜盘日历来源 | 按日期 | `00:00` |
| `ymm_data_sdk.get_price` | 日线、1 分钟和 Tick 行情 | 按日期 | `17:00` |
<!-- END UPDATE BATCH A -->

`get_price` 的 1 分钟行情成功后，会自动重算本地 `5m`、`15m`、`30m`、`60m`、
`120m` 和 `240m` 行情；这些派生频率不需要等待额外批次。

#### B 批

批次最早执行时间：`20:30`。

<!-- BEGIN UPDATE BATCH B -->
| SDK 数据接口 | 主要数据 | 数据性质 | 数据源保守就绪时间 |
| --- | --- | --- | --- |
| `ymm_data_sdk.all_instruments` | 全品种合约基础信息 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.instruments` | 单个或多个合约详细信息 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_tick_size` | 合约最小变动价位 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_trading_dates` | 交易日历 | 日历 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.all_instruments` | 可转债基础资料 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.instruments` | 单个或多个可转债详细资料 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.futures.get_contract_multiplier` | 期货合约乘数 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.futures.get_trading_parameters` | 期货交易参数，包括当天夜盘参数 | 全量快照 | `19:00` |
| `ymm_data_sdk.convertible.get_conversion_price` | 可转债转股价格 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.get_conversion_info` | 可转债转股资料 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.get_call_info` | 可转债赎回资料 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.get_put_info` | 可转债回售资料 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.get_cash_flow` | 可转债票息和现金流 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.get_call_announcement` | 可转债赎回公告 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.get_credit_rating` | 可转债信用评级 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_ex_factor` | 股票复权因子 | 按日期 | `20:10` |
| `ymm_data_sdk.futures.get_dominant` | 期货主力合约 | 按日期 | `19:20` |
| `ymm_data_sdk.futures.get_ex_factor` | 期货连续合约复权因子 | 按日期 | `19:20` |
| `ymm_data_sdk.options.get_dominant_month` | 期权主力月份 | 按日期 | `17:30` |
| `ymm_data_sdk.options.get_dominant_month_rank2` | 期权次主力月份 | 按日期 | `17:30` |
| `ymm_data_sdk.options.get_indicators` | 期权日度指标 | 按日期 | `18:40` |
| `ymm_data_sdk.convertible.get_std_discount` | 可转债标准转股溢价率 | 按日期 | `18:20` |
<!-- END UPDATE BATCH B -->

可转债票息、赎回资料和 A 批收盘行情就绪后，系统会随 B 批更新本地应计利息、净价和
全价。股票及期货复权因子更新后，系统会同步修复本地因子的有效区间。

#### C 批

批次最早执行时间：`23:35`。

<!-- BEGIN UPDATE BATCH C -->
| SDK 数据接口 | 主要数据 | 数据性质 | 数据源保守就绪时间 |
| --- | --- | --- | --- |
| `ymm_data_sdk.all_instruments` | 全品种合约基础信息 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.instruments` | 单个或多个合约详细信息 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_tick_size` | 合约最小变动价位 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.get_trading_dates` | 交易日历 | 日历 | 不依赖目标日期 |
| `ymm_data_sdk.options.get_contract_property` | 期权合约属性 | 全量快照 | 不依赖目标日期 |
| `ymm_data_sdk.convertible.get_indicators` | 可转债日度指标 | 按日期 | `20:40` |
| `ymm_data_sdk.get_split` | 股票拆分和合并事件 | 按日期 | `23:25` |
| `ymm_data_sdk.options.get_greeks` | 期权日度及 1 分钟 Greeks | 按日期 | `23:25` |
<!-- END UPDATE BATCH C -->

以上是当前默认日常安排。历史日期补跑不受当天时钟限制，但仍使用显式日期范围，并且同样
必须在整批发布和校验成功后才会替换线上对应日期的数据。

### 4.3 合约代码命名规则

SDK 统一使用米筐标准合约代码 `order_book_id`。建议优先从 `all_instruments()`、
`instruments()`、`futures.get_contracts()` 或 `options.get_contracts()` 的结果中取得代码，
不要仅凭交易所代码手工猜测。

#### 股票、基金、指数和可转债

沪深交易所证券使用六位证券代码加交易所后缀：

| 交易所 | 常见交易所代码 | SDK 标准代码 |
| --- | --- | --- |
| 上海证券交易所 | `600000.SH` | `600000.XSHG` |
| 深圳证券交易所 | `000001.SZ` | `000001.XSHE` |

其中 `.XSHG` 对应上交所 `SH`，`.XSHE` 对应深交所 `SZ`。ETF、LOF、指数和可转债通常也遵循相同的交易所后缀规则。

#### 期货

期货标准代码统一遵循以下规则：

1. 品种代码统一转为大写。
2. 合约月份统一写成四位 `YYMM`；郑商所原始三位合约月份需要补全实际年份。
3. 代码中不添加交易所后缀，例如不使用 `.SHFE`、`.DCE` 等写法。

各期货交易所示例：

| 交易所 | 交易所原始代码 | SDK 标准代码 | 转换说明 |
| --- | --- | --- | --- |
| 上海期货交易所（SHFE） | `ad2608` | `AD2608` | 品种代码转为大写 |
| 郑州商品交易所（CZCE） | `AP610` | `AP2610` | 品种代码转为大写，并将三位合约月份补全为四位 `YYMM` |
| 广州期货交易所（GFEX） | `lc2609` | `LC2609` | 品种代码转为大写 |
| 大连商品交易所（DCE） | `a2609` | `A2609` | 品种代码转为大写 |
| 上海国际能源交易中心（INE） | `bc2608` | `BC2608` | 品种代码转为大写 |
| 中国金融期货交易所（CFFEX） | `if2606` | `IF2606` | 金融期货同样使用大写品种代码和四位合约月份 |

例如，郑商所 `SH509` 对应 `SH2509`，`SH609` 对应 `SH2609`。郑商所代码的年份不能只根据
最后一位机械补全；不确定时应使用 `id_convert()` 或合约查询接口转换。

期货连续合约使用品种代码加连续合约后缀，例如：

| 标准代码 | 说明 |
| --- | --- |
| `AG88` | 主力连续合约 |
| `AG88A2` | 次主力连续合约 |
| `AG88A3` | 次次主力连续合约 |
| `AG888`、`AG889` | 复权连续合约 |
| `AG99` | 指数连续合约 |

#### 期权

期货期权标准代码统一遵循以下规则：

1. 品种代码统一转为大写。
2. 合约月份统一写成四位 `YYMM`；郑商所原始三位合约月份需要补全实际年份。
3. 代码中不添加交易所后缀。
4. 去掉交易所原始代码中的 `-` 等连接符。
5. 按“大写品种代码 + 四位合约月份 + `C`/`P` + 行权价”排列。

各期货交易所示例：

| 交易所 | 交易所原始代码 | SDK 标准代码 | 转换说明 |
| --- | --- | --- | --- |
| 上海期货交易所（SHFE） | `ad2608C19700` | `AD2608C19700` | 品种代码转为大写 |
| 郑州商品交易所（CZCE） | `AP610C10000` | `AP2610C10000` | 三位合约月份补全为四位 `YYMM` |
| 广州期货交易所（GFEX） | `lc2609-C-100000` | `LC2609C100000` | 品种代码转为大写并去掉 `-` |
| 大连商品交易所（DCE） | `a2609-C-3400` | `A2609C3400` | 品种代码转为大写并去掉 `-` |
| 上海国际能源交易中心（INE） | `bc2608C100000` | `BC2608C100000` | 品种代码转为大写 |
| 中国金融期货交易所（CFFEX） | `IO2606-C-4000` | `IO2606C4000` | 金融期权同样去掉 `-`，不添加交易所后缀 |

`C` 表示看涨期权，`P` 表示看跌期权。ETF 期权等品种可能使用交易所数字合约代码，建议直接通过
`options.get_contracts()` 获取，不要自行拼接。

交易所代码可使用 `id_convert()` 转换为 SDK 标准代码：

```python
ymm_data_sdk.id_convert([
    "600000.SH",
    "000001.SZ",
    "ag2606",
    "a2606-C-5000",
])
```

## 5. 大查询建议

- 如果需要进行大量查询，建议按日期分段查询并存入本地，避免你的 pandas 内存先成为瓶颈。
- 查询失败后缩短日期范围，不要在无限循环中立即重试大型请求。
- `8 GB` 以上结果应主动分块；数据库和网络允许返回，不代表你的电脑能够一次性容纳。

## 6. 常见问题

### `WinError 10061` 或连接超时

先检查服务端是否启动、IP/端口是否正确、防火墙是否开放，以及 Python 是否错误使用了
本机 HTTP 代理。

### `401: invalid token`

token 不存在、已被撤销或复制时包含了多余空格。重新向管理员确认 token。

### `403: method is not allowed`

客户端和服务端版本可能不一致。所有有效用户应获得同一套 registry；先同时升级服务端和
用户端 wheel。

### `YMMDataSDKRemoteError`

表示远程服务返回了未映射异常。保留完整 traceback、方法参数和发生时间，
交给管理员排查。

### IDE 没有签名提示

确认 VS Code/Jupyter 选择的是安装 wheel 的 Python 环境，并在升级后重启语言服务和 kernel。

# ymm_data_sdk Python API 文档

## 跨品种通用 API

## 行情、交易日及合约信息

### all_instruments - 获取所有合约基础信息

```python
ymm_data_sdk.all_instruments(type=None, date=None, market='cn')
```

获取指定市场的所有合约基础信息，支持按合约类型和日期筛选。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| type | str, str list | 需要查询的合约类型。例如 `type='CS'` 代表股票。默认查询所有类型。 |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 指定日期，筛选指定日期处于生命周期内的合约。 |
| market | str | 默认是中国内地市场 `'cn'`。当前 ymm_data_sdk 本地方法仅支持 `'cn'`。 |

**其中 type 参数传入的合约类型和对应解释如下：（其中加粗字体为当前数据库已支持）**

| 合约类型 | 说明 |
| --- | --- |
| **CS** | **Common Stock，即股票** |
| **ETF** | **Exchange Traded Fund，即交易所交易基金** |
| **LOF** | **Listed Open-Ended Fund，即上市型开放式基金** |
| **INDX** | **Index，即指数** |
| **Future** | **Futures，即期货，包含股指、国债和商品期货** |
| Spot | Spot，即现货 |
| **Option** | **期权** |
| **Convertible** | **沪深两市场内有交易的可转债合约** |
| Repo | 沪深两市交易所交易的回购合约 |
| REITs | 不动产投资信托基金 |
| FUND | 除了 ETF、LOF、REITs 之外的基金 |

#### 返回

pandas DataFrame - 所有合约的基础信息。

#### 特殊说明

- 当前返回基础字段集合，主要包括：
  - `order_book_id`
  - `symbol`
  - `abbrev_symbol`
  - `type`
  - `listed_date`
  - `de_listed_date`

### instruments - 获取合约详细信息

```python
ymm_data_sdk.instruments(order_book_ids, market='cn')
```

获取一个或多个合约最新的详细信息，支持查询单个合约或合约列表。

注意事项：

目前系统并不支持跨市场的同时调用，传入的 `order_book_id` list 必须属于同一国家市场，不能混合多个国家市场的 `order_book_id`。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str OR str list | 必填参数，合约代码，可传入 `order_book_id` 或 `order_book_id` list。中国市场的股票、ETF、指数、可转债合约代码通常以 `.XSHG` 或 `.XSHE` 结尾；期货无此要求。 |
| market | str | 默认是中国内地市场 `'cn'`。当前仅支持 `'cn'`。 |

#### 返回

一个 Instrument 对象，或一个 Instrument list。

Instrument 对象支持字段属性访问，例如 `order_book_id`、`symbol`、`type`、`exchange`、`listed_date`、`de_listed_date` 等。不同合约类型的字段集合不同。

**Instrument 对象也支持如下方法：**

* 合约已上市天数。

```python
days_from_listed(date=None)
```

默认返回合约上市距离当前日期的天数。`date` 支持 str；如果合约尚未上市或已经退市，则天数值为 `-1`。

* 合约距离到期天数。

```python
days_to_expire(date=None)
```

如果合约没有到期日，或已经到期，则天数值为 `-1`。

* 获取合约最小价格变动单位。

```python
tick_size()
```

#### 特殊说明

- `trading_hours` 有意将每段开盘右标签减一分钟，表达真实 session 起点；rqdatac Instrument 对象返回第一根 bar 的右标签，因此两者文本相差一分钟。

### get_trading_periods - 获取连续竞价交易时间段

```python
ymm_data_sdk.get_trading_periods(order_book_ids, start_date=None, end_date=None, frequency='1m', market='cn')
```

获取一个或多个合约在指定交易日期间内的连续竞价交易时间段。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str OR str list | 必填参数，单个或多个合约代码。 |
| start_date | int、str、datetime.date、datetime.datetime、pandas.Timestamp | 开始日期。两端都不指定时默认查询最近三个月；只指定一端时向前或向后扩展三个月。 |
| end_date | int、str、datetime.date、datetime.datetime、pandas.Timestamp | 结束日期。 |
| frequency | str | 支持 `'1m'` 和 `'tick'`，默认为 `'1m'`。`1m` 返回分钟线右标签起点，`tick` 返回交易所实际 session 起点。 |
| market | str | 默认 `'cn'`，本地当前仅支持中国内地市场。 |

#### 返回

`pandas.DataFrame`，MultiIndex 为 `order_book_id, date`，字段为 `trading_hours`。非交易日及合约生命周期以外的日期不返回。

### id_convert - 交易所代码转换

```python
ymm_data_sdk.id_convert(order_book_ids, to=None)
```

获取交易所、其他平台代码与米筐标准合约代码之间的转换结果，目前仅支持 A 股、期货和期权代码转换。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 必填参数，合约代码，来自米筐或交易所或其他平台。 |
| to | str | `'normal'` 表示由米筐代码转换为交易所和其他平台代码；不填表示由交易所和其他平台代码转换为米筐代码。 |

#### 返回

- 传入一个 `order_book_ids`，函数返回一个标准化合约代码字符串。
- 传入一个 `order_book_ids` list，函数返回一个标准化合约代码字符串 list。

### get_price - 获取合约行情数据

```python
ymm_data_sdk.get_price(order_book_ids, start_date=None, end_date=None, frequency='1d', fields=None, adjust_type='pre', skip_suspended=False, expect_df=True, time_slice=None, market='cn', **kwargs)
```

获取指定合约或合约列表的行情数据，支持日线、分钟线和 tick 数据。例如支持股票、期货、期权、可转债、ETF、常见指数等中国市场合约。

注意事项：

1. `start_date`、`end_date` 统一为交易日日期语义；即使传入带时分秒的字符串或 `datetime`，也只取日期部分。更细粒度日内过滤请使用 `time_slice`，或取出数据后自行处理。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str OR str list | 必填参数，合约代码，可传入 `order_book_id` 或 `order_book_id` list。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| frequency | str | 支持 `1d`、`1m`、`5m`、`15m`、`30m`、`60m`、`120m`、`240m`、`tick`。不支持任意 `Nm` 扩展频率。 |
| fields | str OR str list | 字段名称。 |
| adjust_type | str | 权息修复方案，仅对 `CS`、`ETF`、`LOF` 的日线和分钟线有效；tick 不复权。 |
| skip_suspended | bool | 是否跳过停牌数据。默认为 False。 |
| expect_df | bool | 默认返回 pandas DataFrame。如果调为 False，则返回按合约拆分的原有数据结构。 |
| time_slice | str, datetime.time | 开始、结束时间段，支持分钟和 tick 级别切分。 |
| market | str | 默认是中国内地市场 `'cn'`。当前 ymm_data_sdk 本地方法仅支持 `'cn'`。 |

##### adjust_type 参数

| 取值 | 说明 |
| --- | --- |
| `none` | 不复权 |
| `pre` | 前复权，价格使用复权因子，`volume` 使用拆分因子 |
| `post` | 后复权，价格使用复权因子，`volume` 使用拆分因子 |
| `pre_volume` | 前复权，价格和 `volume` 均使用复权因子 |
| `post_volume` | 后复权，价格和 `volume` 均使用复权因子 |

#### 返回

pandas DataFrame。

##### bar 数据

常见字段包括 `open`、`close`、`high`、`low`、`limit_up`、`limit_down`、`total_turnover`、`volume`、`num_trades`、`prev_close`、`settlement`、`prev_settlement`、`open_interest`、`trading_date`、`strike_price`、`contract_multiplier`、`iopv`、`day_session_open` 等。不同合约类型和频率的字段集合可能不同。

##### tick 数据

常见字段包括 `datetime`、`open`、`high`、`low`、`last`、`prev_close`、`total_turnover`、`volume`、`num_trades`、`limit_up`、`limit_down`、`open_interest`、`a1` ~ `a5`、`a1_v` ~ `a5_v`、`b1` ~ `b5`、`b1_v` ~ `b5_v`、`trading_date`、`prev_settlement`、`iopv` 等。不同合约类型和本地底表字段可能不同。

#### ymm_data_sdk 返回说明

- 日内bar仅支持`frequency='5m'`、`'15m'`、`'30m'`、`'60m'`、`'120m'`、`'240m'`。
- 当前不支持 `1w` 周线，可取出daily bar自行合成。
- `fields=None` 默认字段集合按 rqdatac 同类型、同频率规则生成；如果本地底表没有某个 rqdatac 派生字段，则不会返回该字段。
- `adjust_type` 默认值与 rqdatac 一致，为 `pre`。兼容旧参数 `adjusted=True/False`，分别映射为 `pre/none`；其他未知 kwargs 会报错。
- `market` 当前仅支持 `'cn'`。

### get_open_auction_info - 获取股票盘前集合竞价数据

```python
ymm_data_sdk.get_open_auction_info(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

获取盘前集合竞价结束、交易所撮合后的 level 1 快照。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 必填参数，合约代码，可传入 `order_book_id` 或 `order_book_id` list。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。如不指定日期，则默认为取当天。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。如不指定日期，则默认为返回所填开始日期当天。 |
| fields | str OR str list | 字段名称，默认返回本地 tick 中可用字段。 |
| market | str | 默认是中国市场 `'cn'`。当前 ymm_data_sdk 本地方法仅支持 `'cn'`。 |

#### 返回

pandas DataFrame。

#### 特殊说明

- 按每个 `order_book_id + trading_date` 在 `09:25:00 <= time < 09:30:00` 的最后一条 tick 作为开盘集合竞价快照。
- 当前默认可返回字段包括：
  - `last`
  - `high`
  - `low`
  - `volume`
  - `total_turnover`
  - `open_interest`
  - `a1` ~ `a5`
  - `b1` ~ `b5`
  - `a1_v` ~ `a5_v`
  - `b1_v` ~ `b5_v`
- 不返回以下字段：
  - `open`
  - `limit_up`
  - `limit_down`
  - `prev_close`
  - `prev_settlement`
- `market` 当前仅支持 `'cn'`。

### get_price_change_rate - 获取历史涨跌幅

```python
ymm_data_sdk.get_price_change_rate(order_book_ids, start_date=None, end_date=None, expect_df=True, market='cn')
```

获取指定标的的历史涨跌幅，该涨跌幅基于后复权价格计算。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 合约代码，可输入 `order_book_id` 或 `order_book_id` list。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；不传入 `start_date`、`end_date` 时，rqdatac 默认返回最近三个月的数据。 |
| expect_df | bool | 默认返回 pandas DataFrame。如果调为 False，则返回原有数据结构。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame。若输入一只股票，返回 pandas Series；否则返回 pandas DataFrame。

#### 特殊说明

- 当前支持类型：`CS`、`ETF`、`LOF`、`INDX`、`Convertible`、`Future`。
- 不支持 `Option`；如果传入的合约全部不支持，则返回 `None`；如果混合传入，则仅返回支持类型的列。
- 计算口径：
  - `CS`、`ETF`、`LOF`：使用日线 `close` 和 `ex_factor.ex_cum_factor` 计算后复权收盘价，再计算相邻交易日收益率。
  - `INDX`、`Convertible`、`Future`：使用日线 `close / 前一交易日 close - 1`。
- `expect_df=True` 时返回 pandas DataFrame；`expect_df=False` 且结果仅一列时返回 pandas Series，否则返回 pandas DataFrame。
- `market` 当前仅支持 `'cn'`。

### get_trading_dates - 获取交易日列表

```python
ymm_data_sdk.get_trading_dates(start_date, end_date, market='cn')
```

获取某个国家市场的交易日列表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| market | str | 默认是中国内地市场 `'cn'`。当前仅支持 `'cn'`。 |

#### 返回

list[datetime.date] - 交易日期列表。

#### 特殊说明

- 本地 `trading_dates` 表包含交易日、自然日和夜盘标记；`ymm_data_sdk.get_trading_dates()` 只返回 `is_trading = true` 的日期。
- 当前本地方法仅支持 `'cn'`。

### get_previous_trading_date - 获取上一交易日

```python
ymm_data_sdk.get_previous_trading_date(date, n=1, market='cn')
```

获取指定日期之前第 `n` 个交易日。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 指定日期。 |
| n | int | 指定往前第几个交易日，默认为 1。 |
| market | str | 默认是中国内地市场 `'cn'`。当前 ymm_data_sdk 本地方法仅支持 `'cn'`。 |

#### 返回

datetime.date - 指定日期之前第 `n` 个交易日。

### get_next_trading_date - 获取下一交易日

```python
ymm_data_sdk.get_next_trading_date(date, n=1, market='cn')
```

获取指定日期之后第 `n` 个交易日。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 指定日期。 |
| n | int | 指定往后第几个交易日，默认为 1。 |
| market | str | 默认是中国内地市场 `'cn'`。当前 ymm_data_sdk 本地方法仅支持 `'cn'`。 |

#### 返回

datetime.date - 指定日期之后第 `n` 个交易日。

### get_yield_curve - 获取收益率曲线

```python
ymm_data_sdk.get_yield_curve(start_date=None, end_date=None, tenor=None, market='cn')
```

获取某个国家市场在一段时间内收益率曲线水平，包含起止日期。目前 rqdatac 仅支持中国市场。数据为 2002 年至今的中债国债收益率曲线，来源于中央国债登记结算有限责任公司。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；不传入 `start_date`、`end_date` 时默认返回最近三个月的数据。 |
| tenor | str | 标准期限，默认返回全部。例如 `'0S'` 为隔夜，`'1M'` 为 1 个月，`'1Y'` 为 1 年。 |
| market | str | 默认是中国市场 `'cn'`。 |

#### 返回

pandas DataFrame - 查询时间段内无风险收益率曲线。

### get_vwap - 获取成交量加权平均价格

```python
ymm_data_sdk.get_vwap(order_book_ids, start_date=None, end_date=None, frequency='1d')
```

获取日度或分钟级别成交量加权平均价格。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 合约代码，可传入 `order_book_id` 或 `order_book_id` list。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| frequency | str | 历史数据频率，默认为 `'1d'`。支持 `'1m'`、`'1d'`，分钟可选取不同频率，例如 `'5m'` 代表 5 分线。 |

#### 返回

pandas Series - vwap。

#### ymm_data_sdk 返回说明

- 计算公式为 `total_turnover / volume`，返回 pandas Series，index为 `order_book_id, date/datetime`。
- 当前本地支持频率：`1d`、`1m`、`5m`、`15m`、`30m`、`60m`、`120m`、`240m`。
- `volume = 0` 时返回 `NaN`。

## A股

## A股基础数据

### get_share_transformation - 获取股票代码变更信息

```python
ymm_data_sdk.get_share_transformation(predecessor=None, market='cn')
```

查询股票因代码变更或并购等情况更换了股票代码的信息。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| predecessor | str | 历史股票代码。为空时返回所有发生过股票代码变更的记录。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含以下字段：

- `predecessor`：历史股票代码。
- `successor`：变更后股票代码。
- `effective_date`：变更生效日期。
- `share_conversion_ratio`：股票变更比例。
- `predecessor_delisted`：变更后旧代码是否退市。
- `discretionary_execution`：是否有变更自主选择权。
- `predecessor_delisted_date`：历史股票代码退市日期。
- `event`：股票代码变更原因。

### sector - 获取板块股票列表

```python
ymm_data_sdk.sector(code, market='cn')
```

获得属于某一板块的所有股票列表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| code | str | 板块名称或板块代码，例如能源板块可填写 `'Energy'`、`'energy'` 或 `'能源'`。 |
| market | str | 默认是中国内地市场 `'cn'`。当前 ymm_data_sdk 本地方法仅支持 `'cn'`。 |

#### 返回

list - 属于该板块的股票 `order_book_id` 列表。

#### 特殊说明

- 当前不支持 `sector_code.Energy` 这类常量对象写法，只接受字符串。

### industry - 获取行业股票列表

```python
ymm_data_sdk.industry(code, market='cn')
```

获得属于某一行业的所有股票列表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| code | str | 行业名称或行业代码，例如农业可填写 `'A01'` 或 `'农业'`。 |
| market | str | 默认是中国内地市场 `'cn'`。当前 ymm_data_sdk 本地方法仅支持 `'cn'`。 |

#### 返回

list - 属于该行业的股票 `order_book_id` 列表。

#### 特殊说明

- 当前不支持 `industry_code.A01` 这类常量对象写法，只接受字符串。

### get_concept_list - 获取股票概念列表

```python
ymm_data_sdk.get_concept_list(start_date=None, end_date=None, market='cn')
```

获得股票概念列表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询概念纳入日期的开始时间。不传入时默认返回所有时段数据。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询概念纳入日期的结束时间。不传入时默认返回所有时段数据。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas Series，index 为概念纳入日期，值为概念名称。

### get_concept - 获取概念对应股票列表

```python
ymm_data_sdk.get_concept(concepts, start_date=None, end_date=None, market='cn')
```

获取所选概念对应的股票列表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| concepts | str or str list | 概念名称，可从 `get_concept_list()` 返回的概念列表中选择一个或多个概念。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询股票纳入概念日期的开始时间。不传入时默认返回所有时段数据。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询股票纳入概念日期的结束时间。不传入时默认返回所有时段数据。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，index 为概念名称，包含以下字段：

- `order_book_id`：合约代码。
- `inclusion_date`：股票纳入概念日期。

### get_industry_mapping - 获取行业分类映射

```python
ymm_data_sdk.get_industry_mapping(source='citics_2019', date=None, market='cn')
```

通过传入分类依据，获得对应的一二三级行业代码和名称。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| source | str | 分类依据。`citics` 表示中信旧分类，`gildata` 表示聚源，`citics_2019` 表示中信 2019 分类，默认为 `citics_2019`。 |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询日期，默认为当前最新日期。 |
| market | str | 默认是中国内地市场 `'cn'`，也可选择香港市场 `'hk'`。 |

#### 返回

pandas DataFrame，包含以下字段：

- `first_industry_code`、`first_industry_name`
- `second_industry_code`、`second_industry_name`
- `third_industry_code`、`third_industry_name`

### get_industry - 获取行业股票列表

```python
ymm_data_sdk.get_industry(industry, source='citics_2019', date=None, market='cn')
```

通过传入行业名称、行业指数代码或者行业代号，获取指定行业的股票列表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| industry | str | 行业名称、行业指数代码或者行业代号。 |
| source | str | 分类依据。`citics` 表示中信旧分类，`gildata` 表示聚源，`citics_2019` 表示中信 2019 分类，默认为 `citics_2019`。 |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询日期，默认为当前最新日期。 |
| market | str | 默认是中国内地市场 `'cn'`，也可选择香港市场 `'hk'`。 |

#### 返回

list - 指定行业的股票 `order_book_id` 列表。

### get_industry_change - 获取行业股票纳入剔除日期

```python
ymm_data_sdk.get_industry_change(industry, source='citics_2019', level=None, market='cn')
```

通过传入行业名称、行业指数代码或者行业代号，获取指定行业中股票的纳入和剔除日期。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| industry | str | 行业名称、行业指数代码或者行业代号。 |
| source | str | 分类依据。`citics_2019` 表示中信新分类，`citics` 表示中信旧分类，`gildata` 表示聚源，默认为 `citics_2019`。 |
| level | int | 行业分类级别，共三级，`1`、`2`、`3` 分别对应一、二、三级行业；默认一级分类。 |
| market | str | 默认是中国内地市场 `'cn'`，也可选择香港市场 `'hk'`。 |

#### 返回

pandas DataFrame，index 为 `order_book_id`，包含以下字段：

- `start_date`：起始日期。
- `cancel_date`：取消日期，`2200-12-31` 表示未披露。

### get_instrument_industry - 获取股票行业分类

```python
ymm_data_sdk.get_instrument_industry(order_book_ids, source='citics_2019', level=1, date=None, market='cn')
```

通过股票 `order_book_id` 获取指定日期的行业分类。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| source | str | 分类依据。`citics_2019` 表示中信新分类，`citics` 表示中信旧分类，`gildata` 表示聚源，默认为 `citics_2019`。 |
| level | int or str | 行业分类级别。`0` 返回三级分类完整信息，`1`、`2`、`3` 分别返回对应级别；当 `source='citics_2019'` 时也可传入 `'citics_sector'`。 |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询日期，默认为当前最新日期。 |
| market | str | 默认是中国内地市场 `'cn'`，也可选择香港市场 `'hk'`。 |

#### 返回

pandas DataFrame。根据 `level` 返回对应级别的行业代码和行业名称；`level=0` 时包含一、二、三级行业代码和名称。

### get_shares - 获取历史股本数据

```python
ymm_data_sdk.get_shares(order_book_ids, start_date=None, end_date=None, fields=None, expect_df=True, market='cn')
```

获取股票或者股票列表在一段时间内的股本数据，包含起止日期。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；不传入开始和结束日期时默认返回最近三个月的数据。 |
| fields | str or str list | 返回字段，默认返回全部有效字段。 |
| expect_df | bool | 默认返回 pandas DataFrame；设为 False 时返回 rqdatac 原有数据结构。 |
| market | str | 默认是中国内地市场 `'cn'`，也可选择香港市场 `'hk'`。 |

#### 返回

pandas DataFrame。中国市场支持以下字段：

- `total`：总股本。
- `circulation_a`：流通 A 股。
- `non_circulation_a`：非流通 A 股。
- `total_a`：A 股总股本。
- `free_circulation`：自由流通股本。
- `preferred_shares`：优先股。

`management_circulation` 已被废弃。

### get_main_shareholder - 获取主要股东信息

```python
ymm_data_sdk.get_main_shareholder(order_book_ids, start_date=None, end_date=None, is_total=False, start_rank=None, end_rank=None, market='cn')
```

获取 A 股主要股东构成、持股比例及持股性质等信息。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码。 |
| start_date | date-like | 开始日期，默认为去年当日。 |
| end_date | date-like | 结束日期，默认为查询当日。 |
| is_total | bool | False 表示按流通 A 股，True 表示按全部发行 A 股。 |
| start_rank | int | 排名开始值。 |
| end_rank | int | 排名结束值；两个排名参数均为空时返回全部名单。 |
| market | str | 默认中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含公告日、截止日、排名、股东名称与性质、占总股本及流通股比例、质押和冻结股数等字段。

### get_private_placement - 获取定向增发信息

```python
ymm_data_sdk.get_private_placement(order_book_ids, start_date=None, end_date=None, progress='complete', issue_type='private', market='cn')
```

获取股票定向或公开增发信息，以首次公告发布日期为查询基准。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码。 |
| start_date | date-like | 开始日期，默认全部。 |
| end_date | date-like | 结束日期，默认全部。 |
| progress | str | `complete`、`incomplete` 或 `all`。 |
| issue_type | str | `private`、`public` 或 `all`。 |
| market | str | 默认中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame。

### get_allotment - 获取配股信息

```python
ymm_data_sdk.get_allotment(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

获取股票配股信息。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码。 |
| start_date | date-like | 开始日期。 |
| end_date | date-like | 结束日期。 |
| fields | str or str list | 返回字段，默认全部。 |
| market | str | 默认中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含配股比例、实际配股比例和数量、配股价格、股权登记日及除权除息日等字段。

### get_block_trade - 获取大宗交易数据

```python
ymm_data_sdk.get_block_trade(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取股票大宗交易数据。

#### 返回

pandas DataFrame，包含成交价、成交量、成交额、买方营业部和卖方营业部。

### get_symbol_change_info - 获取证券简称变更信息

```python
ymm_data_sdk.get_symbol_change_info(order_book_ids, market='cn')
```

获取一个或多个合约的历史简称变更信息。

#### 返回

pandas DataFrame，包含变更日期、信息发布日期和证券简称。

### get_special_treatment_info - 获取特殊处理状态信息

```python
ymm_data_sdk.get_special_treatment_info(order_book_ids, market='cn')
```

获取证券 ST、`*ST` 及撤销特殊处理等历史状态信息。

#### 返回

pandas DataFrame，包含实施日期、信息发布日期、证券简称、处理类别和事项描述。

### get_holder_number - 获取股东户数

```python
ymm_data_sdk.get_holder_number(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取一个或多个股票的股东户数数据。

#### 返回

pandas DataFrame，包含截止日期、股东总户数、A 股股东户数及各类户均持股数。

### get_abnormal_stocks - 获取龙虎榜每日明细

```python
ymm_data_sdk.get_abnormal_stocks(start_date=None, end_date=None, types=None, market='cn')
```

获取指定日期范围和异动类型的龙虎榜每日明细。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| start_date | date-like | 开始日期，默认为去年当日。 |
| end_date | date-like | 结束日期，默认为查询当日。 |
| types | str or str list | 异动类型，默认全部。 |
| market | str | 默认中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含异动类型、起止日期、成交量和成交额、涨跌幅、换手率、振幅、偏离值及上榜原因。

### get_abnormal_stocks_detail - 获取龙虎榜机构交易明细

```python
ymm_data_sdk.get_abnormal_stocks_detail(order_book_ids, start_date=None, end_date=None, sides=None, types=None, market='cn')
```

获取指定股票的龙虎榜机构交易明细。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码。 |
| start_date | date-like | 开始日期。 |
| end_date | date-like | 结束日期。 |
| sides | str or str list | `buy`、`sell` 或 `cum`，默认全部。 |
| types | str or str list | 异动类型，默认全部。 |
| market | str | 默认中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含买卖方向、排名、营业部名称、买卖金额、异动类型和上榜原因。

### get_buy_back - 获取股份回购数据

```python
ymm_data_sdk.get_buy_back(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

获取股票股份回购信息。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码。 |
| start_date | date-like | 起始日期，默认最近三个月。 |
| end_date | date-like | 结束日期，默认最近三个月。 |
| fields | str or str list | 返回字段，默认全部。 |
| market | str | 默认中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含公告与完成日期、回购期限、回购数量和金额、价格上下限、回购目的、比例及回购方式等字段。

### get_turnover_rate - 获取历史换手率

```python
ymm_data_sdk.get_turnover_rate(order_book_ids, start_date=None, end_date=None, fields=None, expect_df=True, market='cn')
```

获取股票或者股票列表在一段时间内的历史换手率。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；不传入开始和结束日期时默认返回最近三个月的数据。 |
| fields | str or str list | 返回字段，默认返回全部字段。 |
| expect_df | bool | 默认返回 pandas DataFrame；设为 False 时返回 rqdatac 原有数据结构。 |
| market | str | 默认是中国内地市场 `'cn'`，也可选择香港市场 `'hk'`。 |

#### 返回

pandas DataFrame，支持以下字段：

- `today`：当天换手率。
- `week`：过去一周平均换手率。
- `month`：过去一个月平均换手率。
- `year`：过去一年平均换手率。
- `current_year`：当年平均换手率。

### get_dividend_info - 获取历史分红信息

```python
ymm_data_sdk.get_dividend_info(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取股票在一段时间内的分红情况，包含起止日期；不指定日期时默认返回全部分红数据。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| market | str | 默认是中国市场 `'cn'`，目前仅支持中国市场。 |

#### 返回

pandas DataFrame，包含以下字段：

- `info_date`：公布日期。
- `effective_date`：常规分红对应的有效财政季度；特殊分红对应股权登记日。
- `dividend_type`：分红形式。
- `ex_dividend_date`：除权除息日。

### get_dividend - 获取历史现金分红

```python
ymm_data_sdk.get_dividend(order_book_ids, start_date=None, end_date=None, adjusted=False, expect_df=True, market='cn')
```

获取股票或股票列表在一段时间内的现金分红情况，以分红宣布日作为日期查询基准；不指定日期时默认返回全部分红数据。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| adjusted | bool | 是否获取调整后的分红；rqdatac 当前尚不支持 `True`，默认值为 False。 |
| expect_df | bool | 默认返回 pandas DataFrame；设为 False 时返回 rqdatac 原有数据结构。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含以下字段：

- `declaration_announcement_date`：分红宣布日。
- `book_closure_date`：股权登记日。
- `dividend_cash_before_tax`：税前分红。
- `ex_dividend_date`：除权除息日。
- `payable_date`：分红到账日。
- `round_lot`：分红最小单位。
- `advance_date`：股东会日期。
- `quarter`：报告期。

### get_dividend_amount - 获取历年分红总额

```python
ymm_data_sdk.get_dividend_amount(order_book_ids, start_quarter=None, end_quarter=None, date=None, market='cn')
```

获取股票历年分红总额数据。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_quarter | str | 起始报告期，例如 `'2023q4'`，默认返回全部。 |
| end_quarter | str | 截止报告期，例如 `'2023q4'`，默认返回全部。 |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询日期，默认为当前最新日期。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含以下字段：

- `event_procedure`：事件进程，例如预案、决案、方案实施。
- `info_date`：公告日期。
- `amount`：分红总额。

### get_split - 获取股票拆分信息

```python
ymm_data_sdk.get_split(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取股票或股票列表在一段时间内的拆分情况，以股权登记日为查询基准；不指定日期时默认返回全部。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期，默认返回全部。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期，默认返回全部。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含 `book_closure_date`、`order_book_id`、`split_coefficient_to`、`payable_date`、`split_coefficient_from` 和 `cum_factor`。

#### 特殊说明

- 当前方法仅支持 `market='cn'`。

### get_ex_factor - 获取股票复权因子

```python
ymm_data_sdk.get_ex_factor(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取股票在一段时间内的复权因子，以除权除息日为查询基准；不指定日期时默认返回全部。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期，默认返回全部。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期，默认返回全部。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，以 `ex_date` 为 index，包含 `order_book_id`、`announcement_date`、`ex_cum_factor`、`ex_end_date` 和 `ex_factor`。

#### 特殊说明

- 当前本地方法仅支持 `market='cn'`。

### is_suspended - 判断股票是否停牌

```python
ymm_data_sdk.is_suspended(order_book_ids, start_date=None, end_date=None, market='cn')
```

判断股票在一段时间内是否全天停牌，查询区间包含起止日期。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期；单只股票默认从上市日期开始。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；默认到当前日期，已退市股票默认到退市前一交易日。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，index 为交易日，columns 为股票代码，值为 bool。

#### ymm_data_sdk 返回说明

- 当前本地方法仅支持 `market='cn'`。

### is_st_stock - 判断股票是否为 ST 股

```python
ymm_data_sdk.is_st_stock(order_book_ids, start_date=None, end_date=None, market='cn')
```

判断一只或多只股票在一段时间内是否为 ST 股，查询区间包含起止日期。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票合约代码，可传入单个或多个 `order_book_id`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期；单只股票默认从上市日期开始。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；默认到当前日期，已退市股票默认到退市前一交易日。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，index 为交易日，columns 为股票代码，值为 bool。

#### 特殊说明

- 当前本地方法仅支持 `market='cn'`，并且只支持 `CS` 股票。

## A股财务数据

### get_pit_financials_ex - 查询季度财务信息

```python
ymm_data_sdk.get_pit_financials_ex(order_book_ids, fields, start_quarter, end_quarter, date=None, statements='latest', market='cn')
```

查询季度财务信息，以 point-in-time 形式返回。以给定一个报告期回溯的方式获取季度基础财务数据，即利润表、资产负债表、现金流量表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 合约代码，可传入 `order_book_id` 或 `order_book_id` list。 |
| fields | list | 需要返回的财务字段。支持字段仅限利润表、资产负债表、现金流量表三大表字段。 |
| start_quarter | str | 财报回溯查询的起始报告期，例如 `'2015q2'` 代表 2015 年半年报。 |
| end_quarter | str | 财报回溯查询的截止报告期，例如 `'2015q4'` 代表 2015 年年报。 |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询日期，默认查询日期为当前最新日期。 |
| statements | str | 基于查询日期，返回某一个报告期的所有记录或最新一条记录。`all` 返回所有记录，`latest` 返回最新一条记录，默认为 `latest`。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame。

返回内容包含 `quarter`、`info_date`、请求的财务字段、`if_adjusted` 等字段。

### current_performance - 查询财务快报数据

```python
ymm_data_sdk.current_performance(order_book_ids, info_date=None, quarter=None, interval='1q', fields=None, market='cn')
```

查询财务快报数据。默认返回给定 `order_book_id` 当前最近一期的快报数据。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 合约代码。 |
| info_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 公告日期。如果不填 `info_date` 和 `quarter`，则返回当前日期的最新发布的快报。如果填写，则从 `info_date` 当天或者之前最新的报告开始抓取。 |
| quarter | str | `info_date` 参数优先级高于 `quarter`。如果没有填写 `info_date` 而填写了 `quarter`，则以该报告期开始查询，例如 `'2015q2'`、`'2015q4'`。 |
| interval | str | 查询财务数据的间隔。例如 `'5y'` 代表从报告期开始回溯 5 年，每年为相同报告期数据；`'3q'` 代表从报告期开始向前回溯 3 个季度。不填写默认抓取一期。 |
| fields | str or str list | 抓取对应有效字段返回。默认返回所有字段。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame。

### performance_forecast - 查询业绩预告数据

```python
ymm_data_sdk.performance_forecast(order_book_ids, info_date=None, end_date=None, fields=None, market='cn')
```

查询业绩预告数据。默认返回给定 `order_book_ids` 当前最近一期的业绩预告数据。

业绩预告主要用来调取公司对即将到来的财务季度的业绩预期信息。有时同一个财务季度会有多条记录，分别是季度预期和累计预期，即本年至今。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 合约代码，可传入 `order_book_id` 或 `order_book_id` list。 |
| info_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 公告日期。如果不填 `info_date` 和 `end_date`，则返回当前日期的最新发布的业绩预告。如果填写，则从 `info_date` 当天或者之前最新的报告开始抓取。`info_date` 优先级高于 `end_date`。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 对应财务预告期末日期，例如 `'20150331'`。 |
| fields | str or str list | 抓取对应有效字段返回。默认返回所有字段。 |
| market | str | 默认是中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame。

## A股因子数据

## 融资融券和南北向数据

### get_securities_margin - 获取融资融券信息

```python
ymm_data_sdk.get_securities_margin(order_book_ids, start_date=None, end_date=None, fields=None, expect_df=True, market='cn')
```

获取个股或沪深市场整体融资融券信息。融资融券数据起始于 2010 年 3 月 31 日。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股票代码；`XSHG`/`sh` 和 `XSHE`/`sz` 表示市场整体。 |
| start_date | date-like | 开始日期，默认最近三个月。 |
| end_date | date-like | 结束日期，默认最新数据日期。 |
| fields | str or str list | 返回字段，默认全部。 |
| expect_df | bool | 默认返回 DataFrame，False 返回 rqdatac 原有结构。 |
| market | str | 默认中国内地市场 `'cn'`。 |

#### 返回

pandas DataFrame，包含融资余额、融资买入与偿还额、融券余额与余量、融券卖出与偿还量以及融资融券余额。

### get_margin_stocks - 获取融资融券标的列表

```python
ymm_data_sdk.get_margin_stocks(date=None, exchange=None, margin_type='stock', market='cn')
```

获取指定日期和交易所的融资或融券标的证券列表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| date | date-like | 查询日期，默认今天的上一交易日。 |
| exchange | str | `XSHE`/`sz` 或 `XSHG`/`sh`，默认全部。 |
| margin_type | str | `stock` 表示融券卖出，`cash` 表示融资买入。 |
| market | str | 默认中国内地市场 `'cn'`。 |

#### 返回

list；无数据时返回空 list。

### get_eligible_securities_margin - 获取可充抵保证金证券

```python
ymm_data_sdk.get_eligible_securities_margin(date=None, exchange=None, market='cn')
```

获取指定日期和交易所的可充抵保证金证券列表。

#### 返回

list；无数据时返回空 list。

### get_margin_haircut - 获取保证金折算率

```python
ymm_data_sdk.get_margin_haircut(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取可充抵保证金证券的折扣率和折算率。

#### 返回

pandas DataFrame，包含 `haircut_rate` 和 `advance_rate`。

### get_stock_connect - 获取陆股通持股数据

```python
ymm_data_sdk.get_stock_connect(order_book_ids, start_date=None, end_date=None, fields=None, expect_df=True)
```

获取股票在香港上市交易的持股情况；也可使用 `shanghai_connect`、`shenzhen_connect` 或 `all_connect` 查询对应范围。

#### 返回

pandas DataFrame，包含持股量、持股比例和调整后持股比例。

### current_stock_connect_quota - 获取当前沪深港通额度

```python
ymm_data_sdk.current_stock_connect_quota(connect=None, fields=None)
```

获取当前沪深港通每日额度数据。`connect` 支持 `hk_to_sh`、`hk_to_sz`、`sh_to_hk` 和 `sz_to_hk`。

#### 返回

pandas DataFrame，包含额度余额、余额占比、买方金额和卖方金额

### get_stock_connect_quota - 获取历史沪深港通额度

```python
ymm_data_sdk.get_stock_connect_quota(connect=None, start_date=None, end_date=None, fields=None)
```

获取沪深港通历史每日额度数据。

#### 返回

pandas DataFrame，包含额度余额、余额占比、买方金额和卖方金额。

## 公告相关

### get_incentive_plan - 获取股权激励数据

```python
ymm_data_sdk.get_incentive_plan(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取合约股权激励数据。若指定开始日期或结束日期，另一个日期也必须提供。

#### 返回

pandas DataFrame，包含信息发布日期、首次发布日期、生效日期、激励股票数量、激励价格、激励模式和公告类型。

### get_investor_ra - 获取投资者关系活动数据

```python
ymm_data_sdk.get_investor_ra(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取参与人员、调研机构、接待日期和活动类别等投资者关系活动信息。

### get_announcement - 获取公司公告

```python
ymm_data_sdk.get_announcement(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

获取公司公告数据，可按字段筛选。

#### 返回

pandas DataFrame，包含发布日期、媒体出处、内容类别、标题、语言、文件格式、信息类别、公告链接和入库时间。

### get_audit_opinion - 获取财务报告审计意见

```python
ymm_data_sdk.get_audit_opinion(order_book_ids, start_quarter, end_quarter, date=None, type=None, opinion_types=None, market='cn')
```

获取指定报告期内的财务报告审计意见。`type` 支持 `financial_statements` 和 `internal_control`，`opinion_types` 可筛选审计意见类型。

### get_restricted_shares - 获取限售解禁明细

```python
ymm_data_sdk.get_restricted_shares(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取限售股份解禁日期、股东信息、解禁数量和解禁原因等数据。

### get_staff_count - 获取员工数量

```python
ymm_data_sdk.get_staff_count(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取公司各报告期员工数量数据。

#### 返回

pandas DataFrame，包含发布日期、截止日期和员工数量。

### get_leader_shares_change - 获取高管持股变动

```python
ymm_data_sdk.get_leader_shares_change(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取高管姓名、职务、持股变动数量、变动后持股数、变动比例、价格和原因。

### get_forecast_report_date - 获取定期报告预约披露日

```python
ymm_data_sdk.get_forecast_report_date(order_book_ids, start_quarter, end_quarter, market='cn')
```

获取指定报告期的首次预约日、历次变更日和实际披露日。

### get_investor_qa - 获取投资者问答

```python
ymm_data_sdk.get_investor_qa(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取提问来源、提问者、问题内容、回复内容和回复时间等投资者问答数据。

## 可转债

可转债历史日行情、分钟线及 tick 行情统一通过 `ymm_data_sdk.get_price` 获取。

### convertible.all_instruments - 获取所有可转债基础信息

```python
ymm_data_sdk.convertible.all_instruments(date=None, market='cn')
```

获取所有可转债基础信息；传入日期时筛选该日处于上市交易状态的合约。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 指定日期，筛选该日期可交易的合约。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，字段、顺序、日期类型及数值类型与 rqdatac 保持一致。

### convertible.instruments - 获取可转债合约基础信息

```python
ymm_data_sdk.convertible.instruments(order_book_ids, market='cn')
```

获取一个或多个可转债合约的基础信息。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 可转债合约代码。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

单个代码返回轻量 Instrument 对象；多个代码返回 Instrument list。对象支持属性访问以及 `get/items/keys/values`。

#### 特殊说明

- 未知单个代码会发出 warning 并返回 `None`；批量查询会忽略未知代码。
- `coupon_rate_table()` 查询分段票息。
- `option(option_type=None)` 查询赎回、回售和转股价修正等条款，支持 `option_type=1..7`。

### convertible.get_conversion_price - 获取转股价变动

```python
ymm_data_sdk.convertible.get_conversion_price(order_book_ids, start_date=None, end_date=None, market='cn')
```

返回以 `order_book_id, info_date` 为 MultiIndex 的 pandas DataFrame，包含 `effective_date`、`conversion_price` 和 `change_reason`。

### convertible.get_conversion_info - 获取转股规模变动

```python
ymm_data_sdk.convertible.get_conversion_info(order_book_ids, start_date=None, end_date=None, market='cn')
```

返回以 `order_book_id, info_date` 为 MultiIndex 的 pandas DataFrame，字段、顺序和 dtype 与 rqdatac 一致。

### convertible.get_call_info - 获取强制赎回信息

```python
ymm_data_sdk.convertible.get_call_info(order_book_ids, start_date=None, end_date=None, market='cn')
```

返回可转债强制赎回事件 DataFrame。

### convertible.get_put_info - 获取持有人回售信息

```python
ymm_data_sdk.convertible.get_put_info(order_book_ids, start_date=None, end_date=None, market='cn')
```

返回可转债回售事件 DataFrame。

### convertible.get_cash_flow - 获取现金流数据

```python
ymm_data_sdk.convertible.get_cash_flow(order_book_ids, start_date=None, end_date=None, market='cn')
```

返回以 `order_book_id, payment_date` 为 MultiIndex 的现金流 DataFrame。

### convertible.is_suspended - 判断可转债是否停牌

```python
ymm_data_sdk.convertible.is_suspended(order_book_ids, start_date=None, end_date=None)
```

获取可转债在一段时间内是否停牌。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 可转债合约代码，可传入 `order_book_id` 或 `order_book_id` list。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |

#### 返回

pandas DataFrame，index 为交易日，columns 为可转债合约代码，值为 bool。

#### 特殊说明

- 单标的查询会按上市、退市日期裁剪边界；默认起点为上市日，默认终点为当前最新交易日或退市前一个交易日。多标的默认日期范围与 rqdatac 一致，为最近三个月。

### convertible.get_instrument_industry - 获取可转债所属行业

```python
ymm_data_sdk.convertible.get_instrument_industry(order_book_ids, source='citics', level=1, date=None, market='cn')
```

支持 `citics / citics_2019 / gildata` 和 `level=0/1/2/3`，返回对应正股在指定日期的行业分类 DataFrame。

`convertible.get_industry` 另外使用 `convertible_industry_taxonomy` 判断行业代码或名称是否合法，因此能够区分“合法行业但当日没有可转债”（返回 `[]`）和未知行业（返回 `None`）。taxonomy 保存三个 source 历史快照中出现过的一级、二级和三级代码及名称，并分别记录 rqdatac 是否接受代码和名称输入，以保留其少数历史名称不被接口接受的边界行为。

### convertible.get_industry - 获取行业内可转债

```python
ymm_data_sdk.convertible.get_industry(industry, source='citics', date=None, market='cn')
```

返回目标行业的可转债代码 list。指定日期时同时按行业有效区间和转债上市状态过滤；未指定日期时返回该行业涉及的全部历史可转债。

### convertible.get_accrued_interest_eod - 获取日终应计利息

```python
ymm_data_sdk.convertible.get_accrued_interest_eod(order_book_ids, start_date=None, end_date=None)
```

返回日期为 index、合约代码为 columns 的 pandas DataFrame。

### convertible.get_call_announcement - 获取赎回提示性公告

```python
ymm_data_sdk.convertible.get_call_announcement(order_book_ids, start_date=None, end_date=None, market='cn')
```

返回赎回和不赎回公告 DataFrame。

### convertible.get_close_price - 获取收盘净价和全价

```python
ymm_data_sdk.convertible.get_close_price(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

支持 `clean_price / dirty_price` 字段。

### convertible.get_indicators - 获取可转债衍生指标

```python
ymm_data_sdk.convertible.get_indicators(order_book_ids, start_date=None, end_date=None, fields=None)
```

支持 rqdatac 的全部 32 个字段，包括仍由 rqdatac 返回的废弃字段。

### convertible.get_credit_rating - 获取债项评级

```python
ymm_data_sdk.convertible.get_credit_rating(order_book_ids, start_date=None, end_date=None, institutions=None, market='cn')
```

支持按评级机构筛选，返回以 `order_book_id, credit_date` 为 MultiIndex 的 DataFrame。

### convertible.get_std_discount - 获取标准券折算率

```python
ymm_data_sdk.convertible.get_std_discount(order_book_ids, start_date=None, end_date=None, market='cn')
```

返回 `discount_factor` DataFrame。

## 金融、商品期货

### futures.get_dominant - 获取主力合约

```python
ymm_data_sdk.futures.get_dominant(underlying_symbol, start_date=None, end_date=None, rule=0, rank=1, market='cn')
```

获取某一期货品种在一段时间内的主力合约数据，可以查询主力、次主力、次次主力合约，并支持 `rule=0/1/2/3` 主力合约选取规则。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbol | str | 期货合约品种，例如沪深 300 股指期货为 `IF`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期；省略时从本地已有数据的最早日期开始。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；省略时返回到本地已有数据的最新日期。仅指定 `start_date` 时查询该日。 |
| rule | int | 主力合约选取规则，支持 `0/1/2/3`。 |
| rank | int | 合约排名，支持 `1/2/3`。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas Series，index 为日期，值为主力合约代码；无数据时返回 `None`。

### futures.get_dominant_batch - 批量获取主力合约

```python
ymm_data_sdk.futures.get_dominant_batch(underlying_symbol_list, start_date, end_date, rule=0, rank=1, market='cn')
```

批量获取多个期货品种在一段交易日期间内的主力、次主力或次次主力合约。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbol_list | str list | 期货品种列表，例如 `['IF', 'A', 'TF']`。输入统一转为大写，重复品种按首次出现的位置去重。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期，闭区间，必填。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期，闭区间，必填。 |
| rule | int | 主力合约选取规则，支持 `0/1/2/3`。 |
| rank | int | 合约排名，支持 `1/2/3`。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，固定包含以下三列：

| 字段 | 说明 |
| --- | --- |
| underlying_symbol | 期货品种，保持去重后的输入顺序。 |
| date | 交易日，dtype 为 `datetime64[ns]`。 |
| dominant | 指定 `rule/rank` 对应的真实主力合约代码；本地没有对应记录时为 `None`。 |

### futures.get_contracts - 获取可交易合约列表

```python
ymm_data_sdk.futures.get_contracts(underlying_symbol, date=None, market='cn')
```

获取指定期货品种在指定日期可交易的合约列表，返回值按合约代码排序。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbol | str | 期货合约品种，例如沪深 300 股指期货为 `IF`。 |
| date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询日期，默认为当日。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

`list[str]`，指定日期可交易的 `order_book_id` 列表；没有匹配合约时返回空列表。

### futures.get_contracts_batch - 批量获取期货合约链

```python
ymm_data_sdk.futures.get_contracts_batch(underlying_symbol_list, start_date, end_date, market='cn')
```

批量获取多个期货品种在一段交易日期间内的可交易合约列表。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbol_list | str list | 期货品种列表，例如 `['IF', 'A', 'TF']`。重复品种会按首次出现的位置去重。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期，闭区间。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期，闭区间。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，固定包含以下三列：

| 字段 | 说明 |
| --- | --- |
| underlying_symbol | 期货品种。 |
| date | 交易日，dtype 为 `datetime64[ns]`。 |
| future_chain | 当日可交易的 `order_book_id` list，按合约代码排序；没有匹配合约时为空列表。 |

#### 特殊说明

- 只返回 `trading_dates` 中 `is_trading = true` 的日期。每个去重后的品种在每个交易日返回一行。

### futures.get_ex_factor - 获取期货主力连续合约复权因子

```python
ymm_data_sdk.futures.get_ex_factor(underlying_symbols, start_date=None, end_date=None, adjust_method='prev_close_spread', rule=0, rank=1, market='cn')
```

获取期货主力连续合约复权因子数据。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbols | str or str list | 期货品种代码或代码列表。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期；不传入时返回全部。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；不传入时返回全部。 |
| adjust_method | str | 支持 `prev_close_spread`、`open_spread`、`prev_close_ratio`、`open_ratio`。 |
| rule | int | 主力合约选取规则，支持 `0/1/2/3`。 |
| rank | int | 合约排名，支持 `1/2/3`；实际可用组合与 rqdatac 保持一致。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，index 为 `ex_date`，包含以下字段：

| 字段 | 说明 |
| --- | --- |
| underlying_symbol | 期货品种代码。 |
| ex_factor | 单次主力切换复权因子。 |
| ex_end_date | 当前复权因子区间的截止日期。 |
| ex_cum_factor | 累计复权因子。 |

### futures.get_dominant_price - 获取期货主力连续合约行情

```python
ymm_data_sdk.futures.get_dominant_price(underlying_symbols, start_date=None, end_date=None, frequency='1d', fields=None, adjust_type='pre', adjust_method='prev_close_spread', rule=0, rank=1, time_slice=None)
```

获取一个或多个期货品种的主力连续合约行情，支持主力、次主力、次次主力及不同主力合约选取规则。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbols | str or str list | 期货品种代码或代码列表。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。两端均省略时查询最近三个月；仅指定起点时查询至起点三个月后。起点不得早于 `2010-01-04`。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。仅指定终点时查询此前三个月。 |
| frequency | str | 数据频率。本地支持 `1d`、`1m`、`5m`、`15m`、`30m`、`60m`、`120m`、`240m` 和 `tick`。 |
| fields | str or str list | 返回字段。省略时使用 rqdatac 对应频率的默认字段集合。 |
| adjust_type | str | 复权类型，支持 `none`、`pre`、`post`，默认为 `pre`。 |
| adjust_method | str | 复权方法，支持 `prev_close_spread`、`open_spread`、`prev_close_ratio`、`open_ratio`。 |
| rule | int | 主力合约选取规则，支持 `0/1/2/3`。 |
| rank | int | 合约排名，支持 `1/2/3`。 |
| time_slice | tuple or list | 分钟和 tick 的日内时间区间，支持跨午夜；日线传入时不生效并发出警告。 |

#### 返回

pandas DataFrame。日线 index 为 `underlying_symbol, date`，分钟和 tick index 为 `underlying_symbol, datetime`。首个数据列为当日真实合约代码 `dominant_id`；显式请求 `trading_date` 时，该列位于 `dominant_id` 之前。无数据时返回 `None`。

### futures.get_contract_multiplier - 获取期货品种合约乘数

```python
ymm_data_sdk.futures.get_contract_multiplier(underlying_symbols, start_date=None, end_date=None, market='cn')
```

获取一个或多个期货品种在指定日期范围内生效的交易所和合约乘数。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbols | str or str list | 期货品种代码或代码列表。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期；省略时从该品种最早生效日期开始。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；省略时默认为昨天。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，index 为 `underlying_symbol,date`，包含 `exchange` 和 `contract_multiplier` 两列。无有效品种时返回 `None`；有效品种在指定日期范围没有记录时返回同结构空 DataFrame。

### futures.get_exchange_daily - 获取期货交易所日线数据

```python
ymm_data_sdk.futures.get_exchange_daily(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

获取一个或多个真实期货合约的交易所日线行情。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 真实期货合约代码或代码列表。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期；两端均省略时查询最近三个月。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；仅指定一端时按 rqdatac 的三个月规则补齐另一端。 |
| fields | str or str list | 查询字段。默认返回全部交易所日线字段。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，index 为 `order_book_id,date`。默认字段为 `open`、`close`、`high`、`low`、`total_turnover`、`volume`、`settlement`、`prev_settlement`、`open_interest`；无行情时返回 `None`。

### futures.get_continuous_contracts - 获取期货连续合约

```python
ymm_data_sdk.futures.get_continuous_contracts(underlying_symbol, start_date, end_date, type='front_month', market='cn')
```

获取指定期货品种在一段时间内对应的近月、次月、季月或远季连续合约代码。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbol | str | 期货品种代码。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| type | str | 支持 `front_month`、`next_month`、`current_quarter`、`next_quarter`。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas Series，index 名为 `date`，Series 名为 `order_book_id`，值为当日对应的真实期货合约代码。没有匹配记录时返回空 Series。

#### 特殊说明

- `front_month/next_month` 覆盖 rqdatac 实际支持的商品和股指品种；`current_quarter/next_quarter` 只有 `IF/IH/IC/IM` 返回数据。国债期货及不支持的组合与 rqdatac 一样返回空 Series。

### futures.get_member_rank - 获取期货会员持仓排名

```python
ymm_data_sdk.futures.get_member_rank(obj, trading_date=None, rank_by='volume', **kwargs)
```

获取指定期货合约或品种的会员成交量、持买仓量或持卖仓量排名。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| obj | str | 期货合约代码或期货品种代码。 |
| trading_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 交易日期。 |
| rank_by | str | 排名类型，支持 `volume`、`long`、`short`。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 通过关键字参数传入的开始日期，用于查询日期区间。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 通过关键字参数传入的结束日期，用于查询日期区间。 |

#### 返回

pandas DataFrame。返回字段及结构由 rqdatac 根据 `rank_by` 和查询对象确定。

### futures.get_warehouse_stocks - 获取期货仓单数据

```python
ymm_data_sdk.futures.get_warehouse_stocks(underlying_symbols, start_date=None, end_date=None, market='cn')
```

获取一个或多个期货品种在指定日期范围内的交易所仓单数据。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbols | str or str list | 期货品种代码或代码列表。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| market | str | 市场，默认为中国内地市场 `cn`。 |

#### 返回

pandas DataFrame。index 为 `date,underlying_symbol`，字段包括仓单数量、交易所、有效预报、仓单单位、合约乘数和可交割数量等。

### futures.get_basis - 获取股指期货基差数据

```python
ymm_data_sdk.futures.get_basis(order_book_ids, start_date=None, end_date=None, fields=None, frequency='1d', dividend_adjusted=False, market='cn')
```

获取一个或多个股指期货合约的基差、基差率和年化基差率等数据。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股指期货合约代码或代码列表。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| fields | str or str list | 返回字段；省略时返回 rqdatac 默认字段集合。 |
| frequency | str | 数据频率，默认为日线 `1d`；具体支持范围由 rqdatac 决定。 |
| dividend_adjusted | bool | 是否使用分红调整后的指数价格计算基差。 |
| market | str | 市场，默认为中国内地市场 `cn`。 |

#### 返回

pandas DataFrame。日线 index 为 `order_book_id,date`；字段由 `fields` 和 rqdatac 对应频率的默认集合决定。

### futures.get_trading_parameters - 获取期货交易参数

```python
ymm_data_sdk.futures.get_trading_parameters(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

获取期货保证金、手续费、持仓限额和最小下单量等交易参数。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 一个或多个真实期货合约代码；连续合约不返回数据。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期，必须与 `end_date` 同时传入；两端均省略时查询当前交易日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期，必须与 `start_date` 同时传入。 |
| fields | str or str list | 返回字段；省略时返回全部字段。 |
| market | str | 市场，默认为中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，index 为 `order_book_id,trading_date`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| long_margin_ratio | float | 多头保证金率。 |
| short_margin_ratio | float | 空头保证金率。 |
| commission_type | str | 手续费类型，按成交量或按成交额。 |
| open_commission | float | 开仓手续费或费率。 |
| close_commission | float | 平仓手续费或费率。 |
| discount_rate | float | 平今折扣率。 |
| close_commission_today | float | 平今仓手续费或费率。 |
| non_member_limit_rate | float | 非期货会员持仓限额比例。 |
| client_limit_rate | float | 客户持仓限额比例。 |
| non_member_limit | float | 非期货会员持仓限额。 |
| client_limit | float | 客户持仓限额。 |
| min_order_quantity | float | 最小开仓下单量。 |
| max_order_quantity | float | 最大开仓下单量。 |
| min_margin_ratio | float | 最低交易保证金。 |
| trade_unit | str | 交易单位。 |
| price_unit | str | 报价单位。 |

#### 特殊说明

- 数据日期从 `2009-01-05` 开始。

### futures.get_roll_yield - 获取商品期货展期收益率

```python
ymm_data_sdk.futures.get_roll_yield(underlying_symbols, start_date=None, end_date=None, type='main_sub', rule=0, market='cn')
```

获取一个或多个商品期货品种的展期收益率。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbols | str or str list | 商品期货品种代码或代码列表。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期；两端均省略时返回最近三个月数据。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期；两端均省略时返回最近三个月数据。 |
| type | str | 计算类型：`main_sub` 表示主力与次主力，`near_main` 表示近月与主力。 |
| rule | int | 主力合约选取规则，与 `futures.get_dominant` 的 `rule` 一致。 |
| market | str | 市场，默认为中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，index 为 `underlying_symbol,date`，包含 `from_contract`、`to_contract`、`yield`、`annualized_yield` 和 `annualized_yield_trading`。

### futures.get_predicted_dividend_point - 获取股指期货分红点位预测

```python
ymm_data_sdk.futures.get_predicted_dividend_point(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取一个或多个股指期货合约的分红点位预测数据。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| order_book_ids | str or str list | 股指期货合约代码或代码列表。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期。 |
| market | str | 市场，默认为中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，index 为 `order_book_id,date`，包含 `dividend_points` 字段；无数据时返回 `None`。

## 金融、商品期权

### options.get_contracts - 筛选期权合约

```python
ymm_data_sdk.options.get_contracts(underlying, option_type=None, maturity=None, strike=None, trading_date=None)
```

根据期权标的、期权类型、到期月份、行权价和交易日期筛选期权合约。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying | str | 期权标的。可以填写 `M` 这样的期货品种代码，也可以填写 `M1901` 这样的具体期货合约代码；金融期权可以填写 `510050.XSHG`、`510300.XSHG` 或 `000300.XSHG` 等标的代码。只支持单个标的。 |
| option_type | str | `C` 代表认购或看涨期权，`P` 代表认沽或看跌期权；省略时返回全部类型。 |
| maturity | str or int | 期权到期月份，格式为 `YYMM`。该月份指期权自身的到期月份，不是标的期货的交割月份。 |
| strike | float | 行权价。按每个到期日分别向左靠档，返回不大于输入值的最高可用行权价。 |
| trading_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 查询日期；省略时在全部历史合约中筛选。 |

#### 返回

符合条件的期权 `order_book_id` 列表；没有符合条件的合约时返回空列表 `[]`。

#### 特殊说明

- `underlying='M'` 按期货品种查询；`underlying='M1901'` 按具体标的期货合约查询。商品期权、股指期权和 ETF 期权使用同一接口。
- 指定 `trading_date` 时只返回当日已经上市且尚未退市的合约。
- `510050.XSHG`、`510300.XSHG` 和 `159919.XSHE` 在同时指定 `strike` 与 `trading_date` 时支持除权后的历史行权价变化。

### options.get_contracts_batch - 批量获取期权合约链

```python
ymm_data_sdk.options.get_contracts_batch(underlying_list, start_date, end_date, option_type=None, maturity=None, strike=None)
```

批量获取多个期权标的在一段交易日期内的期权合约链。这是 ymm_data_sdk 扩展方法，rqdatac 没有对应的批量接口。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_list | str list | 期权标的列表。每项既可以是 `M` 这样的期货品种，也可以是 `M1901`、`510050.XSHG` 或 `000300.XSHG` 这样的具体标的代码。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期，必填。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期，必填。 |
| option_type | str | `C` 或 `P`；省略时返回全部类型。 |
| maturity | str or int | 期权自身的到期月份，格式为 `YYMM`。 |
| strike | float | 行权价；按每个交易日和到期日分别向左靠档。 |

#### 返回

pandas DataFrame，固定包含三列：

| 字段 | 说明 |
| --- | --- |
| underlying | 输入的期权标的。 |
| date | 交易日期；非交易日不返回。 |
| option_chain | 当日符合条件的期权代码列表；没有合约时为空列表。 |

### options.get_contract_property - 获取 ETF 期权合约属性

```python
ymm_data_sdk.options.get_contract_property(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

获取 ETF 期权每日合约属性，用于追踪标的除权后发生变化的期权简称、合约乘数和行权价。

#### 返回

pandas DataFrame，index 为 `order_book_id,trading_date`，字段包括 `product_name`、`symbol`、`contract_multiplier` 和 `strike_price`。

#### 特殊说明

- 查询时使用本地交易日历展开属性有效区间，不保存逐日重复快照。
- 仅支持交易所 ETF 期权；商品期权和股指期权没有对应数据时返回 `None`。

### options.get_dominant_month - 获取商品期权主力月份

```python
ymm_data_sdk.options.get_dominant_month(underlying_symbol, start_date=None, end_date=None, rule=0, rank=1, market='cn')
```

获取商品期权一段时间的主力或次主力月份。日内不会发生主力月份切换。

#### 返回

pandas Series，index 名为 `date`，以 `YYYYMMDD` 整数表示交易日，Series 名为 `dominant`。

#### 特殊说明

- 覆盖 `rule=0/1` 和 rank 1/2；与 rqdatac 一样，任何 `rank != 1` 的输入均按次主力月份处理。
- 当前仅支持商品期权；无主力月份记录时返回 `None`。

### options.get_greeks - 获取期权风险指标

```python
ymm_data_sdk.options.get_greeks(order_book_ids, start_date=None, end_date=None, fields=None, model='implied_forward', price_type='close', frequency='1d', market='cn')
```

获取基于 BS 模型计算的期权风险指标，字段包括 `iv`、`delta`、`gamma`、`vega`、`theta` 和 `rho`。

#### 特殊说明

- 目前仅支持 `model='implied_forward'` 与 `price_type='close'`；其他组合抛出 `NotImplementedError`。
- 日频覆盖全部期权；1m 覆盖 CFFEX `IO/HO/MO` 和 ETF 期权，商品期权 1m 无数据，返回 `None`。

### options.get_dominant_month_batch - 批量获取商品期权主力月份

```python
ymm_data_sdk.options.get_dominant_month_batch(underlying_symbol_list, start_date, end_date, rule=0, rank=1, market='cn')
```

批量获取多个商品期权品种在一段交易日期间内的主力或次主力月份。

#### 参数

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| underlying_symbol_list | str list | 商品期权品种列表，例如 `['M', 'CU', 'SC']`。输入统一转为大写，重复品种按首次出现的位置去重。 |
| start_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 开始日期，闭区间，必填。 |
| end_date | int, str, datetime.date, datetime.datetime, pandas.Timestamp | 结束日期，闭区间，必填。 |
| rule | int | 主力月份选取规则，支持 `0/1`。 |
| rank | int | `1` 表示主力月份；与 rqdatac 单品种方法一致，其他值按次主力月份处理。 |
| market | str | 市场，当前支持中国内地市场 `cn`。 |

#### 返回

pandas DataFrame，固定包含以下三列：

| 字段 | 说明 |
| --- | --- |
| underlying_symbol | 商品期权品种，保持去重后的输入顺序。 |
| date | 交易日，使用 `int64 YYYYMMDD`，与单品种方法的日期 index 表示一致。 |
| dominant | 指定 `rule/rank` 对应的真实主力月份合约代码；本地没有对应记录时为 `None`。 |

#### 特殊说明

- 仅返回 `trading_dates.is_trading = true` 的日期。每个去重后的品种在每个交易日固定返回一行，因此缺失主力月份可以被明确识别为 `None`。
- 未知标的抛出 `ValueError("Unknown underlying")`；日期范围内没有交易日时返回同列结构的空 DataFrame。

### options.get_indicators - 获取期权衍生指标

```python
ymm_data_sdk.options.get_indicators(underlying_symbols, maturity, start_date=None, end_date=None, fields=None, market='cn')
```

获取指定标的和到期月份的成交额、持仓量、成交量 PCR，以及偏度和 0.25 delta 隐含波动率。

#### 返回

pandas DataFrame，index 为 `underlying_symbol,date`。可用字段为 `AM_PCR`、`OI_PCR`、`VL_PCR`、`skew`、`iv_025_dela` 和 `iv_minus_025_dela`。

#### 特殊说明

- 覆盖商品期权、CFFEX 金融期权和 ETF 期权。
- 默认日期以及只指定起点或终点时，使用与 rqdatac 一致的三个月日期规则。
- 若请求期间内某个指标没有任何有效值，与 rqdatac 一样不返回该空列。

## 指数、场内基金

### index_indicator - 获取指数每日估值指标

```python
ymm_data_sdk.index_indicator(order_book_ids, start_date=None, end_date=None, fields=None, market='cn')
```

获取部分市值加权指数的市盈率、市净率、市值及股息率等每日估值指标。

### index_components - 获取指数成分

```python
ymm_data_sdk.index_components(order_book_id, date=None, start_date=None, end_date=None, return_create_tm=False, market='cn')
```

查询指定日期或日期区间的指数成分股；`return_create_tm=True` 时同时返回数据入库时间。

### index_weights - 获取指数历史成分及权重

```python
ymm_data_sdk.index_weights(order_book_id, date=None, start_date=None, end_date=None, market='cn')
```

获取指数月度更新的历史成分及权重。

### index_weights_ex - 获取指数日度权重

```python
ymm_data_sdk.index_weights_ex(order_book_id, date=None, start_date=None, end_date=None, market='cn')
```

获取 rqdatac 支持指数的日度动态权重。

### etf.get_components - 获取 ETF 申赎清单

```python
ymm_data_sdk.etf.get_components(order_book_ids, date=None, market='cn')
```

获取一个或多个 ETF 在指定交易日的申赎成分清单。

### etf.get_cash_components - 获取 ETF 现金差额

```python
ymm_data_sdk.etf.get_cash_components(order_book_ids, start_date=None, end_date=None, market='cn')
```

获取 ETF 的现金差额、预估现金差额、最小申赎单位资产净值等数据。
