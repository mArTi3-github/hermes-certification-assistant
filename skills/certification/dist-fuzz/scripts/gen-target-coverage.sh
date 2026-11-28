#!/bin/bash -eu
# Узкий отчёт JaCoCo: только классы, которые цель реально затронула.
#
# Зачем: CorpusRunner запускается с полным classpath продукта, поэтому отчёт по всему
# classpath считает знаменателем все классы (в Keycloak — 3 997 классов, 544 439
# инструкций) и показывает 0 % почти везде. Здесь область сужается до классов, покрытых
# хотя бы в одном срезе (seeds или final): проценты становятся осмысленными, а кратность
# не меняется — знаменатель в обоих срезах один и тот же.
#
# Предусловия: в evidence/build/targets/<T>/coverage/ уже лежат jacoco-seeds.xml,
# jacoco-final.xml и .exec обоих срезов (их снимает CorpusRunner под агентом JaCoCo);
# в evidence/build/classes-all/ распакованы классы продукта.
#
# usage: BASE=~/work/certification/<КОМПОНЕНТ>.FUZZ IMAGE=<образ> gen-target-coverage.sh <Target>
# Результат: coverage/target/{jacoco-seeds.xml,jacoco-final.xml,html-seeds/,html-final/,
#            scope-classes.txt,report-*.log}
T=${1:?usage: gen-target-coverage.sh <Target>}
BASE=${BASE:-$PWD}
IMAGE=${IMAGE:?нужен IMAGE — образ с jaCoCo CLI (org.jacoco.cli-*-nodeps.jar в /src/tools)}
BUILD=$BASE/evidence/build
COV=$BUILD/targets/$T/coverage
SUB=/var/tmp/jacoco-subset-$T

[ -f "$COV/jacoco-seeds.xml" ] || { echo "нет $COV/jacoco-seeds.xml — сначала снять срезы"; exit 1; }
[ -d "$BUILD/classes-all/org" ] || { echo "нет $BUILD/classes-all — распакуйте jar-ы продукта"; exit 1; }

rm -rf "$SUB"; mkdir -p "$SUB/classfiles" "$COV/target"

python3 - "$COV" "$BUILD/classes-all" "$SUB" <<'PY'
import os, shutil, sys, xml.etree.ElementTree as ET
cov, classes_all, sub = sys.argv[1:4]
names = set()
for lab in ("seeds", "final"):
    root = ET.parse(os.path.join(cov, "jacoco-%s.xml" % lab)).getroot()
    for package in root.findall("package"):
        for cls in package.findall("class"):
            c = {x.get("type"): int(x.get("covered")) for x in cls.findall("counter")}
            if c.get("INSTRUCTION", 0) > 0:
                names.add(cls.get("name"))
if not names:
    sys.exit("в срезах нет покрытых классов — проверить .exec и classfiles")
n = 0
for name in sorted(names):
    src = os.path.join(classes_all, name + ".class")
    if not os.path.exists(src):
        print("НЕТ КЛАССА:", name); continue
    dst = os.path.join(sub, "classfiles", name + ".class")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst); n += 1
    d, prefix = os.path.dirname(src), os.path.basename(src)[:-6] + "$"
    for fn in sorted(os.listdir(d)):
        if fn.startswith(prefix) and fn.endswith(".class"):
            shutil.copy2(os.path.join(d, fn), os.path.join(os.path.dirname(dst), fn)); n += 1
with open(os.path.join(sub, "scope-classes.txt"), "w", encoding="utf-8") as fh:
    fh.write("# классы, затронутые целью (покрытие > 0 в срезе seeds или final)\n")
    for name in sorted(names):
        fh.write(name + "\n")
print("классов в области: %d, файлов .class: %d" % (len(names), n))
PY

cp "$SUB/scope-classes.txt" "$COV/target/scope-classes.txt"

for LAB in seeds final; do
  docker run --rm -m 4g -v "$BASE/src":/src -v "$BUILD":/out -v "$SUB":/sub:ro "$IMAGE" bash -c "
    java -jar /src/tools/org.jacoco.cli-0.8.12-nodeps.jar report \
      /out/targets/$T/coverage/jacoco-$LAB.exec \
      --classfiles /sub/classfiles --sourcefiles /src/keycloak \
      --html /out/targets/$T/coverage/target/html-$LAB \
      --xml  /out/targets/$T/coverage/target/jacoco-$LAB.xml \
      --name '$T (область: классы, затронутые целью; срез $LAB)' \
      > /out/targets/$T/coverage/target/report-$LAB.log 2>&1
    grep -E 'Analyzing|Writing' /out/targets/$T/coverage/target/report-$LAB.log | tail -2"
done

# Контроль: Total в html-final не должен быть нулевым, если цель работала хотя бы на seeds.
python3 - "$COV/target" <<'PY'
import re, sys, os
p = os.path.join(sys.argv[1], "html-final", "index.html")
if os.path.exists(p):
    h = open(p, encoding="utf-8", errors="replace").read()
    m = re.search(r'<td>Total</td><td class="bar">([^<]*)</td><td class="ctr2">(\d+)%', h)
    print("Total (final):", m.group(1), m.group(2) + "%" if m else "не найден")
PY
echo "готово: $COV/target"
