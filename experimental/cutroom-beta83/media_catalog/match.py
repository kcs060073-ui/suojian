"""P3-M3 语义/内容匹配（脚本 ↔ 候选镜头）。

设计 L5「按当前文案密集抽帧、语义匹配」的可用实现：
- 当前用**词法/关键词匹配**（中文二元组 + 英文/数字整词），对脚本与候选镜头的可选
  文本（文件名、转写、备注）打分排序。
- 真·embedding 语义（sentence_transformers）缺失，列为待升级点；词法匹配在素材命名
  规范时已有实用区分度，且满足「能从真实反馈持续改进」（阈值/权重可迭代）。

不依赖 ffmpeg / 外部模型，纯 Python，可独立验证。
"""

import json
import re
from collections import Counter
from pathlib import Path

# 中文停用/低信息二元组（地名常用字组合无区分度，降权）
STOP_BIGRAM = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "可以", "就是",
    "一个", "没有", "已经", "这些", "那些", "因为", "所以", "但是", "如果",
    "对于", "通过", "进行", "成为", "以及", "并且", "位于", "拥有",
}


def tokenize(text: str) -> Counter:
    """返回 token -> 出现次数。中文取二元组 + 英文/数字整词。"""
    if not text:
        return Counter()
    text = text.lower()
    tokens: Counter = Counter()

    # 英文 / 数字整词（保留，区分度高）
    for m in re.findall(r"[a-z0-9]+", text):
        if len(m) >= 2:
            tokens[m] += 1

    # 中文连续段：去标点后取二元组
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg not in STOP_BIGRAM:
                tokens[bg] += 1
        if len(seg) == 1:
            tokens[seg] += 1
    return tokens


def score(query_tokens: Counter, doc_tokens: Counter) -> float:
    """0..1：命中的查询 token 权重之和 / 查询总权重。"""
    if not query_tokens:
        return 0.0
    q_total = float(sum(query_tokens.values()))
    hit = 0.0
    for tok, w in query_tokens.items():
        if tok in doc_tokens:
            hit += w
    return hit / q_total


def _idf_weight(corpus_tokens: list[Counter]) -> dict:
    """简单 IDF：在越多候选出现的 token 权重越低。"""
    df: Counter = Counter()
    for c in corpus_tokens:
        for tok in c:
            df[tok] += 1
    n = max(1, len(corpus_tokens))
    return {tok: (1.0 / df[tok]) for tok in df}


def rank(script: str, candidates: list[dict], text_key: str = "text") -> list[dict]:
    """对 candidates([{id, text, ...}]) 按脚本相关度排序，附加 score。"""
    q = tokenize(script)
    doc_tokens = [tokenize(c.get(text_key, "")) for c in candidates]
    idf = _idf_weight(doc_tokens)
    for c, dt in zip(candidates, doc_tokens):
        # 用 IDF 调整查询权重：命中稀有词加权
        adj = Counter()
        for tok, w in q.items():
            adj[tok] = w * idf.get(tok, 1.0)
        c["score"] = round(score(adj, dt), 4)
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


# --------------------------------------------------------------------------- #
# 与 DB 集成
# --------------------------------------------------------------------------- #
def candidate_text_for_asset(db, asset_id: str, task_id: str | None = None) -> str:
    """汇总某资产的可用文本：文件名 + 转写（analysis_runs）+ 任务内备注（scene_cards）。"""
    parts: list[str] = []
    row = db.connection.execute(
        "SELECT relative_path FROM asset_locations WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if row:
        parts.append(Path(row["relative_path"]).stem.replace("_", " ").replace("-", " "))
    tr = db.connection.execute(
        "SELECT result_json FROM analysis_runs WHERE asset_id=? AND stage='audio_analysis' AND status='completed'",
        (asset_id,),
    ).fetchone()
    if tr:
        try:
            data = json.loads(tr["result_json"])
            segments = data.get('segments', []) if isinstance(data, dict) else []
            if isinstance(segments, list):
                parts.extend(s['text'] for s in segments if isinstance(s, dict) and isinstance(s.get('text'), str))
        except (TypeError, ValueError):
            pass
    if task_id:
        sc = db.connection.execute(
            "SELECT narrative_text FROM scene_cards WHERE task_id=? AND asset_id=?",
            (task_id, asset_id),
        ).fetchone()
        if sc and sc["narrative_text"]:
            parts.append(sc["narrative_text"])
    return " ".join(p for p in parts if p)


def match_for_task(db, task_id: str, script: str) -> list[dict]:
    rows = db.connection.execute(
        """
        SELECT ta.asset_id, al.relative_path
        FROM task_assets ta
        JOIN asset_locations al USING(asset_id)
        WHERE ta.task_id=?
        """,
        (task_id,),
    ).fetchall()
    cands = [
        {"id": r["asset_id"], "path": r["relative_path"],
         "text": candidate_text_for_asset(db, r["asset_id"], task_id)}
        for r in rows
    ]
    return rank(script, cands)
