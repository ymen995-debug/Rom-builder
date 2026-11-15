#!/usr/bin/env python3
"""GitHub'da cihaza ozel device / vendor / kernel agaclarini otomatik bulur.

Kullanim:
  GH_TOKEN=... python3 find_device_trees.py --brand oppo --codename CPH2269 \
      --model "A16" --country TR --user ymen995-debug --android 11 --rom crdroid \
      --out plan.json

Cikti: plan.json  -> [{"role","url","branch","path"}, ...]
Cikis kodlari: 0 = basarili, 2 = device tree bulunamadi, 3 = sadece recovery agaci, 4 = surum uyusmuyor
"""
import argparse, base64, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
LOG = []
SEEN = []  # incelenen adaylar (repo, dal, android)

# Android surumu -> tercih edilen dal desenleri (en iyi eslesen once)
BRANCH_PREFS = {
    "10": [r"lineage-17\.1", r"^10(\.0)?$", r"android-?10", r"17\.1"],
    "11": [r"lineage-18\.1", r"crdroid.*11", r"^11(\.0)?$", r"android-?11", r"18\.1"],
    "12": [r"lineage-19\.1", r"lineage-19\b", r"^12(\.[01])?$", r"android-?12", r"19\.1"],
    "13": [r"lineage-20(\.0)?", r"^13(\.0)?$", r"android-?13", r"\b20\.0"],
    "14": [r"lineage-21(\.0)?", r"^14(\.0)?$", r"android-?14", r"\b21\.0"],
    "15": [r"lineage-22(\.\d)?", r"^15(\.0)?$", r"android-?15", r"\b22\.\d"],
}
OTHER_VERSION_HINTS = {  # yanlis surume ait dallari cezalandirmak icin
    "11": [r"lineage-(1[79]|2\d)", r"android-?(9|10|12|13|14)\b", r"^(12|13|14)"],
    "12": [r"lineage-(1[78]|20|21)", r"android-?(10|11|13|14)\b", r"^(11|13|14)"],
    "13": [r"lineage-(1[789]|21)", r"android-?(10|11|12|14)\b", r"^(11|12|14)"],
    "14": [r"lineage-(1[789]|20)", r"android-?(10|11|12|13)\b", r"^(11|12|13)"],
}
RECOVERY_WORDS = ("twrp", "recovery", "ofox", "orangefox", "pbrp", "pitch", "shrp", "fox")
SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+-]*$")


def log(msg):
    print(msg, file=sys.stderr)
    LOG.append(msg)


def api(path, params=None, raw=False, retries=4):
    url = path if path.startswith("http") else API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "rom-builder", "Accept": "application/vnd.github+json"}
    if raw:
        headers["Accept"] = "application/vnd.github.raw"
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                data = r.read()
                return data.decode("utf-8", "replace") if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429):  # rate limit
                wait = int(e.headers.get("Retry-After") or 0)
                reset = e.headers.get("X-RateLimit-Reset")
                if not wait and reset:
                    wait = max(5, int(reset) - int(time.time()) + 1)
                wait = min(wait or 30, 70)
                log(f"  rate limit, {wait}s bekleniyor...")
                time.sleep(wait)
                continue
            if e.code >= 500:
                time.sleep(3)
                continue
            log(f"  API hatasi {e.code}: {url}")
            return None
        except Exception as e:  # ag hatasi
            log(f"  ag hatasi: {e}")
            time.sleep(3)
    return None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def safe_path(p):
    return bool(p) and SAFE_PATH.match(p) and ".." not in p.split("/") and not p.startswith("/")


def years_ago(iso):
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - d).days / 365.0
    except Exception:
        return 5.0


def classify(name):
    n = name.lower()
    if "kernel" in n:
        return "kernel"
    if "vendor" in n or "proprietary" in n or "blobs" in n:
        return "vendor"
    if "device" in n:
        return "device"
    return "other"


def is_recovery_name(name):
    n = name.lower()
    return any(w in n for w in RECOVERY_WORDS)


def search_repos(queries):
    seen, out = set(), []
    for q in queries:
        res = api("/search/repositories", {"q": q, "per_page": 30, "sort": "updated"})
        time.sleep(2.2)  # search API: dakikada 30 istek
        for it in (res or {}).get("items", []):
            if it["full_name"] not in seen:
                seen.add(it["full_name"])
                out.append(it)
    return out


def list_branches(full):
    res = api(f"/repos/{full}/branches", {"per_page": 100}) or []
    return [b["name"] for b in res]


def pick_branch(branches, default, android):
    prefs = BRANCH_PREFS.get(android, [])
    best, best_score = None, -1
    for b in branches:
        for i, pat in enumerate(prefs):
            if re.search(pat, b, re.I):
                s = 100 - i
                if s > best_score:
                    best, best_score = b, s
                break
    return best or default or (branches[0] if branches else None), best_score >= 0


LINEAGE_TO_ANDROID = {"16": "9", "17": "10", "18": "11", "19": "12", "20": "13", "21": "14", "22": "15"}
ANDROID_NAMES = {"10": "crDroid 10.0 / lineage-17.1", "11": "crDroid 11.0 / lineage-18.1",
                 "12": "crDroid 12.1 / lineage-19.1", "13": "crDroid 13.0 / lineage-20.0",
                 "14": "crDroid 14.0 / lineage-21.0", "15": "lineage-22.x"}


def branch_android(branch):
    """Dal adindan Android ana surumunu tahmin eder (bilinmezse None)."""
    m = re.match(r"lineage-(\d+)", branch or "", re.I)
    if m:
        return LINEAGE_TO_ANDROID.get(m.group(1))
    m = re.match(r"(?:android-?)?(\d+)(?:\.\d+)?$", branch or "", re.I)
    return m.group(1) if m else None


def branch_mismatch(branch, android):
    return any(re.search(p, branch or "", re.I) for p in OTHER_VERSION_HINTS.get(android, []))


def list_files(full, branch):
    res = api(f"/repos/{full}/contents/", {"ref": branch})
    return [x["name"] for x in res] if isinstance(res, list) else []


def read_file(full, branch, path):
    return api(f"/repos/{full}/contents/{path}", {"ref": branch}, raw=True)


def analyze_tree(files, codename):
    """Agacin ROM'a hazir olup olmadigini anlar."""
    mk = [f for f in files if f.endswith(".mk")]
    rom_mk = [f for f in mk if re.match(r"(lineage|crdroid|aosp|evolution|pixel|derp|aospa)_", f, re.I)]
    rec_mk = [f for f in mk if re.match(r"(twrp|omni|fox|pbrp|ofox)_", f, re.I)]
    has_deps = any(f.endswith(".dependencies") for f in files)
    return {"rom_ready": bool(rom_mk), "recovery_only": bool(rec_mk) and not rom_mk,
            "has_deps": has_deps, "files": files}


def score_repo(item, role, keys, a):
    name = item["name"]
    n = norm(name)
    if not any(norm(k) in n for k in keys if k):
        return None
    s = 0.0
    if item["owner"]["login"].lower() == a.user.lower():
        s += 50
    s += min(item.get("stargazers_count", 0), 50)
    s += max(0, 20 - 8 * years_ago(item.get("pushed_at", "")))
    if a.brand and norm(a.brand) in n:
        s += 25
    if a.country and a.country.lower() in (item.get("description") or "").lower():
        s += 3
    if item.get("archived"):
        s -= 20
    if is_recovery_name(name) and role == "device":
        s -= 120
    return s


def find_role(role, keys, a, extra_queries=()):
    b, c = norm(a.brand), a.codename
    qs = []
    for k in keys:
        if not k:
            continue
        qs += [f"{k} {role} in:name", f"{role}_{b}_{k} in:name"]
        if a.user:
            qs.append(f"{k} {role} user:{a.user}")
    qs += list(extra_queries)
    items = [i for i in search_repos(dict.fromkeys(qs)) if classify(i["name"]) == role]
    scored = []
    for it in items:
        sc = score_repo(it, role, keys, a)
        if sc is not None:
            scored.append((sc, it))
    scored.sort(key=lambda x: -x[0])
    return scored


def best_candidate(scored, a, role, codename, limit=6):
    """En iyi adaylarin dallarini ve icerigini inceleyip birini secer."""
    best = None
    for sc, it in scored[:limit]:
        full = it["full_name"]
        branches = list_branches(full)
        branch, matched = pick_branch(branches, it.get("default_branch"), a.android)
        if not branch:
            continue
        total = sc + ((200 if role == "device" else 60) if matched else 0)
        if branch_mismatch(branch, a.android) and not matched:
            total -= 40
        info = None
        if role == "device":
            info = analyze_tree(list_files(full, branch), codename)
            total += 40 if info["has_deps"] else 0
            total += 80 if info["rom_ready"] else 0
            total -= 100 if info["recovery_only"] else 0
        log(f"  aday: {full}@{branch} puan={total:.0f}" +
            (f" rom_ready={info['rom_ready']} recovery_only={info['recovery_only']}" if info else ""))
        if best is None or total > best["score"]:
            best = {"score": total, "full": full, "branch": branch, "item": it, "info": info,
                    "matched": matched}
        SEEN.append((full, branch, branch_android(branch)))
    return best


def path_from_name(name, role, fallback_brand, codename):
    m = re.match(rf"(?:android_)?{role}_([^_]+)_(.+)$", name, re.I)
    if m:
        return f"{role}/{m.group(1).lower()}/{m.group(2)}"
    return f"{role}/{norm(fallback_brand)}/{codename}"


def resolve_dependency(dep, owners, a, prefer_branch=None):
    """lineage.dependencies girdisini (repo, branch, path) haline getirir."""
    repo = dep.get("repository", "")
    target = dep.get("target_path", "")
    if not repo or not safe_path(target):
        log(f"  atlandi (gecersiz yol): {dep}")
        return None
    names = [repo] if "/" in repo else [repo] + ([f"android_{repo}"] if not repo.startswith("android_") else [])
    cands = [repo] if "/" in repo else [f"{o}/{n}" for n in names for o in owners]
    # son care: adla GitHub genelinde ara
    found = None
    for full in cands:
        found = api(f"/repos/{full}")
        if found:
            break
    if not found:
        base = repo.split("/")[-1]
        hits = [i for i in search_repos([f"{base} in:name"])
                if norm(i["name"]).endswith(norm(base)) or norm(base) in norm(i["name"])]
        hits.sort(key=lambda i: (-(prefer_branch in list_branches(i["full_name"]) if prefer_branch else 0),
                                 -i.get("stargazers_count", 0)))
        found = hits[0] if hits else None
        if found:
            log(f"  bagimlilik aramayla bulundu: {found['full_name']}")
    if not found:
        log(f"  bagimlilik bulunamadi: {repo}")
        return None
    full = found["full_name"]
    branches = list_branches(full)
    branch = None
    for cand in (dep.get("branch"), prefer_branch):
        if cand and cand in branches:
            branch = cand
            break
    if not branch:
        branch, _ = pick_branch(branches, found.get("default_branch"), a.android)
    return {"full": full, "branch": branch, "path": target}


def parse_override(s):
    s = s.strip()
    s = re.sub(r"^https?://github\.com/", "", s).rstrip("/")
    s = re.sub(r"\.git$", "", s)
    branch = None
    if "@" in s:
        s, branch = s.split("@", 1)
    if not re.match(r"^[\w.-]+/[\w.-]+$", s):
        sys.exit("device_tree_override gecersiz. Ornek: kullanici/android_device_oppo_CPH2269@lineage-18.1")
    return s, branch


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--brand", required=True)
    p.add_argument("--codename", required=True)
    p.add_argument("--model", default="")
    p.add_argument("--country", default="")
    p.add_argument("--user", default="")
    p.add_argument("--android", required=True, help="Android ana surumu, orn. 11")
    p.add_argument("--rom", default="crdroid")
    p.add_argument("--device-override", default="")
    p.add_argument("--allow-recovery", action="store_true")
    p.add_argument("--out", default="plan.json")
    a = p.parse_args()
    if a.android not in BRANCH_PREFS:  # bilinmeyen surum icin genel desenler
        BRANCH_PREFS[a.android] = [rf"^{re.escape(a.android)}(\.\d)?$", rf"android-?{re.escape(a.android)}"]

    keys = list(dict.fromkeys([a.codename, norm(a.model) if len(norm(a.model)) >= 4 else ""]))
    plan, seen_paths = [], set()

    def add(role, full, branch, path):
        if path in seen_paths or not safe_path(path):
            return
        seen_paths.add(path)
        plan.append({"role": role, "url": f"https://github.com/{full}", "branch": branch, "path": path})
        log(f"  + {role}: {full}@{branch} -> {path}")

    # 1) DEVICE TREE
    log(f"== Device tree araniyor: {a.brand} {a.codename} (Android {a.android}, {a.rom})")
    if a.device_override:
        full, br = parse_override(a.device_override)
        meta = api(f"/repos/{full}")
        if not meta:
            sys.exit(f"Override repo bulunamadi: {full}")
        br = br or pick_branch(list_branches(full), meta.get("default_branch"), a.android)[0]
        best = {"full": full, "branch": br, "item": meta,
                "info": analyze_tree(list_files(full, br), a.codename)}
    else:
        best = best_candidate(find_role("device", keys, a), a, "device", a.codename)
    if not best:
        log("HATA: GitHub'da bu cihaz icin device tree bulunamadi.")
        log("Cihazin kod adini kontrol edin veya 'device_tree_override' alanina elle repo yazin.")
        json.dump([], open(a.out, "w"))
        write_summary(a, None)
        return 2
    info = best["info"]
    ver = branch_android(best["branch"])
    if not a.device_override and not best.get("matched") and ver != a.android:
        log(f"HATA: Bu cihaz icin Android {a.android} device tree'si bulunamadi.")
        log("Bulunan agaclar ve surumleri:")
        for full, br, v in dict.fromkeys(SEEN):
            log(f"  - {full}@{br}  (Android {v or '?'})")
        vs = sorted({v for _, _, v in SEEN if v})
        for v in vs:
            log(f"=> Android {v} icin su degeri 'android_version' alanina yaz: {ANDROID_NAMES.get(v, v)}")
        log("Baska surum denemek istemiyorsan 'device_tree_override' ile Android " + a.android + " agacini elle ver.")
        write_summary(a, best)
        json.dump([], open(a.out, "w"))
        return 4
    if info["recovery_only"] and not a.allow_recovery:
        log(f"HATA: Bulunan agac ({best['full']}) sadece recovery (TWRP/Fox) agaci. ROM derlenemez.")
        log("Gercek bir ROM agaci bulursaniz 'device_tree_override' ile verin, ya da riski kabul edip 'try_recovery_tree' secin.")
        write_summary(a, best)
        json.dump([], open(a.out, "w"))
        return 3
    dev_path = path_from_name(best["item"]["name"], "device", a.brand, a.codename)
    add("device", best["full"], best["branch"], dev_path)

    # 2) BAGIMLILIKLAR (lineage.dependencies / crdroid.dependencies), en fazla 3 seviye
    owners = list(dict.fromkeys([best["item"]["owner"]["login"], a.user, "LineageOS"]))
    owners = [o for o in owners if o]
    queue = [(best["full"], best["branch"], 0)]
    while queue:
        full, branch, depth = queue.pop(0)
        if depth >= 3:
            continue
        for fn in ("lineage.dependencies", "crdroid.dependencies"):
            txt = read_file(full, branch, fn)
            if not txt:
                continue
            try:
                deps = json.loads(txt)
            except Exception:
                log(f"  {fn} okunamadi ({full})")
                continue
            for dep in deps:
                r = resolve_dependency(dep, owners, a, best["branch"])
                if r and r["path"] not in seen_paths:
                    role = classify(r["full"].split("/")[1])
                    add(role if role != "other" else "dependency", r["full"], r["branch"], r["path"])
                    queue.append((r["full"], r["branch"], depth + 1))

    # 3) VENDOR (bagimlilikta yoksa ara)
    if not any(x["role"] == "vendor" for x in plan):
        log("== Vendor tree araniyor")
        v = best_candidate(find_role("vendor", keys, a), a, "vendor", a.codename, limit=4)
        if v:
            add("vendor", v["full"], v["branch"], path_from_name(v["item"]["name"], "vendor", a.brand, a.codename))
            if not v.get("matched"):
                log(f"UYARI: vendor dali ({v['branch']}) Android {a.android} ile eslesmiyor, blob'lar uyumsuz olabilir.")
        else:
            log("UYARI: vendor tree bulunamadi (blob'lar eksik olabilir).")

    # 4) KERNEL (BoardConfig'te TARGET_KERNEL_SOURCE varsa ve bagimlilikta yoksa)
    if not any(x["role"] == "kernel" for x in plan):
        ksrc = None
        for fn in ("BoardConfig.mk", "BoardConfigCommon.mk"):
            txt = read_file(best["full"], best["branch"], fn) or ""
            m = re.search(r"^\s*TARGET_KERNEL_SOURCE\s*[:?]?=\s*(\S+)", txt, re.M)
            if m and "$" not in m.group(1):
                ksrc = m.group(1)
                break
        if ksrc and safe_path(ksrc) and ksrc not in seen_paths:
            log(f"== Kernel kaynagi gerekli: {ksrc}")
            parts = [x for x in ksrc.split("/") if x not in ("kernel",)]
            ks = find_role("kernel", [parts[-1]], a) if parts else []
            ks = [(s, i) for s, i in ks if all(norm(x) in norm(i["name"]) for x in parts[-2:])] or ks
            k = best_candidate(ks, a, "kernel", parts[-1] if parts else a.codename, limit=4)
            if k:
                add("kernel", k["full"], k["branch"], ksrc)
            else:
                log(f"UYARI: kernel kaynagi ({ksrc}) GitHub'da bulunamadi, derleme basarisiz olabilir.")

    json.dump(plan, open(a.out, "w"), indent=1)
    write_summary(a, best, plan)
    return 0


def write_summary(a, best, plan=None):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as f:
        f.write(f"## Cihaz agaci aramasi: {a.brand} {a.codename}\n\n")
        if plan:
            f.write("| Rol | Repo | Dal | Yol |\n|---|---|---|---|\n")
            for x in plan:
                f.write(f"| {x['role']} | {x['url']} | `{x['branch']}` | `{x['path']}` |\n")
        f.write("\n<details><summary>Arama kaydi</summary>\n\n```\n" + "\n".join(LOG) + "\n```\n</details>\n")


if __name__ == "__main__":
    sys.exit(main())
