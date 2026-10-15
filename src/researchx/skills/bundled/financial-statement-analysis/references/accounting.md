# 会计取数与计算规范

核心科目键见输入schema；不得添加未定义键。资产负债表：cash、receivables、inventory、current_assets、total_assets、current_liabilities、total_liabilities、total_equity、parent_equity、opening_parent_equity。
利润表：revenue、cost、tax_surcharges、selling_expense、admin_expense、rd_expense、finance_expense、other_operating_net、operating_profit、non_operating_net、pretax_profit、income_tax、net_profit、parent_net_profit、minority_profit、adjusted_parent_profit。
现金流量表：operating_cashflow、investing_cashflow、financing_cashflow、fx_cash_effect、net_cash_change。
每个period表示同一期、币种和报表口径。资产负债数据为期末，opening_parent_equity为期初。`rounding_unit`表示原披露舍入精度折合人民币元，例如单位万元且小数两位为100元。
同币种统一换算为元后运算。非CNY币种保留币种，不隐式汇率换算。
毛利率=(收入-成本)/收入；合并净利率=合并净利/收入；负债率=总负债/总资产；流动比率=流动资产/流动负债；现金利润比=经营净现金流/合并净利。
简化ROE=归母净利润/((期初归母权益+期末归母权益)/2)，不等于披露的加权平均ROE；不年化。
勾稽检查：资产=负债+总权益；利润总额=营业利润+营业外净额；税前利润=合并净利+所得税；合并净利=归母+少数股东损益；现金净变动=经营+投资+筹资+汇率影响。
每项舍入误差以披露精度一半计，汇总容差为参与项数乘以半个精度。不强迫数据平衡，失败标待复核。
期初权益缺失不能用期末权益替代。原文是破折号时核对是否明确为零，否则记未知。
