"""Ejecuta PN-001 y PN-002 contra una API real (uvicorn) y guarda la evidencia."""
import copy
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

BASE = sys.argv[1]
DATA = Path(sys.argv[2])
SECRETO = sys.argv[3]
LOTE = json.load(open(sys.argv[4], encoding="utf-8"))
OUT = Path(sys.argv[5])
H = {"X-Webhook-Secret": SECRETO, "Content-Type": "application/json"}
URL = BASE + "/api/v1/interactions/process"
filas = []


def archivos_raw():
    d = DATA / "raw"
    return sorted(p.name for p in d.glob("run-*.json")) if d.exists() else []


def solicitudes(request_id):
    con = sqlite3.connect(DATA / "communitylab.db")
    n = con.execute("SELECT COUNT(*) FROM solicitudes_ingesta WHERE request_id=?", (request_id,)).fetchone()[0]
    h = con.execute("SELECT hash_sha256 FROM solicitudes_ingesta WHERE request_id=?", (request_id,)).fetchone()
    con.close()
    return n, (h[0][:12] if h else None)


def post(etapa, payload, content=None):
    r = httpx.post(URL, headers=H, content=content if content is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=30)
    cuerpo = r.json()
    filas.append({"etapa": etapa, "http": r.status_code, "respuesta": cuerpo,
                  "archivos_en_raw": len(archivos_raw())})
    print(f"{etapa}: HTTP {r.status_code} {json.dumps(cuerpo, ensure_ascii=False)} | raw={len(archivos_raw())}")
    return r


rid = LOTE["request_id"]

# ---------------- PN-001: reenvío idéntico ----------------
r1 = post("PN-001 · 1) primer envío", LOTE)
run_id = r1.json()["run_id"]
r2 = post("PN-001 · 2) reenvío idéntico", LOTE)
# mismo contenido, otro orden de claves y JSON compacto/indentado
reordenado = {k: LOTE[k] for k in reversed(list(LOTE))}
r3 = post("PN-001 · 3) mismo contenido, otro orden de claves", None, content=json.dumps(reordenado, ensure_ascii=False, indent=2).encode("utf-8"))
# 8 reenvíos idénticos en paralelo
with ThreadPoolExecutor(8) as ex:
    paralelas = list(ex.map(lambda _: httpx.post(URL, headers=H, content=json.dumps(LOTE, ensure_ascii=False).encode("utf-8"), timeout=30), range(8)))
codigos = sorted({(x.status_code, x.json().get("run_id"), x.json().get("duplicado")) for x in paralelas}, key=str)
filas.append({"etapa": "PN-001 · 4) 8 reenvíos idénticos en paralelo", "http": sorted({x.status_code for x in paralelas}),
              "respuesta": [list(c) for c in codigos], "archivos_en_raw": len(archivos_raw())})
print("PN-001 · 4) 8 en paralelo:", sorted({x.status_code for x in paralelas}), codigos, "| raw=", len(archivos_raw()))
n, h = solicitudes(rid)
filas.append({"etapa": "PN-001 · 5) estado final en la base", "solicitudes_con_ese_request_id": n, "hash_prefijo": h})
print("PN-001 · 5) filas en solicitudes_ingesta:", n, "| hash:", h)

# ---------------- PN-002: conflicto de identificador ----------------
cambiado = copy.deepcopy(LOTE)
cambiado["interacciones"][0]["texto"] = cambiado["interacciones"][0]["texto"] + " (texto modificado)"
c1 = post("PN-002 · 1) mismo request_id, un mensaje con otro texto", cambiado)
solo_periodo = copy.deepcopy(LOTE)
solo_periodo["periodo_referencia"] = "2099-W01"
c2 = post("PN-002 · 2) mismo request_id, otro periodo_referencia", solo_periodo)
menos = copy.deepcopy(LOTE)
menos["interacciones"] = menos["interacciones"][:-1]
c3 = post("PN-002 · 3) mismo request_id, un mensaje menos", menos)
n2, h2 = solicitudes(rid)
filas.append({"etapa": "PN-002 · 4) el original no cambió", "solicitudes_con_ese_request_id": n2, "hash_prefijo": h2,
              "hash_igual_al_inicial": h2 == h})
print("PN-002 · 4) tras los conflictos: filas =", n2, "| hash igual al inicial:", h2 == h, "| raw =", len(archivos_raw()))
est = httpx.get(f"{BASE}/api/v1/runs/{run_id}", headers=H, timeout=30).json()
filas.append({"etapa": "PN-002 · 5) estado del run original", "respuesta": est})
print("PN-002 · 5) estado del run original:", json.dumps(est, ensure_ascii=False))
# contraste: otro request_id con el mismo contenido sí es un lote nuevo
otro = copy.deepcopy(LOTE)
otro["request_id"] = rid + "-b"
c4 = post("Contraste · mismo contenido con otro request_id", otro)

OUT.write_text(json.dumps({"run_id_original": run_id, "pasos": filas}, ensure_ascii=False, indent=2), encoding="utf-8")
print("evidencia guardada en", OUT)
