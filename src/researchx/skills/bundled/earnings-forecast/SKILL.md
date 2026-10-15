---
name: earnings-forecast
description: 基于完整财年合并收入及逐项假设，执行未来三年基准、乐观、谨慎盈利预测与单年度敏感性校验。
skill_id: earnings-forecast
version: 1.0.0
owner: ResearchX
permissions: [file_read, file_write, shell_execution]
required_tools: [read_file, write_file, bash]
optional_tools: [skill, research_memory, web_search, web_fetch, tool_search, MCP]
compatible_models: [text, tool_calling]
scope: current_conversation
status: active
published_at: '2026-10-09'
deprecation: null
content_hash: null
---

# 三年盈利预测

## 适用与禁用场景
用于A股非金融公司简化利润情景模型。禁止把季度累计收入当全年基期、把预测当实际、
输出自主评级或目标价；不推演完整三大表，也不自动取得付费数据。

## 输入要求
公司身份、带时区的资料截止时间、最近已结束完整财年合并收入（单位、币种、来源定位），
base/optimistic/cautious 各连续三个财年假设。每项含 value、rationale、origin、references；
未知保持 null 和原因。可使用 financial-statement-analysis 当前会话结构化结果。

## 主流程
1. 基期核对时才读取 [预测口径](references/forecast.md)，检查期间、合并口径、币种与来源。
2. 输入准备时读取 [JSON 契约](templates/input.schema.json)；[样例](templates/input.json) 仅展示格式，不能充当事实。
   为兼容原计算，JSON 保留 kind=deep、DeepResult 字段；独立预测允许 sections=[]。
3. 输入准备好后执行 [预测脚本](scripts/forecast.py)：
   `"<SkillTool返回的Python解释器>" "<本Skill根目录>/scripts/forecast.py" --input "<输入JSON>" --output "<会话输出目录>/forecast/computed.json"`。
   不需要读取或注入脚本源码；需要契约时执行 --schema。
4. 通过 read_file 分段读取结果，检查 status/gaps、forecasts、sensitivity、formulas、assumptions、base_references。
5. 报告 Skill 直接消费 computed.json。需要单独下载时按 [结果结构](templates/report.md) 使用
   [导出脚本](scripts/export_report.py) --input "<结果JSON>" --output-dir "<会话输出目录>/forecast"；不重新计算。

## 完成标准
三个情景各三年，显式假设、元单位、币种、基期和计算公式可追溯；敏感性为单年度冲击。
可用产物包含计算 JSON 和缺口，来源用真实 locator/page 或当前会话真实 ID，不伪造 ID。

## 关键约束与失败处理
缺基期、币种或关键假设时不产生该预测链，结果 partial；缺可靠股本时 EPS=null。
零分母、不合法比例、未知假设键、非连续年份应按脚本错误修正；不要手算绕过校验。
前置 Skill 被禁用时列缺口，不自行开启。权限声明只是需求，工具授权仍由运行时执行。
