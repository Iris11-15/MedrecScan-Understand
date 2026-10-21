"""wide —— 病例信息科研总结表产线（六阶段，阶段间用文件交接）。

① s1_ocr        PDF → 逐页 PNG → PaddleX 版面 OCR → 每页 JSON
② s2_preprocess 块文本清洗/纠错 + 表格 HTML → 单元格矩阵 + 空白页标记
③ s3_extract    首页字段候选 + 首程「主诉/查体」原文（LLM 温度 0）
④ s4_normalize  首页实际标签 → 标准列（映射表落盘、带版本）
⑤ s5_qc         校验、缺失率、异常值、变量字典
⑥ s6_table      宽表 CSV/XLSX + 审计 JSONL
"""
