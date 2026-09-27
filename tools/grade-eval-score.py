"""grade-eval.sh が残した採点役の各回の結果を、多数決と重み付きの点数にまとめる。

    python3 tools/grade-eval-score.py <結果の名前の基> <回数> [expected.md]

<結果の名前の基> は `evals/results/grading/<ケース>-<時刻>` で、その `.criteria.md`（重み付きの条件）と
`.vote<N>.json`（採点役の各回の出力）を読み、`.md`（採点の報告）を書く。
"""
import json, re, sys
base, runs = sys.argv[1], int(sys.argv[2])
expected = sys.argv[3] if len(sys.argv) > 3 else ""
criteria = open(f"{base}.criteria.md").read()
weights = dict((cid, int(w)) for cid, w in re.findall(r"^### (\S+)\n\n重み: (\d+)", criteria, re.M))
order = list(weights)

votes, reasons, cost = {c: [] for c in order}, {c: {} for c in order}, 0.0
for i in range(1, runs + 1):
    result = json.load(open(f"{base}.vote{i}.json"))
    cost += result.get("total_cost_usd", 0) or 0
    text = result.get("result") or ""
    open(f"{base}.vote{i}.md", "w").write(text)
    for cid, verdict, body in re.findall(r"^### (\S+)[ \t]*\n\s*判定:\s*(PASS|FAIL)[ \t]*\n(.*?)(?=^### |^合計|\Z)", text, re.M | re.S):
        if cid in votes:
            votes[cid].append(verdict)
            reasons[cid].setdefault(verdict, body.strip())

def majority(vs):
    # 票が足りない条件（採点役が書き落とした、資料が無い）は FAIL に数える。
    return "PASS" if vs.count("PASS") * 2 > runs else "FAIL"

final = {c: majority(votes[c]) for c in order}
total = sum(weights.values())
got = sum(weights[c] for c in order if final[c] == "PASS")
score = round(100 * got / total, 1) if total else 0.0
band = "実用に足る" if score >= 85 else "手直しで使える" if score >= 70 else "作り直しが要る"
lost = sorted((c for c in order if final[c] == "FAIL"), key=lambda c: (-weights[c], order.index(c)))

lines = [f"# 採点：{base.rsplit('/', 1)[-1]}", "",
         f"点数は {score} 点（{got}/{total}）で、帯は「{band}」である。採点役を {runs} 回回し、条件ごとに多数決を取った。費用は ${cost:.2f} だった。", ""]
if lost:
    lines += ["## 減点の大きかった条件", ""]
    for c in lost:
        lines += [f"### {c}（重み {weights[c]}、票 {' '.join(votes[c]) or 'なし'}）", "", reasons[c].get("FAIL", "（資料が無いか、採点役が判定を書かなかった）"), ""]
lines += ["## 条件ごとの判定", "", "| 条件 | 重み | 票 | 判定 |", "|---|---|---|---|"]
lines += [f"| {c} | {weights[c]} | {' '.join(votes[c]) or 'なし'} | {final[c]} |" for c in order]
open(f"{base}.md", "w").write("\n".join(lines) + "\n")

print(f"採点の報告: {base}.md")
print(f"点数: {score}（{got}/{total}） 帯: {band}  採点役: {runs} 回  費用: ${cost:.2f}")
for c in lost:
    print(f"減点\t{c}\t重み {weights[c]}\t票 {' '.join(votes[c]) or 'なし'}")
if expected:
    want, border = {}, set()
    for name, verdict, note in re.findall(r"^- (\S+): (PASS|FAIL)(（境目）)?", open(expected).read(), re.M):
        want[name] = verdict
        if note:
            border.add(name)
    match = sum(final.get(n) == v for n, v in want.items())
    firm = [n for n in want if n not in border]
    for n, v in want.items():
        print(f"{'一致' if final.get(n) == v else '不一致'}\t{n}\t期待 {v}\t多数決 {final.get(n, '無し')}（{' '.join(votes.get(n, []))}）{'（境目）' if n in border else ''}")
    print(f"一致: {match}/{len(want)}（境目を除くと {sum(final.get(n) == want[n] for n in firm)}/{len(firm)}）")
