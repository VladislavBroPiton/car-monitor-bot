"""
Проверка совместимости с Python 3.11 — версией, на которой крутится Render.

Локально может стоять более свежий Python, и тогда ast.parse пропускает
конструкции, которых на проде ещё нет. Самая коварная — вложенные одинаковые
кавычки внутри f-строки: с 3.12 (PEP 701) это законно, на 3.11 — SyntaxError
прямо при импорте модуля, то есть падение деплоя.

Лечится выносом выражения в переменную перед строкой.

Запуск:  python tests/check_py311.py
"""

import ast
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = (3, 11)

# Ищем f-строку, внутри выражения которой встречается та же кавычка,
# что и обрамляющая. Токенизатор 3.11 такое не переваривает.
NESTED_QUOTES = re.compile(
    r'''f"[^"\n]*\{[^}\n]*"[^}\n]*\}|f'[^'\n]*\{[^}\n]*'[^}\n]*\}'''
)


def python_files():
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in {".venv", "venv", "__pycache__", "node_modules"}
               for part in path.parts):
            continue
        yield path


def main() -> int:
    problems = []

    for path in python_files():
        src = io.open(path, encoding="utf-8").read()
        rel = path.relative_to(ROOT)

        # 1. Синтаксис с оглядкой на целевую версию
        try:
            ast.parse(src, filename=str(rel), feature_version=TARGET)
        except SyntaxError as e:
            problems.append(f"{rel}:{e.lineno}: {e.msg}")
            continue

        # 2. Вложенные кавычки в f-строках — ast так не ловится
        for num, line in enumerate(src.splitlines(), 1):
            if NESTED_QUOTES.search(line):
                problems.append(
                    f"{rel}:{num}: одинаковые кавычки внутри f-строки — "
                    f"нужен Python 3.12+, на Render 3.11\n"
                    f"        {line.strip()[:90]}"
                )

    print(f"Проверено файлов: {sum(1 for _ in python_files())}, "
          f"цель: Python {TARGET[0]}.{TARGET[1]}")
    if problems:
        print(f"\nНесовместимо ({len(problems)}):")
        for p in problems:
            print(f"  {p}")
        return 1

    print("Совместимо с Python 3.11")
    return 0


if __name__ == "__main__":
    sys.exit(main())
