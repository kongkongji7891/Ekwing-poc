# -*- coding: utf-8 -*-
"""
ekwing_api — 翼课网（ekwing.com）网页接口映射库

只做「网页请求 <-> Python 数据」的映射，不含任何界面、录音、流程编排：

    登录态  Session / Session.from_file / Session.save / Session.valid
    取登录态 CaptureServer / capture_session()  本机 127.0.0.1 接口，收插件送来的 cookie
    列表    exam_list()                 GET  /exam/student/getexamlist
    详情    exam_html() / exam_detail() GET  /exam/student/examload
    解析    parse_exam()                抠页面内联的 window.spoken_list / spoken_res_list
    草稿    save_draft()                POST /exam/student/savedraft   (gzip)
    提交    submit()                    POST /exam/student/examsubmit  (gzip)
    评测    evaluate()                  wss  wss-edu.hivoice.cn/ws/eval/  (与平台同源引擎)
    组织    build_answers() / make_answer_unit() / eval_units()
    音频    audio_bytes()（腾讯云 COS，公开可读）

依赖：
    必需：Python 标准库
    评测：websocket-client
    转码：soundfile + numpy（只用 to_pcm16k / pcm_from_file 时才需要）
    取登录态：无额外依赖，但需要一个能发 HTTP 的浏览器插件（本项目 ext/ 即可）

返回的都是对象，字段直接点（原始 JSON 字段也能用 dict 语法取）：

    ExamList  .total .items[i] → ExamItem .title .full_score .state .deadline .left .ext
    ExamDetail .url .questions → Question .name .score .mode .units .eval_text .audio
    Unit      .label .text .mode .ques_id .duration .answers
    EvalResult .score .pronunciation .fluency .integrity .words .weak_words() .to_unit()
    AnswerUnit / AnswerBlock  .to_dict()，能直接塞进 json.dumps(default=ek._json_default)

最小可用例子：
    import ekwing_api as ek

    s = ek.Session.from_file()                  # ~/.ekwing/session.json
    lst = ek.exam_list(s, page=1, kind="hw")    # 作业；kind="exam" 是考试
    it = lst[0]                                 # ExamItem
    d = ek.exam_detail(s, it.self_id, it.id)    # ExamDetail
    q = d[0]                                    # Question
    u = q.units[0]                              # Unit（多问题就有多个）

    res = ek.evaluate(ek.to_pcm16k("a.wav"), u.text, q.model_type)   # EvalResult
    unit = res.to_unit(q, u)                    # AnswerUnit，自动按小题均分折算
    answers = ek.build_answers(d.questions, {q.qid: [unit]})
    ek.submit(s, it.self_id, answers, d.url)    # 真实提交，不可逆

从插件拿登录态（阻塞到插件点按钮为止）：
    s = ek.capture_session(port=7788, timeout=180)   # 打印 srv.token 让用户粘到插件

    或者自己掌控生命周期：
    srv = ek.CaptureServer(port=7788).start()
    print(srv.url, srv.token)      # 插件里填这两个
    s = srv.wait(120)              # 拿到 Session（校验结果在 srv.last）
    srv.stop()

已知边界：
    - 解析只认听说题（spoken_list / spoken_res_list），选择填空类题型不在此列
    - audioUrl 可以留空，服务端评分只认 score / details / recordId
    - cookie 只在本机内存与明文文件之间流动，库本身不发往任何第三方
"""

import gzip
import json
import math
import os
import random
import re
import secrets
import string
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote
from urllib.request import Request, urlopen

__all__ = [
    "BASE", "ENDPOINTS", "UA", "SESSION_PATH",
    "EkwingError", "AuthError", "ApiError", "ExamUnavailable", "EvalError",
    "CaptureTimeout",
    "Model", "ExamList", "ExamItem", "ExamDetail", "Question", "Unit",
    "EvalResult", "Word", "AnswerUnit", "AnswerBlock",
    "Session", "clean_cookie",
    "CaptureServer", "capture_session",
    "exam_list", "exam_html", "exam_detail", "parse_exam",
    "save_draft", "submit", "evaluate",
    "build_answers", "make_answer_unit", "eval_units", "MODE_BY_MODEL_TYPE",
    "audio_bytes", "to_pcm16k", "pcm_from_file",
]

# ---------------------------------------------------------------- 常量

BASE = "https://www.ekwing.com"

ENDPOINTS = {
    "exam_list": BASE + "/exam/student/getexamlist",    # GET page/type/mode_type
    "exam_load": BASE + "/exam/student/examload",       # GET id(self_id)/_id(id)
    "savedraft": BASE + "/exam/student/savedraft",      # POST answer/self_id  (gzip)
    "examsubmit": BASE + "/exam/student/examsubmit",    # POST answer/is_force/self_id/href (gzip)
    "eval_ws": "wss://wss-edu.hivoice.cn/ws/eval/",     # 口语评测（无签名）
}

# 与浏览器一致的 UA；被插件抓到真实 UA 时以插件为准
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

ROOT = os.path.join(os.path.expanduser("~"), ".ekwing")
SESSION_PATH = os.path.join(ROOT, "session.json")

# 鉴权门：返回极短页面还带这些字样，就是 cookie 过期了
_GATE_MARK = ("重新登录", "请登录", "110004", "10000",
              # cookie 失效时接口不回 JSON，而是直接吐一段跳首页的脚本
              "location.href", "Home/index", "passport.ekwing.com")

# 题型 -> 评测引擎 mode
#
# 原始依据（页面 main.js）：
#   引擎核心类型  WORD="2" SENTENCE="0" PARAGRAPH="1" ANSWER="5" FREE_TALK="6"
#   model_type 映射表 $ = {1:PARAGRAPH, 2:ANSWER, 3:FREE_TALK, 4:ANSWER, 5:ANSWER,
#                          6:FREE_TALK, 7:ANSWER, 8:ANSWER, 9:ANSWER, 10:WORD,
#                          11:SENTENCE, 12:SENTENCE, 20:SENTENCE, 201:PARAGRAPH,
#                          202:ANSWER, 203:ANSWER, 204:FREE_TALK, 301:SENTENCE,
#                          302:WORD, 303:SENTENCE, 304:SENTENCE, 306:FREE_TALK,
#                          307:PARAGRAPH}
#   核心类型 -> ws mode：PARAGRAPH→para、ANSWER→qa、FREE_TALK→retell、
#                       SENTENCE→sent、WORD→word
MODE_BY_MODEL_TYPE = {
    "1": "para",     # 模仿朗读
    "2": "qa",       # 听选/判断类回答
    "3": "retell",   # 复述/自由表达
    "4": "qa",       # 回答
    "5": "qa",       # 回答
    "6": "retell",   # 信息转述
    "7": "qa",       # 听选信息
    "8": "qa",       # 回答问题
    "9": "qa",       # 询问信息
    "10": "word",    # 单词朗读
    "11": "sent",    # 句子朗读
    "12": "sent",    # 句子朗读
    "20": "sent",    # 绘本朗读
    "201": "para",   # 段落朗读
    "202": "qa",     # 角色扮演（问答）
    "203": "qa",     # 角色扮演（问答）
    "204": "retell", # 自由表达
    "301": "sent",
    "302": "word",
    "303": "sent",
    "304": "sent",
    "306": "retell", # 故事复述
    "307": "para",   # 视频后朗读
}


# ---------------------------------------------------------------- 异常


class EkwingError(Exception):
    """本库所有错误的基类。"""


class AuthError(EkwingError):
    """被鉴权门拦截：cookie 过期 / 未登录。"""


class ApiError(EkwingError):
    """接口返回 errno != 0。"""

    def __init__(self, errno, err_info="", raw=None):
        self.errno = errno
        self.err_info = err_info
        self.raw = raw
        super().__init__(f"errno={errno} {err_info}".strip())


class ExamUnavailable(EkwingError):
    """这套卷子当前不能作答（已收卷 / 未开始 / 不接受作答）。"""


class EvalError(EkwingError):
    """评测服务报错或超时。"""

    def __init__(self, msg, code=None, raw=None):
        self.code = code
        self.raw = raw
        super().__init__(msg)


class CaptureTimeout(EkwingError):
    """等插件送 cookie 超时。"""


# ---------------------------------------------------------------- 小工具


def clean_cookie(s):
    """把从 DevTools / 插件复制来的东西擦干净：去前缀、引号、换行、尾分号。"""
    s = (s or "").strip().strip('"').strip("'")
    s = re.sub(r"^\s*(set-)?cookie\s*:\s*", "", s, flags=re.I)
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(";")


def _cookie_pairs(cookie):
    """cookie 字符串 -> [(name, value)]，值保持原始编码。"""
    out = []
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out.append((k.strip(), v.strip()))
    return out


def _header_safe(s):
    """HTTP 头只能装 latin-1；cookie 里有中文时按 UTF-8 字节原样透传。"""
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8").decode("latin-1")


def _decode(raw):
    """按 BOM / charset / utf-8 / gb18030 顺序猜编码。"""
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    head = raw[:2048].decode("ascii", "ignore")
    m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if m:
        try:
            return raw.decode(m.group(1), "replace")
        except LookupError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def is_gate(text):
    """这段响应是不是「请重新登录」的鉴权门。"""
    return any(m in text for m in _GATE_MARK) and len(text) < 600


def _parse_json(body, what="接口"):
    try:
        return json.loads(body)
    except Exception:
        raise ApiError(None, f"{what}返回的不是 JSON：{body[:200]}")


def clean_text(s):
    """页面文本清洗：解实体、去零宽、把排版换行合成一句。"""
    s = unescape(s or "")
    s = s.replace("\u200b", "").replace("\xa0", " ").replace("\ufeff", "")
    s = s.replace("\x00", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"[ ]*\n[ ]*(?=[\u4e00-\u9fff（【《“‘])", "", s)
    s = re.sub(r"\s*\n\s*", " ", s)
    s = re.sub(r"(?<=[，。、；：！？]) +", "", s)
    return s.strip()


# ---------------------------------------------------------------- 数据对象


def _plain(v):
    """把 Model / 嵌套结构转成纯 dict/list，json 与打印都能用。"""
    if isinstance(v, Model):
        return v.to_dict()
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def _json_default(o):
    if isinstance(o, Model):
        return o.to_dict()
    raise TypeError(f"{type(o).__name__} 不能直接序列化")


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Model:
    """
    所有接口返回对象的基类：**字段当属性用**，也能像 dict 那样取值。

        it  = ek.exam_list(s).items[0]
        it.title            # 类里声明的属性 = 加工后的语义值
        it["start_time"]    # 原始字段，旧写法照样能跑
        it.to_dict()        # 变回普通 dict（json.dumps 用）

    类里没声明的字段一律走原始 JSON（__getattr__ 兜底），
    所以接口以后加字段也不用改库。
    """

    def __init__(self, data=None, **extra):
        self._data = dict(data or {})
        if extra:
            self._data.update(extra)

    # -- 原始字段 -----------------------------------------------------

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return object.__getattribute__(self, "_data")[name]
        except (KeyError, AttributeError):
            raise AttributeError(f"{type(self).__name__} 没有字段 {name!r}") from None

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def __contains__(self, key):
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def to_dict(self):
        return {k: _plain(v) for k, v in self._data.items()}

    def __repr__(self):
        inner = ", ".join(f"{k}={v!r}" for k, v in list(self._data.items())[:4])
        return f"<{type(self).__name__} {inner}>"

    def __eq__(self, other):
        if isinstance(other, Model):
            return self.to_dict() == other.to_dict()
        if isinstance(other, dict):
            return self.to_dict() == other
        return NotImplemented

    # -- 新增字段（不影响原始 JSON）-----------------------------------

    def set(self, key, value):
        """补一个派生字段，链式用。"""
        self._data[key] = value
        return self


class ExamItem(Model):
    """列表里的一条作业/考试。原始字段全保留，下面是常用的语义属性。"""

    def __init__(self, data=None, kind=None, **extra):
        super().__init__(data, **extra)
        self._kind = kind          # "hw" / "exam"，只影响措辞，不进 JSON

    @property
    def kind(self):
        return self._kind or "hw"

    @property
    def self_id(self):
        return str(self._data.get("self_id") or "")

    @property
    def id(self):
        return str(self._data.get("id") or "")

    @property
    def info(self):
        return self._data.get("_self_info") or {}

    @property
    def title(self):
        return str(self.info.get("title") or "")

    @property
    def full_score(self):
        return _num(self.info.get("score"))

    @property
    def my_score(self):
        return _num(self._data.get("score"))

    @property
    def teacher(self):
        return str((self._data.get("_teacher_info") or {}).get("nicename") or "")

    @property
    def submitted(self):
        return str(self._data.get("sub_time") or "0") not in ("0", "", "None")

    @property
    def rebacked(self):
        """被老师退回（非空原因），通常可以重做。"""
        return bool(self._data.get("reback_reason"))

    @property
    def focused(self):
        return str(self._data.get("is_focus") or "").lower() == "y"

    @property
    def left(self):
        """剩余时间 dict {day,hour,min,second}；没有该字段返回 None。"""
        return self._data.get("_left_time")

    @property
    def closed(self):
        """考试已收卷/已截止（靠 _left_time.day < 0 判断）。"""
        left = self.left
        return bool(left) and int(left.get("day") or 0) < 0

    @property
    def deadline(self):
        ts = self.info.get("end_time") or self._data.get("end_time")
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
        except (TypeError, ValueError, OSError):
            return ""

    @property
    def ext(self):
        """_self_info.ext 是字符串里的 JSON，这里直接给 dict。"""
        raw = self.info.get("ext")
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw or "{}")
        except ValueError:
            return {}

    @property
    def can_redo(self):
        return bool(self._data.get("is_again_answer")) or self.rebacked

    @property
    def state(self):
        if self.rebacked:
            return "已退回"
        if self.submitted:
            return "已交"
        if self.closed:
            return "已截止" if self.kind == "hw" else "已收卷"
        return "未交"


class HwItem(Model):
    """
    训练列表（/hw/stuList）里的一条 —— 就是首页「训练」卡片指的那批。

    注意：这跟 exam_list(kind="hw") 完全是两套东西。
    exam 模块的 type=2 也叫「作业」，但列的是 Unit 听说题；
    首页「训练」在 /hw/ 模块，列的是「同步训练11-13」这种。
    """

    @property
    def title(self):
        return str(self._data.get("title") or "").strip()

    @property
    def hid(self):
        return str(self._data.get("hid") or "")

    @property
    def url(self):
        """点进这条训练的真实地址。"""
        return f"{BASE}/hw/do?hid={self.hid}&param_term="

    @property
    def start(self):
        return str(self._data.get("start") or "").strip()

    @property
    def end(self):
        return str(self._data.get("end") or "").strip()

    @property
    def message(self):
        return str(self._data.get("message") or "").strip()

    @property
    def rebacked(self):
        """老师退回留言非空 = 被退回，可以重做。"""
        return bool(str(self._data.get("reback") or "").strip())

    @property
    def pass_score(self):
        try:
            return float(self._data.get("pass") or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def full_score(self):
        """练习按 100 分制。列表页只给达标分，不给满分，所以直接给 100。"""
        return 100.0

    @property
    def state(self):
        return "已退回" if self.rebacked else "未交"

    def __repr__(self):
        return f"<HwItem {self.title} hid={self.hid}>"


class Task(Model):
    """
    tasks() 返回的一条：训练和考试被抹平成一个形状。

    kind   "train"（同步训练，走 /hw/do?hid=）/ "exam"（听说考试，走 examload?id=）
    id     train 是 hid，exam 是 self_id
    url    直接能打开的答题页
    state  未交 / 已退回 / 已交 / 已截止
    raw    原始对象（HwItem 或 ExamItem），要细节时翻这个
    """

    @property
    def kind(self):
        return str(self._data.get("kind") or "")

    @property
    def title(self):
        return str(self._data.get("title") or "")

    @property
    def state(self):
        return str(self._data.get("state") or "")

    @property
    def id(self):
        return str(self._data.get("id") or "")

    @property
    def end(self):
        return str(self._data.get("end") or "")

    @property
    def url(self):
        return str(self._data.get("url") or "")

    @property
    def raw(self):
        return self._data.get("raw")

    # -- 分数与状态：全部转发给原始对象，缺什么给 0 / 空串 / False -------

    def _fwd(self, name, default):
        """从 raw 上取字段；raw 没有就返回默认值（不会抛 AttributeError）。"""
        raw = self.raw
        if raw is None:
            return default
        try:
            return getattr(raw, name)
        except AttributeError:
            return default

    @property
    def full_score(self):
        """满分。考试有；同步训练列表上没有这个数字，给 0。"""
        return _num(self._fwd("full_score", 0))

    @property
    def my_score(self):
        """我的得分。考试有；练习给 0。"""
        return _num(self._fwd("my_score", 0))

    @property
    def pass_score(self):
        """达标分。练习列表上有（要求达标分数）；考试给 0。"""
        return _num(self._fwd("pass_score", 0))

    @property
    def submitted(self):
        return bool(self._fwd("submitted", False))

    @property
    def rebacked(self):
        return bool(self._fwd("rebacked", False))

    @property
    def can_redo(self):
        return bool(self._fwd("can_redo", self.rebacked))

    @property
    def closed(self):
        return bool(self._fwd("closed", False))

    @property
    def left(self):
        return self._fwd("left", None)

    @property
    def teacher(self):
        return str(self._fwd("teacher", ""))

    @property
    def deadline(self):
        return str(self._fwd("deadline", "")) or self.end

    @property
    def ext(self):
        return self._fwd("ext", {}) or {}

    @property
    def self_id(self):
        """只有 exam 有；train 返回空串（训练用的是 hid）。"""
        return self.id if self.kind == "exam" else ""

    @property
    def hid(self):
        """只有 train 有；exam 返回空串（考试用的是 self_id）。"""
        return self.id if self.kind == "train" else ""

    def open(self, session):
        """
        打开这条任务。

            exam  -> ExamDetail，有 .questions / .units，能直接评测
            train -> HWDetail，有 .items（题型清单）和 .spoken_items

        两种都用同一个 id 参数，不用自己判断 kind。
        """
        return paper_detail(session, self.id, kind=self.kind)

    def __repr__(self):
        return f"<Task {self.kind} {self.state} {self.title}>"


class ExamList(Model):
    """exam_list() 的返回：.items 是 ExamItem 列表。"""

    @property
    def items(self):
        return [_as(item, ExamItem) for item in (self._data.get("items") or [])]

    @property
    def total(self):
        return int(self._data.get("total") or 0)

    @property
    def raw(self):
        return self._data.get("raw") or {}

    def __len__(self):
        return len(self._data.get("items") or [])

    def __iter__(self):
        return iter(self.items)

    def __getitem__(self, i):
        if isinstance(i, int):
            return self.items[i]
        return self._data[i]


class Question(Model):
    """parse_exam() 的一道题。"""

    @property
    def no(self):
        return int(self._data.get("no") or 0)

    @property
    def qid(self):
        return str(self._data.get("qid") or "")

    @property
    def name(self):
        return str(self._data.get("name") or "")

    @property
    def model_type(self):
        return str(self._data.get("model_type") or "")

    @property
    def mode(self):
        return MODE_BY_MODEL_TYPE.get(self.model_type, "sent")

    @property
    def score(self):
        return _num(self._data.get("score"))

    @property
    def answer_seconds(self):
        return _num(self._data.get("answer_time"))

    @property
    def prepare_seconds(self):
        return _num(self._data.get("prepare_time"))

    @property
    def audio(self):
        return list(self._data.get("audio") or [])

    @property
    def pic(self):
        return list(self._data.get("pic") or [])

    @property
    def units(self):
        """可评测单元列表（缓存，避免重复解析）。

        考试走 eval_units() 现场解析；
        练习（HWDetail）没有考试那套结构，会把摊好的单元塞进 _fixed_units 直接取用。
        考试数据里永远不会有 _fixed_units 这个键，所以这条分支对考试零影响。
        """
        if "_units" not in self.__dict__:
            fixed = self._data.get("_fixed_units")
            if fixed is None:
                object.__setattr__(self, "_units", eval_units(self))
            else:
                object.__setattr__(self, "_units", [_as(u, Unit) for u in fixed])
        return self._units

    @property
    def eval_text(self):
        """首选参考文本（第一条可评测单元）。"""
        u = self.units
        return u[0].text if u else ""

    @property
    def is_multi(self):
        return len(self._data.get("ques_list") or []) > 1

    @property
    def keywords(self):
        out = []
        for x in (self._data.get("ques_list") or []):
            for g in ((x.get("ext") or {}).get("keywords") or []):
                if isinstance(g, list):
                    out.extend(g)
                elif isinstance(g, str):
                    out.append(g)
        return out

    @property
    def answered(self):
        """页面上已有作答（user_ans 非空）。"""
        return bool(self._data.get("user_ans"))


class Unit(Model):
    """eval_units() 的一个可评测单元。"""

    @property
    def label(self):
        return str(self._data.get("label") or "")

    @property
    def text(self):
        return str(self._data.get("text") or "")

    @property
    def mode(self):
        return str(self._data.get("mode") or "sent")

    @property
    def ques_id(self):
        return str(self._data.get("ques_id") or 0)

    @property
    def answers(self):
        return list(self._data.get("answers") or [])

    @property
    def duration(self):
        return int(self._data.get("duration") or 0)

    @property
    def seconds(self):
        return round(self.duration / 1000.0, 1)


class Word(Model):
    """评测回包里的一个词（sil 表示停顿，不是词）。"""

    @property
    def text(self):
        return str(self._data.get("text") or "")

    @property
    def score(self):
        return _num(self._data.get("score"))

    @property
    def phonetic(self):
        return str(self._data.get("phonetic") or "")

    @property
    def silence(self):
        return self.text.lower() in ("sil", "silv", "unk")

    @property
    def begin(self):
        return _num(self._data.get("begin"))

    @property
    def end(self):
        return _num(self._data.get("end"))


class EvalResult(Model):
    """evaluate() 的返回：引擎回包归一化后的视图。"""

    @property
    def ok(self):
        return self._data.get("overall") is not None

    @property
    def score(self):
        """引擎分（0-100），等于 lines[0].score。"""
        return _num(self._data.get("overall"))

    @property
    def pronunciation(self):
        return _num(self._data.get("pronunciation"))

    @property
    def fluency(self):
        return _num(self._data.get("fluency"))

    @property
    def integrity(self):
        return _num(self._data.get("integrity"))

    @property
    def speed(self):
        return self._data.get("speed")

    @property
    def usertext(self):
        return str(self._data.get("usertext") or "")

    @property
    def sid(self):
        return str(self._data.get("sid") or "")

    @property
    def words(self):
        return [_as(w, Word) for w in (self._data.get("words") or [])]

    @property
    def raw(self):
        """引擎原始回包（errcode/result/sid…），排查时看这个。"""
        return self._data.get("raw") or {}

    def weak_words(self, limit=8, thresh=7.0):
        """念得差的词（0 分是没开口，不算），按分升序。"""
        ws = [w for w in self.words
              if not w.silence and 0 < w.score < thresh]
        ws.sort(key=lambda x: x.score)
        return ws[:limit]

    def said(self):
        """识别出的文本（无标点）。"""
        return self.usertext

    # -- 云端留存地址（云知声文档「拼接评测 audio url」）------------------

    @property
    def sound_url(self):
        """这次录音在云知声的 mp3 地址（sid+time+area 拼出来，无需鉴权）。"""
        raw = self.raw
        if not (raw.get("sid") and raw.get("time") and raw.get("area")):
            return ""
        return ("https://edu.hivoice.cn/WebAudio-1.0-SNAPSHOT/audio/play/"
                f"{raw['sid']}/{raw['time']}/{raw['area']}")

    @property
    def result_url(self):
        """这次评测的完整结果 JSON 地址（含 usertext、逐词分）。"""
        raw = self.raw
        if not (raw.get("sid") and raw.get("time") and raw.get("area")):
            return ""
        return ("https://edu.hivoice.cn/WebAudio-1.0-SNAPSHOT/result/"
                f"{raw['sid']}/{raw['time']}/{raw['area']}")

    def to_unit(self, question, unit=None, count=None, all_timer=None, audio=False):
        """
        直接把评测结果做成答卷单元。

            res.to_unit(q)              # 整段题，audioUrl 留空
            res.to_unit(q, u)           # 多问题：传对应 Unit
            res.to_unit(q, u, audio=True)   # 把录音地址也写进报文
        """
        q = question
        units = q.units if isinstance(q, Question) else eval_units(q)
        n = count if count is not None else max(1, len(units))
        pid = unit.ques_id if isinstance(unit, Unit) else (
            str(unit.get("ques_id") or 0) if isinstance(unit, dict) else 0)
        timer = all_timer
        if timer is None:
            timer = (unit.duration if isinstance(unit, Unit) else
                     int((unit or {}).get("duration") or 0)) or int(
                         _num(q.get("answer_time")) * 1000) or 60000
        return make_answer_unit(self, q.get("qid"), q.get("score"),
                                ques_id=pid, all_timer=timer, count=n,
                                audio_url=(self.sound_url if audio else ""))


class AnswerUnit(Model):
    """答卷里的一个 user_ans 单元（可直接 json 化）。"""

    def to_dict(self):
        return {k: _plain(v) for k, v in self._data.items()}


class AnswerBlock(Model):
    """答卷里的一道大题：{model_id, model_type, vip_type, ques_list}。"""

    def to_dict(self):
        return {k: _plain(v) for k, v in self._data.items()}


class ExamDetail(Model):
    """exam_detail() 的返回：url + questions + 原始 html。"""

    @property
    def kind(self):
        """考试还是练习，用来写统一的分支。练习那边返回 "hw"。"""
        return "exam"

    @property
    def complete(self):
        """True = 题面齐全，可以直接评测；练习是 False。"""
        return True

    @property
    def url(self):
        return str(self._data.get("url") or "")

    @property
    def self_id(self):
        """从答题页 URL 的 ?id= 里取回来，方便和 HWDetail 用同一套字段。"""
        m = re.search(r"[?&]id=(\d+)", self.url)
        return m.group(1) if m else ""

    @property
    def hid(self):
        """考试没有 hid，返回空串，保证字段齐全。"""
        return ""

    @property
    def questions(self):
        return [_as(q, Question) for q in (self._data.get("questions") or [])]

    @property
    def html(self):
        return self._data.get("html") or ""

    def __len__(self):
        return len(self._data.get("questions") or [])

    def __iter__(self):
        return iter(self.questions)

    def __getitem__(self, i):
        if isinstance(i, (int, slice)):
            return self.questions[i]
        return self._data[i]


def _as(obj, cls):
    """已经是目标类型就原样返回，是 dict 就包一层。"""
    if isinstance(obj, cls):
        return obj
    if isinstance(obj, Model):
        return cls(obj.to_dict())
    return cls(obj)


# ---------------------------------------------------------------- 登录态


class Session:
    """
    一个登录态 + 一个请求器。

    只保存 cookie / UA / 浏览器原始请求头，全部来自你自己登录后的浏览器。
    请求默认打 www.ekwing.com（业务接口与页面同源；api.ekwing.com 是 App 另一套鉴权）。

    附带字段（插件送来、可选）：
        names       cookie 里的键名列表
        caps        插件抓到的请求清单（诊断用）
        api_url     最近一次被捕获的接口地址
        api_method  它的 method
    """

    def __init__(self, cookie, ua=None, headers=None, timeout=20,
                 names=None, caps=None, api_url=None, api_method=None):
        self.cookie = clean_cookie(cookie)
        self.ua = ua or UA
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.names = list(names or [])
        self.caps = list(caps or [])
        self.api_url = api_url or ""
        self.api_method = api_method or ""
        self.saved = ""
        if "=" not in self.cookie:
            raise ValueError("cookie 里没有 k=v，检查是不是复制少了")

    # -- 存取 ---------------------------------------------------------

    @classmethod
    def from_file(cls, path=SESSION_PATH):
        """读 ~/.ekwing/session.json（wing serve / 插件写入的格式）。"""
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        cookie = d.get("cookie") or ""
        if not cookie:
            raise EkwingError(f"{path} 里没有 cookie")
        s = cls(cookie, ua=d.get("ua"), headers=d.get("headers"),
                caps=d.get("caps"), api_url=d.get("api_url"),
                api_method=d.get("api_method"))
        s.saved = d.get("saved") or ""
        return s

    @classmethod
    def from_ingest(cls, data):
        """
        直接吃插件 POST /ingest 的整个 JSON：
        {"cookie","names","ua","headers","apiUrl","apiMethod","caps", ...}
        """
        return cls(data.get("cookie") or "",
                   ua=data.get("ua"),
                   headers=data.get("headers") if isinstance(data.get("headers"), dict) else None,
                   names=data.get("names"),
                   caps=data.get("caps") if isinstance(data.get("caps"), list) else None,
                   api_url=data.get("apiUrl"),
                   api_method=data.get("apiMethod"))

    # 旧名字，保留兼容
    from_capture = from_ingest

    def save(self, path=SESSION_PATH):
        """写回 ~/.ekwing/session.json（键名与 wing 一致，两边可互读）。"""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        d = {"cookie": self.cookie,
             "saved": time.strftime("%Y-%m-%d %H:%M")}
        if self.ua:
            d["ua"] = self.ua
        if self.headers:
            d["headers"] = self.headers
        if self.api_url:
            d["api_url"] = self.api_url
        if self.api_method:
            d["api_method"] = self.api_method
        if self.caps:
            d["caps"] = self.caps
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def masked(self):
        out = []
        for p in [x.strip() for x in self.cookie.split(";") if x.strip()]:
            k, _, v = p.partition("=")
            out.append(f"{k}=***{v[-4:]}" if len(v) > 4 else f"{k}=***")
        return "; ".join(out)

    # -- 请求 ---------------------------------------------------------

    def call(self, path, method="GET", form=None, gzip_body=False,
             timeout=None, base=None, headers=None):
        """
        发一个请求，返回 (status, body_text)。

        path 以 http 开头则原样使用，否则拼在 base（默认 www.ekwing.com）后面。
        form 是 dict；gzip_body=True 时按页面 ExamLoad.ajaxPost 的做法
        （$.param → gzip → Content-Encoding: gzip）发送，提交/草稿必须这样。
        """
        url = path if str(path).startswith("http") else (base or BASE) + path
        h = {
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": BASE + "/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept-Encoding": "gzip, deflate",
        }
        # 浏览器真实发过的头（设备指纹 / token 校验相关），原样复用
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "cookie",
                             "connection", "accept-encoding"):
                continue
            h[k] = _header_safe(str(v))
        for k, v in (headers or {}).items():
            h[k] = _header_safe(str(v))
        if self.cookie:
            h["Cookie"] = _header_safe(self.cookie)

        body = None
        if method.upper() == "POST":
            raw_form = urlencode(form or {})
            if gzip_body:
                body = gzip.compress(raw_form.encode("latin-1", "replace"))
                h["Content-Encoding"] = "gzip"
            else:
                body = raw_form.encode()
            h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        req = Request(url, data=body, headers=h, method=method.upper())
        try:
            with urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if "gzip" in enc:
                    raw = gzip.decompress(raw)
                elif "deflate" in enc:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return getattr(r, "status", 200), _decode(raw)
        except HTTPError as e:
            raise ApiError(None, f"HTTP {e.code} {url}", None) from None
        except URLError as e:
            raise EkwingError(f"连不上 {url}：{e.reason}") from None

    def get_json(self, path, **kw):
        """请求并解析 JSON，顺带把鉴权门和 errno 变成异常。"""
        st, body = self.call(path, **kw)
        if is_gate(body):
            raise AuthError("cookie 过期或未登录：重新登录网页再取一次")
        obj = _parse_json(body)
        if isinstance(obj, dict) and obj.get("errno") not in (0, None):
            raise ApiError(obj.get("errno"), obj.get("err_info") or "", obj)
        return obj

    def valid(self):
        """会话是否还有效（打一次列表接口判断，只读）。失效返回 False，不抛。"""
        try:
            self.get_json(f"{ENDPOINTS['exam_list']}?page=1&type=2&mode_type=1")
            return True
        except AuthError:
            return False
        except ApiError as e:
            # 服务端用跳转脚本顶替 JSON 时，同样是登录态没了
            if "不是 JSON" in str(e):
                return False
            raise
        except EkwingError:
            raise


# ---------------------------------------------------------------- 列表 / 详情

KIND_TYPE = {"hw": 2, "homework": 2, "exam": 1, "exams": 1, "test": 1}


def exam_list(session, page=1, kind="hw", mode_type=1):
    """
    列表接口。**type 的含义和直觉相反，2026-11-15 从页面 JS 里确认：**

        type=2  ->  待完成      （/exam/student/examlist 默认打开的就是这个 Tab）
        type=1  ->  考试历史    （那个页面的第二个 Tab）

    依据：examList 组件的 tab 定义是 label='待完成' name='2'、
    label='考试历史' name='1'，取数调用是 findAll({...params, type: this.listType})，
    也就是说 listType 直接当 type 传给 getexamlist。
    实测：type=2 返回 5 条（首页「考试待完成 5 项」），
          type=1 返回 8 条且 _left_time.day 全为负（都是历史）。

    所以 kind="hw"（type=2）拿到的才是"还能做的考试"，
    kind="exam"（type=1）拿到的是已经交过/过期的历史记录。

    返回 ExamList：.items / .total / .get("pages")
    items 里常用字段：
        title          在 _self_info.title
        full_score     在 _self_info.score，我的分在 score
        self_id / id   详情、提交都要用这一对
        sub_time       非 "0" 表示已交；reback_reason 非空表示被退回
        _left_time     剩余时间 {"day":d,"hour":h}，day<0 表示已收卷
    """
    if isinstance(kind, int):
        type_ = kind
    else:
        type_ = KIND_TYPE.get(str(kind).lower(), 2)
    obj = session.get_json(
        f"{ENDPOINTS['exam_list']}?page={int(page)}&type={type_}&mode_type={mode_type}",
        timeout=25)
    data = obj.get("data") or {}
    return ExamList({
        "total": data.get("total") or 0,
        "page": data.get("page") or page,
        "pages": data.get("pages") or 0,
        "type": data.get("type") or type_,
        "items": [ExamItem(x, kind=("exam" if type_ == 1 else "hw"))
                  for x in (data.get("list") or [])],
        "raw": obj,
    })


_PAGES_RE = re.compile(r"共\s*(\d+)\s*页")

# 排序档位：能交的在前，做不了的在后
STATE_RANK = {
    "未交": 0,       # 还没动过，最该做
    "已退回": 1,     # 老师退回来了，可以重交
    "已截止": 2,     # 时间过了，做不了
    "已收卷": 2,     # 同上（考试的说法）
    "已交": 3,       # 做完了，放最后
}


def _page_count(lst):
    """服务端 pages 是 '     <span>共 1 页</span>' 这种串，不是数字；抽不出来返回 0。"""
    m = _PAGES_RE.search(str(lst.get("pages") or ""))
    return int(m.group(1)) if m else 0


def _sort_key(it):
    """
    排序键：(状态档位, 截止时间戳)。
    同档里截止早的排前面；没有 end_time 的排同档末尾。
    状态是服务端新加的、这里没见过 -> 按 0（未交）处理，宁可排前面也别漏掉。
    """
    try:
        ts = int(it.get("end_time") or 0)
    except (TypeError, ValueError):
        ts = 0
    return (STATE_RANK.get(it.state, 0), ts or 1 << 31)


def all_papers(session, kinds=("exam", "hw"), max_pages=50, only_open=False,
               sort=True):
    """
    把考试和训练的**所有页**拉下来，合并成一个列表。

        lst = ek.all_papers(s)                    # 默认已按状态排好
        lst = ek.all_papers(s, kinds=("hw",))     # 只要训练
        lst = ek.all_papers(s, only_open=True)    # 只留还能做的
        lst = ek.all_papers(s, sort=False)        # 要服务端原序

    返回 list[ExamItem]，每条自带 .kind（"exam" / "hw"）。

    排序规则（sort=True，默认）：
        未交  →  已退回  →  已截止/已收卷  →  已交
        同档里按截止时间从早到晚排，没截止时间的排同档最后。

    两个坑，都实测过：
      1. 服务端 pages 字段是 HTML 片段 '     <span>共 1 页</span>'，不是数字；
      2. 页码越界**不返回空列表**，而是把第 1 页原样再发一遍（8 条 self_id 完全一致）。
         所以不能用「本页为空」判断结束，只能靠 self_id 去重 + 整页全是旧的才停。
    """
    out, seen = [], set()
    for kind in kinds:
        pages, page = 1, 1
        while page <= min(pages, max_pages):
            lst = exam_list(session, page=page, kind=kind)
            items = list(lst)
            if page == 1:
                pages = _page_count(lst)
                if not pages:             # pages 解析不出来，就用 total/每页条数 估
                    pages = max(1, -(-lst.total // (len(items) or 1)))
            fresh = 0                     # 本页去掉重复后还剩几条
            for it in items:
                key = it.self_id or it.id
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                fresh += 1
                if only_open and it.state in ("已截止", "已收卷"):
                    continue
                out.append(it)
            if not items or fresh == 0:   # 空页，或服务端把第 1 页又发了一遍
                break
            page += 1
    return sorted(out, key=_sort_key) if sort else out


_HW_FIELDS = (
    ("title", re.compile(r"<h2>(.*?)</h2>", re.S)),
    ("hid", re.compile(r"hid=(\d+)")),
    ("start", re.compile(r"开始时间：\s*([\d\-: ]+)")),
    ("end", re.compile(r"结束时间：\s*([\d\-: ]+)")),
    ("message", re.compile(r"留言：\s*([^<]*)")),
    ("reback", re.compile(r"退回留言：\s*([^<]*)")),
    ("pass", re.compile(r"要求达标分数：\s*(\d+)")),
)


def _parse_hw_items(html):
    """按 busywork_item 切块，每块抠标题/hid/起止时间/留言。"""
    out = []
    for seg in html.split("busywork_item")[1:]:
        d = {}
        for key, rx in _HW_FIELDS:
            m = rx.search(seg)
            if m:
                d[key] = re.sub(r"\s+", " ", m.group(1)).strip()
        if d.get("title") and d.get("hid"):
            out.append(HwItem(d))
    return out


def hw_list(session, method="unfinish", max_pages=20):
    """
    首页「训练」那批的列表 —— /hw/stuList，服务端渲染的 HTML，没有 JSON 接口。

        method  unfinish 待完成（默认）/ new 新布置 / back 被退回 / finish 训练历史
        返回 list[HwItem]，每条有 .title / .hid / .url / .start / .end / .state

    实测：这个列表 10 条一页，page=2 有 1 条，page=3 空。
    跟 exam_list(kind="hw") 不是一回事 —— 那个是 exam 模块的 type=2，
    列的是 Unit 听说题；这里才是首页「训练」卡片点进去看到的东西。
    """
    out, page = [], 1
    while page <= max_pages:
        url = f"{BASE}/hw/stuList?method={method}"
        if page > 1:
            url += f"&page={page}"
        st, body = session.call(url, timeout=25)
        if is_gate(body):
            raise AuthError("cookie 过期或未登录：重新登录网页再取一次")
        items = _parse_hw_items(body)
        if not items:                 # 空页 = 到底了
            break
        out.extend(items)
        page += 1
    return out


def _mk_task(kind, tid, title, state, end, url, raw):
    # 两种来源格式不同：train 是 "2026-11-15 23:55:00"，exam 是 "2026-11-15 23:55"
    end = re.sub(r"(\d\d:\d\d):\d\d$", r"\1", str(end or "")).strip()
    return Task({"kind": kind, "id": str(tid or ""), "title": title,
                 "state": state, "end": end, "url": url}, raw=raw)


def tasks(session, include_history=False):
    """
    首页那两张卡片的合并列表 —— 训练 11 + 考试 5 = 16 条。

        lst = ek.tasks(s)                          # 16 条
        lst = ek.tasks(s, include_history=True)    # 再并上考试历史 8 条

    为什么是这两个来源：
        「训练」卡片 -> /hw/stuList?method=unfinish   （hw 模块，同步训练11-13 那批）
        「考试」卡片 -> getexamlist?type=2            （待完成，5 条）
    而 type=1 是**考试历史**，不是待完成 —— 以前把它并进来，所以才出现
    "明明首页 16 项，列表却是 13 条"这种对不上的情况。

    返回 list[Task]，排序：未交 -> 已退回 -> 已截止 -> 已交，同档按截止时间。
    """
    out = []
    for h in hw_list(session):
        out.append(_mk_task("train", h.hid, h.title, h.state, h.end, h.url, h))
    for it in exam_list(session, kind="hw"):          # type=2 待完成
        out.append(_mk_task("exam", it.self_id, it.title, it.state, it.deadline,
                            f"{BASE}/exam/student/examload?id={it.self_id}", it))
    if include_history:
        for it in exam_list(session, kind="exam"):    # type=1 历史
            out.append(_mk_task("exam", it.self_id, it.title, it.state, it.deadline,
                                f"{BASE}/exam/student/examload?id={it.self_id}", it))
    out.sort(key=lambda t: (STATE_RANK.get(t.state, 0), t.end or "9999"))
    return out


def exam_html(session, self_id, hid=None):
    """
    抓答题页 HTML，返回 (url, html)。

    只有 self_id 是必需的，两个 id 的含义（已实测，见 docstring 末尾）：

        self_id  列表里 item.self_id，等于 _self_info.id，代表「你这条作业的记录」：
                 它同时带着 uid、draft_id、end_time、能不能重做，服务端靠它在
                 题库里找到「给你这份卷」的那一份。提交、草稿、查看解析、重做
                 全链路都用它，页面里还以 <input name="self_id"> 的形式提交。
        hid      列表里 item.id，拼进 URL 的 &_id= 参数。实测服务端不读它：
                 换成别的作业的值、乱填 123456、整个省略，返回的题目完全一致
                 （HTML 字节数、5 个题型、首题原文都一样）。传进来只是为了让
                 url 与浏览器地址栏一模一样；只传 self_id 也能正常取题。

    实测证据（2026-11-13，作业 9532367 / 9531171）：
        ?id=9532367&_id=784595574   → 5 题，首题 "I want to introduce…"
        ?id=9532367&_id=784493789   → 完全相同
        ?id=9532367&_id=123456      → 完全相同
        ?id=9531171&_id=784595574   → 5 题，首题 "Capybaras live…"   ← 换 self_id 才变
        ?id=784595574&_id=9532367   → ExamUnavailable（对调就取不到）
        页面 HTML 里 9532367 出现 9 次（含隐藏 input、"全部重做"链接），
        784595574 出现 0 次。
    """
    url = f"{ENDPOINTS['exam_load']}?id={self_id}"
    if hid not in (None, ""):
        url += f"&_id={hid}"
    st, body = session.call(url, timeout=30)
    if is_gate(body):
        raise AuthError("cookie 过期或未登录：重新登录网页再取一次")
    if "spoken_list" not in body:
        flat = clean_text(body)
        for kw, why in (("已经收卷", "已收卷，不能再作答"),
                        ("还没开始", "还没到开始时间"),
                        ("不能答题", "这套卷子不接受作答")):
            if kw in flat:
                raise ExamUnavailable(why)
        raise ExamUnavailable("页面里没有题目结构（可能不是听说题）")
    return url, body


def exam_detail(session, self_id, hid=None):
    """
    打开一条作业/考试：抓答题页 + 解析内联数据，一次到位。

    等价于「在网页上点开这条作业」那一下 —— 没有返回题目的 JSON 接口，
    题目和音频地址是服务端写进 HTML 的 <script> 里的，所以必须抓页面再解析。

    参数：
        session  登录态
        self_id  考试的 item.self_id；也可以直接传 Task / ExamItem 对象，
                 那条是同步训练时会自动转去 hw_detail（练习没有 self_id）
        hid      item.id，URL 的 &_id=，实测被忽略（可省，见 exam_html）

    返回 ExamDetail：
        .url        实际抓的地址，提交时当 href 用
        .questions  [Question]，每题含 name/qid/model_type/score/audio/ques_list…
        .html       原页面文本（解析器漏字段时自己再抠）

    失败时抛：
        AuthError        cookie 过期（命中登录提示页）
        ExamUnavailable  已收卷 / 还没开始 / 不接受作答 / 页面里没有题目结构
    """
    # 允许直接传对象：Task / ExamItem / HwItem 都行
    if not isinstance(self_id, (str, int)):
        obj = self_id
        if isinstance(obj, Task):
            if obj.kind in ("hw", "train"):
                return hw_detail(session, obj.hid or obj.id)
            self_id = obj.self_id
        else:
            self_id = getattr(obj, "self_id", "") or str(obj.get("self_id") or "")
    self_id = str(self_id or "").strip()
    if not self_id:
        raise ValueError(
            "self_id 是空串。这条是同步训练（练习），练习没有 self_id，"
            "所以拿 it.self_id 去查一定会失败。"
            "直接传对象即可：ek.exam_detail(s, it) 或 it.open(s)，"
            "也可以 ek.hw_detail(s, it.hid)"
        )
    url, html = exam_html(session, self_id, hid)
    qs = parse_exam(html)
    if not qs:
        raise ExamUnavailable("页面里没解析到题目")
    return ExamDetail({"url": url, "questions": qs, "html": html})


def _js_object(text, marker):
    """从 HTML 里抠出 window.xxx = {...}; 那个对象（按括号配对，不做正则硬切）。"""
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(text)):
        ch = text[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[j:k + 1]
    return None


def parse_exam(html):
    """
    解析答题页内联数据，返回题目列表（按页面顺序）。

    每项字段：
        no            页内序号，从 1 开始
        qid           model_id，交卷报文里的 model_id
        name          题型名
        score         满分
        answer_time   作答秒数（字符串）
        prepare_time  备考秒数
        model_type    题型编号（见 MODE_BY_MODEL_TYPE）
        intro_text    引导原文        real_text/dis_text  参考原文
        intro_audio   引导音频地址
        audio         [音频地址...]（腾讯云 COS，公开可读）
        title_text    题面/听力材料原文（信息转述的整段材料在这里）
        title_audio   题面音频
        desc          要点提纲（信息转述给的是省略句提纲）
        answer_tip    答题提示（"你的介绍可以这样开始：…"）
        answers       参考答案列表（信息转述是多个可接受版本）
        parags        分段原文（real_parags / dis_parags）
        vip_type      是否需要 VIP
        user_ans      页面上已有作答（未答则空）
        ques_list     小题数组（关键词 / answer_tip 在这里）
    """
    res_raw = _js_object(html, "window.spoken_res_list")
    list_raw = _js_object(html, "window.spoken_list")
    res, qs = {}, {}
    for raw, box in ((res_raw, res), (list_raw, qs)):
        if raw:
            try:
                box.update(json.loads(raw.replace("\\/", "/")))
            except ValueError:
                pass

    def unesc(v):
        return str(v or "").replace("\\/", "/")

    def flat(v):
        """
        answer 形如 [[显示文本, 参考文本], ...]，平台取的是参考列（t[1]||t[0]）。
        这里只保留参考列，去重后返回。
        """
        picks = []
        for x in (v or []) if isinstance(v, list) else [v]:
            if isinstance(x, (list, tuple)):
                if len(x) > 1 and x[1]:
                    picks.append(str(x[1]))
                elif x:
                    picks.append(str(x[0]))
            elif x:
                picks.append(str(x))
        out_, seen = [], set()
        for s in picks:
            t = clean_text(unesc(s))
            if t and t not in seen:
                seen.add(t)
                out_.append(t)
        return out_

    out = []
    for i, qid in enumerate(list(qs) if qs else list(res), 1):
        info = qs.get(qid) or {}
        r = res.get(qid) or {}

        out.append(Question({
            "no": i,
            "qid": str(qid),
            "name": clean_text(str(info.get("name") or info.get("cn_name") or "")),
            "score": info.get("score") or "",
            "answer_time": info.get("answer_time") or "",
            "prepare_time": info.get("prepare_time") or 0,
            "model_type": str(info.get("model_type") or ""),
            "intro_text": clean_text(unesc(info.get("intro_text"))),
            "real_text": clean_text(unesc(info.get("real_text"))),
            "dis_text": clean_text(unesc(info.get("dis_text"))),
            "intro_audio": unesc(info.get("intro_audio")),
            "title_text": clean_text(unesc(info.get("title_text"))),
            "title_audio": unesc(info.get("title_audio")),
            "desc": clean_text(unesc(info.get("desc"))),
            "answer_tip": clean_text(unesc(info.get("answer_tip"))),
            "answers": flat(info.get("answer")),
            "parags": [clean_text(unesc(x)) for x in
                       (info.get("real_parags") or info.get("dis_parags") or []) if x],
            "audio": [a.replace("\\/", "/") for a in (r.get("audio") or []) if a],
            "pic": [p.replace("\\/", "/") for p in (r.get("pic") or []) if p],
            "vip_type": int(info.get("vip_type") or 0),
            "user_ans": info.get("user_ans") or [],
            "ques_list": info.get("ques_list") or [],
        }))
    return out


# ---------------------------------------------------------------- 报文组装


def eval_units(q):
    """
    把一道题拆成可评测的单元：[{"label","text","mode","ques_id","answers","duration"}]

    取值顺序完全照抄页面 main.js 的 getQuesItems()：
        1. real_text        模仿朗读：送引擎的是 real_text（数字写英文），
                            屏显的 dis_text 只是给人看，别拿去评测
        2. ques_list        听选/回答/询问：一问一个单元，
                            参考文本 = 小题 answer[*][1]（参考列）→ ext.keywords[0] → answer_tip
                            时长 = answer_time / 小题数
        3. title_text       没有 ques_list 的整段题（如信息转述）兜底用 title_text
    平台会把多组可接受答案拼成 JSGF 语法一次送引擎；本库只取第一条，
    要更宽松就自己把 answers 里的候选逐个 evaluate 取最高分。
    """
    mode = MODE_BY_MODEL_TYPE.get(str(q.get("model_type")), "sent")
    if q.get("real_text"):
        return [Unit({"label": q.get("name") or "朗读", "text": q["real_text"],
                      "mode": mode, "ques_id": 0, "answers": [q["real_text"]],
                      "duration": _ms(q.get("answer_time"))})]
    if not q.get("ques_list") and (q.get("title_text") or q.get("answers")):
        refs = q.get("answers") or [q.get("title_text")]
        return [Unit({"label": q.get("name") or "转述", "text": refs[0],
                      "mode": mode, "ques_id": 0, "answers": refs,
                      "duration": _ms(q.get("answer_time"))})]

    subs = q.get("ques_list") or []
    units = []
    for i, x in enumerate(subs, 1):
        ext = x.get("ext") or {}
        refs = []
        for pair in (x.get("answer") or []):
            if isinstance(pair, (list, tuple)) and len(pair) > 1 and pair[1]:
                refs.append(clean_text(str(pair[1])))
            elif isinstance(pair, str):
                refs.append(clean_text(pair))
        if not refs:
            for g in (ext.get("keywords") or []):
                if isinstance(g, list) and g:
                    refs.append(clean_text(str(g[0])))
                    break
                if isinstance(g, str):
                    refs.append(clean_text(g))
                    break
        if not refs and ext.get("answer_tip"):
            refs.append(clean_text(str(ext["answer_tip"])).strip("()（） "))
        refs = [r for r in refs if r]
        if not refs:
            continue
        title = clean_text(str(x.get("title_text") or "")).strip()
        units.append(Unit({"label": f"第{i}问 {title}" if title else f"第{i}问",
                           "text": refs[0],
                           "mode": "qa",
                           # 平台发给服务端的是小题 id（不是序号），见 staAnswer/getAns
                           "ques_id": str(x.get("id") or 0),
                           "answers": refs,
                           "duration": _ms(q.get("answer_time")) // max(1, len(subs))}))
    return units


def _ms(seconds):
    """页面里的时长单位是毫秒；answer_time 是秒（可能字符串）。"""
    try:
        return int(float(seconds or 0) * 1000) or 0
    except (TypeError, ValueError):
        return 0


def make_answer_unit(result, model_id, full_score, ques_id=0, all_timer=60000,
                     count=1, request_id="", audio_url="", wavetime=0,
                     delay_time=0, systime=0):
    """
    评测结果 -> 平台要的 user_ans 单元（逐字段照抄页面 main.js getAns()）：

        score      = floor( 100 * (parseInt(引擎分)/100) * (满分/小题数) ) / 100
        precision  = lines[0].pronunciation      （不是顶层 precision 字段）
        details[]  = {score, text: line.text, words:[{score,text}]}，sil 保留
        recordId   = 评测 sid
        requestId  = yzs 链路下平台发的是空串（tokenId 只有驰声引擎才有）
        _from      = "unisound" / _score_engine = "yzs"（我们用的就是云知声）
    平台用「满分/小题数」均分到每个单元，所以同一道多问题要把 count 传进来。

    三个时间字段（页面全填 0，这里默认也是 0，想改再传）：
        all_timer   答题时限，毫秒，题目自带（如 8000 = 8 秒）
        wavetime    这段录音的时长，毫秒
        delay_time  思考/准备时长，毫秒
        systime     系统耗时，毫秒
    注意：这三个只管"每段作答"，**整卷用时 use_time 是服务端算的，改不了**。
    """
    engine_score = int(float(result.get("overall") or 0))      # 页面是 parseInt
    try:
        share = float(full_score) / max(1, int(count))
        scaled = math.floor(100 * (engine_score / 100.0 * share)) / 100
    except (TypeError, ValueError):
        scaled = float(engine_score)

    lines = result.get("lines") or []
    head = lines[0] if lines else {}
    prec = head.get("pronunciation")
    if prec is None:
        prec = result.get("pronunciation") or 0

    details = []
    for L in lines:
        words = [{"score": w.get("score"), "text": (w.get("text") or "").strip()}
                 for w in (L.get("words") or []) if (w.get("text") or "").strip()]
        details.append({"score": L.get("score"),
                        "text": L.get("text") or L.get("sample") or "",
                        "words": words})
    return AnswerUnit({
        "allTimer": int(all_timer),
        "audioUrl": audio_url,
        "model_id": model_id,
        "precision": float(prec),
        "ques_id": ques_id,
        "rank": 100,
        "details": details,
        "recordId": result.get("sid") or "",
        "requestId": request_id,
        "score": scaled,
        "offline": False,
        "use_uni": False,
        "delayTime": int(delay_time), "pretime": 0, "pron": 0, "res": 0,
        "systime": int(systime), "version": 0, "wavetime": int(wavetime),
        "_client": "js",
        "_from": "unisound",
        "_score_engine": "yzs",
    })


def build_answers(questions, pool=None):
    """
    组交卷/草稿报文：每题的 ques_list 就是作答单元数组。

    pool 形如 {qid: [unit, ...]}；题里没有就退回页面上已有的 user_ans。
    """
    pool = pool or {}
    arr = []
    for q in questions:
        units = pool.get(str(q.get("qid"))) or q.get("user_ans") or []
        arr.append(AnswerBlock({
            "model_id": str(q.get("qid")),
            "model_type": str(q.get("model_type") or ""),
            "vip_type": int(q.get("vip_type") or 0),
            "ques_list": [_plain(u) for u in units],   # 纯 dict，json.dumps 直接可用
        }))
    return arr


def _answers_json(answers):
    if isinstance(answers, str):
        return answers
    return json.dumps(answers, ensure_ascii=False, default=_json_default)


# ---------------------------------------------------------------- 练习（同步训练）

# 这些 type_code 是要开口的，其余（阅读/语法/听力选择）走不了评测链路
SPOKEN_TYPES = {"BIZ_WORD", "BIZ_SEN", "BIZ_PARA", "BIZ_QA", "BIZ_RETELL"}

# 练习的题型 -> 评测引擎的 model_type。**这是按语义推断的占位值**，
# 服务端在练习页里没有下发 model_type，所以只能先这么映射，
# 让 evaluate() 能查到正确的 mode；等拿到真实 id 再改这里。
_HW_MODEL_TYPE = {
    "BIZ_WORD": "10",      # 读记词语 -> word
    "BIZ_SEN": "11",       # 课文跟读 -> sent
    "BIZ_PARA": "1",       # 段落朗读 -> para
    "BIZ_QA": "7",         # 问答 -> qa
    "BIZ_RETELL": "6",     # 复述 -> retell
}


class HWItem(Model):
    """
    同步训练里的一个题型条目（/hw/do 页面 hwCntVm 那段 JSON 的一项）。

    字段：id / number / biz / type_code / type_name / group_name / ids / duration
    """

    @property
    def id(self):
        return str(self._data.get("id") or "")

    @property
    def number(self):
        return str(self._data.get("number") or "")

    @property
    def biz(self):
        return str(self._data.get("biz") or "")

    @property
    def type_code(self):
        return str(self._data.get("type_code") or "")

    @property
    def type_name(self):
        return str(self._data.get("type_name") or "")

    @property
    def group_name(self):
        return str(self._data.get("group_name") or "")

    @property
    def ids(self):
        """题目 id 列表（原字段是逗号串）。"""
        return [x for x in str(self._data.get("ids") or "").split(",") if x]

    @property
    def duration(self):
        return _num((self._data.get("ext") or {}).get("duration"))

    @property
    def finished(self):
        return str(self._data.get("finish") or "") == "y"

    @property
    def submitted(self):
        """和 ExamItem 同名，让 Task 转发时两边行为一致。"""
        return self.finished

    @property
    def spoken(self):
        """要不要开口。读记词语、课文跟读都算。"""
        return self.type_code in SPOKEN_TYPES or self.group_name == "口语"

    @property
    def model_type(self):
        """推断出来的 model_type，让 Question.mode 能查到 word/sent/para。"""
        return _HW_MODEL_TYPE.get(self.type_code, "")

    @property
    def mode(self):
        return MODE_BY_MODEL_TYPE.get(self.model_type, "sent")

    def __repr__(self):
        return f"<HWItem {self.type_code} {self.type_name} ids={len(self.ids)}>"


class HWDetail(Model):
    """
    练习详情。**接口形状和 ExamDetail 完全一致**，考试那套循环原样能用：

        for q in d:                 # q 是 Question，有 name/qid/score/units
            for u in q.units:       # u 是 Unit，有 label/text/mode/ques_id
                print(q.name, u.label, u.text)

    和考试的唯一差别：练习的题面这页拿不到，所以
        u.text  统一是空串
        q.score 是 0
        u.duration 用的是题型时长，不是每句时长
    其余属性都能正常读，不会 AttributeError。原始题型清单在 .items 里。
    """

    @property
    def kind(self):
        return "hw"

    @property
    def complete(self):
        """False = 只有题型骨架，题面文本缺失，不能直接拿来评测。"""
        return False

    @property
    def hid(self):
        return str(self._data.get("hid") or "")

    @property
    def url(self):
        return str(self._data.get("url") or "")

    @property
    def self_id(self):
        """练习没有 self_id，返回空串，保证字段齐全（和 HWDetail 的 hid 对称）。"""
        return ""

    @property
    def html(self):
        return self._data.get("html") or ""

    @property
    def info(self):
        """页面 infoVm 里的原始信息（param_term / sys / can_submit / finish …）。"""
        return self._data.get("info") or {}

    @property
    def name(self):
        return str(self.info.get("name") or "")

    @property
    def param_term(self):
        return str(self.info.get("param_term") or "")

    @property
    def full_score(self):
        """练习按 100 分制。页面只给达标分不给满分，所以默认 100。"""
        return 100.0

    @property
    def count(self):
        """题目总数（按 id 摊开后的道数）。"""
        return sum(len(it.ids) or 1 for it in self.items)

    @property
    def sys(self):
        return str(self.info.get("sys") or "train")

    @property
    def items(self):
        """原始题型清单（HWItem）。"""
        return [_as(x, HWItem) for x in (self._data.get("items") or [])]

    @property
    def questions(self):
        """
        **一个分类（题型）= 一道大题，里面的每个单词/句子 = 一个小题。**

            BIZ_WORD 读记词语 31 个词 -> 1 道大题，31 个 unit
            BIZ_SEN  课文跟读 1 句话  -> 1 道大题，1 个 unit

        大题分数按小题数占比分摊到总分，最后一道吃掉零头，
        保证 sum(q.score) 正好等于 full_score（100）。
        """
        items = self.items
        total_units = sum(len(it.ids) or 1 for it in items) or 1
        out, acc = [], 0.0
        for no, it in enumerate(items, 1):
            ids = it.ids or [""]
            if no == len(items):
                score = round(self.full_score - acc, 2)     # 末题补差
            else:
                score = round(self.full_score * len(ids) / total_units, 2)
                acc += score
            units = [{
                "label": str(k + 1),
                "text": "",                              # 题面缺失
                "mode": it.mode,
                "ques_id": qid,
                "answers": [],
                "duration": int(it.duration * 1000),
            } for k, qid in enumerate(ids)]
            out.append(Question({
                "no": no,
                "qid": it.id,
                "name": it.type_name,
                "model_type": it.model_type,
                "score": score,
                "answer_time": it.duration,
                "_fixed_units": units,
            }))
        return out

    @property
    def spoken_items(self):
        return [x for x in self.items if x.spoken]

    @property
    def has_spoken(self):
        return bool(self.spoken_items)

    def __len__(self):
        return len(self.questions)

    def __iter__(self):
        return iter(self.questions)

    def __getitem__(self, i):
        if isinstance(i, (int, slice)):
            return self.questions[i]
        return self._data[i]

    def __repr__(self):
        return f"<HWDetail hid={self.hid} 题型={len(self.items)}>"


def _parse_hw_cnt(html):
    """从 /hw/do 的 HTML 里抠出 id="hwCntVm" 那段隐藏 JSON 数组。"""
    i = html.find('id="hwCntVm"')
    if i < 0:
        return []
    start = html.find("[", i)
    if start < 0:
        return []
    try:
        arr, _ = json.JSONDecoder().raw_decode(html[start:])
    except ValueError:
        return []
    return arr if isinstance(arr, list) else []


def _parse_hw_info(html):
    """从 /hw/do 的 HTML 里抠出 id="infoVm" 那段隐藏 JSON 对象。"""
    i = html.find('id="infoVm"')
    if i < 0:
        return {}
    start = html.find("{", i)
    if start < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[start:])
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def hw_detail(session, hid):
    """
    打开一条同步训练（/hw/do?hid=xxx），返回 HWDetail。

        d = ek.hw_detail(s, "40224")
        for q in d:                      # 和考试一样的循环
            print(q.name, q.qid, len(q.units))
        for it in d.spoken_items:        # 只看要开口的题型
            print(it.type_name, it.duration)

    取不到时抛 ExamUnavailable（已交完 / 已过期 / 不是这条训练）。
    """
    hid = str(hid)
    url = f"{BASE}/hw/do?hid={hid}&param_term="
    st, body = session.call(url, timeout=30)
    if is_gate(body):
        raise AuthError("cookie 过期或未登录：重新登录网页再取一次")
    items = _parse_hw_cnt(body)
    if not items:
        flat = clean_text(body)
        for kw, why in (("已经收卷", "已收卷，不能再作答"),
                        ("不能答题", "这套训练不接受作答"),
                        ("完成所有训练", "页面没带题型数据，可能已经提交过")):
            if kw in flat:
                raise ExamUnavailable(why)
        raise ExamUnavailable("页面里没有 hwCntVm 题型数据（可能不是同步训练，或已交）")
    return HWDetail({"hid": hid, "url": url, "items": items,
                     "info": _parse_hw_info(body), "html": body})


def hw_submit(session, hid, answers=None, href="", param_term=None, sys=None,
              check=1, timeout=40):
    """
    交练习卷（整卷交卷）。

    **check 是标志位，不是答案内容。** 页面原文：

        submit: function(i) { void 0 === i && (i = 1);
            $.ajax({url: ... + "Hw/submit",
                    data: {hid: o.id, check: i, param_term: o.param_term, sys: ...}}) }

    所以这里 answers 收下但不发（保留位置，和 submit() 的参数顺序对齐）。
    每个题型的答案是在题型页里各自提交的，这个接口只负责「全做完了，收卷」。

    hid 可以是 hid 字符串、Task、HWDetail。
    param_term / sys 不传时，会从对象上找，还找不到就现抓一次页面补上。
    返回服务端 JSON，正常是 {"status":0,...}。
    """
    if not isinstance(hid, str):
        obj = hid                                  # HWDetail 或 Task
        hid = (str(obj.get("hid") or "") if isinstance(obj, HWDetail)
               else getattr(obj, "hid", "") or getattr(obj, "id", ""))
        if param_term is None:
            param_term = getattr(obj, "param_term", None)
        if sys is None:
            sys = getattr(obj, "sys", None)
    hid = str(hid or "").strip()
    if not hid:
        raise ValueError("hw_submit 需要 hid（练习没有 self_id，别把空串传进来）")

    if not param_term or not sys:              # 缺参数就照页面抓一次 infoVm
        try:
            info = _parse_hw_info(session.call(
                f"{BASE}/hw/do?hid={hid}&param_term=", timeout=25)[1])
            param_term = param_term or info.get("param_term") or ""
            sys = sys or info.get("sys") or ""
        except Exception:
            param_term = param_term or ""

    form = {"hid": hid, "check": int(check), "param_term": str(param_term or "")}
    if sys:
        form["sys"] = sys
    st, body = session.call(f"{BASE}/Hw/submit", "POST", form, timeout=timeout)
    if is_gate(body):
        raise AuthError("cookie 过期或未登录")
    obj = _parse_json(body, "hw submit ")
    if obj.get("status") not in (0, "0", None):
        raise ApiError(obj.get("status"), obj.get("err_info") or obj.get("msg") or "", obj)
    return obj


def paper_detail(session, pid, kind=None):
    """
    统一入口：考试和练习都吃同一个 id 参数。

        d = ek.paper_detail(s, "9327746")    # 考试 -> ExamDetail
        d = ek.paper_detail(s, "40224")      # 练习 -> HWDetail

    不传 kind 时自动试：先当考试取，取不到再当练习取。
    id 传的是考试 self_id 还是练习 hid 由你自己知道，也可以直接指定：
        ek.paper_detail(s, pid, kind="exam") / kind="hw"
    """
    pid = str(pid)
    if kind == "exam":
        return exam_detail(session, pid)
    if kind in ("hw", "train"):
        return hw_detail(session, pid)

    try:
        return exam_detail(session, pid)
    except ExamUnavailable as first:
        try:
            return hw_detail(session, pid)
        except ExamUnavailable:
            raise first from None


def paper_submit(session, target, answers, href="", **kw):
    """
    统一交卷：考试和练习都吃同一个 target。

        ek.paper_submit(s, it, ans)      # it 是 Task
        ek.paper_submit(s, d, ans)       # d 是 ExamDetail / HWDetail
        ek.paper_submit(s, "9327746", ans, href=d.url)   # 直接给考试 self_id

    内部按 kind 分派：练习走 Hw/submit，考试走 examsubmit。
    """
    kind = getattr(target, "kind", "")
    if kind in ("hw", "train") or isinstance(target, HWDetail):
        return hw_submit(session, target, answers, href=href, **kw)
    if isinstance(target, str):
        return submit(session, target, answers, href, **kw)
    pid = getattr(target, "self_id", "") or getattr(target, "id", "")
    return submit(session, pid, answers, href or getattr(target, "url", ""), **kw)


# ---------------------------------------------------------------- 草稿 / 提交


def save_draft(session, self_id, answers, timeout=40):
    """
    存草稿（GET 一次、POST 一次都不会影响正式提交）。
    风险低：网页自己每 60 秒也会覆盖一次。
    """
    st, body = session.call(ENDPOINTS["savedraft"], "POST",
                            {"answer": _answers_json(answers),
                             "self_id": str(self_id)},
                            gzip_body=True, timeout=timeout)
    if is_gate(body):
        raise AuthError("cookie 过期或未登录")
    obj = _parse_json(body, "savedraft ")
    if obj.get("errno") not in (0, None):
        raise ApiError(obj.get("errno"), obj.get("err_info") or "", obj)
    return obj


def submit(session, self_id, answers, href, is_force=False, timeout=40):
    """
    真正交卷 —— 不可逆，会消耗一次机会、退回次数 +1。

    href 传 exam_detail()["url"]，页面就靠它判断是哪张卷子。
    返回服务端 JSON（正常是 {"errno":0,...}）。

    self_id 也可以直接传 Task / ExamDetail：
      * 考试 -> 走 examsubmit，和以前完全一样
      * 练习 -> 自动转 hw_submit（练习没有 self_id，考试接口会回 errno 9999）
    传空 self_id 但 href 里带 hid= 时，同样会转成练习提交。
    """
    if not isinstance(self_id, (str, int)):
        obj = self_id
        if isinstance(obj, Task) and obj.kind in ("hw", "train"):
            return hw_submit(session, obj.hid or obj.id, answers, href=href)
        self_id = getattr(obj, "self_id", "") or str(obj.get("self_id") or "")

    self_id = str(self_id or "").strip()
    if not self_id:
        m = re.search(r"[?&]hid=(\d+)", href or "")
        if m:
            return hw_submit(session, m.group(1), answers, href=href)
        raise ValueError(
            "self_id 是空串，交不上去。考试要传 it.self_id（Task 的 exam 类型才有），"
            "练习请传 it 或者 it.hid：ek.submit(s, it, ans, d.url) / ek.hw_submit(s, it, ans)"
        )

    form = {
        "answer": _answers_json(answers),
        "is_force": "true" if is_force else "false",
        "self_id": self_id,
        "href": href,
    }
    st, body = session.call(ENDPOINTS["examsubmit"], "POST", form,
                            gzip_body=True, timeout=timeout)
    if is_gate(body):
        raise AuthError("cookie 过期或未登录")
    obj = _parse_json(body, "examsubmit ")
    if obj.get("errno") not in (0, None):
        raise ApiError(obj.get("errno"), obj.get("err_info") or "", obj)
    return obj


# ---------------------------------------------------------------- 评测

_EVAL_APPKEY = "uus46rhwq3x75562p2cd7f7qplwso6wt5xd4qqae"   # 页面 SDK 内置公共 key


def _rid(n=16):
    pool = string.ascii_lowercase + string.digits
    return "".join(random.choice(pool) for _ in range(n))


def evaluate(pcm, text, model_type=None, mode=None, timeout=30,
             chunk=3200, appkey=_EVAL_APPKEY):
    """
    把 16k/16bit/单声道 PCM 丢给评测服务，返回归一化结果。

    text 是参考原文；mode 不给就按 model_type 查表（MODE_BY_MODEL_TYPE）。
    返回：overall(0-100) / pronunciation / fluency / integrity / speed /
          usertext / words[] / lines[] / sid / area / time / raw
    出错抛 EvalError。
    """
    try:
        import websocket
    except ImportError:
        raise ImportError("评测需要 websocket-client：pip install websocket-client") from None

    if not mode:
        mode = MODE_BY_MODEL_TYPE.get(str(model_type or ""), "sent")
    if not text:
        raise ValueError("text 不能为空：评测要拿原文对齐")
    if not pcm:
        raise ValueError("pcm 不能为空")

    eof = _rid()
    start = {"mode": mode, "appkey": appkey, "displayText": text,
             "audioFormat": "pcm", "eof": eof, "scoreCoefficient": "1"}
    try:
        ws = websocket.create_connection(
            ENDPOINTS["eval_ws"], timeout=timeout,
            header=["Origin: " + BASE])
    except Exception as e:
        raise EvalError(f"连不上评测服务：{type(e).__name__} {e}") from None

    try:
        ws.send(json.dumps(start))
        for i in range(0, len(pcm), chunk):
            ws.send_binary(pcm[i:i + chunk])
        ws.send(eof)

        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = ws.recv()
            if isinstance(msg, bytes):
                continue
            obj = json.loads(msg)
            if obj.get("errcode") == 0:
                return EvalResult(_normalize(obj.get("result") or {}, obj))
            raise EvalError(obj.get("errmsg") or f"errcode {obj.get('errcode')}",
                            code=obj.get("errcode"), raw=obj)
        raise EvalError("评测超时")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _normalize(res, env=None):
    """引擎回包 -> 扁平结果；sil / unk 这类占位词剔掉。"""
    env = env or {}
    lines = res.get("lines") or []
    if not lines:
        raise EvalError("评测服务没有返回评分结果", raw=res)
    head = lines[0]
    words = []
    for w in (head.get("words") or []):
        t = (w.get("text") or "").strip()
        if not t or t.lower() in ("sil", "silv", "unk"):
            continue
        words.append({"text": t, "score": w.get("score"),
                      "phonetic": w.get("phonetic") or ""})
    return {
        "lines": lines,
        "precision": res.get("precision"),
        "point_system": res.get("pointSystem"),
        "overall": head.get("score"),
        "fluency": head.get("fluency"),
        "integrity": head.get("integrity"),
        "pronunciation": head.get("pronunciation"),
        "speed": head.get("voiceSpeed"),
        "usertext": (head.get("usertext") or head.get("sample") or "").strip(),
        "words": words,
        "sid": env.get("sid"),
        "area": env.get("area"),
        "time": env.get("time"),
        "raw": env,
    }


# ---------------------------------------------------------------- 音频


def audio_bytes(url, timeout=30):
    """下载题目音频（腾讯云 COS，公开可读，不需要 cookie）。"""
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def pcm_from_file(path, sr_out=16000):
    """任意音频文件 -> 16k/16bit/单声道 PCM（需要 soundfile + numpy）。"""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        raise ImportError("转码需要 soundfile + numpy："
                          "pip install soundfile numpy") from None

    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != sr_out and len(mono) > 1:
        n = int(round(len(mono) * sr_out / sr))
        mono = np.interp(np.linspace(0, len(mono) - 1, n),
                         np.arange(len(mono)), mono)
    peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
    if peak > 0.98:
        mono = mono * (0.98 / peak)
    return (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()


# 兼容旧名字
to_pcm16k = pcm_from_file


# ---------------------------------------------------------------- 取插件登录态

INGEST_PATH = "/ingest"


def _default_token():
    """
    令牌：优先沿用 wing 写在 config.json 里的 serve_token（这样插件里存的那串不用改），
    没有就现生成一串（只在本次进程有效，换进程要重新粘）。
    """
    try:
        with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as f:
            t = (json.load(f) or {}).get("serve_token") or ""
        if t:
            return t
    except (OSError, ValueError):
        pass
    return secrets.token_hex(6)


class CaptureServer:
    """
    本机 127.0.0.1 上的登录态接收端，协议与本项目 ext/ 插件一致：

        POST /ingest   头 X-Wing-Token: <令牌>
                       体 {"cookie","ua","headers","caps","apiUrl", ...}
                       回 {"ok":bool, "detail":str, "user":str}
        GET  /ping     健康检查 {"ok": true}

    只监听回环地址、只认令牌，收到后可选校验会话并写 session.json。
    用法：
        srv = ek.CaptureServer(port=7788).start()
        print(srv.url, srv.token)
        s = srv.wait(120)            # 阻塞到插件点按钮；超时抛 CaptureTimeout
        srv.stop()
    也可以当上下文管理器：with ek.CaptureServer() as srv: s = srv.wait(60)
    """

    def __init__(self, port=7788, token=None, path=INGEST_PATH,
                 session_path=SESSION_PATH, save=True, verify=True,
                 host="127.0.0.1", max_body=200000, timeout=15):
        self.port = int(port)
        self.host = host
        self.token = token or _default_token()
        self.path = path
        self.session_path = session_path
        self.save = save
        self.verify = verify
        self.max_body = int(max_body)
        self.timeout = timeout

        self.sessions = []          # 每次收到的 Session（新的在前）
        self.last = {}              # 最近一次校验结果 {ok, detail, user, caps}
        self._srv = None
        self._thread = None
        self._ready = threading.Event()

    # -- 生命周期 -----------------------------------------------------

    @property
    def url(self):
        return f"http://{self.host}:{self.port}{self.path}"

    @property
    def latest(self):
        """最近一次收到的 Session（还没收到就是 None）。"""
        return self.sessions[0] if self.sessions else None

    def start(self):
        if self._srv is not None:
            return self
        try:
            srv = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        except OSError as e:
            raise EkwingError(
                f"端口 {self.port} 起不来：{e}（换一个端口，或先关掉占用的程序）") from None
        srv.daemon_threads = True
        self._srv = srv
        self._thread = threading.Thread(target=srv.serve_forever,
                                        kwargs={"poll_interval": 0.2},
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """关服务、收线程；重复调用安全。"""
        srv, th = self._srv, self._thread
        self._srv, self._thread = None, None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
        if th is not None and th.is_alive():
            th.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    def wait(self, timeout=None):
        """阻塞等插件送登录态；拿到返回 Session，超时抛 CaptureTimeout。"""
        if self._srv is None:
            self.start()
        if not self.sessions and not self._ready.wait(timeout):
            raise CaptureTimeout(
                f"{timeout} 秒内没收到登录态：检查插件端口/令牌，端点 {self.url}")
        got = self.latest
        if got is None:
            raise CaptureTimeout("没收到登录态")
        return got

    # -- 内部 ---------------------------------------------------------

    def _check(self, sess):
        """校验会话是否有效；返回 (ok, detail, user)。"""
        if not self.verify:
            return True, "", ""
        url = sess.api_url if str(sess.api_url).startswith("http") else \
            f"{ENDPOINTS['exam_list']}?page=1&type=2&mode_type=1"
        try:
            if url == sess.api_url:
                st, body = sess.call(url, timeout=self.timeout)
                if is_gate(body):
                    return False, body[:200], ""
                obj = _parse_json(body)
                if isinstance(obj, dict) and obj.get("errno") not in (0, None):
                    return False, str(obj.get("err_info") or obj.get("errno")), ""
            else:
                obj = sess.get_json(url, timeout=self.timeout)
        except AuthError as e:
            return False, str(e), ""
        except EkwingError as e:
            return False, str(e), ""
        data = obj.get("data") if isinstance(obj, dict) else None
        user = ""
        if isinstance(data, dict):
            for k in ("uname", "name", "nickname", "user_name",
                      "uid", "school", "school_name", "grade"):
                if data.get(k):
                    user += f"{k}: {data[k]}\n"
        if not user:
            # 列表接口不带用户名，退回 cookie 里能找到的那些
            for k, v in _cookie_pairs(sess.cookie):
                if k.lower() in ("uname", "realname", "nickname", "user_name"):
                    user = f"{k}: {unquote(v)}"
                    break
        return True, "", user.strip()

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ekwing_api"
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, X-Wing-Token")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

            def _json(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if self.path.startswith("/ping"):
                    return self._json(200, {"ok": True})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self.path.startswith(outer.path):
                    return self._json(404, {"error": "not found"})
                if self.headers.get("X-Wing-Token") != outer.token:
                    return self._json(403, {"error": "令牌不对"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > outer.max_body:
                    return self._json(400, {"error": "长度不对"})
                try:
                    data = json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return self._json(400, {"error": "body 不是合法 JSON"})
                if not isinstance(data, dict) or "=" not in clean_cookie(
                        data.get("cookie") or ""):
                    return self._json(400, {"error": "没收到 cookie"})

                try:
                    sess = Session.from_ingest(data)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})

                if outer.save:
                    try:
                        sess.save(outer.session_path)
                    except OSError as e:
                        return self._json(500, {"error": f"写 session 失败：{e}"})

                ok, detail, user = outer._check(sess)
                outer.sessions.insert(0, sess)
                outer.last = {"ok": ok, "detail": detail, "user": user,
                              "caps": len(sess.caps), "saved": bool(outer.save)}
                outer._ready.set()
                return self._json(200, {"ok": ok, "detail": detail, "user": user})

        return Handler


def capture_session(port=7788, token=None, timeout=180, save=True, verify=True,
                    session_path=SESSION_PATH, on_start=None):
    """
    一次性便利函数：起服务 → 等插件送登录态 → 关服务 → 返回 Session。

    on_start(server) 在服务起来后立刻回调，用来把 server.url / server.token
    告诉用户（库本身不打印）。
    """
    srv = CaptureServer(port=port, token=token, save=save, verify=verify,
                        session_path=session_path).start()
    try:
        if on_start:
            on_start(srv)
        return srv.wait(timeout)
    finally:
        srv.stop()
