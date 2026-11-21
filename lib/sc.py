import math
import lib.ekwing_api as ek


def manual_unit(question, unit=None, score=None, sid="", audio="",
                engine=None, precision=None, details=None,
                all_timer=None, count=None, model_id=None, request_id=""):
    """
    不经评测，直接拼一条 user_ans —— EvalResult.to_unit() 的替代品。

        manual_unit(q, u)                       # 满分
        manual_unit(q, u, score=1.5)            # 直接给折算后的分
        manual_unit(q, u, engine=88)            # 给 0-100，内部折算
        manual_unit(q, u, score=1.5, sid="abc-123",audio="https://example.com/a.mp3")
    """
    q = question
    units = q.units if isinstance(q, ek.Question) else ek.eval_units(q)
    n = count if count is not None else max(1, len(units))

    # u 只贡献两样：ques_id 和时长
    if isinstance(unit, ek.Unit):
        pid, udur = unit.ques_id, unit.duration
    elif isinstance(unit, dict):
        pid, udur = str(unit.get("ques_id") or 0), int(unit.get("duration") or 0)
    else:
        pid, udur = 0, 0

    timer = all_timer
    if timer is None:
        timer = udur or int(ek._num(q.get("answer_time")) * 1000) or 60000

    # 不给 score 就用 engine（默认 100）折算：满分/段数 × 百分比
    if score is None:
        eng = ek._num(engine if engine is not None else 100.0)
        share = ek._num(q.get("score")) / n
        score = math.floor(100 * (int(eng) / 100.0 * share)) / 100

    return ek.AnswerUnit({
        "allTimer": int(timer),
        "audioUrl": audio or "",
        "model_id": model_id if model_id is not None else q.get("qid"),
        "precision": float(precision or 0.0),
        "ques_id": pid,
        "rank": 100,
        "details": list(details or []),
        "recordId": sid or "",
        "requestId": request_id,
        "score": score,
        "offline": False, "use_uni": False,
        "delayTime": 0, "pretime": 0, "pron": 0, "res": 0,
        "systime": 0, "version": 0, "wavetime": 0,
        "_client": "js", "_from": "unisound", "_score_engine": "yzs",
    })


class ManualRes:
    """
    EvalResult 的替身，签名与 res.to_unit 一致，可直接替换 res：

        res = ManualRes(score=1.5, sid="abc-123", audio="https://…/a.mp3")
        unit = res.to_unit(q, u)

    to_unit 的 audio 参数：None/True＝用构造时的地址，False＝强制留空，
    字符串＝用这个字符串。
    """

    def __init__(self, score=None, sid="", audio="", engine=None,
                 precision=None, details=None):
        self.score = score
        self.sid = sid
        self.audio = audio or ""
        self.engine = engine
        self.precision = precision
        self.details = details

    def to_unit(self, question, unit=None, count=None, all_timer=None, audio=None):
        if audio is None or audio is True:
            url = self.audio
        elif audio is False:
            url = ""
        else:
            url = str(audio)
        return manual_unit(question, unit, score=self.score, sid=self.sid,
                           audio=url, engine=self.engine,
                           precision=self.precision, details=self.details,
                           all_timer=all_timer, count=count)

    def __repr__(self):
        return (f"ManualRes(score={self.score!r}, sid={self.sid!r}, "
                f"audio={'有' if self.audio else '空'})")
