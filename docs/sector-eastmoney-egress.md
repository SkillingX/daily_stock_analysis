# 板块专项东财公共行情出站代理（SECTOR_EASTMONEY_PROXY_URL）

2026-11-08。**部署可选能力；未配置不注入代理/redirect 选项、既有行为保持；不是"已修复生产直连故障"的声明。**

## 背景与边界

2026-11-08 生产只读诊断（A直连-B临时受控出口-A直连的路径对照）观察到：同一生产
进程直连东财公共行情 API（push2/push2his 家族）存在**路径相关**的访问失败
（clist 断连、stock/get 502、原 BK kline 断连），经临时受控出口同路径可取得合格
数据。诊断用原始证据与分析记录属一次性工作产物（存放于本机工作目录，**不作为
本仓库的常设可访问链接**），此处只保留有边界的结论：这是路径对照证据，不是对
供应商拒绝规则的证明，也不是永久修复的验收。本能力提供的是：给板块专项对东财
公共行情的出站配置一个**可选、作用域受限**的正向代理出口，等待获授权的稳定
服务端出口到位后即可启用；无授权资源时保持既有直连行为不变。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `SECTOR_EASTMONEY_PROXY_URL` | 空（未设置） | 板块专项东财公共行情出站正向代理；空=既有直连行为不变 |

- 合法形态：`http://[user:pass@]host[:port]` 或 `https://...`（基于已安装
  requests 的标准正向代理能力，不新增依赖、不内置代理进程）。主机按
  `ipaddress`/DNS 形态做语法校验（localhost、IPv4/IPv6、内网短名与多段域名
  均支持）；控制字符在**归一化前**拒绝（换行前缀等形态不放行）。
- 认证 userinfo（如需）允许配置，但**只写在部署侧**（本机私有 `.env` 或部署
  凭据注入）：配置值属部署敏感信息，不得写入仓库、日志、报告、截图或工单。
- 无效配置（scheme 非 http/https、缺主机、主机语法非法、带 path/query/fragment、
  控制字符、端口越界）在进程启动加载配置时**显式报错**，错误消息只含变量名、
  绝不回显配置值、不链原始解析错误。
- 系统设置页该键按敏感字段掩码显示（server-side masked、只读），密钥类推断
  不覆盖此键名，已显式注册（`src/core/config_registry.py`）；Config dataclass
  字段 `repr=False`（不进生成 repr）；**`export_env()` / 桌面端导出备份
  以 dotenv 语法感知方式按完整赋值 span 重组并剔除该键**（`export `
  前缀、单/双引号键、完整多行引号值、重复赋值、顶层历史注释赋值行；与
  ConfigManager 同一解析引擎定位 span），其余内容逐字节保留（无关多行
  值内的赋值形注释、U+2028、无尾换行、BOM）。**无法证明已安全剔除时
  导出显式失败**（`ConfigExportError`，错误只含安全语义、不含原始语句/
  值/原因链）：畸形语句（未闭合引号/尾随垃圾等）中出现该键名、解析失败、
  span 序列不完整时一律拒绝导出、绝不返回未过滤原文；不含该键名的无关
  畸形语句原样保留。部署侧 `.env` 本身不受影响。
- 修改后需重启对应进程（调度/API/Web 常驻进程不会热加载此值；调度器任务执行
  前的配置重载会重新读取进程环境与 `.env`，但已在线程上下文中的取数不受影响）。

## 作用域（精确主机 allowlist）

仅当板块专项取数 worker 真正发出对以下主机的出站时，才使用该代理：

- `push2.eastmoney.com`、`push2his.eastmoney.com`
- 上述两个主域的数字分片子域（如 `17.push2.eastmoney.com`、`7.push2his.eastmoney.com`）

主机按 URL 正确解析后**精确匹配**（大小写规范化）；`evilpush2.eastmoney.com`、
`push2.eastmoney.com.attacker.com` 等子串/后缀欺骗形态不匹配。以下流量**不路由**，
保持既有路径与数据源优先级：

- datacenter F10（`datacenter.eastmoney.com`）、东财授权接口（anonflow2）、
  搜索/文章、腾讯基准行情、iFinD、Choice/MX、LLM 等一切非板块行情出站
- 非板块专项路径对同名主机的访问（个股研究等共享 fetcher 路径不受影响——
  路由按"板块 worker 线程上下文 + 主机 allowlist"双条件判定，不因"正在生成
  板块报告"而路由全部请求）

实现边界：ContextVar（`src/patches/eastmoney_patch.py` 的
`sector_eastmoney_proxy`）由板块取数 worker（`_ak_with_retry` /
`_http_get_json`）在**实际执行线程内** bind/reset（executor submit 不继承调用方
上下文）；现有 `requests.Session.request` 预算边界消费该上下文。请求级显式
`proxies` 覆盖进程 `HTTP(S)_PROXY`/`NO_PROXY` 合并结果（进程环境变量、Session
全局默认、原 SDK 优先级零改动）。

## 失败与重定向语义

- scoped 代理失败（连接拒绝、407、TLS 失败等）按既有供应商失败路径显式记
  缺口/降块，**无隐藏直连回退**；既有跨供应商 fallback（如腾讯基准）仅按其
  既有源/缺口契约继续可用，不视为独立东财替代源。
- scoped 请求**不跟随重定向**（`allow_redirects=False`）：3xx（含合法形状的
  响应体）按显式供应商失败处理（`SectorScopedRedirectError`），不把 scoped
  路径泄漏到重定向目标域，不采纳重定向响应体。
- **HTTP 4xx/5xx 在共享传输边界、任何 SDK 解析响应体之前**按显式供应商失败
  拒绝（requests `HTTPError` 类别）：已安装的 AkShare `stock_board_industry_hist_em`
  等公共接口解析 `r.json()` 前不查状态码，HTTP 502 + 合法形状响应体会被
  直接采为业务数据；scoped 边界拒绝后响应体不进任何 SDK/业务（同体在
  HTTP 200 下正常采纳，由回归对照锁定这一行为差异）。
  未配置代理时此边界不生效，既有 `raise_for_status` 等行为保持。
- TLS 证书校验零改动（不注入 `verify` 覆盖）。
- 异常诊断在传播前按**实际渲染状态**脱敏再截断（先 redact 后 truncate）：
  递归覆盖异常树的 `args` 字符串、OSError 渲染字段（`strerror`/`filename`/
  `filename2`——构造时快照、事后改 `args` 不回填）、urllib3 `reason`
  `%r` 渲染路径、`__cause__`/`__context__` 链接段，以及 URL 变体
  （netloc/host:port/userinfo/凭证及其 % 编码形态）；外层 `str`/`repr`、
  格式化 traceback、报告缺口文本、日志、系统设置展示、Config repr 均
  不出现配置 URL/user/password。
- 异常类别、errno 语义与预算/预算截止语义保留：脱敏只改渲染字段，
  不吞错、不降级、不改变异常类型。
- 预算/截止/停止/迟到结果丢弃/数据资格化/严格 schema/partial 语义全部不变。

## 回滚

- 运行时回滚：清空/移除 `SECTOR_EASTMONEY_PROXY_URL` 并重启对应进程即恢复
  既有直连路由（未配置时不注入代理/redirect 选项，既有请求选项与优先级
  保持；patch 边界仍在 `Session.request` 上，由既有回归测试保护）。
- 代码回滚：本改动合入后 revert 相应 commit 即可（配置键未配置时全链路行为
  与既有一致，revert 无数据迁移问题）。**不要**执行 `git clean -fd` 或
  整树 discard 之类的粗粒度回滚——那会连带丢弃工作区内无关的未提交成果；
  需要部分回滚时按文件 revert 本改动涉及的文件即可。

## 验收边界（如实声明）

- 已覆盖（离线确定性测试，`tests/test_sector_eastmoney_egress.py`，全部回环
  fixture、**进程级测试隔离**——TCP 仅放行 fixture 持有的精确回环端口、
  DNS 仅放行数字地址与 fixture 主机映射、系统代理自动发现在测试进程内
  neutralize（本机回环上可能存在可中转外网的系统代理，回环放行≠隔离；
  含未持有回环端口不可用的哨兵负向对照）、`ENV_FILE=/dev/null` + 隔离存储）：
  配置校验与只含变量名的错误（含原始输入控制字符先于 strip 拒绝、主机语法
  校验）、主机 allowlist 完整矩阵、`_http_get_json` 与 `_bounded_ak` 两条
  worker 路径经真实回环代理 fixture 观测、环境 HTTP(S)_PROXY/NO_PROXY 干扰下
  scoped 代理仍生效、**真实 AkShare `stock_board_industry_hist_em` 经伪代理
  的状态边界对照（同一合法形状 klines 体：HTTP 200 采为 DataFrame；
  HTTP 407/502 在 SDK 解析前显式拒绝，wire 状态码与请求数被观测）**、
  3xx 不跟随且响应体不采纳、畸形响应/连接失败/预算耗尽显式失败且零直连
  回退、未配置/非板块消费者的 wire 级直连观测、**自签证书 MITM fixture 证明
  证书校验保留**（校验在位时握手在证书链处失败、隧道内零 HTTP 请求；负向
  对照强制 `verify=False` 时同一 fixture 会被采纳伪造体——即本断言对校验
  降级敏感；CONNECT 观测本身只证明路由）、带凭证代理错误在外层 `str`/
  既有 gap 截断/格式化 traceback/日志零泄漏（先脱敏后截断；覆盖 errno/
  strerror/filename OSError 渲染字段、嵌套/包装/链接异常树与 URL 变体）、
  并发 sector/非 sector 隔离与执行器复用无上下文泄漏、registry/系统设置
  掩码展示、Config repr 与两路导出剔除（dotenv span 级 + 导出→回析等价）、
  导入/更新语义保持。
  以上为**回环行为检查**，不构成生产/真实出口验收，也不是全仓离线套件的
  替代。
- **未验收（显式缺口，不冒称已修复）**：
  - 真实稳定服务端出口的多时点生产验收（当前无授权资源；诊断用临时隧道
    已关闭，不作为产品基础设施）；
  - 真实出口下的完整报告生成、板块行情快照、所有供应商与健康度；
  - 长期出口稳定性、供应商访问策略随时间的变化。
- 依赖关系如实声明：AkShare 与 Efinance 对**同一东财公共源家族**的调用
  （板块内外、个股研究、排行、深研共享上下文等）**互不为独立 fallback**——
  同源同故障域；本能力不改变它们的路径。Choice 原生 SDK / iFinD 是**独立
  通道**，有各自的覆盖面/口径/契约，其健康状态与公共 HTTP 不能互相推断。
  其他 AkShare 函数可能使用不同主机家族，不因包名一概断言故障。

## 工具风险矩阵（静态依赖 ≠ 当前实时故障 ≠ 已验证路径）

| 数据用途 | 通道 | 依赖主机家族 | scoped 代理覆盖 | 当前状态口径 |
| --- | --- | --- | --- | --- |
| 板块主行业身份/成员（F10） | 公共 HTTP datacenter | datacenter.eastmoney.com | 否（保持直连） | 诊断期可用性未经本轮对照；未验收 |
| 板块单标的行情（stock/get） | 公共 HTTP push2 | push2.eastmoney.com | 是 | 生产直连存在路径相关失败；代理路径待真实出口验收 |
| 板块分钟累计 backup（trends2） | 公共 HTTP push2his | push2his.eastmoney.com | 是 | 同上 |
| 板块日线（AkShare） | 公共 HTTP push2his 分片 | \*.push2his.eastmoney.com | 是 | 临时受控出口下 20/20 合格（诊断证据，非长期验收） |
| 基准日线 fallback（AkShare `stock_zh_index_daily_tx`） | 腾讯 | proxy.finance.qq.com（ifzqgtimg kline 路由） | 否 | 既有契约；独立供应商源家族，不能替代东财 BK 板块身份/日线 |
| F10 成员实时行情补采 | 腾讯 | qt.gtimg.cn/q | 否 | 与基准日线是**不同路由**（实时批量行情 vs 日线），既有契约 |
| 深研共享行业资金上下文 | efinance/akshare 排行 | push2 家族 | 否 | 当前 `include_sector_flows=False`，不得断言仍实时调用已退役排行路由；两者与东财公共源互不为独立 fallback |
| 个股研究/排行 | efinance/akshare_fetcher | push2 家族 | 否 | 静态依赖存在；本轮未改变其路径，实时健康未逐工具验证（不因包名断言全部函数故障） |
| 基本面 Choice 快照 | Choice 原生 SDK | 东方财富专用通道 | 否 | 与公共 HTTP 是不同通道，各有覆盖/口径/契约，健康状态不能互相推断 |
