#!/usr/bin/env python3
"""
Диагностика хранилища токена Granola. НЕ печатает сам токен — только структуру
файлов и то, какая стратегия расшифровки сработала (имена ключей JSON, длины).
Запусти:  python granola_diag.py
"""
import os, json, base64, pathlib, binascii

APPDATA = os.environ.get("APPDATA", "")
G = pathlib.Path(APPDATA) / "Granola"
DPAPI_MAGIC = binascii.unhexlify("01000000d08c9ddf")

def describe(name):
    p = G / name
    if not p.exists():
        print(f"  {name}: НЕТ ФАЙЛА")
        return None
    raw = p.read_bytes()
    head = raw[:8]
    info = f"  {name}: {len(raw)} байт, первые8={head.hex()}"
    # похоже на base64-текст?
    b64 = None
    try:
        txt = raw.decode("ascii")
        if all(c.isalnum() or c in "+/=\r\n" for c in txt.strip()):
            b64 = base64.b64decode(txt.strip())
            info += f", base64->{len(b64)}байт первые8={b64.hex()[:16]}"
    except Exception:
        pass
    if raw[:8] == DPAPI_MAGIC:
        info += "  [сырой DPAPI-блоб]"
    if b64 and b64[:8] == DPAPI_MAGIC:
        info += "  [base64 DPAPI-блоб]"
    print(info)
    return raw, b64

def try_dpapi(data, label):
    import win32crypt
    try:
        out = win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]
        print(f"    DPAPI OK ({label}): {len(out)} байт ключа/данных")
        return out
    except Exception as e:
        print(f"    DPAPI FAIL ({label}): {e}")
        return None

def show_json_keys(plain, label):
    try:
        d = json.loads(plain)
        print(f"    [{label}] это JSON, ключи верхнего уровня: {list(d.keys())}")
        for k, v in d.items():
            if isinstance(v, str) and v.strip().startswith("{"):
                try:
                    print(f"        {k} -> вложенный JSON, ключи: {list(json.loads(v).keys())}")
                except Exception:
                    pass
        return True
    except Exception:
        print(f"    [{label}] не JSON (первые байты: {plain[:12]!r})")
        return False

def try_aesgcm(key, blob, label):
    from Crypto.Cipher import AES
    layouts = {
        "nonce12+ct+tag16": (blob[:12], blob[12:-16], blob[-16:]),
    }
    if blob[:3] == b"v10" or blob[:3] == b"v11":
        layouts["v1x prefix"] = (blob[3:15], blob[15:-16], blob[-16:])
    for lname, (nonce, ct, tag) in layouts.items():
        try:
            plain = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
            print(f"    AES-GCM OK ({label} / {lname})")
            show_json_keys(plain, f"{label}/{lname}")
            return True
        except Exception as e:
            print(f"    AES-GCM FAIL ({label} / {lname}): {e}")
    return False

print("== Файлы Granola ==")
dek = describe("storage.dek")
enc = describe("supabase.json.enc")
plain_sb = describe("supabase.json")  # вдруг всё-таки открытый
print()

print("== Стратегия A: supabase.json.enc напрямую под DPAPI ==")
if enc:
    raw, b64 = enc
    for data, lbl in ((raw, "raw"), (b64, "base64")):
        if data:
            out = try_dpapi(data, f"enc {lbl}")
            if out:
                show_json_keys(out, f"enc {lbl}")
print()

print("== Стратегия B: ключ из storage.dek (DPAPI) + AES-GCM над enc ==")
key = None
if dek:
    raw, b64 = dek
    for data, lbl in ((raw, "dek raw"), (b64, "dek base64")):
        if data:
            k = try_dpapi(data, lbl)
            if k:
                key = k
                break
if key and enc:
    raw, b64 = enc
    for data, lbl in ((raw, "enc raw"), (b64, "enc base64")):
        if data:
            try_aesgcm(key, data, lbl)
print()

print("== Стратегия C: ключ из Local State (os_crypt, как в Chromium) ==")
ls = G / "Local State"
if ls.exists():
    try:
        import win32crypt
        from Crypto.Cipher import AES
        conf = json.loads(ls.read_text(encoding="utf-8", errors="ignore"))
        enck = conf.get("os_crypt", {}).get("encrypted_key")
        if enck:
            blob = base64.b64decode(enck)
            print(f"  Local State os_crypt.encrypted_key найден, префикс={blob[:5]!r}")
            key2 = win32crypt.CryptUnprotectData(blob[5:], None, None, None, 0)[1]
            print(f"  master-ключ снят: {len(key2)} байт")
            if enc:
                raw, b64 = enc
                for data, lbl in ((raw, "enc raw"), (b64, "enc base64")):
                    if data:
                        try_aesgcm(key2, data, f"LocalState/{lbl}")
        else:
            print("  os_crypt.encrypted_key в Local State отсутствует")
    except Exception as e:
        print(f"  Local State ошибка: {e}")
else:
    print("  Local State нет")
print("\nГотово. Скопируй весь вывод в чат.")
