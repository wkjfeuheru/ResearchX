# 事件分类与评分

merger=并购重组，shareholding=增减持，earnings_guidance=业绩预告/快报，financial_report=财报，financing_dividend=融资分红，litigation_regulation=诉讼监管，operating_contract=经营合同，management=人事，other=其他。
文本情绪与基本面影响分别评估，范围-2明显负面、-1轻度负面、0中性/均衡、1轻度正面、2明显正面。
0要求有理由认定影响均衡；影响未知使用null，并说明缺少哪些条款/规模/经营信息。
置信度high=原文清晰且所需数据充分，medium=可用但存在限制，low=摘要/冲突/关键条件不明。
评分是可审计的模型判断，不是价格预测或机械情绪交易信号。每项给reason，关联事件原文references。
