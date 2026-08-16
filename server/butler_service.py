#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
管家后端小服务（Flask 版）
-------------------------
职责：藏 API Key、读角色卡与记忆档案、拼 system prompt、调大模型 API、回写对话记录。

部署位置：服务器 /var/www/guanjia/butler_service.py
依赖：python3-flask（apt 安装即可），其余全部标准库。

环境变量（写在 /var/www/guanjia/secrets.env，systemd EnvironmentFile 注入）：
  BUTLER_TOKEN      聊天口令（前端 X-Token 必须等于它）
  DEEPSEEK_API_KEY  DeepSeek API Key
  BUTLER_MODEL      可选，默认 deepseek-v4-pro（可降配 deepseek-v4-flash；换 Kimi/通义时改这里和 BUTLER_API_URL）
  BUTLER_API_URL    可选，默认 https://api.deepseek.com/chat/completions
  GUANJIA_DIR       可选，默认 /var/www/guanjia

文件布局（GUANJIA_DIR 下）：
  character-card.md   角色卡（system prompt 原料）
  memory/facts.md     事实档案（全量注入）
  memory/daily-log.md 每日小结（注入末尾约 7 天；本服务追加对话记录）
  memory/monthly.md   月度档案（注入末尾一段）
"""

import datetime
import json
import os
import pathlib
import urllib.request

from flask import Flask, jsonify, request

BASE = pathlib.Path(os.environ.get("GUANJIA_DIR", "/var/www/guanjia"))
TOKEN = os.environ.get("BUTLER_TOKEN", "")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = os.environ.get("BUTLER_MODEL", "deepseek-v4-pro")
API_URL = os.environ.get("BUTLER_API_URL", "https://api.deepseek.com/chat/completions")

MAX_HISTORY = 20          # 前端最多带 20 条历史
MAX_MSG_CHARS = 2000      # 单条消息长度上限，防呆
DAILY_TAIL_LINES = 150    # daily-log 注入末尾行数（约覆盖 7 天）
MONTHLY_TAIL_LINES = 60   # monthly 注入末尾行数

# 四个档位的追加指令（对应前端 data-mode：butler/english/book/plan）
MODE_NUDGES = {
    "butler": "",
    "english": "\n\n【当前档位：英语陪练】从现在起只用英语回复。主人的错误逐条记录，攒够 5 条集中讲一次。卡壳记次数，不记失败。规则照旧：不鼓励、报数据。",
    "book": "\n\n【当前档位：书友】这个档位只谈书不谈计划，不看打卡数据。追问'哪句话戳到你'，不接受'挺好'这种零信息量书评。",
    "plan": "\n\n【当前档位：计划监督员】这个档位里你可以主动开口报数：哪些计划未完成、逾期多久。规则照旧：不安慰、不鼓励、报数带截止时间。",
}

app = Flask(__name__)


def read_text(path, tail_lines=None):
    """读文件；不存在返回空串。tail_lines 给了就只取末尾 N 行。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    if tail_lines:
        lines = text.splitlines()
        if len(lines) > tail_lines:
            text = "\n".join(lines[-tail_lines:])
    return text.strip()


def build_system_prompt(mode):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    card = read_text(BASE / "character-card.md")
    facts = read_text(BASE / "memory" / "facts.md")
    daily = read_text(BASE / "memory" / "daily-log.md", DAILY_TAIL_LINES)
    monthly = read_text(BASE / "memory" / "monthly.md", MONTHLY_TAIL_LINES)

    parts = [
        f"【当前时间】{now}（你报数时的数据截止时间以此为准）",
        f"【角色卡全文】\n{card}" if card else "（警告：角色卡文件缺失，按'精准克制的记录者'行事）",
        f"【事实档案】\n{facts}" if facts else "",
        f"【近期每日小结】\n{daily}" if daily else "",
        f"【月度档案节选】\n{monthly}" if monthly else "",
    ]
    prompt = "\n\n".join(p for p in parts if p)
    prompt += MODE_NUDGES.get(mode, "")
    return prompt


def append_daily_log(user_msg, reply):
    """把本轮对话追加进 daily-log.md（原始记录；精编小结以后再做）。"""
    now = datetime.datetime.now()
    date_header = f"## {now:%Y-%m-%d}"
    time_stamp = now.strftime("%H:%M")
    user_short = user_msg.replace("\n", " ")[:60]
    reply_short = reply.replace("\n", " ")[:80]
    line = f"- [{time_stamp}] 主人：{user_short} ｜ 管家：{reply_short}"

    log_path = BASE / "memory" / "daily-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    old = read_text(log_path)
    if date_header in old:
        new = old + "\n" + line + "\n"
    else:
        new = old + ("\n\n" if old else "") + date_header + "\n" + line + "\n"
    log_path.write_text(new, encoding="utf-8")


def call_model(system_prompt, messages):
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "temperature": 0.7,
        "max_tokens": 1200,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


@app.route("/api/chat", methods=["POST"])
def chat():
    # 1. 口令
    if not TOKEN or request.headers.get("X-Token") != TOKEN:
        return jsonify({"error": "口令不对"}), 401
    if not API_KEY:
        return jsonify({"error": "服务器没配 API Key"}), 500

    # 2. 入参
    body = request.get_json(silent=True) or {}
    message = str(body.get("message", "")).strip()[:MAX_MSG_CHARS]
    mode = str(body.get("mode", "butler"))
    if not message:
        return jsonify({"error": "空消息"}), 400

    history = []
    for item in (body.get("history") or [])[-MAX_HISTORY:]:
        role = item.get("role")
        content = str(item.get("content", ""))[:MAX_MSG_CHARS]
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})

    # 3. 调模型
    try:
        reply = call_model(build_system_prompt(mode),
                           history + [{"role": "user", "content": message}])
    except Exception as exc:  # 网络/限流/欠费都落在这里
        app.logger.warning("model call failed: %s", exc)
        return jsonify({"error": "模型调用失败，稍后再试"}), 502

    # 4. 回写档案（失败不影响回复）
    try:
        append_daily_log(message, reply)
    except Exception as exc:
        app.logger.warning("daily log append failed: %s", exc)

    return jsonify({"reply": reply})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "card": (BASE / "character-card.md").exists(),
        "memory": (BASE / "memory" / "facts.md").exists(),
        "model": MODEL,
    })


if __name__ == "__main__":
    # 只监听本机回环，公网流量一律走 nginx /api 反代进来
    app.run(host="127.0.0.1", port=5000)
