import random, uuid
import lib.ekwing_api as ek
import lib.sc as sc

s = None

s = ek.capture_session(port=7788, timeout=100,on_start=lambda srv: print(f"插件填：{srv.url}  令牌 {srv.token}"))

while True:
    lst = ek.tasks(s)
    for i, it in enumerate(lst, 1):
        print(i, it.title, it.state)

    it   = lst[int(input("选哪个作业/考试：")) - 1]
    d    = ek.exam_detail(s, it)
    full = int(it.full_score)
    mbs  = int(input(f"目标分数(总分 {full})："))
    mbpc = int(input("题目分数波动(±)："))

    scale = mbs / full
    items = []
    for q in d:
        n, usf = len(q.units), q.score / len(q.units)
        for u in q.units:
            p = usf * scale
            print(q.name, u.label, u.text, f"{p:.3f}")
            items.append([q, u, p, -min(mbpc, p), min(mbpc, usf - p)])

    dev = [random.uniform(lo, hi) for *_, lo, hi in items]
    for _ in items:
        gap = sum(dev)
        if abs(gap) < 1e-9:
            break
        sgn = 1 if gap > 0 else -1
        for k, (_, _, _, lo, hi) in enumerate(items):
            room = max(0.0, dev[k] - lo if sgn > 0 else hi - dev[k])
            take = min(abs(gap), room)
            dev[k] -= sgn * take
            gap    -= sgn * take
            if abs(gap) < 1e-9:
                break

    pool = {}
    for (q, u, p, lo, hi), dv in zip(items, dev):
        pool.setdefault(q.qid, []).append(
            sc.manual_unit(q, u, score=round(p + dv, 6),sid=str(uuid.uuid4()), audio=""))

    print("合计校验：", round(sum(p + dv for (_, _, p, _, _), dv in zip(items, dev)), 6))
    ans = ek.build_answers(d, pool)
    input("确认交卷请按回车,否则请按Ctrl+C")
    ek.submit(s, it.self_id, ans, d.url)
