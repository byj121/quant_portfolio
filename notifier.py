# -*- coding: utf-8 -*-
"""
notifier.py
职责：调仓触发时发送提醒通知，支持邮件 / webhook 两种渠道。
配置见 config.py 的 NOTIFY_METHOD / EMAIL_CONFIG / WEBHOOK_CONFIG。

配置来源：config.EMAIL_CONFIG / config.WEBHOOK_CONFIG —— 两者均由 config._env()
从项目根 .env 读取，敏感信息不写死在代码里。
"""
from __future__ import annotations
import logging
import smtplib
from email.mime.text import MIMEText
from email.header import Header

import requests

import config

logger = logging.getLogger("notifier")


def send_email(subject: str, content: str) -> bool:
    cfg = config.EMAIL_CONFIG
    missing = [k for k in ("sender", "sender_auth_code", "receiver") if not cfg.get(k)]
    if missing:
        # 这条路径会让"调仓提醒"静默丢失，必须用 error 级别暴露，不能降级成 warning。
        logger.error("调仓提醒未能发出：EMAIL_CONFIG 缺少 %s。请在项目根 .env 里补上 "
                     "EMAIL_SENDER / EMAIL_AUTH_CODE / EMAIL_RECEIVER"
                     "（AUTH_CODE 是邮箱的 SMTP 授权码，不是登录密码）。",
                     "、".join(missing))
        return False
    try:
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = cfg["sender"]
        msg["To"] = cfg["receiver"]

        server = smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"])
        server.login(cfg["sender"], cfg["sender_auth_code"])
        server.sendmail(cfg["sender"], [cfg["receiver"]], msg.as_string())
        server.quit()
        logger.info("邮件通知发送成功")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def send_webhook(subject: str, content: str) -> bool:
    """
    通用 webhook 发送，兼容 Server酱 / PushPlus / 企业微信机器人等大部分服务。
    不同服务的请求体字段名不完全一致，这里用最通用的字段名，
    如果对接的服务报错，需要按该服务的文档调整 payload 字段名。
    """
    cfg = config.WEBHOOK_CONFIG
    if not cfg["url"]:
        logger.error("调仓提醒未能发出：WEBHOOK_CONFIG 缺少 url。"
                     "请在项目根 .env 里补上 WEBHOOK_URL。")
        return False
    try:
        payload = {"title": subject, "content": content, "text": subject, "desp": content}
        resp = requests.post(cfg["url"], json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Webhook 通知发送成功")
        return True
    except Exception as e:
        logger.error(f"Webhook 发送失败: {e}")
        return False


def notify(subject: str, content: str) -> bool:
    """统一入口，根据 config.NOTIFY_METHOD 选择发送渠道"""
    if config.NOTIFY_METHOD == "email":
        return send_email(subject, content)
    elif config.NOTIFY_METHOD == "webhook":
        return send_webhook(subject, content)
    else:
        logger.info(f"通知渠道设置为 '{config.NOTIFY_METHOD}'，本次不发送通知")
        return False


def format_rebalance_message(rebalance_result: dict) -> tuple[str, str]:
    """把调仓检测结果格式化成邮件/消息标题+正文"""
    subject = "【quant_portfolio】调仓提醒 - 触发再平衡" if rebalance_result["triggered"] \
        else "【quant_portfolio】调仓检查 - 暂不触发"

    lines = [f"触发状态: {'✅ 已触发' if rebalance_result['triggered'] else '⏸️ 未触发'}",
             f"原因: {rebalance_result['reason']}", ""]

    if rebalance_result["triggered"] and rebalance_result.get("instructions"):
        lines.append("交易指令清单：")
        for ins in rebalance_result["instructions"]:
            lines.append(f"  {ins.action} {ins.code}  {ins.shares}股  约¥{ins.amount:.0f}  ({ins.reason})")
    elif rebalance_result["triggered"]:
        lines.append("触发但无具体交易指令（可能都被微量拦截过滤）。")

    content = "\n".join(lines)
    return subject, content
