"""PLA Reverse Finder (GUI)
============================
원하는 [조우 타입 / 종류 / IV / 성격 / (야생) 키·몸무게]를 넣으면,
그 개체를 만들어내는 합법 생성 세트(EC / PID / IVs / 특성 / 성격 / 성별 / 키 / 몸무게 / seed)를
여러 개 역산해서 표로 보여준다.
PKHeX에 한 행을 통째로 넣고 'PID 유형: Xoroshiro'가 뜨는 걸 쓰면 됨.

게임 생성 순서 (FixInitSpec):
    EC → (가짜 TID/SID) → PID(롤) → IV → 특성 → [성별] → 성격 → [키 → 몸무게]

  * 성별: 무성/고정 성별 종류는 성별을 안 굴려서 뒤의 성격·키·몸무게가 한 칸 밀림.
          → '성별 롤' 토글(자동=양쪽 다 시도)로 처리. 되는 쪽을 쓰면 됨.
  * 키/몸무게(사이즈 스칼라 0~255):
      - 야생 오버월드 : 성격 다음에 seed로 굴려짐 → 역산·검색 가능
      - 전설/정적     : 항상 127 고정 (seed 무관)
      - 알파          : 항상 255 고정 (seed 무관)

자체완결(파이썬 표준 라이브러리만 사용). 다른 파일 없이 단독 실행.
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

# 조우 타입 -> (보장IV 기본, 사이즈모드, 성별롤 기본 인덱스)
#   성별롤: 0=자동(양쪽) 1=있음 2=없음
ENCOUNTERS = [
    ("야생 오버월드",        0, SIZE_WILD,   1),
    ("전설/정적 (크레세리아 등)", 3, SIZE_STATIC, 2),
    ("우두머리",             3, SIZE_ALPHA,  0),
]
GENDER_ROLL_LABELS = ["자동 (양쪽 다 시도)", "있음 (성별 1롤)", "없음 (무성/고정)"]

# 성별 비율 프리셋: (라벨, 암컷 비율).  성별 롤이 '있음'일 때 숫자를 암/수로 해석.
GENDER_RATIOS = [
    ("암1 : 수1", 0.5), ("암1 : 수3", 0.25), ("암1 : 수7", 0.125),
    ("암3 : 수1", 0.75), ("암7 : 수1", 0.875),
    ("암컷만", 1.0), ("수컷만", 0.0),
]
GENDER_RATIO_LABELS = [g[0] for g in GENDER_RATIOS]

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


def resolve_gender(gender_val, roll_gender, code, female_frac):
    """생성된 성별 숫자를 암/수/무성 문자열로.
    code(종별 성별코드)가 있으면 우선 사용, 없으면 female_frac(수동 프리셋) 사용."""
    if roll_gender and gender_val is not None:
        f = (code / 8) if (code is not None and 1 <= code <= 7) else female_frac
        return "암" if gender_val <= f * 252 else "수"
    if code == 0:
        return "수"
    if code == 8:
        return "암"
    if code == -1:
        return "무성"
    return "무성/고정"


def code_to_roll_index(code):
    """종 성별코드 -> 성별롤 콤보 인덱스 (0 자동 / 1 있음 / 2 없음)."""
    return 1 if (code is not None and 1 <= code <= 7) else 2


def code_to_ratio_label(code):
    """종 성별코드 -> 성별비율 프리셋 라벨 (표시용)."""
    return {1: "암1 : 수7", 2: "암1 : 수3", 3: "암1 : 수1", 4: "암1 : 수1",
            5: "암1 : 수1", 6: "암3 : 수1", 7: "암7 : 수1"}.get(code, "암1 : 수1")


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
    """진짜 TID/SID(16비트) 기준으로 이 PID가 샤이니인지 (게임/PKHeX 표시 기준)."""
    return ((tid ^ sid ^ (pid >> 16) ^ (pid & 0xFFFF)) < 0x10)


def force_pla_shiny_pid(raw_pid, sidtid, tid, sid):
    """PLA 실제 샤이니 PID 규칙 (Lincoln-LM pla-pid-iv/main.py verify_all_seeds 그대로).

      xor        = (temp&0xFFFF) ^ (temp>>16),  temp = raw_pid ^ sidtid   (가짜TID 기준 샤이니 xor)
      real_shiny = (raw_low ^ raw_high ^ 내TSV) < 16
      - 원본이 이미 내 트레이너 샤이니면 → 원본 그대로.
      - 아니면 → 상위16 = 하위16 ^ 내TSV ^ (xor>0 ? 1 : 0)   (사각=0 / 별=1), 하위16 보존.
    → 엄격한 체커(pla-pid-iv)까지 통과하는 합법 샤이니 PID."""
    tsv = (tid ^ sid) & 0xFFFF
    temp = raw_pid ^ sidtid
    xor = ((temp & 0xFFFF) ^ (temp >> 16)) & 0xFFFF
    if ((raw_pid & 0xFFFF) ^ (raw_pid >> 16) ^ tsv) < 0x10:   # 이미 내 기준 샤이니
        return raw_pid & 0xFFFFFFFF
    low = raw_pid & 0xFFFF
    high = (low ^ tsv ^ (1 if xor > 0 else 0)) & 0xFFFF
    return (high << 16) | low


def real_shiny_type(pid, tid, sid):
    """진짜 TID/SID 기준 샤이니 종류: 사각★ / 별☆ / 비샤. TID/SID 없으면 None."""
    if tid is None or sid is None:
        return None
    x = tid ^ sid ^ (pid >> 16) ^ (pid & 0xFFFF)
    if x == 0:
        return "사각★"
    if x < 0x10:
        return "별☆"
    return "비샤"


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
    나머지는 rand(32). 지정 IV가 만족될 확률을 모든 보장-조합에 대해 평균낸다."""
    specified = [(i, t) for i, t in enumerate(want_ivs) if t is not None]
    subsets = list(combinations(range(6), giv)) if 0 <= giv <= 6 else []
    if not subsets:
        p_iv = 0.0
    else:
        total = 0.0
        for guaranteed in subsets:
            gset = set(guaranteed)
            f = 1.0
            for i, t in specified:
                if i in gset:
                    f *= 1.0 if t == 31 else 0.0   # 보장칸은 무조건 31
                else:
                    f *= 1 / 32                     # 랜덤칸: 원하는 값 나올 확률
            total += f
        p_iv = total / len(subsets)

    p = p_iv
    if want_nat is not None:
        p *= 1 / 25
    if want_slot is not None:
        p *= 1 / 2
    if shiny:
        p *= 1 - (1 - 1 / 4096) ** max(1, rolls)   # 생성 샤이니(내 TSV로는 확정 조작)
    if want_h is not None:
        p *= 1 / 128
    if want_w is not None:
        p *= 1 / 128
    return (1 / p) if p > 0 else float("inf")


def _check_seed(seed, rg, want_ivs, want_nat, want_slot, giv, rolls, shiny,
                size_mode, want_h, want_w, tid, sid, use_real):
    """한 seed(+성별롤)를 정확 생성해 모든 조건을 검사. 통과하면 결과 튜플, 아니면 None.

    샤이니 규칙(PLA 실제 방식):
      - 필터는 '생성 샤이니'(seed의 가짜 TID 기준)로 한다 → 합법 생성 경로.
      - 샤이니 + TID/SID: 출력 PID를 '가짜TSV→내TSV'로 상위16만 갈아끼움(하위16 보존).
        → seed 추적 가능 + 내 게임에서 반짝임 + PKHeX 샤이니 PID 공식과 일치.
      - 비샤 + TID/SID: 원본 PID가 내 기준 우연히 샤이니면 제외(불일치 방지)."""
    ec, pid, ivs, ability, gender, nature, sh, height, weight, sidtid = \
        generate(seed, rolls, giv, rg, size_mode)
    if sh != shiny:
        return None
    if want_nat is not None and nature != want_nat:
        return None
    if want_slot is not None and ability != want_slot:
        return None
    if not all(t is None or t == v for t, v in zip(want_ivs, ivs)):
        return None
    if want_h is not None and height != want_h:
        return None
    if want_w is not None and weight != want_w:
        return None
    out_pid = pid
    if use_real:
        if shiny:
            out_pid = force_pla_shiny_pid(pid, sidtid, tid, sid)   # 내 TSV 기준 샤이니로 확정
        elif is_real_shiny(pid, tid, sid):
            return None                                            # 비샤인데 내 기준 샤이니면 제외
    return (seed, rg, ec, out_pid, ivs, ability, gender, nature, height, weight, sh)


def iter_matches(want_ivs, want_nat, want_slot, giv, rolls, shiny,
                 size_mode, want_h, want_w, gender_variants,
                 max_attempts, stop_check=None, progress=None, rng_seed=None,
                 tid=None, sid=None):
    """조건에 맞는 결과를 (seed, roll_gender, ec, pid, ivs, ability, gender,
       nature, height, weight, gen_shiny) 튜플로 하나씩 yield.
    샤이니+TID/SID면 PID를 PLA 방식으로 내 TSV 샤이니로 확정(하위16 보존)."""
    rng = random.Random(rng_seed)
    use_real = tid is not None and sid is not None
    for attempt in range(max_attempts):
        if stop_check is not None and (attempt & 0x3FFFF) == 0 and stop_check():
            return
        if progress is not None and attempt % 200000 == 0:
            progress(attempt)
        seed = rng.getrandbits(64)
        for rg in gender_variants:
            row = _check_seed(seed, rg, want_ivs, want_nat, want_slot, giv, rolls,
                              shiny, size_mode, want_h, want_w, tid, sid, use_real)
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

            # row 1-2: IVs
            ttk.Label(frm, text="원하는 IV (빈칸/x = 아무값)").grid(
                row=1, column=0, columnspan=7, sticky="w", **pad)
            self.iv_vars = []
            for i, name in enumerate(IV_NAMES):
                ttk.Label(frm, text=name).grid(row=2, column=i, **pad)
                v = tk.StringVar(value="31")
                ttk.Entry(frm, width=5, textvariable=v, justify="center").grid(row=3, column=i, **pad)
                self.iv_vars.append(v)
            ttk.Button(frm, text="6V", width=4,
                       command=lambda: self.set_ivs([31] * 6)).grid(row=3, column=6, **pad)
            ttk.Button(frm, text="5V(공격무시)", width=12,
                       command=lambda: self.set_ivs([31, "", 31, 31, 31, 31])).grid(row=2, column=6, **pad)

            # row 4: 성격 / 특성 슬롯 / 성별 롤
            ttk.Label(frm, text="성격").grid(row=4, column=0, sticky="e", **pad)
            self.nature_var = tk.StringVar(value="조심 (Modest)")
            ttk.Combobox(frm, textvariable=self.nature_var, values=NATURE_LABELS,
                         state="readonly", width=16).grid(row=4, column=1, columnspan=2, sticky="w", **pad)
            ttk.Label(frm, text="특성 슬롯").grid(row=4, column=3, sticky="e", **pad)
            self.slot_var = tk.StringVar(value="무관")
            ttk.Combobox(frm, textvariable=self.slot_var, values=["무관", "0", "1"],
                         state="readonly", width=6).grid(row=4, column=4, sticky="w", **pad)
            ttk.Label(frm, text="성별 롤").grid(row=4, column=5, sticky="e", **pad)
            self.gender_var = tk.StringVar(value=GENDER_ROLL_LABELS[2])
            ttk.Combobox(frm, textvariable=self.gender_var, values=GENDER_ROLL_LABELS,
                         state="readonly", width=16).grid(row=4, column=6, sticky="w", **pad)

            # row 5: 보장IV / 롤 / 개수 / 샤이니
            ttk.Label(frm, text="보장 31 IV").grid(row=5, column=0, sticky="e", **pad)
            self.giv_var = tk.IntVar(value=3)
            # 조우 타입으로 자동 결정(우두머리·전설/정적=3, 야생=0) → 수동 편집 불가
            ttk.Spinbox(frm, from_=0, to=6, textvariable=self.giv_var, width=5,
                        state="readonly").grid(row=5, column=1, sticky="w", **pad)
            ttk.Label(frm, text="롤 수").grid(row=5, column=2, sticky="e", **pad)
            self.rolls_var = tk.IntVar(value=1)
            ttk.Spinbox(frm, from_=1, to=99, textvariable=self.rolls_var, width=5).grid(
                row=5, column=3, sticky="w", **pad)
            ttk.Label(frm, text="개수").grid(row=5, column=4, sticky="e", **pad)
            self.count_var = tk.IntVar(value=12)
            ttk.Spinbox(frm, from_=1, to=300, textvariable=self.count_var, width=5).grid(
                row=5, column=5, sticky="w", **pad)
            self.shiny_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(frm, text="샤이니만", variable=self.shiny_var).grid(
                row=5, column=6, sticky="w", **pad)

            # row 6: 키 / 몸무게 (야생 전용)
            ttk.Label(frm, text="키(0~255)").grid(row=6, column=0, sticky="e", **pad)
            self.h_var = tk.StringVar(value="")
            self.h_entry = ttk.Entry(frm, width=6, textvariable=self.h_var, justify="center")
            self.h_entry.grid(row=6, column=1, sticky="w", **pad)
            ttk.Label(frm, text="몸무게(0~255)").grid(row=6, column=2, sticky="e", **pad)
            self.w_var = tk.StringVar(value="")
            self.w_entry = ttk.Entry(frm, width=6, textvariable=self.w_var, justify="center")
            self.w_entry.grid(row=6, column=3, sticky="w", **pad)
            self.size_hint = ttk.Label(frm, text="(스칼라 0~255 · 빈칸=아무값 · 모든 조우 검색 가능)")
            self.size_hint.grid(row=6, column=4, columnspan=3, sticky="w", **pad)

            # row 7: 성별 비율 (성별 롤 '있음/자동'일 때 숫자를 암/수로 해석)
            ttk.Label(frm, text="성별 비율").grid(row=7, column=0, sticky="e", **pad)
            self.gratio_var = tk.StringVar(value=GENDER_RATIO_LABELS[0])
            ttk.Combobox(frm, textvariable=self.gratio_var, values=GENDER_RATIO_LABELS,
                         state="readonly", width=10).grid(row=7, column=1, columnspan=2,
                                                           sticky="w", **pad)
            ttk.Label(frm, text="(성별을 암/수로 표시 · 무성은 성별 롤을 '없음'으로)").grid(
                row=7, column=3, columnspan=4, sticky="w", **pad)

            # row 8: 진짜 TID/SID (입력 시 샤이니를 내 트레이너 기준으로 표시·검색)
            self.tid_lbl = ttk.Label(frm, text="TID(6자리)")
            self.tid_lbl.grid(row=8, column=0, sticky="e", **pad)
            self.tid_var = tk.StringVar(value="")
            ttk.Entry(frm, width=8, textvariable=self.tid_var, justify="center").grid(
                row=8, column=1, sticky="w", **pad)
            self.sid_lbl = ttk.Label(frm, text="SID(4자리)")
            self.sid_lbl.grid(row=8, column=2, sticky="e", **pad)
            self.sid_var = tk.StringVar(value="")
            ttk.Entry(frm, width=8, textvariable=self.sid_var, justify="center").grid(
                row=8, column=3, sticky="w", **pad)
            self.raw16_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(frm, text="16비트로 입력", variable=self.raw16_var,
                            command=self.on_id_mode).grid(row=8, column=4, sticky="w", **pad)
            ttk.Label(frm, text="(입력 시 샤이니를 내 트레이너 기준으로 표시·검색)").grid(
                row=8, column=5, columnspan=2, sticky="w", **pad)

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
            cols = ("#", "EC", "PID", "IVs", "특성", "성격", "성별", "샤이니",
                    "키", "몸무게", "성별롤", "seed")
            widths = (30, 74, 74, 188, 44, 100, 44, 52, 40, 50, 48, 124)
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
        def set_ivs(self, vals):
            for v, val in zip(self.iv_vars, vals):
                v.set(str(val))

        def current_encounter(self):
            for name, giv, size_mode, gidx in ENCOUNTERS:
                if name == self.enc_var.get():
                    return giv, size_mode, gidx
            return 3, SIZE_STATIC, 2

        def on_id_mode(self):
            if self.raw16_var.get():
                self.tid_lbl.config(text="TID(16bit)")
                self.sid_lbl.config(text="SID(16bit)")
            else:
                self.tid_lbl.config(text="TID(6자리)")
                self.sid_lbl.config(text="SID(4자리)")

        def on_species(self):
            typed = self.species_var.get().strip()
            canon = SPECIES_CANON.get(typed) or SPECIES_CANON.get(typed.lower())
            if canon and canon != typed:      # 영어 소문자 → 정식표기 (한국어는 그대로)
                self.species_var.set(canon)
                typed = canon
            code = lookup_gender_code(typed)
            if code is None:
                return
            self.gender_var.set(GENDER_ROLL_LABELS[code_to_roll_index(code)])
            if 1 <= code <= 7:
                self.gratio_var.set(code_to_ratio_label(code))
            self.status.set(f"'{typed}' 성별: {code_gender_desc(code)} — 성별 롤 자동 설정됨")

        def on_encounter(self):
            giv, size_mode, gidx = self.current_encounter()
            self.giv_var.set(giv)             # 보장 31 IV: 조우 타입에 따라 자동 적용
            # 종이 지정돼 있으면 종 기준을 우선(성별 롤 유지), 아니면 조우 타입 기본값
            if lookup_gender_code(self.species_var.get()) is None:
                self.gender_var.set(GENDER_ROLL_LABELS[gidx])

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
            want_ivs = []
            for v in self.iv_vars:
                s = v.get().strip().lower()
                if s in ("", "x", "*", "?"):
                    want_ivs.append(None)
                else:
                    iv = int(s)
                    if not 0 <= iv <= 31:
                        raise ValueError(f"IV는 0~31: {s}")
                    want_ivs.append(iv)
            nl = self.nature_var.get()
            want_nat = None if nl == "(무관)" else NATURE_LABELS.index(nl) - 1
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
            want_h = parse_size(self.h_var.get())
            want_w = parse_size(self.w_var.get())

            def parse_id(s, name, maxv):
                s = s.strip()
                if s in ("", "x", "*", "?"):
                    return None
                v = int(s, 16) if s.lower().startswith("0x") else int(s)
                if not 0 <= v <= maxv:
                    raise ValueError(f"{name}는 0~{maxv}")
                return v

            if self.raw16_var.get():          # 16비트 직접 입력
                tid = parse_id(self.tid_var.get(), "TID(16bit)", 0xFFFF)
                sid = parse_id(self.sid_var.get(), "SID(16bit)", 0xFFFF)
            else:                             # 스위치 표시형식(6자리/4자리) → 16비트 변환
                g7tid = parse_id(self.tid_var.get(), "TID(6자리)", 999999)
                g7sid = parse_id(self.sid_var.get(), "SID(4자리)", 4294)
                if (g7tid is None) != (g7sid is None):
                    raise ValueError("TID와 SID는 둘 다 입력하거나 둘 다 비워야 함")
                tid, sid = (None, None) if g7tid is None else g7_to_id16(g7tid, g7sid)
            if (tid is None) != (sid is None):
                raise ValueError("TID와 SID는 둘 다 입력하거나 둘 다 비워야 함")
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
            giv_conf, size_mode, _ = self.current_encounter()

            gl = self.gender_var.get()
            if gl == GENDER_ROLL_LABELS[1]:
                gender_variants = [True]
            elif gl == GENDER_ROLL_LABELS[2]:
                gender_variants = [False]
            else:
                gender_variants = [True, False]

            # 난이도(예상 시도수) 추정 → 너무 드물면 미리 경고
            # 샤이니를 원하는데 TID/SID가 없으면 내 트레이너 기준 샤이니를 보장 못 함
            if self.shiny_var.get() and tid is None:
                if not messagebox.askyesno(
                        "TID/SID 없이 샤이니?",
                        "TID/SID를 넣어야 '내 게임에서 실제로 반짝이는' 합법 샤이니를 찾습니다.\n"
                        "없이 진행하면 생성 샤이니만 맞춘 PID라, 당신 게임/PKHeX에서 샤이니로\n"
                        "안 보이거나 'PID 유형: None'이 될 수 있습니다.\n\n"
                        "그래도 계속할까요? (권장: TID/SID 입력 후 다시)"):
                    return

            giv_cur = self.giv_var.get()
            est = estimate_attempts(want_ivs, want_nat, want_slot, giv_cur,
                                    self.rolls_var.get(), self.shiny_var.get(),
                                    size_mode, want_h, want_w)
            if est == float("inf"):
                messagebox.showerror(
                    "불가능한 조건",
                    "이 조건을 만족하는 개체가 존재할 수 없습니다.\n"
                    "(예: 보장 IV로 6칸 전부 31인데 특정 스탯에 31이 아닌 값을 지정)\n\n"
                    "IV 조건을 확인해 주세요.")
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

            self.cur_frac = dict(GENDER_RATIOS)[self.gratio_var.get()]
            self.gender_code = lookup_gender_code(self.species_var.get())
            self.cur_tid, self.cur_sid = tid, sid
            self.stop_flag.clear()
            self.find_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.status.set(f"탐색 중... (예상 1개당 ~{est:,.0f}회)")
            args = (want_ivs, want_nat, want_slot, self.giv_var.get(), self.rolls_var.get(),
                    self.shiny_var.get(), size_mode, want_h, want_w, gender_variants,
                    self.count_var.get(), tid, sid)
            self.worker = threading.Thread(target=self._search, args=args, daemon=True)
            self.worker.start()
            self.root.after(100, self._poll)

        def stop_search(self):
            self.stop_flag.set()

        def _search(self, want_ivs, want_nat, want_slot, giv, rolls, shiny,
                    size_mode, want_h, want_w, gender_variants, count, tid, sid):
            LIMIT = 2_000_000_000
            found = 0
            gen = iter_matches(
                want_ivs, want_nat, want_slot, giv, rolls, shiny, size_mode,
                want_h, want_w, gender_variants, LIMIT,
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
                         gender, nature, height, weight, sh) = payload
                        if self.cur_tid is not None and self.cur_sid is not None:
                            shiny_cell = real_shiny_type(pid, self.cur_tid, self.cur_sid)
                        else:
                            shiny_cell = "생성Y" if sh else "생성N"
                        self.tree.insert("", "end", values=(
                            n, f"{ec:08X}", f"{pid:08X}", "/".join(str(v) for v in ivs),
                            f"슬롯{ability}", f"{NATURES[nature][1]}({NATURES[nature][0]})",
                            resolve_gender(gender, rg, self.gender_code, self.cur_frac),
                            shiny_cell, height, weight,
                            "Y" if rg else "N", f"{seed:016X}"))
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
    want_nat, want_slot = 15, None  # 조심(Modest)
    rows = []
    for row in iter_matches([None] * 6, want_nat, want_slot, 3, 1, False, SIZE_STATIC,
                            None, None, [False], 5_000_000, rng_seed=42):
        rows.append(row)
        if len(rows) >= 8:
            break
    assert rows, "무작위 탐색이 아무것도 못 찾음"
    for seed, rg, ec, pid, ivs, ability, gender, nature, h, w, sh in rows:
        g = generate(seed, 1, 3, rg, SIZE_STATIC)
        assert (g[0], g[1], g[5]) == (ec, pid, nature), "seed 재생성 불일치"
        assert nature == want_nat, "성격 필터 위반"
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

    # 5) 성별 라벨 매핑 (수동 프리셋)
    assert resolve_gender(None, False, None, 0.5) == "무성/고정"
    assert resolve_gender(10, True, None, 0.5) == "암" and resolve_gender(240, True, None, 0.5) == "수"
    # 6) 종 성별코드 기반 매핑
    assert resolve_gender(None, False, -1, 0.5) == "무성"    # 무성
    assert resolve_gender(None, False, 0, 0.5) == "수"       # 수컷만
    assert resolve_gender(None, False, 8, 0.5) == "암"       # 암컷만
    assert resolve_gender(10, True, 1, 0.5) == "암"          # 87.5% 수 → 낮은값=암
    assert resolve_gender(240, True, 1, 0.5) == "수"
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
    e_wild6 = estimate_attempts([31] * 6, 15, None, 0, 1, False, SIZE_WILD, None, None)
    e_leg6 = estimate_attempts([31] * 6, 15, None, 3, 1, False, SIZE_STATIC, None, None)
    e_wild_any = estimate_attempts([None] * 6, 15, None, 0, 1, False, SIZE_WILD, None, None)
    assert e_wild6 > e_leg6 > e_wild_any, "난이도 추정 순서 이상"
    assert estimate_attempts([31, 5, 31, 31, 31, 31], None, None, 6, 1, False,
                             SIZE_STATIC, None, None) == float("inf"), "불가능 조건 inf 아님"
    print(f"  난이도 추정 OK (야생6V≈{e_wild6:,.0f} > 전설6V≈{e_leg6:,.0f} > 야생무관≈{e_wild_any:.0f})")

    # 9) 실제 샤이니(진짜 TID/SID 기준) 판정
    assert is_real_shiny(0x00010001, 0, 0) and real_shiny_type(0x00010001, 0, 0) == "사각★"
    assert is_real_shiny(0x00010002, 0, 0) and real_shiny_type(0x00010002, 0, 0) == "별☆"
    assert not is_real_shiny(0x00010020, 0, 0) and real_shiny_type(0x00010020, 0, 0) == "비샤"
    assert real_shiny_type(0x12345678, None, None) is None
    # G7 표시형식(6자리/4자리) -> 16비트 변환 (역산 확인)
    tid16, sid16 = 0x1234, 0x5678
    full = (sid16 << 16) | tid16
    g7tid, g7sid = full % 1_000_000, full // 1_000_000
    assert g7_to_id16(g7tid, g7sid) == (tid16, sid16), "G7->16bit 변환 실패"
    # 샤이니 PID: pla-pid-iv(verify_all_seeds)의 정확한 공식과 100% 일치하는지 검증
    def pla_pid_iv_expected(raw_pid, sidtid, tt, ss):
        """Lincoln-LM pla-pid-iv/main.py 로직 그대로 (기대 저장 PID)."""
        tsv = (tt ^ ss) & 0xFFFF
        temp = raw_pid ^ sidtid
        xor = ((temp & 0xFFFF) ^ (temp >> 16)) & 0xFFFF
        real_shiny = ((raw_pid & 0xFFFF) ^ (raw_pid >> 16) ^ tsv) < 16
        if real_shiny:                                   # 이미 내 기준 샤이니 → 원본 유지
            return raw_pid & 0xFFFFFFFF
        return (raw_pid & 0xFFFF) | (
            (((raw_pid & 0xFFFF) ^ tsv ^ (1 if xor > 0 else 0)) & 0xFFFF) << 16)

    tt, ss = 54321, 1234
    got = list(iter_matches([None] * 6, None, None, 0, 1, True, SIZE_WILD,
                            None, None, [True], 3_000_000, rng_seed=9,
                            tid=tt, sid=ss))
    assert got, "샤이니 탐색 결과 없음"
    for seed, rg, ec, out_pid, ivs, ab, g, nat, h, w, sh in got:
        raw_pid, sidtid = generate(seed, 1, 0, rg, SIZE_WILD)[1], generate(seed, 1, 0, rg, SIZE_WILD)[9]
        assert sh, "생성 샤이니 아님"
        assert out_pid == pla_pid_iv_expected(raw_pid, sidtid, tt, ss), \
            "pla-pid-iv 공식과 불일치"
        assert is_real_shiny(out_pid, tt, ss), "출력 PID가 내 기준 샤이니 아님"
    print(f"  샤이니: pla-pid-iv 정확 공식과 100% 일치 확인 OK ({len(got)}개)")
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
