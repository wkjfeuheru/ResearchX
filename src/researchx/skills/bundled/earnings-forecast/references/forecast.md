# 三年利润模型

基期用最新已结束完整财年的合并收入，不用季报累计数。三种情景base/optimistic/cautious，每种连续三个财年，所有假设逐项提供value、rationale、origin和references。
必需键：growth、gross_margin、selling_rate、admin_rate、rd_rate、finance_rate、tax_surcharge_rate、other_operating_net、non_operating_net、tax_rate、parent_share。
各rate/share用小数而非百分数，growth=-0.1表示下降10%；其他经营/营业外净额以元表示，不得隐式置0。没有这些输入时保持缺口。
收入_t=收入_(t-1)*(1+growth)。毛利=收入*gross_margin。营业利润=毛利-收入*(selling_rate+admin_rate+rd_rate+finance_rate+tax_surcharge_rate)+other_operating_net。
税前利润=营业利润+non_operating_net；合并净利润=税前利润*(1-tax_rate)；归母净利润=合并净利*parent_share。
parent_share为显式简化归母比例，不推演完整权益及少数股东结构。tax_rate为有效税率假设，亏损期税负/税收收益需在假设解释中明确，不能宣称完整会计预测。
可选shares须有可靠股本依据且>0，EPS=归母净利润/shares。缺股本不计算EPS。
敏感性按每个基准预测年分别对收入增速与毛利率加减2个百分点，其他假设和上年收入保持基准，不宣称多年度冲击路径。
不预测资产负债表和现金流、不生成自主评级/目标价。输入不充分则预测未完成，正文已知部分仍可交付。
