#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预留接口 —— 任务书「不做」清单在这里登记插桩点，**本轮一律不实现**。

调用任何一个函数都会抛 NotImplementedError，并说明该在哪一阶段接进去。
存在的意义只有两个：① 范围边界写进代码，不靠口头承诺；② 后续接手时不用猜插桩位置。

范围外的五组能力（2026-10-09 任务书 §一「不做」）：
  1. 检验(lab) / 用药(medication) / 诊断(diagnosis) 三组字段抽取
  2. ICD-10 / RxNorm / ATC / MedDRA / LOINC 编码映射
  3. RAG / Embedding 检索 / Reranker
  4. CDISC-CDASH / REDCap / OMOP 目标模型导出
  5. 训练集 / 验证集分离

插桩位置（现有六阶段不变，扩展只往两头接）：
  阶段③ s3_extract   每组一个 extract_group()，产物落 03_extract/<case>/<group>.json
  阶段④ s4_normalize 编码映射在标准列写完之后跑，产物落 07_coding/<case>/codes.json
  阶段⑤ s5_qc        训练/验证分离在 _summary 里加 split.csv，只打标不改数据
  阶段⑥ s6_table     目标模型导出另出文件，宽表 wide.csv 的 34 列定义不动（config/columns.yaml）
"""
from __future__ import annotations

GROUPS = ("lab", "medication", "diagnosis")
CODE_SYSTEMS = ("icd10", "rxnorm", "atc", "meddra", "loinc")
TARGET_MODELS = ("cdisc_cdash", "redcap", "omop")
RETRIEVAL_KINDS = ("rag", "embedding", "reranker")


class NotInScope(NotImplementedError):
    """范围外能力被调用时抛这个 —— 消息里写清楚缺什么、往哪接。"""


def _nope(what: str, where: str) -> None:
    raise NotInScope(f"{what} 本轮未实现（任务书范围外）。插桩点：{where}")


def extract_group(group: str, case_dir, cfg: dict) -> dict:
    """检验 / 用药 / 诊断组抽取。接在阶段③，每组一份 <group>.json。"""
    if group not in GROUPS:
        raise ValueError(f"未知组 {group!r}，可选：{', '.join(GROUPS)}")
    _nope(f"{group} 组抽取", "src/wide/s3_extract.py 之后，落 03_extract/<case>/%s.json" % group)


def map_code(system: str, value: str, case_id: str = "") -> dict:
    """标准编码映射。接在阶段④之后，落 07_coding/<case>/codes.json。"""
    if system not in CODE_SYSTEMS:
        raise ValueError(f"未知编码体系 {system!r}，可选：{', '.join(CODE_SYSTEMS)}")
    _nope(f"{system} 编码映射", "新增阶段 07_coding（读 04_norm/<case>/fields.json）")


def retrieve(kind: str, query: str, top_k: int = 5) -> list:
    """RAG / Embedding / Reranker 检索。本轮产线不检索任何外部语料。"""
    if kind not in RETRIEVAL_KINDS:
        raise ValueError(f"未知检索类型 {kind!r}，可选：{', '.join(RETRIEVAL_KINDS)}")
    _nope(f"{kind} 检索", "阶段③之前（若要做检索增强抽取）")


def export_target(model: str, wide_rows: list) -> list:
    """CDISC-CDASH / REDCap / OMOP 导出。接在阶段⑥之后，另出文件，不动 wide.csv。"""
    if model not in TARGET_MODELS:
        raise ValueError(f"未知目标模型 {model!r}，可选：{', '.join(TARGET_MODELS)}")
    _nope(f"{model} 导出", "src/wide/s6_table.py 之后，落 08_export/<model>/")


def split_train_valid(case_ids: list, cfg: dict) -> dict:
    """训练/验证集分离。接在阶段⑤，只写 05_qc/_summary/split.csv 打标。"""
    _nope("训练/验证集分离", "src/wide/s5_qc.py 之后，落 05_qc/_summary/split.csv")
