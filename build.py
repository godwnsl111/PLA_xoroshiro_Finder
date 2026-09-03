"""exe 빌드 스크립트.

사용법:
    pip install pyinstaller
    python build.py

결과: dist/PLA_Reverse_Finder.exe  (Windows 64bit, 파이썬 설치 없이 실행)

text_species.txt 는 드롭다운 종 목록용으로 exe 안에 함께 넣습니다.
(pla_gender_data.py / pla_names_ko.py 는 import 라서 PyInstaller가 자동 포함합니다.)
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# --add-data 구분자: Windows=';', mac/Linux=':'
SEP = ";" if os.name == "nt" else ":"

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "PLA_Reverse_Finder",
    "--add-data", f"{os.path.join(HERE, 'text_species.txt')}{SEP}.",
    "--noconfirm",
    os.path.join(HERE, "pla_reverse_finder.py"),
]

print("실행:", " ".join(cmd))
raise SystemExit(subprocess.call(cmd))
