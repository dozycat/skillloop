"""An offline demo project for the simulated world (mock agent + mock LLM).

It exists so the full loop - including a harmful candidate being stopped by the gate - can be
watched end to end with no API key, in a few seconds: ``skillloop init demo --demo && skillloop evolve -p demo``."""

from __future__ import annotations

from .core.config import Config
from .core.models import Case
from .core.skill import Skill

RULES = {
    "refund-window": "收货超过 30 天的退款申请不走自动退款,转人工复核并说明原因。",
    "order-lookup": "回答任何订单问题前,先用订单号查询订单状态,不要凭用户描述推断。",
    "partial-refund": "部分退款金额 = 商品实付金额 × 退货数量 / 购买数量,运费不退。",
    "escalation": "用户第二次表达不满或提到投诉/监管时,立即升级到二线并给出工单号。",
    "identity-check": "修改收货地址或手机号前,必须核验下单手机号后四位。",
    "tone": "先确认用户的问题再给方案;不使用“亲”“哦”等口语化称呼。",
    "coupon": "退款时已使用的优惠券按比例退回,过期优惠券不补发。",
    "invoice": "已开发票的订单退款前需先提示用户退回或冲红发票。",
}

SCENARIOS = {
    "refund-window": ["我 45 天前收到的耳机想退款", "两个月前买的外套现在能退吗", "收货 31 天了,申请退货"],
    "order-lookup": ["我的订单怎么还没到", "帮我看看订单 88231 现在什么状态", "快递显示签收但我没收到"],
    "partial-refund": ["买了 3 件退 1 件,退多少钱", "一套四个杯子碎了一个,怎么退", "退两本书,运费退吗"],
    "escalation": ["我已经说过一次了,你们到底管不管", "再不处理我就去 12315 投诉", "上次的客服根本没解决问题"],
    "identity-check": ["把收货地址改到公司", "我换手机号了,帮我更新一下", "订单的收件人电话写错了"],
    "tone": ["你们这个流程太麻烦了", "能不能说清楚到底怎么办", "我只想知道结果"],
    "coupon": ["退款后我用的满减券还回来吗", "用了优惠券的订单退货,券怎么算", "券过期了退款还能补吗"],
    "invoice": ["开过发票的订单可以退吗", "我要退货,但已经开了专票", "退款和发票哪个先处理"],
}

REQUIREMENT = (
    """# 客服工单处理 Skill 需求

## 业务目标
让 agent 按公司售后政策处理退款、改址、投诉类工单,首次解决率 ≥ 90%。

## 范围
- 退款(全额/部分)、优惠券与发票的联动处理
- 订单状态查询、收货信息修改
- 投诉升级

## 验收标准
"""
    + "\n".join(f"- {text}" for text in RULES.values())
    + """

## 约束
- 不承诺政策之外的补偿
- 涉及个人信息的修改必须先核验身份
"""
)

SEED_SKILL = Skill(
    name="support-ticket",
    description="处理电商售后工单(退款、改址、投诉)时使用,按公司售后政策给出处理方案。",
    body="""# 售后工单处理

## Overview

按公司售后政策处理用户工单,给出明确的处理结论和下一步。

## Rules

- [order-lookup] 回答任何订单问题前,先用订单号查询订单状态,不要凭用户描述推断。
""",
)


def demo_cases() -> list[Case]:
    cases = []
    markers = list(RULES)
    n = 0
    for i, marker in enumerate(markers):
        for j, text in enumerate(SCENARIOS[marker]):
            n += 1
            requires = [marker]
            if j == 2:  # every third scenario also needs a neighbouring rule
                requires.append(markers[(i + 3) % len(markers)])
            cases.append(
                Case(
                    id=f"ticket-{n:02d}",
                    input=text,
                    expected=f"处理方案#{n:02d}",
                    tags=[marker],
                    meta={
                        "requires": requires,
                        "hints": {m: RULES[m] for m in requires},
                        "wrong_answer": "抱歉,这个问题我需要再确认一下。",
                    },
                )
            )
    return cases


def demo_config() -> Config:
    cfg = Config()
    cfg.project.name = "support-ticket-demo"
    cfg.llm.options = {"max_fixes_per_call": 1, "regress_every": 3}
    cfg.evolve.max_iterations = 10
    cfg.evolve.analyst_batch_size = 4
    cfg.evolve.max_failures_per_iteration = 8
    cfg.evolve.patience = 3
    cfg.evolve.train_ratio, cfg.evolve.val_ratio = 0.34, 0.33  # 1/1/1 per rule
    return cfg
