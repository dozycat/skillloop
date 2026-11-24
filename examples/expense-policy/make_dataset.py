"""Regenerates dataset.jsonl. Expected answers are computed here from the policy, never typed by hand."""

import json

TIER1 = {"北京", "上海", "广州", "深圳"}


def meal(city, days, same_day=False):
    rate = 120 if city in TIER1 else 80
    if same_day:
        return rate * 0.5
    return rate * (days - 2) + rate  # two half days + full days in between


def hotel(city, nights):
    cap = 500 if city in TIER1 else 350
    return sum(min(n, cap) for n in nights)


def taxi(rides):
    return sum(amount if (hour >= 22 or hour < 6) else min(amount, 60) for hour, amount in rides)


CASES = [
    # (id, tags, question, expected)
    ("meal-01", ["meal"], "小王去成都出差 3 天(周一出发,周三返回),没有其他费用。应报销多少?", meal("成都", 3)),
    ("meal-02", ["meal"], "小李去上海出差 4 天(含出发日和返回日),没有其他费用。应报销多少?", meal("上海", 4)),
    ("meal-03", ["meal"], "小赵早上去苏州见客户,当天晚上返回,没有任何票据。应报销多少?", meal("苏州", 1, True)),
    (
        "meal-04",
        ["meal"],
        "小周去深圳出差 2 天(第一天出发,第二天返回),提交了 300 元的餐饮发票。应报销多少?",
        meal("深圳", 2),
    ),
    ("meal-05", ["meal"], "小陈去武汉出差 5 天(含出发日和返回日),自费吃饭共花了 620 元。应报销多少?", meal("武汉", 5)),
    (
        "hotel-01",
        ["hotel"],
        "小吴去杭州出差 2 天 1 晚,住宿发票 420 元。应报销多少?",
        meal("杭州", 2) + hotel("杭州", [420]),
    ),
    (
        "hotel-02",
        ["hotel"],
        "小郑去北京出差 3 天 2 晚,两晚住宿分别是 560 元和 430 元。应报销多少?",
        meal("北京", 3) + hotel("北京", [560, 430]),
    ),
    (
        "hotel-03",
        ["hotel"],
        "小冯去西安出差 3 天 2 晚,两晚住宿分别是 300 元和 400 元。应报销多少?",
        meal("西安", 3) + hotel("西安", [300, 400]),
    ),
    (
        "hotel-04",
        ["hotel"],
        "小卫去广州出差 2 天 1 晚,住宿 480 元。应报销多少?",
        meal("广州", 2) + hotel("广州", [480]),
    ),
    (
        "train-01",
        ["train"],
        "小蒋当天往返南京,去程高铁一等座 230 元(同车次二等座 145 元),返程二等座 145 元。应报销多少?",
        meal("南京", 1, True) + 145 + 145,
    ),
    (
        "train-02",
        ["train"],
        "小沈当天往返天津,往返都坐商务座,每程 310 元(同车次二等座每程 55 元)。应报销多少?",
        meal("天津", 1, True) + 55 * 2,
    ),
    (
        "train-03",
        ["train"],
        "小韩去长沙出差 2 天 1 晚,往返高铁二等座各 315 元,住宿 340 元。应报销多少?",
        meal("长沙", 2) + 630 + hotel("长沙", [340]),
    ),
    (
        "taxi-01",
        ["taxi"],
        "小杨当天往返无锡,白天 14:20 打车一次 85 元。应报销多少?",
        meal("无锡", 1, True) + taxi([(14, 85)]),
    ),
    (
        "taxi-02",
        ["taxi"],
        "小朱当天往返宁波,23:10 打车回家 132 元。应报销多少?",
        meal("宁波", 1, True) + taxi([(23, 132)]),
    ),
    (
        "taxi-03",
        ["taxi"],
        "小秦当天往返佛山,上午 09:30 打车 48 元,晚上 21:50 打车 96 元。应报销多少?",
        meal("佛山", 1, True) + taxi([(9, 48), (21, 96)]),
    ),
    (
        "taxi-04",
        ["taxi"],
        "小尤去上海出差 2 天 1 晚,住宿 500 元,凌晨 05:40 打车去机场方向的高铁站 118 元,下午 15:00 打车 40 元。应报销多少?",
        meal("上海", 2) + hotel("上海", [500]) + taxi([(5, 118), (15, 40)]),
    ),
    (
        "stale-01",
        ["stale"],
        "小许 6 月 1 日提交报销:3 月 10 日至 11 日去青岛出差 2 天 1 晚,住宿发票(3 月 11 日开具)330 元;另有一张 1 月 5 日的打车票 45 元(下午)。应报销多少?",
        meal("青岛", 2) + 330,
    ),  # Mar 11 -> Jun 1 = 82 days ok; Jan 5 -> Jun 1 = 147 days stale
    (
        "stale-02",
        ["stale"],
        "小何 9 月 30 日提交报销:5 月 20 日当天往返嘉兴,高铁二等座往返共 96 元(票据日期 5 月 20 日)。应报销多少?",
        meal("嘉兴", 1, True),
    ),  # 133 days: tickets stale, meal allowance unaffected
    (
        "stale-03",
        ["stale"],
        "小吕 4 月 15 日提交报销:4 月 2 日至 3 日去重庆出差 2 天 1 晚,住宿 380 元(4 月 3 日开具),4 月 2 日 22:30 打车 70 元。应报销多少?",
        meal("重庆", 2) + hotel("重庆", [380]) + 70,
    ),
    (
        "mix-01",
        ["mix"],
        "小施去深圳出差 3 天 2 晚,往返高铁一等座各 480 元(二等座各 300 元),两晚住宿各 520 元,第二天 10:00 打车 75 元。应报销多少?",
        meal("深圳", 3) + 600 + hotel("深圳", [520, 520]) + taxi([(10, 75)]),
    ),
    (
        "mix-02",
        ["mix"],
        "小张去合肥出差 4 天 3 晚,三晚住宿分别 350、360、340 元,往返高铁二等座各 210 元,最后一天 22:00 整打车 66 元。应报销多少?",
        meal("合肥", 4) + hotel("合肥", [350, 360, 340]) + 420 + taxi([(22, 66)]),
    ),
    (
        "mix-03",
        ["mix"],
        "小孔当天往返广州,去程商务座 620 元(二等座 215 元),返程二等座 215 元,中午 12:10 打车 58 元。应报销多少?",
        meal("广州", 1, True) + 215 + 215 + taxi([(12, 58)]),
    ),
    (
        "mix-04",
        ["mix"],
        "小曹去昆明出差 2 天 1 晚,住宿 290 元,没有其他票据。应报销多少?",
        meal("昆明", 2) + hotel("昆明", [290]),
    ),
    (
        "mix-05",
        ["mix"],
        "小严去北京出差 5 天 4 晚,四晚住宿各 500 元,往返高铁二等座各 553 元。应报销多少?",
        meal("北京", 5) + hotel("北京", [500] * 4) + 1106,
    ),
]

with open(__file__.replace("make_dataset.py", "dataset.jsonl"), "w", encoding="utf-8") as f:
    for cid, tags, question, expected in CASES:
        value = int(expected) if float(expected).is_integer() else expected
        f.write(json.dumps({"id": cid, "input": question, "expected": value, "tags": tags}, ensure_ascii=False) + "\n")
print(len(CASES), "cases written")
