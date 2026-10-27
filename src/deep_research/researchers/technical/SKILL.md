---
name: researcher_technical
display_name: 技术面研究员
category: research
required_tools: ['get_daily_history', 'analyze_trend']
data_preparation: code
---

# 技术面研究员

见模块 `src/deep_research/researchers/technical/`（分析框架/契约/解析）。

自 issue #56 起为单轮试点：行情、日线、已计算指标（MA/MACD/RSI/量能）与缠论
结构事实由代码经既有有界入口准备后整体注入，模型执行一次无工具循环的结构化
分析；无可解释有效资料时不启动模型，局部缺项如实披露。试点轮职责限定为固定
资料的分区解读（唯一 JSON 输出）：评级/目标价/时间维度、交易行动词表与免责
声明由既有规则与聚合层负责；既有统计展示直接引用，等值单位展示保留原值含义
与币种，缺项与未给的统计范围不推定；时点按已给时点/财期作解读，未给更新核验
结果不声称全局最新，时效讨论写明所依据时钟，财期截止日不当作观测或披露时间。
