"""PLA Reverse Finder (GUI)
============================
원하는 [조우 타입 / 종류 / IV / 성격 / 성별 / 키·몸무게 / 색이 다른 포켓몬]를 넣으면,
그 개체를 만들어내는 합법 생성 세트(EC / PID / IVs / 특성 / 성격 / 성별 / 키 / 몸무게 / seed)를
여러 개 역산해서 표로 보여준다. PKHeX에 한 행을 통째로 넣어 쓰면 됨.

게임 생성 순서 (FixInitSpec, pla-reverse fixed_seed 로 확인):
    EC → (가짜 TID/SID) → PID(색이 다른 포켓몬 롤) → IV → 특성 → [성별 rand(253)+1]
      → 성격 → 키(rand0x81+rand0x80) → 몸무게(rand0x81+rand0x80)

  * 성별: 무성/고정성별 종은 성별을 안 굴림. 종류를 입력하면 내장 성별표로
          굴림 여부·비율을 자동 판정하고, '성별' 필터(무관/수컷/암컷)로 거른다.
  * 키/몸무게: 조우 타입과 무관하게 항상 seed로 굴려짐(스칼라 0~255). 범위 검색 가능.
  * 색이 다른 포켓몬 + TID/SID: PLA 실제 규칙대로 PID를 확정 (pla-pid-iv 공식과 일치).

의존성 없음(파이썬 표준 라이브러리만). 종/한국어명 데이터는 같은 폴더의
pla_gender_data.py / pla_names_ko.py, 드롭다운 목록은 text_species.txt 사용(없어도 동작).
CLI 자체검증:  python pla_reverse_finder.py --selftest
"""
import os
import sys
import random
import threading
import queue
import argparse
from itertools import combinations


# ---------------------------------------------------------------------------
# Xoroshiro128+ (게임 RNG) + 개체 생성 로직  (main.py / xoroshiro.py와 동일)
# ---------------------------------------------------------------------------
class XOROSHIRO:
    ulongmask = 2 ** 64 - 1
    uintmask = 2 ** 32 - 1

    def __init__(self, seed0, seed1=0x82A2B175229D6A5B):
        self.seed = [seed0, seed1]

    @staticmethod
    def rotl(number, k):
        return ((number << k) | (number >> (64 - k))) & XOROSHIRO.ulongmask

    def next(self):
        seed0, seed1 = self.seed
        result = (seed0 + seed1) & XOROSHIRO.ulongmask
        seed1 ^= seed0
        self.seed = [XOROSHIRO.rotl(seed0, 24) ^ seed1 ^ ((seed1 << 16) & XOROSHIRO.ulongmask),
                     XOROSHIRO.rotl(seed1, 37)]
        return result

    @staticmethod
    def get_mask(maximum):
        maximum -= 1
        for i in range(6):
            maximum |= maximum >> (1 << i)
        return maximum

    def rand(self, maximum=uintmask):
        mask = XOROSHIRO.get_mask(maximum)
        res = self.next() & mask
        while res >= maximum:
            res = self.next() & mask
        return res


# size 모드 상수
SIZE_WILD = "wild"      # 성격 다음에 키/몸무게 롤
SIZE_STATIC = "static"  # 127 고정
SIZE_ALPHA = "alpha"    # 255 고정


def generate(seed, rolls, guaranteed_ivs, roll_gender, size_mode):
    """고정 seed로부터 개체 정보 생성 (main.py::generate_from_seed 확장).

    roll_gender : True면 성별 1롤 소비, False면 무성/고정이라 성별 롤 없음.
    size_mode   : SIZE_WILD / SIZE_STATIC / SIZE_ALPHA
    반환: ec, pid, ivs, ability, gender(None=롤없음), nature, shiny, height, weight
    """
    rng = XOROSHIRO(seed)
    ec = rng.rand(0xFFFFFFFF)
    sidtid = rng.rand(0xFFFFFFFF)
    pid = 0
    shiny = False
    for _ in range(rolls):
        pid = rng.rand(0xFFFFFFFF)
        shiny = ((pid >> 16) ^ (sidtid >> 16) ^ (pid & 0xFFFF) ^ (sidtid & 0xFFFF)) < 0x10
        if shiny:
            break
    ivs = [-1] * 6
    for _ in range(guaranteed_ivs):
        idx = rng.rand(6)
        while ivs[idx] != -1:
            idx = rng.rand(6)
        ivs[idx] = 31
    for i in range(6):
        if ivs[i] == -1:
            ivs[i] = rng.rand(32)
    ability = rng.rand(2)
    if roll_gender:
        gender = rng.rand(253) + 1          # 게임 실제: rand(253)+1 (pla-reverse 확인)
    else:
        gender = None
    nature = rng.rand(25)
    # 키/몸무게: 모든 고정 생성에서 nature 다음에 굴려진다 (야생·전설·알파 동일)
    height = rng.rand(0x81) + rng.rand(0x80)
    weight = rng.rand(0x81) + rng.rand(0x80)
    return ec, pid, ivs, ability, gender, nature, shiny, height, weight, sidtid


# index -> (English, 한글)
NATURES = [
    ("Hardy", "노력"), ("Lonely", "외로움"), ("Brave", "용감"), ("Adamant", "고집"),
    ("Naughty", "개구쟁이"), ("Bold", "대담"), ("Docile", "온순"), ("Relaxed", "무사태평"),
    ("Impish", "장난꾸러기"), ("Lax", "촐랑"), ("Timid", "겁쟁이"), ("Hasty", "성급"),
    ("Serious", "성실"), ("Jolly", "명랑"), ("Naive", "천진난만"), ("Modest", "조심"),
    ("Mild", "의젓"), ("Quiet", "냉정"), ("Bashful", "수줍음"), ("Rash", "덜렁"),
    ("Calm", "차분"), ("Gentle", "얌전"), ("Sassy", "건방"), ("Careful", "신중"),
    ("Quirky", "변덕"),
]
NATURE_LABELS = ["(무관)"] + [f"{kr} ({en})" for en, kr in NATURES]
IV_NAMES = ["HP", "공격", "방어", "특공", "특방", "스피드"]

# 조우 타입 -> (보장IV 기본, 사이즈모드)
ENCOUNTERS = [
    ("야생",        0, SIZE_WILD),
    ("전설/고정 인카운터", 3, SIZE_STATIC),
    ("우두머리",             3, SIZE_ALPHA),
]

# 성별 필터 (원하는 성별 지정)
GENDER_FILTERS = ["무관", "수컷", "암컷"]

# 종 성별코드(암컷 1/8 확률) -> 게임 성별비율 바이트 (female if rand(253)+1 < byte)
#   표준값: 1/8=31, 2/8=63, 4/8=127, 6/8=191, 7/8=225 (3/8·5/8은 실제 종에 거의 없음)
RATIO_BYTE = {1: 31, 2: 63, 3: 95, 4: 127, 5: 159, 6: 191, 7: 225}

# 종별 성별 코드 (PokeAPI 기반 자동 생성 표): -1=무성, 0=수컷만, 8=암컷만, 1~7=암컷 1/8확률
try:
    from pla_gender_data import GENDER_CODE
except Exception:
    GENDER_CODE = {}

# 대소문자 무시 조회용 (arceus / Arceus / ARCEUS 모두 인식)
GENDER_CODE_LC = {k.lower(): v for k, v in GENDER_CODE.items()}

# 한국어 종명 -> 영어 canonical (PokeAPI 기반 자동 생성 표)
try:
    from pla_names_ko import KO_TO_EN
except Exception:
    KO_TO_EN = {}
EN_TO_KO = {en: ko for ko, en in KO_TO_EN.items()}
KO_TO_CODE = {ko: GENDER_CODE[en] for ko, en in KO_TO_EN.items() if en in GENDER_CODE}


def lookup_gender_code(name):
    """종명으로 성별코드 조회 (영어 대소문자·한국어·앞뒤공백 무시). 없으면 None."""
    if not name:
        return None
    n = name.strip()
    if n in GENDER_CODE:          # 영어 정식 표기
        return GENDER_CODE[n]
    if n in KO_TO_CODE:           # 한국어
        return KO_TO_CODE[n]
    return GENDER_CODE_LC.get(n.lower())  # 영어 소문자 등


def species_rolls_gender(code):
    """이 종이 성별을 굴리는가(=성별 롤 1회 소비). 무성/고정성별은 안 굴림.
    종을 모르면(None) 일반적인 성별 있는 종으로 가정."""
    if code is None:
        return True
    return 1 <= code <= 7


def compute_gender(code, gender_roll):
    """종 성별코드 + 성별 롤 값(rand(253)+1) -> '암' / '수' / '무성'."""
    if code == -1:
        return "무성"
    if code == 0:
        return "수"
    if code == 8:
        return "암"
    ratio_byte = RATIO_BYTE.get(code, 127)      # 종 모르면 1:1 가정
    if gender_roll is None:
        return "?"
    return "암" if gender_roll < ratio_byte else "수"


def code_gender_desc(code):
    """종 성별코드 -> 사람이 읽는 설명."""
    return {-1: "무성", 0: "수컷만", 8: "암컷만",
            1: "수컷 87.5%", 2: "수컷 75%", 3: "수컷 62.5%",
            4: "암수 1:1", 5: "암컷 62.5%", 6: "암컷 75%", 7: "암컷 87.5%"}.get(code, "?")


def g7_to_id16(g7tid, g7sid):
    """스위치 표시형식(TID 6자리 + SID 4자리) -> 내부 16비트 (tid16, sid16).
    full32 = g7sid*1,000,000 + g7tid ; tid16=하위16, sid16=상위16."""
    full = (g7sid * 1_000_000 + g7tid) & 0xFFFFFFFF
    return full & 0xFFFF, (full >> 16) & 0xFFFF


def is_real_shiny(pid, tid, sid):
    """진짜 TID/SID(16비트) 기준으로 이 PID가 색이 다른 포켓몬인지 (게임/PKHeX 표시 기준)."""
    return ((tid ^ sid ^ (pid >> 16) ^ (pid & 0xFFFF)) < 0x10)


def force_pla_shiny_pid(raw_pid, sidtid, tid, sid):
    """PLA 실제 색이 다른 포켓몬 PID 규칙 (Lincoln-LM pla-pid-iv/main.py verify_all_seeds 그대로).

      xor        = (temp&0xFFFF) ^ (temp>>16),  temp = raw_pid ^ sidtid   (가짜TID 기준 색이 다른 포켓몬 xor)
      real_shiny = (raw_low ^ raw_high ^ 내TSV) < 16
      - 원본이 이미 내 트레이너 색이 다른 포켓몬면 → 원본 그대로.
      - 아니면 → 상위16 = 하위16 ^ 내TSV ^ (xor>0 ? 1 : 0)   (사각=0 / 별=1), 하위16 보존.
    → 엄격한 체커(pla-pid-iv)까지 통과하는 합법 색이 다른 포켓몬 PID."""
    tsv = (tid ^ sid) & 0xFFFF
    temp = raw_pid ^ sidtid
    xor = ((temp & 0xFFFF) ^ (temp >> 16)) & 0xFFFF
    if ((raw_pid & 0xFFFF) ^ (raw_pid >> 16) ^ tsv) < 0x10:   # 이미 내 기준 색이 다른 포켓몬
        return raw_pid & 0xFFFFFFFF
    low = raw_pid & 0xFFFF
    high = (low ^ tsv ^ (1 if xor > 0 else 0)) & 0xFFFF
    return (high << 16) | low


def real_shiny_type(pid, tid, sid):
    """진짜 TID/SID 기준 색이 다른 포켓몬 종류: ■=사각 / ★=별 / X=비샤. TID/SID 없으면 None."""
    if tid is None or sid is None:
        return None
    x = tid ^ sid ^ (pid >> 16) ^ (pid & 0xFFFF)
    if x == 0:
        return "■"
    if x < 0x10:
        return "★"
    return "X"


def load_species():
    """text_species.txt 가 옆이나 static/resources 에 있으면 종류 목록 로드 (없으면 빈 목록)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for path in (
        os.path.join(base, "text_species.txt"),
        os.path.join(base, "static", "resources", "text_species.txt"),
        os.path.join(os.getcwd(), "static", "resources", "text_species.txt"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                return [ln.strip() for ln in f if ln.strip()]
        except OSError:
            continue
    return []


# ---------------------------------------------------------------------------
# 검색 코어 (GUI/CLI 공용)
# ---------------------------------------------------------------------------
def estimate_attempts(want_ivs, want_nat, want_slot, giv, rolls, shiny,
                      size_mode, want_h, want_w):
    """이 조건 1개를 찾는 데 평균 몇 번 시도해야 하는지 추정.
    불가능하면 inf. (브루트포스 난이도 경고용)

    IV 부분은 정확 계산: 보장 IV는 6칸 중 giv칸을 무작위(중복 없이) 31로 채우고
    나머지는 rand(32). 지정 IV 범위가 만족될 확률을 모든 보장-조합에 대해 평균낸다.
    want_ivs 각 칸: None(무관) 또는 (lo, hi) 범위(lo/hi None이면 한쪽 무제한)."""
    def norm(r):
        lo, hi = r
        return (0 if lo is None else lo), (31 if hi is None else hi)
    specified = [(i, norm(r)) for i, r in enumerate(want_ivs) if r is not None]
    subsets = list(combinations(range(6), giv)) if 0 <= giv <= 6 else []
    if not subsets:
        p_iv = 0.0
    else:
        total = 0.0
        for guaranteed in subsets:
            gset = set(guaranteed)
            f = 1.0
            for i, (lo, hi) in specified:
                if i in gset:                          # 보장칸은 항상 31
                    f *= 1.0 if lo <= 31 <= hi else 0.0
                else:                                  # 랜덤칸: 범위 폭 / 32
                    width = min(hi, 31) - max(lo, 0) + 1
                    f *= (width / 32) if width > 0 else 0.0
            total += f
        p_iv = total / len(subsets)

    p = p_iv
    if want_nat is not None:
        p *= len(want_nat) / 25     # 허용 성격 개수 / 25
    if want_slot is not None:
        p *= 1 / 2
    if shiny:
        p *= 1 - (1 - 1 / 4096) ** max(1, rolls)   # 생성 색이 다른 포켓몬(내 TSV로는 확정 조작)

    def size_frac(rng_tuple):
        if rng_tuple is None:
            return 1.0
        lo, hi = rng_tuple
        lo = 0 if lo is None else lo
        hi = 255 if hi is None else hi
        return max(1, hi - lo + 1) / 256    # 근사(실제 분포는 삼각형이지만 경고용)
    p *= size_frac(want_h)
    p *= size_frac(want_w)
    return (1 / p) if p > 0 else float("inf")


def _check_seed(seed, roll_gender, gender_filter, gender_code, want_ivs, want_nat,
                want_slot, giv, rolls, shiny, size_mode, want_h, want_w,
                tid, sid, use_real):
    """한 seed를 정확 생성해 모든 조건을 검사. 통과하면 결과 튜플, 아니면 None.

    성별: roll_gender(종이 성별 굴리는지) 로 생성, gender_filter(무관/수컷/암컷) 로 필터.
    색이 다른 포켓몬: 필터는 '생성 색이 다른 포켓몬', 출력 PID는 PLA 방식으로 내 TSV 색이 다른 포켓몬로 확정(하위16 보존)."""
    ec, pid, ivs, ability, gender_roll, nature, sh, height, weight, sidtid = \
        generate(seed, rolls, giv, roll_gender, size_mode)
    if sh != shiny:
        return None
    if want_nat is not None and nature not in want_nat:   # want_nat: 허용 성격 집합
        return None
    if want_slot is not None and ability != want_slot:
        return None
    for r, v in zip(want_ivs, ivs):
        if r is None:
            continue
        lo, hi = r
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return None
    if want_h is not None:
        lo, hi = want_h
        if (lo is not None and height < lo) or (hi is not None and height > hi):
            return None
    if want_w is not None:
        lo, hi = want_w
        if (lo is not None and weight < lo) or (hi is not None and weight > hi):
            return None
    if gender_filter != "무관":
        glabel = compute_gender(gender_code, gender_roll)
        if gender_filter == "수컷" and glabel != "수":
            return None
        if gender_filter == "암컷" and glabel != "암":
            return None
    out_pid = pid
    if use_real:
        if shiny:
            out_pid = force_pla_shiny_pid(pid, sidtid, tid, sid)   # 내 TSV 기준 색이 다른 포켓몬로 확정
        elif is_real_shiny(pid, tid, sid):
            return None                                            # 비샤인데 내 기준 색이 다른 포켓몬면 제외
    return (seed, roll_gender, ec, out_pid, ivs, ability, gender_roll,
            nature, height, weight, sh)


def iter_matches(want_ivs, want_nat, want_slot, giv, rolls, shiny,
                 size_mode, want_h, want_w, roll_gender, gender_filter, gender_code,
                 max_attempts, stop_check=None, progress=None, rng_seed=None,
                 tid=None, sid=None):
    """조건에 맞는 결과를 (seed, roll_gender, ec, pid, ivs, ability, gender_roll,
       nature, height, weight, gen_shiny) 튜플로 하나씩 yield.
    성별은 종 기준으로 굴리고 gender_filter(무관/수컷/암컷)로 거른다.
    색이 다른 포켓몬+TID/SID면 PID를 PLA 방식으로 내 TSV 색이 다른 포켓몬로 확정(하위16 보존)."""
    rng = random.Random(rng_seed)
    use_real = tid is not None and sid is not None
    for attempt in range(max_attempts):
        if stop_check is not None and (attempt & 0x3FFFF) == 0 and stop_check():
            return
        if progress is not None and attempt % 200000 == 0:
            progress(attempt)
        seed = rng.getrandbits(64)
        row = _check_seed(seed, roll_gender, gender_filter, gender_code, want_ivs,
                          want_nat, want_slot, giv, rolls, shiny, size_mode,
                          want_h, want_w, tid, sid, use_real)
        if row is not None:
            yield row


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    SPECIES = load_species()
    KO_SPECIES = [EN_TO_KO[s] for s in SPECIES if s in EN_TO_KO]  # 도감순 한국어명
    SPECIES_VALUES = KO_SPECIES + SPECIES                          # 한국어 먼저, 영어도 선택 가능
    SPECIES_CANON = {s.lower(): s for s in SPECIES}                # 영어 소문자 -> 정식표기
    for ko in KO_SPECIES:
        SPECIES_CANON[ko] = ko                                     # 한국어는 그대로 유지

    class App:
        def __init__(self, root):
            self.root = root
            root.title("PLA Reverse Finder — 합법 세트 역산")
            root.geometry("880x620")
            self.q = queue.Queue()
            self.worker = None
            self.stop_flag = threading.Event()
            pad = dict(padx=6, pady=3)

            frm = ttk.LabelFrame(root, text="찾을 조건")
            frm.pack(fill="x", padx=10, pady=8)

            # row 0: 조우 타입 / 종류
            ttk.Label(frm, text="조우 타입").grid(row=0, column=0, sticky="e", **pad)
            self.enc_var = tk.StringVar(value=ENCOUNTERS[1][0])  # 전설/정적 기본
            enc_cb = ttk.Combobox(frm, textvariable=self.enc_var,
                                  values=[e[0] for e in ENCOUNTERS],
                                  state="readonly", width=22)
            enc_cb.grid(row=0, column=1, columnspan=2, sticky="w", **pad)
            enc_cb.bind("<<ComboboxSelected>>", lambda e: self.on_encounter())

            ttk.Label(frm, text="종류(선택)").grid(row=0, column=3, sticky="e", **pad)
            self.species_var = tk.StringVar(value="")
            sp = ttk.Combobox(frm, textvariable=self.species_var, values=SPECIES_VALUES, width=18)
            sp.grid(row=0, column=4, columnspan=3, sticky="w", **pad)
            if not SPECIES:
                sp.configure(state="disabled")
            sp.bind("<<ComboboxSelected>>", lambda e: self.on_species())
            sp.bind("<Return>", lambda e: self.on_species())
            sp.bind("<FocusOut>", lambda e: self.on_species())

            # rows 1-3: 개체값 범위 (각 스탯 최소~최대, 빈칸=제한없음)
            ttk.Button(frm, text="6V", width=4, command=self.set_6v).grid(row=1, column=0, **pad)
            for i, name in enumerate(IV_NAMES):
                ttk.Label(frm, text=name).grid(row=1, column=i + 1, **pad)
            ttk.Label(frm, text="최소").grid(row=2, column=0, sticky="e", **pad)
            ttk.Label(frm, text="최대").grid(row=3, column=0, sticky="e", **pad)
            self.iv_min_vars, self.iv_max_vars = [], []
            for i, name in enumerate(IV_NAMES):
                vmin = tk.StringVar(value="")          # 최소 기본 빈칸
                ttk.Entry(frm, width=4, textvariable=vmin, justify="center").grid(
                    row=2, column=i + 1, **pad)
                self.iv_min_vars.append(vmin)
                vmax = tk.StringVar(value="31")        # 최대 기본 31
                ttk.Entry(frm, width=4, textvariable=vmax, justify="center").grid(
                    row=3, column=i + 1, **pad)
                self.iv_max_vars.append(vmax)

            # row 4: 성격 / 특성 슬롯 / 성별(원하는 성별 지정)
            ttk.Label(frm, text="성격").grid(row=4, column=0, sticky="e", **pad)
            self.nature_sel = {15}   # 선택된 성격 index 집합(빈 set=무관). 기본 조심(Modest)
            self.nature_btn = ttk.Button(frm, width=16, command=self.open_nature_dialog)
            self.nature_btn.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
            self.update_nature_label()
            ttk.Label(frm, text="특성 슬롯").grid(row=4, column=3, sticky="e", **pad)
            self.slot_var = tk.StringVar(value="무관")
            ttk.Combobox(frm, textvariable=self.slot_var, values=["무관", "0", "1"],
                         state="readonly", width=6).grid(row=4, column=4, sticky="w", **pad)
            ttk.Label(frm, text="성별").grid(row=4, column=5, sticky="e", **pad)
            self.gender_var = tk.StringVar(value=GENDER_FILTERS[0])
            ttk.Combobox(frm, textvariable=self.gender_var, values=GENDER_FILTERS,
                         state="readonly", width=8).grid(row=4, column=6, sticky="w", **pad)

            # row 5: 색이 다른 포켓몬 롤 / 개수 / 색이 다른 포켓몬
            # 보장 31 IV는 조우 타입으로 자동 결정(전설·우두머리=3, 야생=0) → UI 없이 내부 처리
            self.giv_var = tk.IntVar(value=3)
            ttk.Label(frm, text="색이 다른 포켓몬 롤").grid(row=5, column=0, sticky="e", **pad)
            self.rolls_var = tk.IntVar(value=1)
            ttk.Spinbox(frm, from_=1, to=99, textvariable=self.rolls_var, width=5).grid(
                row=5, column=1, sticky="w", **pad)
            ttk.Label(frm, text="개수").grid(row=5, column=2, sticky="e", **pad)
            self.count_var = tk.IntVar(value=20)
            ttk.Spinbox(frm, from_=1, to=300, textvariable=self.count_var, width=5).grid(
                row=5, column=3, sticky="w", **pad)
            self.shiny_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(frm, text="색이 다른 포켓몬만", variable=self.shiny_var).grid(
                row=5, column=4, columnspan=2, sticky="w", **pad)

            # row 6: 키 / 몸무게 범위 (한 줄, min~max 붙여서)
            ttk.Label(frm, text="키(0~255)").grid(row=6, column=0, sticky="e", **pad)
            kf = ttk.Frame(frm)
            kf.grid(row=6, column=1, columnspan=2, sticky="w", **pad)
            self.hmin_var = tk.StringVar(value="")
            ttk.Entry(kf, width=4, textvariable=self.hmin_var, justify="center").pack(side="left")
            ttk.Label(kf, text="~").pack(side="left", padx=2)
            self.hmax_var = tk.StringVar(value="")
            ttk.Entry(kf, width=4, textvariable=self.hmax_var, justify="center").pack(side="left")

            ttk.Label(frm, text="몸무게(0~255)").grid(row=6, column=3, sticky="e", **pad)
            wf = ttk.Frame(frm)
            wf.grid(row=6, column=4, columnspan=2, sticky="w", **pad)
            self.wmin_var = tk.StringVar(value="")
            ttk.Entry(wf, width=4, textvariable=self.wmin_var, justify="center").pack(side="left")
            ttk.Label(wf, text="~").pack(side="left", padx=2)
            self.wmax_var = tk.StringVar(value="")
            ttk.Entry(wf, width=4, textvariable=self.wmax_var, justify="center").pack(side="left")

            # row 7: 진짜 TID(6자리) / SID(4자리)
            ttk.Label(frm, text="TID(6자리)").grid(row=7, column=0, sticky="e", **pad)
            self.tid_var = tk.StringVar(value="")
            ttk.Entry(frm, width=8, textvariable=self.tid_var, justify="center").grid(
                row=7, column=1, sticky="w", **pad)
            ttk.Label(frm, text="SID(4자리)").grid(row=7, column=2, sticky="e", **pad)
            self.sid_var = tk.StringVar(value="")
            ttk.Entry(frm, width=8, textvariable=self.sid_var, justify="center").grid(
                row=7, column=3, sticky="w", **pad)

            # 버튼
            btns = ttk.Frame(root)
            btns.pack(fill="x", padx=10)
            self.find_btn = ttk.Button(btns, text="찾기", command=self.start_search)
            self.find_btn.pack(side="left", padx=4)
            self.stop_btn = ttk.Button(btns, text="중지", command=self.stop_search, state="disabled")
            self.stop_btn.pack(side="left", padx=4)
            self.status = tk.StringVar(value="준비됨")
            ttk.Label(btns, textvariable=self.status).pack(side="left", padx=12)

            # 결과 테이블
            cols = ("#", "EC", "PID", "IVs", "특성", "성격", "성별", "색이 다른 포켓몬",
                    "키", "몸무게", "seed")
            widths = (30, 76, 76, 190, 44, 100, 44, 108, 42, 52, 132)
            self.tree = ttk.Treeview(root, columns=cols, show="headings", height=12)
            for c, w in zip(cols, widths):
                self.tree.heading(c, text=c)
                self.tree.column(c, width=w, anchor="center")
            self.tree.pack(fill="both", expand=True, padx=10, pady=8)

            # 복사 버튼
            cp = ttk.Frame(root)
            cp.pack(fill="x", padx=10, pady=(0, 6))
            ttk.Button(cp, text="EC 복사", command=lambda: self.copy_col(1)).pack(side="left", padx=3)
            ttk.Button(cp, text="PID 복사", command=lambda: self.copy_col(2)).pack(side="left", padx=3)
            ttk.Button(cp, text="IV 복사", command=lambda: self.copy_col(3)).pack(side="left", padx=3)
            ttk.Label(cp, text="※ 한 행의 EC·PID·IV·성격을 통째로 PKHeX에 · 사이즈는 Auto 체크 권장").pack(
                side="left", padx=10)

            self.on_encounter()  # 초기 상태 반영

        # -- helpers --
        def set_6v(self):
            for vmin, vmax in zip(self.iv_min_vars, self.iv_max_vars):
                vmin.set("31")
                vmax.set("31")

        def update_nature_label(self):
            s = sorted(self.nature_sel)
            if not s:
                txt = "무관 (전체)"
            elif len(s) <= 2:
                txt = ", ".join(NATURES[i][1] for i in s)
            else:
                txt = f"{len(s)}개 선택"
            self.nature_btn.config(text=txt)

        def open_nature_dialog(self):
            top = tk.Toplevel(self.root)
            top.title("성격 선택 (여러 개 가능)")
            top.transient(self.root)
            top.grab_set()
            cbvars = []
            for i, (en, kr) in enumerate(NATURES):
                v = tk.BooleanVar(value=(i in self.nature_sel))
                ttk.Checkbutton(top, text=f"{kr} ({en})", variable=v).grid(
                    row=i // 5, column=i % 5, sticky="w", padx=6, pady=2)
                cbvars.append(v)

            def set_all(val):
                for v in cbvars:
                    v.set(val)
            bar = ttk.Frame(top)
            bar.grid(row=5, column=0, columnspan=5, sticky="we", pady=(8, 4))
            ttk.Button(bar, text="전체 선택", command=lambda: set_all(True)).pack(side="left", padx=4)
            ttk.Button(bar, text="전체 해제", command=lambda: set_all(False)).pack(side="left", padx=4)

            def confirm():
                self.nature_sel = {i for i, v in enumerate(cbvars) if v.get()}
                self.update_nature_label()
                top.destroy()
            ttk.Button(bar, text="확인", command=confirm).pack(side="right", padx=4)
            top.bind("<Return>", lambda e: confirm())

        def current_encounter(self):
            for name, giv, size_mode in ENCOUNTERS:
                if name == self.enc_var.get():
                    return giv, size_mode
            return 3, SIZE_STATIC

        def on_species(self):
            typed = self.species_var.get().strip()
            canon = SPECIES_CANON.get(typed) or SPECIES_CANON.get(typed.lower())
            if canon and canon != typed:      # 영어 소문자 → 정식표기 (한국어는 그대로)
                self.species_var.set(canon)
                typed = canon
            code = lookup_gender_code(typed)
            if code is None:
                return
            # 무성/고정성별 종은 '성별' 필터가 의미 없으니 무관으로
            if code in (-1, 0, 8):
                self.gender_var.set("무관")
            self.status.set(f"'{typed}' 성별: {code_gender_desc(code)}")

        def on_encounter(self):
            giv, size_mode = self.current_encounter()
            self.giv_var.set(giv)             # 보장 31 IV: 조우 타입에 따라 자동 적용

        def copy_col(self, col_index):
            sel = self.tree.selection()
            if not sel:
                messagebox.showinfo("복사", "행을 먼저 선택해줘.")
                return
            val = self.tree.item(sel[0], "values")[col_index]
            self.root.clipboard_clear()
            self.root.clipboard_append(val)
            self.status.set(f"복사됨: {val}")

        def parse_conditions(self):
            def parse_iv(s):
                s = s.strip().lower()
                if s in ("", "x", "*", "?"):
                    return None
                iv = int(s)
                if not 0 <= iv <= 31:
                    raise ValueError(f"개체값은 0~31: {s}")
                return iv
            want_ivs = []
            for vmin, vmax, name in zip(self.iv_min_vars, self.iv_max_vars, IV_NAMES):
                lo, hi = parse_iv(vmin.get()), parse_iv(vmax.get())
                if lo is None and hi is None:
                    want_ivs.append(None)
                else:
                    if lo is not None and hi is not None and lo > hi:
                        raise ValueError(f"{name} 개체값 범위가 거꾸로 (최소 > 최대)")
                    want_ivs.append((lo, hi))
            want_nat = set(self.nature_sel) if self.nature_sel else None  # 성격 집합(None=무관)
            slot = self.slot_var.get()
            want_slot = None if slot == "무관" else int(slot)

            def parse_size(s):
                s = s.strip().lower()
                if s in ("", "x", "*", "?"):
                    return None
                val = int(s)
                if not 0 <= val <= 255:
                    raise ValueError("키/몸무게는 0~255")
                return val

            def parse_range(lo_s, hi_s, name):
                lo, hi = parse_size(lo_s), parse_size(hi_s)
                if lo is None and hi is None:
                    return None
                if lo is not None and hi is not None and lo > hi:
                    raise ValueError(f"{name} 범위가 거꾸로 (min > max)")
                return (lo, hi)
            want_h = parse_range(self.hmin_var.get(), self.hmax_var.get(), "키")
            want_w = parse_range(self.wmin_var.get(), self.wmax_var.get(), "몸무게")

            def parse_id(s, name, maxv):
                s = s.strip()
                if s in ("", "x", "*", "?"):
                    return None
                v = int(s, 16) if s.lower().startswith("0x") else int(s)
                if not 0 <= v <= maxv:
                    raise ValueError(f"{name}는 0~{maxv}")
                return v

            # 스위치 표시형식(TID 6자리 / SID 4자리) → 내부 16비트 변환
            g7tid = parse_id(self.tid_var.get(), "TID(6자리)", 999999)
            g7sid = parse_id(self.sid_var.get(), "SID(4자리)", 4294)
            if (g7tid is None) != (g7sid is None):
                raise ValueError("TID와 SID는 둘 다 입력하거나 둘 다 비워야 함")
            tid, sid = (None, None) if g7tid is None else g7_to_id16(g7tid, g7sid)
            return want_ivs, want_nat, want_slot, want_h, want_w, tid, sid

        # -- search --
        def start_search(self):
            try:
                want_ivs, want_nat, want_slot, want_h, want_w, tid, sid = \
                    self.parse_conditions()
            except ValueError as e:
                messagebox.showerror("입력 오류", str(e))
                return
            for i in self.tree.get_children():
                self.tree.delete(i)
            giv_conf, size_mode = self.current_encounter()
            gender_code = lookup_gender_code(self.species_var.get())
            roll_gender = species_rolls_gender(gender_code)
            gender_filter = self.gender_var.get()

            # 색이 다른 포켓몬를 원하는데 TID/SID가 없으면 내 트레이너 기준 색이 다른 포켓몬를 보장 못 함
            if self.shiny_var.get() and tid is None:
                if not messagebox.askyesno(
                        "TID/SID 없이 색이 다른 포켓몬?",
                        "TID/SID를 넣어야 '내 게임에서 실제로 반짝이는' 합법 색이 다른 포켓몬를 찾습니다.\n"
                        "없이 진행하면 생성 색이 다른 포켓몬만 맞춘 PID라, 당신 게임/PKHeX에서 색이 다른 포켓몬로\n"
                        "안 보이거나 'PID 유형: None'이 될 수 있습니다.\n\n"
                        "그래도 계속할까요? (권장: TID/SID 입력 후 다시)"):
                    return

            giv_cur = self.giv_var.get()
            est = estimate_attempts(want_ivs, want_nat, want_slot, giv_cur,
                                    self.rolls_var.get(), self.shiny_var.get(),
                                    size_mode, want_h, want_w)
            # 성별 필터 난이도 반영
            if gender_filter != "무관":
                if roll_gender:
                    fem = 0.5 if gender_code is None else \
                        (RATIO_BYTE.get(gender_code, 127) - 1) / 253
                    frac = fem if gender_filter == "암컷" else (1 - fem)
                    est = (est / frac) if frac > 0 else float("inf")
                else:  # 고정 성별: 원하는 성별과 다르면 불가능
                    want = "암" if gender_filter == "암컷" else "수"
                    if compute_gender(gender_code, None) != want:
                        est = float("inf")

            if est == float("inf"):
                messagebox.showerror(
                    "불가능한 조건",
                    "이 조건을 만족하는 개체가 존재할 수 없습니다.\n"
                    "(예: 무성/고정성별 종에 성별을 지정, 또는 보장 IV로 6칸 전부\n"
                    " 31인데 특정 스탯에 31이 아닌 값을 지정)\n\n"
                    "성별·IV 조건을 확인해 주세요.")
                return
            if est > 30_000_000:
                extra = ("\n\n야생은 '보장 31 IV = 0'이라 원하는 IV가 많을수록 확률이 급격히 낮아집니다."
                         if size_mode == SIZE_WILD else "")
                if not messagebox.askyesno(
                        "조건이 매우 드묾",
                        f"이 조건은 평균 약 {est:,.0f} 회 시도해야 1개가 나옵니다.{extra}\n\n"
                        "권장: IV 일부를 x(아무값)로 바꾸거나, 보장 IV가 있는\n"
                        "전설/우두머리 조우 타입을 사용하세요.\n\n"
                        "그래도 계속 찾을까요? (오래 걸리거나 못 찾을 수 있음)"):
                    self.find_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    return

            self.gender_code = gender_code
            self.cur_tid, self.cur_sid = tid, sid
            self.stop_flag.clear()
            self.find_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.status.set(f"탐색 중... (예상 1개당 ~{est:,.0f}회)")
            args = (want_ivs, want_nat, want_slot, self.giv_var.get(), self.rolls_var.get(),
                    self.shiny_var.get(), size_mode, want_h, want_w,
                    roll_gender, gender_filter, gender_code, self.count_var.get(), tid, sid)
            self.worker = threading.Thread(target=self._search, args=args, daemon=True)
            self.worker.start()
            self.root.after(100, self._poll)

        def stop_search(self):
            self.stop_flag.set()

        def _search(self, want_ivs, want_nat, want_slot, giv, rolls, shiny,
                    size_mode, want_h, want_w, roll_gender, gender_filter,
                    gender_code, count, tid, sid):
            LIMIT = 2_000_000_000
            found = 0
            gen = iter_matches(
                want_ivs, want_nat, want_slot, giv, rolls, shiny, size_mode,
                want_h, want_w, roll_gender, gender_filter, gender_code, LIMIT,
                stop_check=self.stop_flag.is_set, tid=tid, sid=sid,
                progress=lambda s: self.q.put(("progress", f"탐색 중... {s // 1000}k 확인, {found}개")))
            for row in gen:
                found += 1
                self.q.put(("row", (found,) + row))
                if found >= count:
                    self.q.put(("done", f"완료 — {found}개 발견"))
                    return
            if self.stop_flag.is_set():
                self.q.put(("done", f"중지됨 — {found}개"))
            elif found == 0:
                self.q.put(("done", "0개 — 조건이 너무 드묾. IV를 x로 완화하거나 "
                                    "전설/우두머리(보장 IV)를 쓰세요."))
            else:
                self.q.put(("done", f"탐색 한계 도달 — {found}개"))

        def _poll(self):
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == "progress":
                        self.status.set(payload)
                    elif kind == "row":
                        (n, seed, rg, ec, pid, ivs, ability,
                         gender_roll, nature, height, weight, sh) = payload
                        if self.cur_tid is not None and self.cur_sid is not None:
                            shiny_cell = real_shiny_type(pid, self.cur_tid, self.cur_sid)
                        else:
                            shiny_cell = "생성Y" if sh else "생성N"
                        self.tree.insert("", "end", values=(
                            n, f"{ec:08X}", f"{pid:08X}", "/".join(str(v) for v in ivs),
                            f"슬롯{ability}", f"{NATURES[nature][1]}({NATURES[nature][0]})",
                            compute_gender(self.gender_code, gender_roll),
                            shiny_cell, height, weight, f"{seed:016X}"))
                    elif kind == "done":
                        self.status.set(payload)
                        self.find_btn.config(state="normal")
                        self.stop_btn.config(state="disabled")
                        return
            except queue.Empty:
                pass
            self.root.after(100, self._poll)

    root = tk.Tk()
    App(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI 자체검증 (GUI 없이 로직 확인)
# ---------------------------------------------------------------------------
def selftest():
    print("[selftest] 생성 로직 / 무작위 탐색 검증")
    # 1) 무작위 탐색: 반환된 결과가 모두 (a)필터를 만족하고 (b)seed 재생성과 일치하는지,
    #    그리고 (c)seed 들이 초기값에 몰리지 않고 크게 흩어지는지
    want_nat, want_slot = {15}, None  # 조심(Modest) — 성격은 집합(여러 개 가능)
    rows = []
    for row in iter_matches([None] * 6, want_nat, want_slot, 3, 1, False, SIZE_STATIC,
                            None, None, False, "무관", None, 5_000_000, rng_seed=42):
        rows.append(row)
        if len(rows) >= 8:
            break
    assert rows, "무작위 탐색이 아무것도 못 찾음"
    for seed, rg, ec, pid, ivs, ability, gender, nature, h, w, sh in rows:
        g = generate(seed, 1, 3, rg, SIZE_STATIC)
        assert (g[0], g[1], g[5]) == (ec, pid, nature), "seed 재생성 불일치"
        assert nature in want_nat, "성격 필터 위반"
    top_bits = [r[0] >> 40 for r in rows]  # 상위 24비트가 다양한지
    assert len(set(top_bits)) > 1 and max(r[0] for r in rows) > (1 << 40), \
        "seed가 초기값에 몰림(무작위화 실패)"
    print(f"  {len(rows)}개 결과, 전부 필터·재생성 일치, seed 상위비트 분산 OK")
    print(f"    예) {rows[0][0]:016X}, {rows[1][0]:016X}, {rows[2][0]:016X}")

    # 2) 키/몸무게는 모든 조우 타입에서 rand(0x81)+rand(0x80) = 0~255 로 굴려짐
    #    (pla-reverse fixed_seed 확인: 전설/알파도 고정 아님)
    for sm in (SIZE_STATIC, SIZE_ALPHA, SIZE_WILD):
        _, _, _, _, _, _, _, hh, ww, _ = generate(555, 1, 3, False, sm)
        assert 0 <= hh <= 255 and 0 <= ww <= 255, "사이즈 범위 오류"
    # 같은 seed면 size_mode 무관하게 동일(사이즈는 seed로만 결정)
    s_a = generate(999, 1, 3, False, SIZE_STATIC)
    s_b = generate(999, 1, 3, False, SIZE_ALPHA)
    assert s_a[7:9] == s_b[7:9], "size가 size_mode에 의존(버그)"
    print(f"  키/몸무게 항상 굴림 OK (예 {s_a[7]}/{s_a[8]}, 조우타입 무관)")

    # 3) 성별 공식: rand(253)+1 (게임 실제). 1~253 범위인지
    got_g = [generate(s, 1, 0, True, SIZE_WILD)[4] for s in range(2000, 2050)]
    assert all(1 <= gv <= 253 for gv in got_g), "성별 롤 범위(1~253) 오류"
    print("  성별 롤 rand(253)+1 범위 OK")

    # 4) 성별 롤 유무가 이후 성격/사이즈를 밀어내는지(소비량 차이) 확인
    a = generate(777, 1, 0, True, SIZE_WILD)
    b = generate(777, 1, 0, False, SIZE_WILD)
    assert (a[5], a[7], a[8]) != (b[5], b[7], b[8]), "성별 롤 소비 차이 없음"
    print("  성별 롤 on/off 시 성격·사이즈 시프트 OK")

    # 5) 성별 판정 (종 성별코드 + 롤 값)
    assert compute_gender(-1, None) == "무성"
    assert compute_gender(0, None) == "수" and compute_gender(8, None) == "암"
    # code 4 = 1:1 (byte 127): 롤<127 이면 암, 아니면 수
    assert compute_gender(4, 10) == "암" and compute_gender(4, 200) == "수"
    # code 1 = 수컷 87.5% (byte 31): 롤 10<31 암, 100>=31 수
    assert compute_gender(1, 10) == "암" and compute_gender(1, 100) == "수"
    assert species_rolls_gender(4) and not species_rolls_gender(-1) \
        and not species_rolls_gender(0) and not species_rolls_gender(8)
    # 성별 필터가 실제로 그 성별만 뽑는지 (code 4, 수컷)
    for r in iter_matches([None] * 6, None, None, 0, 1, False, SIZE_WILD, None, None,
                          True, "수컷", 4, 300_000, rng_seed=1):
        assert compute_gender(4, r[6]) == "수", "성별 필터 위반"

    # 5-2) 키/몸무게 범위 필터: 결과가 지정 범위(키 200~255) 안에 드는지
    cnt = 0
    for r in iter_matches([None] * 6, None, None, 0, 1, False, SIZE_WILD,
                          (200, 255), (None, 100), False, "무관", None,
                          2_000_000, rng_seed=3):
        assert 200 <= r[8] <= 255, "키 범위 필터 위반"
        assert r[9] <= 100, "몸무게 상한 필터 위반"
        cnt += 1
        if cnt >= 20:
            break
    assert cnt > 0, "범위 조건 결과 없음"
    print(f"  키/몸무게 범위 필터 OK (키200~255 & 몸무게~100, {cnt}개 확인)")
    # 7) 데이터 표 정합성 (있으면)
    if GENDER_CODE:
        assert GENDER_CODE.get("Cresselia") == 8, "크레세리아=암컷만 아님"
        assert GENDER_CODE.get("Heatran") == 4, "히트란=1:1 아님"
        assert GENDER_CODE.get("Ditto") == -1, "메타몽=무성 아님"
        assert lookup_gender_code("arceus") == lookup_gender_code("Arceus") == \
            lookup_gender_code("  ARCEUS ") == GENDER_CODE.get("Arceus"), "대소문자 조회 실패"
        print(f"  성별코드표 {len(GENDER_CODE)}종 로드 · 검증 OK · 대소문자무시 조회 OK")
    if KO_TO_EN:
        # 한국어 이름으로도 동일 성별코드가 나오는지
        for ko in list(KO_TO_EN)[:50]:
            en = KO_TO_EN[ko]
            if en in GENDER_CODE:
                assert lookup_gender_code(ko) == GENDER_CODE[en], f"한국어 조회 불일치: {ko}"
        assert lookup_gender_code("크레세리아") == 8, "크레세리아 한국어 조회 실패"
        assert lookup_gender_code("메타몽") == -1, "메타몽 한국어 조회 실패"
        print(f"  한국어 이름표 {len(KO_TO_EN)}종 로드 · 한국어 조회 OK (예: 크레세리아→암컷만)")
    print("  성별 암/수/무성 매핑 OK")

    # 8) 난이도 추정: 야생 6V가 전설 6V보다 훨씬 어렵고, 불가능 조건은 inf
    v31 = [(31, 31)] * 6
    e_wild6 = estimate_attempts(v31, {15}, None, 0, 1, False, SIZE_WILD, None, None)
    e_leg6 = estimate_attempts(v31, {15}, None, 3, 1, False, SIZE_STATIC, None, None)
    e_wild_any = estimate_attempts([None] * 6, {15}, None, 0, 1, False, SIZE_WILD, None, None)
    assert e_wild6 > e_leg6 > e_wild_any, "난이도 추정 순서 이상"
    # 보장 IV로 6칸 전부 31인데 한 칸에 5 지정 → 불가능(inf)
    impossible = [(31, 31), (5, 5), (31, 31), (31, 31), (31, 31), (31, 31)]
    assert estimate_attempts(impossible, None, None, 6, 1, False,
                             SIZE_STATIC, None, None) == float("inf"), "불가능 조건 inf 아님"
    # IV 범위 추정: 전설 6V(정확31)가 25~31 범위보다 어렵다
    e_range = estimate_attempts([(25, 31)] * 6, {15}, None, 3, 1, False, SIZE_STATIC, None, None)
    assert e_leg6 > e_range, "범위(25~31)가 정확31보다 쉬워야 함"
    print(f"  난이도 추정 OK (야생6V≈{e_wild6:,.0f} > 전설6V≈{e_leg6:,.0f} > 야생무관≈{e_wild_any:.0f})")

    # 9) 실제 색이 다른 포켓몬(진짜 TID/SID 기준) 판정 (■=사각 / ★=별 / X=비샤)
    assert is_real_shiny(0x00010001, 0, 0) and real_shiny_type(0x00010001, 0, 0) == "■"
    assert is_real_shiny(0x00010002, 0, 0) and real_shiny_type(0x00010002, 0, 0) == "★"
    assert not is_real_shiny(0x00010020, 0, 0) and real_shiny_type(0x00010020, 0, 0) == "X"
    assert real_shiny_type(0x12345678, None, None) is None
    # G7 표시형식(6자리/4자리) -> 16비트 변환 (역산 확인)
    tid16, sid16 = 0x1234, 0x5678
    full = (sid16 << 16) | tid16
    g7tid, g7sid = full % 1_000_000, full // 1_000_000
    assert g7_to_id16(g7tid, g7sid) == (tid16, sid16), "G7->16bit 변환 실패"
    # 색이 다른 포켓몬 PID: pla-pid-iv(verify_all_seeds)의 정확한 공식과 100% 일치하는지 검증
    def pla_pid_iv_expected(raw_pid, sidtid, tt, ss):
        """Lincoln-LM pla-pid-iv/main.py 로직 그대로 (기대 저장 PID)."""
        tsv = (tt ^ ss) & 0xFFFF
        temp = raw_pid ^ sidtid
        xor = ((temp & 0xFFFF) ^ (temp >> 16)) & 0xFFFF
        real_shiny = ((raw_pid & 0xFFFF) ^ (raw_pid >> 16) ^ tsv) < 16
        if real_shiny:                                   # 이미 내 기준 색이 다른 포켓몬 → 원본 유지
            return raw_pid & 0xFFFFFFFF
        return (raw_pid & 0xFFFF) | (
            (((raw_pid & 0xFFFF) ^ tsv ^ (1 if xor > 0 else 0)) & 0xFFFF) << 16)

    tt, ss = 54321, 1234
    got = list(iter_matches([None] * 6, None, None, 0, 1, True, SIZE_WILD,
                            None, None, True, "무관", None, 3_000_000, rng_seed=9,
                            tid=tt, sid=ss))
    assert got, "색이 다른 포켓몬 탐색 결과 없음"
    for seed, rg, ec, out_pid, ivs, ab, g, nat, h, w, sh in got:
        raw_pid, sidtid = generate(seed, 1, 0, rg, SIZE_WILD)[1], generate(seed, 1, 0, rg, SIZE_WILD)[9]
        assert sh, "생성 색이 다른 포켓몬 아님"
        assert out_pid == pla_pid_iv_expected(raw_pid, sidtid, tt, ss), \
            "pla-pid-iv 공식과 불일치"
        assert is_real_shiny(out_pid, tt, ss), "출력 PID가 내 기준 색이 다른 포켓몬 아님"
    print(f"  색이 다른 포켓몬: pla-pid-iv 정확 공식과 100% 일치 확인 OK ({len(got)}개)")
    print("[selftest] 통과 ✅")


def main():
    ap = argparse.ArgumentParser(description="PLA Reverse Finder")
    ap.add_argument("--selftest", action="store_true", help="GUI 없이 로직 검증")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    else:
        run_gui()


if __name__ == "__main__":
    main()
