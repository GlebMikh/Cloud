#!/usr/bin/env python3
"""
granola_pull.py — тянет встречи из Granola через её приватный API под токеном
твоего аккаунта (бесплатно, тот же путь, что и само приложение) и кладёт готовый
.md (заметки + транскрипт в формате Me/Them) в папку-инбокс, откуда его
подхватывает скилл meeting-report-conductor.

Использование:
    python granola_pull.py --list           # показать последние встречи
    python granola_pull.py                   # выгрузить самую свежую встречу
    python granola_pull.py --index 2         # выгрузить N-ю по свежести (0 = самая свежая)
    python granola_pull.py --id <document_id># выгрузить конкретную встречу

Токен ищется по порядку:
    1. переменная окружения GRANOLA_TOKEN или строка GRANOLA_TOKEN=... в Cloud/.env
    2. открытый %APPDATA%\\Granola\\supabase.json (старые версии)
    3. зашифрованный supabase.json.enc + storage.dek (DPAPI) — этот шаг трогает
       твои креды, поэтому запускай скрипт в СВОЁМ терминале, а не через агента.
"""
import os, sys, json, base64, re, argparse, datetime, pathlib

APPDATA = os.environ.get("APPDATA", "")
GRANOLA_DIR = pathlib.Path(APPDATA) / "Granola"
ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"
INBOX = pathlib.Path(os.environ["USERPROFILE"]) / "Downloads" / "granola_inbox"
API = "https://api.granola.ai"

# ------------------------------------------------------------------ token ----
def _from_env():
    tok = os.environ.get("GRANOLA_TOKEN")
    if tok:
        return tok.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("GRANOLA_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None

def _extract_access_token(blob_text):
    """Из JSON Granola достаёт access_token (формат менялся между версиями)."""
    try:
        data = json.loads(blob_text)
    except Exception:
        return None
    # встречались варианты: cognito_tokens (строка JSON), workos_tokens, прямой access_token
    for key in ("cognito_tokens", "workos_tokens", "tokens"):
        v = data.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                pass
        if isinstance(v, dict) and v.get("access_token"):
            return v["access_token"]
    if data.get("access_token"):
        return data["access_token"]
    return None

def _from_plaintext():
    p = GRANOLA_DIR / "supabase.json"
    if p.exists():
        return _extract_access_token(p.read_text(encoding="utf-8", errors="ignore"))
    return None

def _win_master_key():
    """Мастер-ключ Chromium/Electron из Local State (DPAPI-обёрнут)."""
    import win32crypt
    ls = json.loads((GRANOLA_DIR / "Local State").read_text(encoding="utf-8", errors="ignore"))
    blob = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if blob[:5] == b"DPAPI":
        blob = blob[5:]
    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]

def _aesgcm(key, blob):
    """AES-GCM с автоопределением раскладки: 'v10'+nonce12+ct+tag16 или nonce12+ct+tag16."""
    from Crypto.Cipher import AES
    layouts = []
    if blob[:3] in (b"v10", b"v11"):
        layouts.append((blob[3:15], blob[15:-16], blob[-16:]))
    layouts.append((blob[:12], blob[12:-16], blob[-16:]))
    last = None
    for nonce, ct, tag in layouts:
        try:
            return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
        except Exception as e:
            last = e
    raise RuntimeError(f"AES-GCM не сошёлся: {last}")

def _from_encrypted():
    """Трёхступенчато: Local State → мастер-ключ → storage.dek (DEK) → supabase.json.enc."""
    enc = GRANOLA_DIR / "supabase.json.enc"
    dek = GRANOLA_DIR / "storage.dek"
    if not (enc.exists() and dek.exists()):
        return None

    master = _win_master_key()
    dek_plain = _aesgcm(master, dek.read_bytes())

    # DEK может быть сырым ключом или base64-строкой ключа
    key_candidates = [dek_plain]
    try:
        b = base64.b64decode(dek_plain)
        if len(b) in (16, 24, 32):
            key_candidates.insert(0, b)
    except Exception:
        pass
    for k in (dek_plain[:32], dek_plain[:16]):
        if k not in key_candidates:
            key_candidates.append(k)

    enc_blob = enc.read_bytes()
    last = None
    for key in key_candidates:
        try:
            plain = _aesgcm(key, enc_blob).decode("utf-8", "ignore")
            tok = _extract_access_token(plain)
            if tok:
                return tok
        except Exception as e:
            last = e
    raise RuntimeError(f"не удалось расшифровать supabase.json.enc (последняя ошибка: {last})")

def get_token():
    for fn in (_from_env, _from_plaintext, _from_encrypted):
        try:
            t = fn()
            if t:
                return t
        except Exception as e:
            print(f"[token] {fn.__name__}: {e}", file=sys.stderr)
    raise SystemExit(
        "Не нашёл токен Granola. Убедись, что приложение установлено и ты в него вошёл,\n"
        "либо положи GRANOLA_TOKEN=... в " + str(ENV_FILE)
    )

# -------------------------------------------------------------------- api ----
def _session(token):
    import requests
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": "Granola/6 (granola_pull)",
        "X-Client-Version": "6.0.0",
    })
    return s

def get_documents(s, limit=15):
    r = s.post(f"{API}/v2/get-documents", json={"limit": limit, "offset": 0})
    r.raise_for_status()
    data = r.json()
    docs = data.get("docs") or data.get("documents") or data
    return docs if isinstance(docs, list) else data.get("data", [])

def get_transcript(s, doc_id):
    for path, payload in (
        ("/v1/get-document-transcript", {"document_id": doc_id}),
        ("/v2/get-document-transcript", {"document_id": doc_id}),
    ):
        try:
            r = s.post(f"{API}{path}", json=payload)
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return []

# ---------------------------------------------------------------- render ----
def prosemirror_to_text(node, out):
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        for ch in node.get("content", []) or []:
            prosemirror_to_text(ch, out)
        if node.get("type") in ("paragraph", "heading", "listItem"):
            out.append("\n")
    elif isinstance(node, list):
        for ch in node:
            prosemirror_to_text(ch, out)

def notes_text(doc):
    for key in ("notes_markdown", "notes_plain"):
        if doc.get(key):
            return doc[key].strip()
    for key in ("notes", "content", "last_viewed_panel"):
        n = doc.get(key)
        if isinstance(n, dict):
            out = []
            prosemirror_to_text(n, out)
            t = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
            if t:
                return t
    return ""

def transcript_text(segments):
    """microphone → «Me», system/others → «Them»; склеиваем подряд идущие."""
    if isinstance(segments, dict):
        segments = segments.get("transcript") or segments.get("segments") or segments.get("data") or []
    lines, cur_spk, buf = [], None, []
    def flush():
        if buf:
            lines.append(f"**{cur_spk}:** " + " ".join(buf).strip())
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        src = (seg.get("source") or seg.get("speaker") or "").lower()
        spk = "Me" if src in ("microphone", "me", "mic") else "Them"
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        if spk != cur_spk:
            flush(); cur_spk, buf = spk, []
        buf.append(txt)
    flush()
    return "\n\n".join(lines)

def slugify(title):
    t = re.sub(r"[^\w\-]+", "-", (title or "meeting").strip().lower(), flags=re.U)
    return re.sub(r"-{2,}", "-", t).strip("-")[:60] or "meeting"

def build_md(doc, segments):
    title = doc.get("title") or "Встреча"
    created = doc.get("created_at") or doc.get("createdAt") or ""
    try:
        d = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except Exception:
        d = datetime.datetime.now()
    date_iso = d.strftime("%Y-%m-%d")
    parts = [f"Meeting Title: {title}", f"Date: {d.strftime('%b %d')}", ""]
    notes = notes_text(doc)
    if notes:
        parts += ["## Notes (Granola)", "", notes, ""]
    parts += ["## Transcript", "", transcript_text(segments) or "(транскрипт пуст)"]
    return date_iso, slugify(title), "\n".join(parts)

# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--id")
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()

    token = get_token()
    s = _session(token)
    docs = get_documents(s, args.limit)
    if not docs:
        raise SystemExit("API вернул пустой список встреч (проверь токен/доступ).")

    if args.list:
        for i, d in enumerate(docs):
            print(f"[{i}] {d.get('created_at','?')[:16]}  {d.get('title','(без названия)')}  id={d.get('id') or d.get('document_id')}")
        return

    if args.id:
        doc = next((d for d in docs if (d.get("id") or d.get("document_id")) == args.id), None)
        if not doc:
            raise SystemExit(f"встреча {args.id} не найдена среди последних {args.limit}")
    else:
        doc = docs[args.index]

    doc_id = doc.get("id") or doc.get("document_id")
    segs = get_transcript(s, doc_id)
    date_iso, slug, md = build_md(doc, segs)

    INBOX.mkdir(parents=True, exist_ok=True)
    out = INBOX / f"{date_iso}-{slug}.md"
    out.write_text(md, encoding="utf-8")
    print(f"OK: {out}  ({len(md)} символов, транскрипт-сегментов: "
          f"{len(segs) if isinstance(segs, list) else 'n/a'})")

if __name__ == "__main__":
    main()
