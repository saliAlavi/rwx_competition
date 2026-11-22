"""Collect ground-truth result files into one data dir (commits/ + tests.json + answers.json)."""
import glob, json, os, shutil, sys
src_dirs, out = sys.argv[1], sys.argv[2]  # src glob like /x/mut/o*, out dir
tests = json.load(open("sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
kinds = json.load(open(os.environ.get("BR_KINDS", "/tmp/br_mut/a/kinds.json")))
os.makedirs(f"{out}/commits", exist_ok=True)
answers = {}
for f in glob.glob(f"{src_dirs}/[mv]*.json"):
    cid = os.path.basename(f)[:-5]
    r = json.load(open(f))
    failing = {num[n] for n in r["failed"] if n in num}
    for b in r["broken"]:
        failing.update(by_file.get(b, []))
    unknown = [n for n in r["failed"] if n not in num]
    if unknown:
        print("unknown ids", cid, unknown[:3])
    answers[cid] = {"failing": sorted(failing), "kind": kinds.get(cid, "?")}
    shutil.copy(os.path.join(os.environ.get("BR_MUTDIR", "/tmp/br_mut/a"), "commits", f"{cid}.patch"), f"{out}/commits/")
shutil.copy("sample/tests.json", f"{out}/tests.json")
json.dump({"commits": answers}, open(f"{out}/answers.json", "w"))
print(len(answers), "commits")
